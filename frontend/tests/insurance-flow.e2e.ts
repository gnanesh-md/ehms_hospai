import "./shim"
import { InsuranceEngineService as E } from "../src/services/insuranceDb"
import { BedDatabase } from "../src/services/bedDb"
import { PLAN } from "../src/components/insurance/planSteps"
import { BillingDatabase, resolveErItemPrice } from "../src/services/billingDb"
import { PharmacyDatabase } from "../src/services/pharmacyDb"
import { PriceMaster } from "../src/services/priceMaster"
import { db } from "../src/services/db"
import { settlementPosition } from "../src/utils/settlementMath"
import { aadhaarDigits, aadhaarState, formatAadhaar } from "../src/utils/aadhaar"
import { getDoctorConsultationFee } from "../src/services/doctorMaster"
import { DiagnosticTariffDatabase } from "../src/services/diagnosticTariffDb"
import { ALL_LAB_TESTS } from "../src/components/laboratory/labCatalogueSchema"
import { priceForTest } from "../src/services/labOrdersDb"
import { HospitalContacts, helplineText, cardHelplines } from "../src/services/hospitalContacts"
import { getBranding } from "../src/lib/branding"
import { renderTemplate } from "../src/services/patientMessaging"
import { MASTER_HOSPITAL_SERVICES, getServiceCatalogue, getOrderableTests } from "../src/components/DoctorWorkflow"
const results: string[] = []
const ok = (m: string) => results.push("PASS  " + m)
const bad = (m: string, e: unknown) => results.push("FAIL  " + m + " -> " + ((e as Error)?.message || e))
async function step(name: string, fn: () => unknown) {
  try { await fn(); ok(name) } catch (e) { bad(name, e) }
}

;(async () => {
  const policy: any = { insurerName: "Star Health", tpaName: "MediAssist", policyNumber: "POL-1", memberId: "M1" }
  let id = ""
  await step("open encounter case (IP)", () => {
    const c = E.openEncounterCase({ patientId: "UMR9001", patientName: "E2E Patient", encounterType: "IP", department: "General Medicine" }, policy)
    id = c.id
    if (c.status !== "ELIGIBILITY_PENDING") throw new Error("status " + c.status)
  })
  await step("duplicate case for same patient is blocked", () => {
    try { E.openEncounterCase({ patientId: "UMR9001", patientName: "E2E Patient", encounterType: "IP", department: "General Medicine" }, policy) }
    catch { return }
    throw new Error("duplicate allowed")
  })
  await step("cannot approve pre-auth before eligibility/submission", () => {
    try { E.recordPreAuthResponse(id, { outcome: "Approved", amount: 1000, approvalCode: "A1", note: "x" }) } catch { return }
    throw new Error("approval accepted without submission")
  })
  await step("eligibility verified", () => E.recordEligibility(id, { status: "Eligible", balanceAvailable: 500000, inNetwork: true } as any))
  await step("pre-auth draft", () => E.savePreAuthDraft(id, { diagnosis: "Appendicitis", clinicalSummary: "Acute", treatingDoctor: "Dr A", admissionType: "Planned", procedureCodes: [], estimatedOtherCharges: 80000, estimatedHospitalStayDays: 3 }))
  await step("pre-auth submit (needs mandatory docs)", () => {
    const c = E.getClaimById(id)!
    c.documents.forEach((d: any) => { if (d.isMandatory) { E.uploadDocument(id, d.id, "f.pdf"); E.verifyDocument(id, d.id, true) } })
    E.submitPreAuth(id, "Portal", "REF1")
  })
  await step("pre-auth approved -> treatment in progress", () => {
    E.recordPreAuthResponse(id, { outcome: "Approved", amount: 80000, approvalCode: "AUTH-1", note: "ok" })
    if (E.getClaimById(id)!.status !== "TREATMENT_IN_PROGRESS") throw new Error("status " + E.getClaimById(id)!.status)
  })
  await step("enhancement request", () => E.requestEnhancement(id, 100000, "extra stay"))
  await step("discharge initiated", () => E.initiateDischarge(id))
  await step("final claim cannot be submitted before final bill", () => {
    try { E.submitClaim(id, "Portal", "R") } catch { return }
    throw new Error("claim submitted without final bill")
  })
  // Final bill is produced by IP billing; simulate its hand-over so the claim stage can run.
  await step("final bill handed over (simulated billing hand-off)", () =>
    (E as any).act(id, (c: any) => { c.status = "FINAL_BILL_READY"; c.finalClaimAmount = 90000; c.totalHospitalBill = 100000 }))
  await step("submit claim (docs verified)", () => {
    const c = E.getClaimById(id)!
    c.documents.forEach((d: any) => { if (d.isMandatory && d.status !== "Verified") { E.uploadDocument(id, d.id, "f.pdf"); E.verifyDocument(id, d.id, true) } })
    E.submitClaim(id, "Portal", "CL-REF")
  })
  await step("insurer query -> respond -> back under review", () => {
    E.raiseClaimQuery(id, "Need OT notes", 3)
    const q = E.getClaimById(id)!.queries[0]
    E.respondToQuery(id, q.id, "Attached", [])
    if (E.getClaimById(id)!.status !== "CLAIM_SUBMITTED") throw new Error("status " + E.getClaimById(id)!.status)
  })
  await step("partial approval needs matching deductions", () => {
    try { E.recordClaimDecision(id, { outcome: "Partially Approved", approvedAmount: 70000, deductions: [], note: "n" }) } catch { return }
    throw new Error("accepted without deductions")
  })
  await step("partial approval with deductions creates settlement", () => {
    E.recordClaimDecision(id, { outcome: "Partially Approved", approvedAmount: 70000, deductions: [{ id: "d1", category: "Room Rent Capping", amount: 20000, remark: "cap" }], note: "n" })
    const c = E.getClaimById(id)!
    if (!c.settlement || c.settlement.expectedAmount !== 70000) throw new Error("no settlement")
  })
  await step("cannot reconcile before payment", () => {
    try { E.reconcile(id, "x") } catch { return }
    throw new Error("reconciled before payment")
  })
  await step("settlement advice", () => E.recordSettlementAdvice(id, "ADV-1", "2026-12-01"))
  const pos = () => settlementPosition(E.getClaimById(id)!)
  await step("money maths: unpaid is 'awaiting', not an unexplained gap; ₹5L approved / ₹4L received leaves ₹1L to explain", () => {
    const a = settlementPosition({ finalClaimAmount: 560000, settlement: { expectedAmount: 500000, receivedAmount: 0 } as any })
    if (a.state !== "awaiting" || a.unexplained !== 0 || a.toCollect !== 500000 || a.insurerCut !== 60000) throw new Error(JSON.stringify(a))
    const b = settlementPosition({ finalClaimAmount: 560000, settlement: { expectedAmount: 500000, receivedAmount: 400000 } as any })
    if (b.state !== "short_unexplained" || b.unexplained !== 100000 || b.gap !== 100000) throw new Error(JSON.stringify(b))
    const d = settlementPosition({ finalClaimAmount: 500000, settlement: { expectedAmount: 500000, receivedAmount: 520000 } as any })
    if (d.state !== "excess" || d.excess !== 20000) throw new Error(JSON.stringify(d))
  })
  await step("first instalment (₹40,000 of ₹70,000) is 'partly paid', not a final short payment", () => {
    E.recordPayment(id, { amount: 40000, utr: "UTR1", paymentDate: "2026-12-01", bankAccount: "HDFC" })
    const c = E.getClaimById(id)!
    if (c.settlement!.reconciliationStatus !== "Partially Received") throw new Error(c.settlement!.reconciliationStatus)
    if (pos().gap !== 30000 || pos().unexplained !== 30000) throw new Error(JSON.stringify(pos()))
  })
  await step("the same UTR cannot be entered twice on a claim", () => {
    try { E.recordPayment(id, { amount: 1000, utr: "utr1", paymentDate: "2026-12-01", bankAccount: "HDFC" }) } catch { return }
    throw new Error("duplicate UTR accepted")
  })
  await step("cannot reconcile while ₹30,000 is unexplained", () => {
    try { E.reconcile(id, "all fine") } catch (e) { if (!/not explained/.test((e as Error).message)) throw e; return }
    throw new Error("reconciled with unexplained money")
  })
  await step("cannot explain more than is missing; write-off needs a reason", () => {
    try { E.recordShortfall(id, { amount: 99999, kind: "TDS deducted by insurer", remark: "" }) } catch { /* ok */ } finally { if (pos().closed) throw new Error("recorded") }
    try { E.recordShortfall(id, { amount: 1000, kind: "Written off by hospital", remark: " " }) } catch { return }
    throw new Error("write-off without reason accepted")
  })
  await step("₹5,000 recorded as TDS (closed), ₹25,000 as 'insurer to pay later' (open)", () => {
    E.recordShortfall(id, { amount: 5000, kind: "TDS deducted by insurer", remark: "" })
    E.recordShortfall(id, { amount: 25000, kind: "Insurer to pay later", remark: "promised by 15 Dec" })
    const q = pos()
    if (q.closed !== 5000 || q.followUp !== 25000 || q.unexplained !== 0 || q.state !== "short_followup") throw new Error(JSON.stringify(q))
  })
  await step("cannot reconcile while the insurer still owes ₹25,000", () => {
    try { E.reconcile(id, "") } catch (e) { if (!/still being chased/.test((e as Error).message)) throw e; return }
    throw new Error("reconciled with money still owed")
  })
  await step("the later ₹25,000 arrives as a second payment (own UTR); claim becomes reconcilable", () => {
    E.recordPayment(id, { amount: 25000, utr: "UTR2", paymentDate: "2026-12-15", bankAccount: "HDFC" })
    const c = E.getClaimById(id)!
    if (c.status !== "PAYMENT_RECEIVED" || c.settlement!.receivedAmount !== 65000 || c.settlement!.payments!.length !== 2) throw new Error(JSON.stringify(c.settlement))
    if (pos().state !== "short_explained" || pos().outstanding !== 0) throw new Error(JSON.stringify(pos()))
  })
  await step("reconcile then close", () => { E.reconcile(id, ""); E.closeClaim(id) })
  await step("a reconciled claim takes no more payments or entries", () => {
    try { E.recordPayment(id, { amount: 1, utr: "UTR3", paymentDate: "2026-12-16", bankAccount: "HDFC" }) } catch { return }
    throw new Error("payment accepted after reconcile")
  })
  await step("no transition out of CLOSED", () => {
    try { E.initiateDischarge(id) } catch { return }
    throw new Error("moved out of closed")
  })
  // ---- Reimbursement journey: patient pays first, then claims ----
  let rid = ""
  await step("reimbursement case opens", () => {
    const c = E.openEncounterCase({ patientId: "UMR9002", patientName: "Reimb Patient", encounterType: "IP", department: "General Medicine" }, policy, { claimType: "Reimbursement" })
    rid = c.id
    if (c.claimType !== "Reimbursement") throw new Error("claimType " + c.claimType)
  })
  await step("reimbursement: eligibility then treatment without pre-auth", () => {
    E.recordEligibility(rid, { status: "Eligible", balanceAvailable: 300000, inNetwork: false } as any)
    E.startTreatmentWithoutPreAuth(rid)
  })
  await step("reimbursement: discharge + final bill hand-off", () => {
    E.initiateDischarge(rid)
    ;(E as any).act(rid, (c: any) => { c.status = "FINAL_BILL_READY"; c.finalClaimAmount = 50000; c.totalHospitalBill = 50000 })
    E.getClaimById(rid)!.documents.forEach((d: any) => { if (d.isMandatory) { E.uploadDocument(rid, d.id, "f.pdf"); E.verifyDocument(rid, d.id, true) } })
  })
  await step("reimbursement: claim blocked until the patient has paid", () => {
    try { E.submitClaim(rid, "Patient", "R1") } catch { return }
    throw new Error("submitted without patient payment")
  })
  await step("patient payment recorded; duplicate receipt rejected", () => {
    E.recordPatientPayment(rid, { amount: 50000, receiptNo: "RCPT-1" })
    try { E.recordPatientPayment(rid, { amount: 1, receiptNo: "RCPT-1" }) } catch { return }
    throw new Error("duplicate receipt accepted")
  })
  await step("reimbursement: claim blocked until the pack is generated", () => {
    try { E.submitClaim(rid, "Patient", "R1") } catch { return }
    throw new Error("submitted without pack")
  })
  await step("claim pack contains patient, receipts and total", () => {
    const html = E.buildReimbursementPack(rid)
    if (!html.includes("Reimb Patient") || !html.includes("RCPT-1") || !html.includes("50,000")) throw new Error("pack incomplete")
  })
  await step("patient submits -> approved -> paid to the patient -> closed", () => {
    E.submitClaim(rid, "Patient", "INS-REF-77")
    E.recordClaimDecision(rid, { outcome: "Approved", approvedAmount: 50000, deductions: [], note: "ok" })
    E.recordSettlementAdvice(rid, "ADV-9", "2026-12-10")
    E.recordPayment(rid, { amount: 50000, utr: "UTR9", paymentDate: "2026-12-10", bankAccount: "Patient A/C" })
    const c = E.getClaimById(rid)!
    if (c.settlement?.paidTo !== "Patient") throw new Error("paidTo " + c.settlement?.paidTo)
    E.reconcile(rid, "")
    E.closeClaim(rid)
  })
  // ---- Billing hand-over must not skip eligibility / pre-authorisation ----
  let billId = ""
  await step("insured IP bill opens a case at ELIGIBILITY (not later)", () => {
    const bill = BillingDatabase.createClaim({
      patientId: "UMR9003", patientName: "Handover Patient", mrn: "9003", age: 40, gender: "Male", phone: "9",
      department: "Inpatient", carePathway: "Inpatient", dateOfService: "2026-11-12", insuranceProvider: "Star Health",
      status: "Draft", items: [{ id: "i1", description: "Room", category: "Room / Bed Charges", cptCode: "", quantity: 1, unitPrice: 20000, total: 20000, insuranceCovered: 0, patientPayable: 20000 }],
    } as any)
    billId = bill.id
    const c = E.getClaims().find((x) => x.billingClaimId === billId)
    if (!c) throw new Error("no case")
    if (c.status !== "ELIGIBILITY_PENDING") throw new Error("case started at " + c.status)
  })
  await step("billing cannot hand the bill to insurance before discharge", () => {
    try { BillingDatabase.billToInsurance(billId, { provider: "Star Health", policyNumber: "P1" }) }
    catch (e) { if (!/not ready for the final bill/.test((e as Error).message)) throw e; return }
    throw new Error("bill handed over early")
  })
  await step("even if a hand-over is recorded early, the case does not jump ahead", () => {
    BillingDatabase.updateClaim(billId, { tpa: { billedAt: new Date().toISOString(), billedBy: "Counter", claimAmount: 20000 } } as any)
    const c = E.getClaims().find((x) => x.billingClaimId === billId)!
    if (c.status !== "ELIGIBILITY_PENDING") throw new Error("jumped to " + c.status)
  })
  await step("an already-wrong case (final bill, no eligibility) is repaired", () => {
    const raw = JSON.parse(localStorage.getItem("hospai_insurance_cases_v2") || "[]")
    const target = raw.find((x: any) => x.billingClaimId === billId)
    target.status = "FINAL_BILL_READY"
    delete target.eligibility
    delete target.preAuth
    localStorage.setItem("hospai_insurance_cases_v2", JSON.stringify(raw))
    const c = E.getClaims().find((x) => x.billingClaimId === billId)!
    if (c.status !== "ELIGIBILITY_PENDING") throw new Error("not repaired: " + c.status)
  })
  // ---- Insured admission: the patient's own bill is zero, the insurance record carries the full amount ----
  await step("insured IP patient's bill shows ₹0 payable; insurance record shows the full amount", () => {
    const b = BillingDatabase.getClaimById(billId)!
    const c = E.getClaims().find((x) => x.billingClaimId === billId)!
    if (b.patientPortion !== 0 || b.balanceDue !== 0) throw new Error(`patient owes ${b.balanceDue}`)
    if (b.insurancePortion !== b.totalAmount || !(b.totalAmount > 0)) throw new Error("insurer portion " + b.insurancePortion)
    if (c.totalHospitalBill !== b.totalAmount || c.finalClaimAmount !== b.totalAmount) throw new Error(`case shows ${c.totalHospitalBill}/${c.finalClaimAmount}`)
    if (!b.items.every((it) => it.patientPayable === 0 && it.insuranceCovered === it.total)) throw new Error("line split not applied")
  })
  // ---- An inpatient stay is billed at the bed's own rate and nothing invented ----
  await step("IP bill = days x the bed's assigned rate only (no built-in nursing / rounds / defaults)", () => {
    BedDatabase.save([
      { id: 901, ward: "3rd floor", room_no: "301", bed_no: "1", bed_type: "ICU", status: "Occupied", daily_rate: 8000, patient_id: "ER-PAT-7777",
        patient_name: "Naruto", patient_last_name: "Uzumaki", admission_date: new Date().toISOString(), allocated_at: new Date().toISOString() } as any,
    ])
    const bill = BillingDatabase.getClaims().find((c) => c.patientId === "ER-PAT-7777")
    if (!bill) throw new Error("no running bill for the occupied bed")
    if (bill.patientName !== "Naruto Uzumaki") throw new Error("name " + bill.patientName)
    if (bill.items.length !== 1) throw new Error("lines: " + bill.items.map((i) => i.description).join(" | "))
    if (bill.totalAmount !== 8000) throw new Error("total " + bill.totalAmount)
    if (bill.age !== 0 || /555-0100/.test(String((bill as any).contactPhone || ""))) throw new Error("invented demographics")
    const clr = BillingDatabase.getInpatientFinancialClearance("ER-PAT-7777", "Naruto Uzumaki")
    if (clr.balanceDue !== 8000) throw new Error("card would show due " + clr.balanceDue)
  })
  // ---- ER: the complete amount (consultation + bed + charges) is shown, and reaches billing only when sent ----
  await step("ER total includes the bed; it is not on the billing counter until 'Send to Billing'", () => {
    const info = { visitNo: "ER-2026-00888", patientId: "ER-PAT-888", patientName: "Send Test", age: 30, gender: "Male" as const, phone: "9", doctor: "Dr A" }
    BillingDatabase.openErClaim({ ...info, consultationFee: 500 })
    BillingDatabase.setErBedCharge(info, { label: "ER Bed 2", perDay: 1200, days: 1 })
    // a medicine the pharmacy does not hold has no price: nothing is billed
    if (resolveErItemPrice("Unlisted Drug X", "medication").unitPrice !== 0) throw new Error("an unknown medicine was priced from a rate card")
    BillingDatabase.addErClinicalCharge(info.visitNo, { patientId: info.patientId, patientName: info.patientName }, { description: "Medication: Unlisted Drug X", unitPrice: 0 })
    if (BillingDatabase.getErCharges(info.visitNo).some((i) => /Unlisted/.test(i.description))) throw new Error("an unpriced medicine was billed")
    // a medicine in pharmacy stock is billed at the pharmacy MRP x quantity
    PharmacyDatabase.addMedicine({ id: "MED-ER-T1", medicineName: "Test Paracetamol", genericName: "", brandName: "", manufacturer: "", dosageForm: "Inj", strength: "1g", unit: "vial", barcode: "", taxPercentage: 0, reorderLevel: 0, storageCondition: "", scheduleType: "", controlledSubstanceFlag: false, activeStatus: "Active", createdAt: new Date().toISOString(), mrp: 40, stock: 20 } as any)
    const listed = BillingDatabase.getErPharmacyStock().find((m) => m.name === "Test Paracetamol")
    if (!listed || listed.price !== 40 || listed.stock !== 20) throw new Error("pharmacy medicine not offered with its MRP and stock: " + JSON.stringify(listed))
    const pr = resolveErItemPrice("Test Paracetamol Inj 1g", "medication")
    if (pr.unitPrice !== 40) throw new Error("pharmacy MRP not used: " + pr.unitPrice)
    const medBefore = BillingDatabase.getErCharges(info.visitNo).reduce((n, i) => n + i.total, 0)
    BillingDatabase.addErClinicalCharge(info.visitNo, { patientId: info.patientId, patientName: info.patientName }, { description: "Medication: Test Paracetamol", unitPrice: pr.unitPrice, quantity: 3 })
    const medAfter = BillingDatabase.getErCharges(info.visitNo).reduce((n, i) => n + i.total, 0)
    if (medAfter - medBefore !== 120) throw new Error("medicine charge should be 3 x 40 = 120, got " + (medAfter - medBefore))
    const med = BillingDatabase.getErCharges(info.visitNo).find((i) => /Test Paracetamol/.test(i.description))!
    BillingDatabase.removeErCharge(info.visitNo, med.id)
    // a charge the desk types is added; removing it takes it off again
    BillingDatabase.addErManualCharge(info, { description: "Nebulisation", category: "Procedure / Surgery", unitPrice: 50, quantity: 1 })
    try { BillingDatabase.addErManualCharge(info, { description: "No amount", category: "Other", unitPrice: 0, quantity: 1 }) } catch { /* expected */ }
    if (BillingDatabase.getErCharges(info.visitNo).some((i) => i.description === "No amount")) throw new Error("a charge without an amount was accepted")
    const nebu = BillingDatabase.getErCharges(info.visitNo).find((i) => i.description === "Nebulisation")!
    BillingDatabase.removeErCharge(info.visitNo, nebu.id)
    if (BillingDatabase.getErCharges(info.visitNo).some((i) => i.id === nebu.id)) throw new Error("charge was not removed")
    BillingDatabase.addErManualCharge(info, { description: "ECG", category: "Procedure / Surgery", unitPrice: 50, quantity: 1 })
    const before = BillingDatabase.getErFinancialClearance(info.visitNo, info.patientName)
    if (before.status !== "unbilled" || before.unbilledAmount !== 1750) throw new Error(`before send: ${before.status} / ${before.unbilledAmount} (expected unbilled 1750 = 500 + 1200 + 50)`)
    const onCounter = (): boolean => BillingDatabase.getClaims().filter((c) => c.erSentToBilling !== false).some((c) => c.encounterId === info.visitNo)
    if (onCounter()) throw new Error("the bill was on the billing counter before it was sent")
    const run = BillingDatabase.getClaims().find((c) => c.encounterId === info.visitNo)!
    BillingDatabase.updateClaim(run.id, { erSentToBilling: true })
    const after = BillingDatabase.getErFinancialClearance(info.visitNo, info.patientName)
    if (!onCounter() || after.status !== "due" || after.balanceDue !== 1750) throw new Error(`after send: ${after.status} / ${after.balanceDue}`)
  })
  // ---- A patient moved to a new bed: that bed is billed from the move, other dues are shown apart ----
  await step("moved patient: new bed billed from the move (not the admission) and other dues kept separate", () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 86400000).toISOString()
    BedDatabase.save([
      { id: 951, ward: "5th floor", room_no: "501", bed_no: "1", bed_type: "General", status: "Occupied", daily_rate: 100, patient_id: "PAT-8888",
        patient_name: "Mini", patient_last_name: "Luffy", admission_date: threeDaysAgo, allocated_at: new Date().toISOString() } as any,
    ])
    // an earlier, unpaid invoice for the bed the patient left
    BillingDatabase.createClaim({
      patientId: "PAT-8888", patientName: "Mini Luffy", mrn: "8888", age: 38, gender: "Male", phone: "9",
      department: "ICU", carePathway: "ICU invoice INV-IP-1", dateOfService: "2026-11-12", insuranceProvider: "Self-Pay", status: "Draft",
      items: [{ id: "old", description: "ICU bed (earlier stay)", category: "Room / Bed Charges", cptCode: "", quantity: 1, unitPrice: 8000, total: 8000, insuranceCovered: 0, patientPayable: 8000 }],
    } as any)
    const clr = BillingDatabase.getInpatientFinancialClearance("PAT-8888", "Mini Luffy")
    const bedLine = (clr.lines || []).find((l) => /Bed 1 Accommodation/.test(l.description))
    if (!bedLine || bedLine.total !== 100) throw new Error(`this bed's line is ${bedLine?.total}, expected 100 (1 day at the bed's rate)`)
    if (!(clr.lines || []).some((l) => l.total === 8000)) throw new Error("the earlier bed's 8000 is not itemised")
    if (clr.balanceDue !== 8100) throw new Error(`total ${clr.balanceDue}`)
  })
  // ---- ER bed charge: typed by hand, one line per visit, replaced when changed ----
  await step("ER bed charge: no default, must be entered, saved once and replaced on change", () => {
    const info = { visitNo: "ER-2026-00777", patientId: "ER-PAT-777", patientName: "Bed Test", age: 30, gender: "Male" as const, phone: "9", doctor: "Dr A" }
    if (BillingDatabase.getErBedCharge(info.visitNo) !== null) throw new Error("a bed charge existed before anything was entered")
    try { BillingDatabase.setErBedCharge(info, { label: "ER Bed 4", perDay: 0, days: 1 }) } catch { /* expected */ }
    if (BillingDatabase.getErBedCharge(info.visitNo) !== null) throw new Error("a zero amount was accepted")
    BillingDatabase.setErBedCharge(info, { label: "ER Bed 4", perDay: 1200, days: 2 })
    let bill = BillingDatabase.getClaims().find((c) => c.encounterId === info.visitNo)!
    if (bill.totalAmount !== 2400 || bill.items.filter((i) => i.cptCode === "ER-BED").length !== 1) throw new Error("bill " + bill.totalAmount)
    BillingDatabase.setErBedCharge(info, { label: "ER Bed 5", perDay: 1500, days: 1 })
    bill = BillingDatabase.getClaims().find((c) => c.encounterId === info.visitNo)!
    const bed = BillingDatabase.getErBedCharge(info.visitNo)!
    if (bill.totalAmount !== 1500 || bed.label !== "ER Bed 5" || bed.total !== 1500) throw new Error(`after edit: ${bill.totalAmount} / ${bed.label}`)
  })
  // ---- The step-by-step plan is live: it reads the real case ----
  const states = (id: string) => Object.fromEntries(PLAN.flatMap((st) => st.items.map((it) => [it.id, it.check(E.getClaimById(id)!)])))
  await step("plan: a finished cashless case has no step left to do", () => {
    const left = Object.entries(states(id)).filter(([, v]) => v === "todo").map(([k]) => k)
    if (left.length) throw new Error("still todo: " + left.join(", ") + " | unverified: " + E.getClaimById(id)!.documents.filter((d: any) => d.isMandatory && d.status !== "Verified").map((d: any) => d.category + ":" + d.documentType).join("; "))
  })
  await step("plan: a finished reimbursement case has no step left to do", () => {
    const left = Object.entries(states(rid)).filter(([, v]) => v === "todo").map(([k]) => k)
    if (left.length) throw new Error("still todo: " + left.join(", "))
  })
  await step("plan: reimbursement skips the pre-authorisation steps", () => {
    const st = states(rid)
    if (st.request !== "na" || st.send !== "na" || st.decision !== "na") throw new Error(JSON.stringify(st))
  })
  await step("plan: a brand-new case shows eligibility as the work to do", () => {
    const c = E.openEncounterCase({ patientId: "UMR9100", patientName: "Plan Patient", encounterType: "IP", department: "General Medicine" }, policy)
    const st = states(c.id)
    if (st.policy !== "done" || st.record !== "todo" || st.request !== "todo") throw new Error(JSON.stringify(st))
    E.recordEligibility(c.id, { status: "Eligible", balanceAvailable: 100000, inNetwork: true } as any)
    if (states(c.id).record !== "done") throw new Error("eligibility step did not tick after recording it")
  })
  await step("rate master: nothing is built in; amounts set by Administration are what every screen reads", () => {
    if (PriceMaster.registrationFee(true) !== 0 || PriceMaster.bedRate("ICU") !== 0 || PriceMaster.service("Minor Wound Dressing & Sterile Bandaging") !== 0) throw new Error("an amount existed before Administration set one")
    const cbc = ALL_LAB_TESTS.find((t) => t.code === "CBC")!
    if (cbc.price !== 0) throw new Error("built-in lab price still present: " + cbc.price)
    if (priceForTest("CBC") !== 0) throw new Error("lab fallback price invented")
    PriceMaster.setFee("registration_new", 150)
    PriceMaster.setBedRate("ICU", 6000)
    PriceMaster.setService("Minor Wound Dressing & Sterile Bandaging", 75)
    DiagnosticTariffDatabase.setCatalogPrice({ code: "CBC", name: cbc.name, category: "Laboratory", department: "Laboratory" }, 740)
    if (PriceMaster.registrationFee(true) !== 150 || PriceMaster.registrationFee(false) !== 0) throw new Error("registration fee")
    if (PriceMaster.bedRate("icu") !== 6000) throw new Error("bed rate")
    const svc = MASTER_HOSPITAL_SERVICES.find((x) => x.name === "Minor Wound Dressing & Sterile Bandaging")!
    if (svc.price !== 75) throw new Error("service price not read from the master: " + svc.price)
    if (cbc.price !== 740 || priceForTest("CBC") !== 740) throw new Error("lab price not read from the tariff: " + cbc.price)
    PriceMaster.setFee("registration_new", null)
    DiagnosticTariffDatabase.setCatalogPrice({ code: "CBC", name: cbc.name, category: "Laboratory", department: "Laboratory" }, null)
    if (PriceMaster.registrationFee(true) !== 0 || cbc.price !== 0) throw new Error("clearing did not reset to 0")
    PriceMaster.setBedRate("ICU", null)
    PriceMaster.setService("Minor Wound Dressing & Sterile Bandaging", null)
  })
  await step("rate master: extra bed types, fees and charges can be added and removed", () => {
    PriceMaster.addBedType("Deluxe Suite", 9000)
    if (!PriceMaster.allBedTypes().includes("Deluxe Suite") || PriceMaster.bedRate("deluxe suite") !== 9000) throw new Error("added bed type not usable")
    try { PriceMaster.addBedType("icu", 100); throw new Error("duplicate bed type accepted") } catch (e: any) { if (/accepted/.test(e.message)) throw e }
    const c = PriceMaster.addCustom({ name: "Ambulance (city)", category: "Ambulance / Transport", price: 1200 })
    if (PriceMaster.customPrice("ambulance (city)") !== 1200) throw new Error("extra charge not found by name")
    try { PriceMaster.addCustom({ name: "Free thing", category: "Other", price: 0 }); throw new Error("zero charge accepted") } catch (e: any) { if (/accepted/.test(e.message)) throw e }
    PriceMaster.removeCustom(c.id)
    PriceMaster.removeBedType("Deluxe Suite")
    if (PriceMaster.allBedTypes().includes("Deluxe Suite") || PriceMaster.customPrice("Ambulance (city)") !== 0) throw new Error("removal failed")
  })
  await step("doctor workspace / billing pick lists come from the rate master; deleted items leave them and can be restored", () => {
    const names = () => getOrderableTests().map((t) => t.name)
    if (!names().includes("CBC / Complete Hemogram")) throw new Error("lab catalogue test missing from the doctor's test list")
    PriceMaster.setHidden("lab:CBC", true)
    if (names().includes("CBC / Complete Hemogram")) throw new Error("deleted test still offered")
    PriceMaster.setHidden("lab:CBC", false)
    if (!names().includes("CBC / Complete Hemogram")) throw new Error("restore failed")
    DiagnosticTariffDatabase.addTariffItem({ code: "LAB-X-1", name: "Special Panel X", category: "Laboratory", department: "Laboratory", subCategory: "Added tests", hospitalAvailability: "Enabled", price: 900 } as any)
    const sp = getOrderableTests().find((t) => t.name === "Special Panel X")
    if (!sp || sp.price !== 900) throw new Error("added test not offered with its price")
    DiagnosticTariffDatabase.deleteTariffItem("LAB-X-1")
    const c = PriceMaster.addCustom({ name: "Oxygen per hour", category: "Procedure / Surgery", price: 120 })
    const svc = getServiceCatalogue().find((x) => x.name === "Oxygen per hour")
    if (!svc || svc.price !== 120) throw new Error("added service not in the doctor's service list")
    PriceMaster.setHidden("svc:Minor Wound Dressing & Sterile Bandaging", true)
    if (getServiceCatalogue().some((x) => x.name === "Minor Wound Dressing & Sterile Bandaging")) throw new Error("deleted service still offered")
    PriceMaster.setHidden("svc:Minor Wound Dressing & Sterile Bandaging", false)
    PriceMaster.removeCustom(c.id)
    PriceMaster.setHidden("bed:ICU", true)
    if (PriceMaster.allBedTypes().includes("ICU")) throw new Error("deleted bed type still offered")
    PriceMaster.setHidden("bed:ICU", false)
  })
  await step("registration fee + consultation fee follow the rates for patients already registered, and bills pick them up; paid visits keep their amount", () => {
    const reg = (db as any).registerNewPatient({ firstName: "Rate", lastName: "Probe", age: 40, sex: "Female", phone: "9000000001", dept: "General Medicine" } as any)
    const id = reg.encounter.id
    db.updateEncounter(id, { assignedDoctor: "Dr Probe", status: "Consultation Completed" } as any)
    if (getDoctorConsultationFee("") !== 0 || getDoctorConsultationFee("Nobody Known") !== 0) throw new Error("doctor fee fallback invented")
    const billOf = () => BillingDatabase.getClaims().find((c) => (c.encounterId || "").toLowerCase() === id.toLowerCase() && c.status !== "Voided")
    const regLine = () => billOf()?.items.find((it) => it.id === "ITEM-REG-" + id)
    if (regLine()) throw new Error("a registration line existed with no fee set")
    PriceMaster.setFee("registration_new", 150)
    const enc1 = db.getEncounters().find((e) => e.id === id)!
    if (enc1.billing.registrationFee !== 150) throw new Error("existing patient's registration fee not updated: " + enc1.billing.registrationFee)
    if (!regLine() || regLine()!.total !== 150) throw new Error("bill has no Rs 150 registration line: " + JSON.stringify(regLine()))
    PriceMaster.setFee("registration_new", 200)
    db.getEncounters()
    if (regLine()?.total !== 200) throw new Error("bill did not follow the changed fee: " + regLine()?.total)
    db.updateEncounter(id, { billing: { status: "Paid" } } as any)
    PriceMaster.setFee("registration_new", 999)
    db.getEncounters()
    const paidEnc = db.getEncounters().find((e) => e.id === id)!
    if (paidEnc.billing.registrationFee === 999) throw new Error("a paid visit changed amount")
    PriceMaster.setFee("registration_new", null)
  })
  const mkBill = (patientId: string, lines: Array<[string, string, number]>) => {
    const items = lines.map(([description, category, total], k) => ({ id: `LG-${patientId}-${k}`, description, category: category as any, cptCode: "", quantity: 1, unitPrice: total, total, insuranceCovered: 0, patientPayable: total }))
    const sum = items.reduce((a, it) => a + it.total, 0)
    return BillingDatabase.createClaim({ patientName: "Ledger " + patientId, patientId, mrn: patientId, age: 40, gender: "Male", phone: "", department: "Outpatient", insuranceProvider: "Self-Pay", policyNumber: "", status: "Draft", items, subtotal: sum, discount: 0, tax: 0, totalAmount: sum, insurancePortion: 0, patientPortion: sum, amountPaid: 0, balanceDue: sum, dateOfService: "2026-11-13" } as any)
  }
  const pay = (id: string, amount: number, extra: any = {}) => BillingDatabase.recordPayment(id, { amount, paymentMethod: "Cash", collectedBy: "Test", ...extra })
  const attempt = (fn: () => unknown) => { try { fn(); return null } catch (e: any) { return String(e?.message || e) } }
  const bal = (id: string) => { const c = BillingDatabase.getClaims().find((x) => x.id === id || x.invoiceNo === id); if (!c) throw new Error("bill " + id + " not found among " + BillingDatabase.getClaims().map((x) => x.id + ":" + x.patientId).join(",")); return c }

  await step("investigation ledger: payment already taken on the bill comes off what the investigation desk asks (no hard-coded amounts)", () => {
    const c = mkBill("LGR1", [["Consultation", "Consultation", 500], ["MRI", "Radiology / Imaging", 0], ["ESR", "Laboratory", 2000], ["CBC", "Laboratory", 120], ["RFT", "Laboratory", 0]])
    pay(c.invoiceNo, 520) // general money (consultation + a small amount that spills onto the investigations)
    const l = BillingDatabase.getInvestigationLedger(bal(c.invoiceNo))
    if (l.invGross !== 2120 || l.invPaid !== 20 || l.invDue !== 2100 || l.otherDue !== 0) throw new Error("ledger wrong: " + JSON.stringify(l))
    if (bal(c.invoiceNo).balanceDue !== 2100) throw new Error("OP bill balance should be 2100, is " + bal(c.invoiceNo).balanceDue)
  })

  await step("investigation payment: never above the outstanding balance, partial then final, same collection recorded once", () => {
    const c = mkBill("LGR2", [["Consultation", "Consultation", 500], ["ESR", "Laboratory", 2000], ["CBC", "Laboratory", 120]])
    pay(c.invoiceNo, 520)
    const over = attempt(() => pay(c.invoiceNo, 2120, { scope: "investigation" }))
    if (!over || !/more than the outstanding/.test(over)) throw new Error("over-collection was accepted: " + over)
    pay(c.invoiceNo, 1000, { scope: "investigation", idempotencyKey: "K-A" })
    const again = pay(c.invoiceNo, 1000, { scope: "investigation", idempotencyKey: "K-A" }) // double click / second device with the same view
    if (bal(c.invoiceNo).payments.length !== 2) throw new Error("same collection recorded twice: " + bal(c.invoiceNo).payments.length)
    if (again.payment.amount !== 1000) throw new Error("retry did not return the first payment")
    let l = BillingDatabase.getInvestigationLedger(bal(c.invoiceNo))
    if (l.invDue !== 1100 || bal(c.invoiceNo).balanceDue !== 1100) throw new Error("after part payment: " + JSON.stringify(l))
    const stale = attempt(() => pay(c.invoiceNo, 1100 + 1, { scope: "investigation", idempotencyKey: "K-B" }))
    if (!stale) throw new Error("a stale second device was allowed to overcollect")
    pay(c.invoiceNo, 1100, { scope: "investigation", idempotencyKey: "K-C" })
    l = BillingDatabase.getInvestigationLedger(bal(c.invoiceNo))
    if (l.invDue !== 0 || bal(c.invoiceNo).balanceDue !== 0 || bal(c.invoiceNo).status !== "Paid") throw new Error("final state: " + JSON.stringify(l) + " " + bal(c.invoiceNo).status)
    if (!attempt(() => pay(c.invoiceNo, 1, { scope: "investigation", idempotencyKey: "K-D" }))) throw new Error("payment on a settled bill was accepted")
  })

  await step("investigation ledger: advance, consultation paid separately, discount", () => {
    const adv = mkBill("LGR3", [["Consultation", "Consultation", 500], ["CT", "Radiology / Imaging", 3000]])
    pay(adv.invoiceNo, 300, { scope: "investigation" }) // advance toward the scan
    let l = BillingDatabase.getInvestigationLedger(bal(adv.invoiceNo))
    if (l.invPaid !== 300 || l.invDue !== 2700 || l.otherDue !== 500) throw new Error("advance: " + JSON.stringify(l))
    pay(adv.invoiceNo, 500) // consultation paid separately: must not touch the investigations
    l = BillingDatabase.getInvestigationLedger(bal(adv.invoiceNo))
    if (l.invPaid !== 300 || l.otherDue !== 0 || l.invDue !== 2700) throw new Error("consultation payment leaked into investigations: " + JSON.stringify(l))
    const d = mkBill("LGR4", [["Consultation", "Consultation", 500], ["MRI", "Radiology / Imaging", 6000]])
    pay(d.invoiceNo, 500)
    pay(d.invoiceNo, 5400, { scope: "investigation", discount: 600 })
    l = BillingDatabase.getInvestigationLedger(bal(d.invoiceNo))
    if (l.invDue !== 0 || bal(d.invoiceNo).balanceDue !== 0 || bal(d.invoiceNo).discount !== 600) throw new Error("discount: " + JSON.stringify(l) + " bal " + bal(d.invoiceNo).balanceDue + " disc " + bal(d.invoiceNo).discount + " total " + bal(d.invoiceNo).totalAmount + " pp " + bal(d.invoiceNo).patientPortion + " paid " + bal(d.invoiceNo).amountPaid)
    if (!attempt(() => pay(mkBill("LGR5", [["Lab", "Laboratory", 100]]).invoiceNo, 10, { discount: 500 }))) throw new Error("a discount above the balance was accepted")
  })

  await step("investigation ledger: refunds and cancelled services are accounted for", () => {
    const base = { id: "x", invoiceNo: "x", status: "Accepted", balanceDue: 0, amountPaid: 0, discount: 0 } as any
    const claim = (items: any[], payments: any[], balanceDue: number, amountPaid: number) => ({ ...base, items, payments, balanceDue, amountPaid })
    const item = (id: string, category: string, total: number) => ({ id, description: id, category, total })
    const pm = (amount: number, scope: any, at: string) => ({ id: at, amount, scope, paymentDate: at })
    // 1000 paid for the investigations, then 400 refunded (cancelled test): 600 stays paid, 400 due again
    let l = BillingDatabase.getInvestigationLedger(claim([item("a", "Laboratory", 1000)], [pm(1000, "investigation", "2026-01-01"), pm(-400, "investigation", "2026-01-02")], 400, 600))
    if (l.invPaid !== 600 || l.invDue !== 400) throw new Error("refund: " + JSON.stringify(l))
    // the service is cancelled (line removed): nothing left to pay for it, its payment shows as paid-in-excess for refund handling
    l = BillingDatabase.getInvestigationLedger(claim([item("a", "Laboratory", 600)], [pm(600, "investigation", "2026-01-01")], 0, 600))
    if (l.invDue !== 0 || l.invGross !== 600) throw new Error("cancelled line: " + JSON.stringify(l))
    // the amount due can never exceed the bill's own outstanding balance
    l = BillingDatabase.getInvestigationLedger(claim([item("a", "Laboratory", 2000)], [], 150, 1850))
    if (l.invDue !== 150) throw new Error("due above the bill's balance: " + JSON.stringify(l))
  })
  await step("aadhaar: 12 digits with a valid check digit; mistyped, short and masked values are not accepted", () => {
    const ok = "273645182903" // checksum-valid sample number
    if (aadhaarState(ok) !== "valid") throw new Error("a valid number was refused")
    if (aadhaarState("273645182904") !== "invalid") throw new Error("a mistyped last digit was accepted")
    if (aadhaarState("2736 4518") !== "partial" || aadhaarState("") !== "empty") throw new Error("partial / empty state")
    if (aadhaarState("100000000000") !== "invalid") throw new Error("a number starting with 1 was accepted")
    if (aadhaarDigits("2736-4518 2903 99") !== ok) throw new Error("digits not cleaned / capped at 12: " + aadhaarDigits("2736-4518 2903 99"))
    if (formatAadhaar(ok) !== "2736 4518 2903") throw new Error("grouping: " + formatAadhaar(ok))
  })
  await step("bed card Due is the stay's bill only: an unpaid OP bill of the same patient is not added to the bed", () => {
    const pid = "BEDDUE-1"
    const line = (id: string, description: string, category: string, total: number) => ({ id, description, category: category as any, cptCode: "", quantity: 1, unitPrice: total, total, insuranceCovered: 0, patientPayable: total })
    const make = (department: string, items: any[], encounterId: string) => {
      const sum = items.reduce((a: number, it: any) => a + it.total, 0)
      return BillingDatabase.createClaim({ patientName: "Bed Due", patientId: pid, mrn: pid, age: 39, gender: "Male", phone: "", department, encounterId, insuranceProvider: "Self-Pay", policyNumber: "", status: "Draft", items, subtotal: sum, discount: 0, tax: 0, totalAmount: sum, insurancePortion: 0, patientPortion: sum, amountPaid: 0, balanceDue: sum, dateOfService: "2026-11-13" } as any)
    }
    make("Outpatient", [line("o1", "Patient Registration Fee", "Registration", 20), line("o2", "Physician Consultation Fee", "Consultation", 500)], "ENC-OPX")
    make("Inpatient", [line("i1", "Ward - Bed 1 Accommodation (1 day)", "Room / Bed", 1000)], "ADM-X1")
    const c = BillingDatabase.getInpatientFinancialClearance(pid)
    if (c.balanceDue !== 1000) throw new Error("bed Due should be the stay's 1000, got " + c.balanceDue + " " + JSON.stringify((c.lines || []).map((l) => l.description + ":" + l.total)))
    if ((c.lines || []).some((l) => /Registration|Consultation/.test(l.description))) throw new Error("OP lines leaked into the bed breakdown")
  })
  await step("helpline numbers: nothing built in; Administration's numbers (primary first) reach the OP card, messages and headers", () => {
    for (const h of HospitalContacts.list()) HospitalContacts.remove(h.id)
    const card = (b: ReturnType<typeof getBranding>) => cardHelplines(b).map((n) => `${n.label}<b>${n.number}</b>`).join("")
    if (getBranding().helpline !== "" || getBranding().helplines.length !== 0) throw new Error("a helpline is built in: " + getBranding().helpline)
    if (card(getBranding()) !== "") throw new Error("the card prints a number nobody set: " + card(getBranding()))
    if (/Helpline/.test(renderTemplate("Your visit is booked. Helpline: {helpline}"))) throw new Error("an empty helpline label is sent in messages")
    let threw = false
    try { HospitalContacts.add("Helpline", "abc") } catch { threw = true }
    if (!threw) throw new Error("a non-number was accepted")
    HospitalContacts.add("Helpline", "0891-2000111")
    const list = HospitalContacts.add("Emergency", "108")
    if (list.length !== 2) throw new Error("two numbers expected")
    HospitalContacts.setPrimary(list.find((x) => x.label === "Emergency")!.id)
    const b = getBranding()
    if (b.helplines[0].number !== "108" || b.helplines[1].number !== "0891-2000111") throw new Error("primary is not first: " + JSON.stringify(b.helplines))
    const html = card(b)
    if (!html.includes("Emergency<b>108</b>") || !html.includes("Helpline<b>0891-2000111</b>")) throw new Error("numbers are not on the OP card")
    if (!/108/.test(renderTemplate("Call {helpline}"))) throw new Error("messages do not use the number")
    if (helplineText() !== "Emergency 108 · Helpline 0891-2000111") throw new Error("header line: " + helplineText())
    HospitalContacts.remove(HospitalContacts.list()[0].id)
    if (card(getBranding()).includes("Emergency<b>108</b>")) throw new Error("a deleted number is still on the card")
    for (const h of HospitalContacts.list()) HospitalContacts.remove(h.id)
  })
  const out = results.join("\n")
  console.log(out)
  console.log("STATUS NOW:", E.getClaimById(id)?.status)
  process.exit(results.some((r) => r.startsWith("FAIL")) ? 1 : 0)
})().catch((e) => { console.log(results.join("\n")); console.log("HARNESS ERROR", e); process.exit(1) })
