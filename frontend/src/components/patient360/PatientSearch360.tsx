import { Fragment, useState } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { patientPageUrl } from "../patientRecord/patientRecord"
import { p360, type Row } from "./api"
import { ClinicalTab, CommsTab, ConsentTab, EncountersTab, JourneyTab, MedicationsTab, NewTask, SurveysTab, TimelineTab } from "./PatientWorkspace"
import TasksView from "./TasksView"
import { Badge, btnCls, Card, cx, Empty, ErrorBox, errMsg, fmtDate, fmtDateTime, fullName, inputCls, Loading, pretty, Table, useAsync, firstSymptoms } from "./ui"

type Tab = "overview" | "timeline" | "clinical" | "vitals" | "care_team" | "billing" | "encounters" | "medications" | "labs" | "appointments" | "journey" | "surveys" | "communications" | "tasks" | "notes" | "documents" | "consent"

const TABS: Array<{ key: Tab; label: string; icon: string }> = [
  { key: "overview", label: "Overview", icon: "🏠" },
  { key: "timeline", label: "Timeline", icon: "🕒" },
  { key: "clinical", label: "Clinical", icon: "📋" },
  { key: "vitals", label: "Vitals", icon: "❤️" },
  { key: "encounters", label: "Encounters", icon: "🩺" },
  { key: "care_team", label: "Care Team", icon: "👥" },
  { key: "medications", label: "Medications", icon: "💊" },
  { key: "labs", label: "Labs & Imaging", icon: "🧪" },
  { key: "appointments", label: "Appointments", icon: "📅" },
  { key: "journey", label: "Care Journey", icon: "🧭" },
  { key: "surveys", label: "Surveys", icon: "📝" },
  { key: "communications", label: "Communications", icon: "💬" },
  { key: "tasks", label: "Tasks", icon: "✅" },
  { key: "notes", label: "Notes", icon: "🗒️" },
  { key: "billing", label: "Billing & Insurance", icon: "💳" },
  { key: "documents", label: "Documents", icon: "📄" },
  { key: "consent", label: "Consent & Audit", icon: "🔒" },
]

/** The server compares phone digits only, so the query is sent as typed (just trimmed). */
const normalise = (raw: string) => raw.trim().replace(/\s+/g, " ")
const initials = (p: Row) => (fullName(p).split(" ").filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase()).join("") || "?")
const money = (v: any) => (v === null || v === undefined || v === "" ? "—" : `₹${Number(v).toLocaleString("en-IN")}`)
const DONE = ["completed", "delivered", "sent", "skipped"]

/** Lab/imaging order state, from what the Laboratory module actually recorded. */
const labState = (x: Row) => (x.reported_at || ["reported", "completed", "published"].includes(String(x.order_status).toLowerCase())) ? "Available" : "Pending"

function Tile({ icon, label, value }: { icon: string; label: string; value: number }) {
  return (
    <div className="rounded-lg bg-[#EEF3FF] px-2 py-3 text-center">
      <div className="text-xl" aria-hidden>{icon}</div>
      <div className="text-2xl font-semibold leading-tight text-[#1E3A6E]">{String(value).padStart(2, "0")}</div>
      <div className="mt-0.5 text-[11px] leading-tight text-[#475569]">{label}</div>
    </div>
  )
}

function Panel({ title, icon, badge, children, action, className }: { title: string; icon?: string; badge?: React.ReactNode; children: React.ReactNode; action?: { label: string; onClick: () => void }; className?: string }) {
  return (
    <section className={cx("flex flex-col rounded-xl border border-[#DDE2EC] bg-white p-4 shadow-sm", className)}>
      <header className="mb-2 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-[13.5px] font-semibold text-gray-900">{icon && <span aria-hidden className="flex h-7 w-7 items-center justify-center rounded-full bg-[#EEF3FF] text-sm">{icon}</span>}{title}</h3>
        {badge}
      </header>
      <div className="flex-1 text-[12px] text-[#334155]">{children}</div>
      {action && <button className="mt-3 self-end rounded-md border border-[#CBD5E1] bg-white px-3 py-1 text-[11.5px] font-medium text-[#1B4FD8] hover:bg-[#EEF3FF]" onClick={action.onClick}>{action.label} →</button>}
    </section>
  )
}

const LISTS = ["diagnoses", "vitals", "medications", "investigations", "lab_orders", "documents", "invoices", "claims", "surgeries", "beds", "er_visits", "admissions", "encounters", "appointments", "further_appointments", "journeys", "checkpoints", "tasks", "survey_responses", "communications", "authorizations"]

const none = (t: string) => <p className="text-[#64748B]">{t}</p>

function Overview({ d, go }: { d: Row; go: (t: Tab) => void }) {
  const p = d.patient
  const latestVisit = d.encounters[0] || d.admissions[0]
  const nextAppt = (d.further_appointments && d.further_appointments[0]) || d.appointments.filter((a: Row) => ["scheduled", "checked_in", "confirmed", "booked", "pending"].includes(a.status))[0] || d.appointments[0]
  const whatsappSymptomText = firstSymptoms(d.whatsapp_symptoms?.symptoms, d.patient?.symptoms, latestVisit?.symptoms, latestVisit?.chief_complaint)
  const whatsappSymptomDate = d.whatsapp_symptoms?.received_at || d.patient?.updated_at
  const isAwaitingTriage = latestVisit && (
    latestVisit.status === "Awaiting Triage" ||
    latestVisit.status === "Registered" ||
    latestVisit.status === "In Queue" ||
    latestVisit.status === "Symptoms Captured" ||
    latestVisit.department === "Awaiting Triage" ||
    (latestVisit.status || "").toLowerCase().includes("triage")
  )
  const activeJourney = d.journeys.find((j: Row) => j.state === "active") || d.journeys[0]
  const jCps = activeJourney ? d.checkpoints.filter((c: Row) => c.journey_id === activeJourney.id && c.status !== "cancelled") : []
  const progress = jCps.length ? Math.round((jCps.filter((c: Row) => DONE.includes(c.status)).length / jCps.length) * 100) : 0
  const nextCp = jCps.find((c: Row) => ["scheduled", "queued", "overdue"].includes(c.status))
  const upcoming = d.appointments.filter((a: Row) => ["scheduled", "checked_in"].includes(a.status))[0]
  const h = d.clinical_history
  const pendingReports = d.investigations.filter((x: Row) => labState(x) === "Pending").length
  const allergy = p.allergies || h?.allergies
  const due = d.invoices.reduce((n: number, i: Row) => n + Number(i.due_amount || 0), 0)
  const bed = d.beds.find((b: Row) => !b.released_at)
  const activity = [
    ...d.appointments.slice(0, 3).map((a: Row) => ({ at: a.created_at || a.appointment_date, text: `Appointment booked — ${a.department || "General"} (${pretty(a.status)})`, mod: "Appointments" })),
    ...d.encounters.slice(0, 2).map((e: Row) => ({ at: e.arrival_at, text: `${e.encounter_type} encounter — ${e.chief_complaint || "visit"}`, mod: e.encounter_type === "OP" ? "OPD" : "Inpatient" })),
    ...d.investigations.slice(0, 3).map((x: Row) => ({ at: x.created_at, text: `Lab investigation ordered — ${x.test_name}`, mod: "Laboratory" })),
    ...d.medications.slice(0, 2).map((m: Row) => ({ at: m.created_at, text: "Prescription recorded", mod: "Pharmacy" })),
    ...d.invoices.slice(0, 2).map((i: Row) => ({ at: i.created_at, text: `Invoice ${i.invoice_no} — ${pretty(i.payment_status)}`, mod: "Billing" })),
    { at: p.created_at, text: "Patient registered", mod: "Patient Management" },
  ].filter((e) => e.at).sort((a, b) => +new Date(b.at) - +new Date(a.at)).slice(0, 5)

  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-3">
        <Panel title="Patient Overview">
          <div className="grid grid-cols-4 gap-2">
            <Tile icon="🩺" label="Total Encounters" value={d.encounters.length + d.admissions.length} />
            <Tile icon="💊" label="Active Medications" value={d.medications.filter((m: Row) => m.status !== "cancelled").length} />
            <Tile icon="🧪" label="Pending Reports" value={pendingReports} />
            <Tile icon="📋" label="Open Tasks" value={d.tasks.filter((t: Row) => !["resolved", "cancelled"].includes(t.status)).length} />
          </div>
        </Panel>
        <Panel
          title="Latest Encounter"
          badge={
            latestVisit && (
              <div className="flex items-center gap-1.5 flex-wrap">
                <Badge status={latestVisit.status || (latestVisit.discharge_date ? "completed" : "active")} />
                {isAwaitingTriage && (
                  <span className="inline-flex items-center gap-1 rounded bg-amber-100 px-2 py-0.5 text-[10.5px] font-bold text-amber-900 border border-amber-300">
                    ⏳ Triage Pending
                  </span>
                )}
              </div>
            )
          }
          action={{ label: "View Details", onClick: () => go("encounters") }}
        >
          {latestVisit ? (
            <>
              <div className="text-[14px] font-semibold text-gray-900">{latestVisit.encounter_type === "OP" ? "OPD Consultation" : latestVisit.encounter_type ? `${latestVisit.encounter_type} visit` : "Inpatient admission"}</div>
              <div>{fmtDate(latestVisit.arrival_at || latestVisit.admission_date)} · {d.appointments[0]?.department || latestVisit.department || "—"}</div>
              <div className="mt-2 text-[#64748B]">Consulting Doctor</div>
              <div className="font-medium text-gray-900">{d.appointments[0]?.doctor_name || latestVisit.attending_doctor || "—"}</div>
              <div className="mt-2 text-[#64748B]">Complaint / Summary</div>
              <div>{latestVisit.chief_complaint || latestVisit.admission_reason || d.diagnoses[0]?.diagnosis_name || "Not recorded"}</div>

              {/* Further Appointments & Current Symptoms beside Awaiting Triage */}
              <div className="mt-3 space-y-2 border-t border-[#E2E8F0] pt-2.5">
                <div className="rounded border border-[#DCE6FB] bg-[#F4F7FE] p-2 text-[11.5px]">
                  <div className="flex items-center justify-between font-semibold text-gray-800">
                    <span className="flex items-center gap-1">📅 Further Appointment</span>
                    {nextAppt && <Badge status={nextAppt.status} />}
                  </div>
                  {nextAppt ? (
                    <div className="mt-0.5 text-gray-900 font-medium">
                      <span className="text-[#1B4FD8] font-bold">{fmtDateTime(nextAppt.appointment_date)}</span> · {nextAppt.doctor_name || "Doctor"} ({nextAppt.department || "General"})
                    </div>
                  ) : (
                    <div className="mt-0.5 text-[#64748B]">No further appointments scheduled.</div>
                  )}
                </div>

                <div className="rounded border border-emerald-200 bg-emerald-50/70 p-2 text-[11.5px]">
                  <div className="flex items-center justify-between font-semibold text-emerald-900">
                    <span className="flex items-center gap-1">🩺 Symptoms</span>
                    {whatsappSymptomText && whatsappSymptomDate && <span className="text-[10px] text-emerald-700 font-normal">{fmtDate(whatsappSymptomDate)}</span>}
                  </div>
                  <div className="mt-0.5 text-gray-900 font-medium italic">
                    {whatsappSymptomText ? `"${whatsappSymptomText}"` : "None reported"}
                  </div>
                </div>
              </div>
            </>
          ) : none("No encounters recorded.")}
        </Panel>
        <Panel title="Care Journey" icon="🧭" badge={activeJourney && <Badge status={activeJourney.state} />} action={{ label: "View Journey", onClick: () => go("journey") }}>
          {activeJourney ? (
            <>
              <div className="text-[14px] font-semibold text-gray-900">{pretty(activeJourney.template_key)}</div>
              <div className="mt-2 flex items-center gap-3">
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-[#E2E8F0]"><div className="h-full rounded-full bg-[#1B4FD8]" style={{ width: `${progress}%` }} /></div>
                <span className="text-[15px] font-semibold text-[#1E3A6E]">{progress}%</span>
              </div>
              <div className="mt-2 text-[#64748B]">{nextCp ? <>Next checkpoint: <span className="text-gray-900">{nextCp.label}</span> · {fmtDate(nextCp.due_at)}</> : "No pending checkpoint."}</div>
            </>
          ) : none("Not enrolled in a care journey.")}
        </Panel>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <Panel title="Clinical Context" icon="📋" badge={<Badge tone="blue">Source-linked</Badge>} action={{ label: "Open Clinical", onClick: () => go("clinical") }}>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2.5">
            <div><dt className="text-[#64748B]">Medical History</dt><dd className="font-medium text-gray-900">{h ? "Available" : "Not recorded"}</dd></div>
            <div><dt className="text-[#64748B]">Allergies</dt><dd className={allergy ? "font-semibold text-[#B45309]" : "font-medium text-gray-900"}>{allergy || "None recorded"}</dd></div>
            <div><dt className="text-[#64748B]">Surgical History</dt><dd className="font-medium text-gray-900">{h?.previous_surgeries || "Not verified"}</dd></div>
            <div><dt className="text-[#64748B]">Current Diagnosis</dt><dd className="font-medium text-gray-900">{d.diagnoses[0]?.diagnosis_name || "See consultation"}</dd></div>
          </dl>
        </Panel>
        <Panel title="Medication Summary" icon="💊" action={{ label: "View All", onClick: () => go("medications") }}>
          {d.medications.length ? (
            <>
              <div className="flex items-center justify-between"><span className="font-semibold text-gray-900">Prescribed medicines</span><Badge tone="blue">{String(d.medications.length).padStart(2, "0")} records</Badge></div>
              <ul className="mt-1.5 space-y-0.5">{d.medications.slice(0, 3).map((m: Row) => <li key={m.id}>{m.medicines.map((x: Row) => x.name).join(", ") || "—"} <Badge status={m.status} /></li>)}</ul>
              <div className="mt-1.5 text-[#64748B]">Linked to Pharmacy</div>
            </>
          ) : none("No prescriptions on record.")}
        </Panel>
        <Panel title="Labs & Imaging" icon="🧪" action={{ label: "View All", onClick: () => go("labs") }}>
          {d.investigations.length ? <ul className="space-y-2">{d.investigations.slice(0, 3).map((x: Row) => <li key={x.id} className="flex items-center justify-between gap-2"><span className="font-medium text-gray-900">{x.test_name}</span><Badge tone={labState(x) === "Available" ? "green" : "amber"}>{labState(x)}</Badge></li>)}</ul> : none("No investigations ordered.")}
        </Panel>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <Panel title="Upcoming Follow-up" icon="📅" badge={upcoming && <Badge status="pending" />} action={{ label: "View Appointments", onClick: () => go("appointments") }}>
          {upcoming ? <><div className="font-semibold text-gray-900">{upcoming.visit_type || "Appointment"}</div><div>{fmtDateTime(upcoming.appointment_date)} · {upcoming.department || "—"}</div><div className="text-[#64748B]">{upcoming.doctor_name || "—"} · Token {upcoming.token_no ?? "—"}</div></> : none("No upcoming appointment. Check the scheduler for booking status.")}
        </Panel>
        <Panel title="Recent Activity" action={{ label: "Full Timeline", onClick: () => go("timeline") }}>
          <ul className="space-y-1.5">{activity.map((e, i) => <li key={i} className="border-l-2 border-[#1B4FD8] pl-2"><span className="font-medium text-gray-900">{e.text}</span><span className="text-[#94A3B8]"> · {e.mod} · {fmtDate(e.at)}</span></li>)}</ul>
        </Panel>
        <Panel title="Billing & Insurance" icon="💳" className="bg-gradient-to-br from-[#EEF3FF] to-white">
          <div className="grid grid-cols-2 gap-2">
            <div><div className="text-[#64748B]">Invoices</div><div className="text-[15px] font-semibold text-gray-900">{d.invoices.length}</div></div>
            <div><div className="text-[#64748B]">Outstanding</div><div className={cx("text-[15px] font-semibold", due > 0 ? "text-[#B91C1C]" : "text-gray-900")}>{money(due)}</div></div>
            <div><div className="text-[#64748B]">Insurance claims</div><div className="font-medium text-gray-900">{d.claims.length ? `${d.claims.length} · ${pretty(d.claims[0].claim_status)}` : "None"}</div></div>
            <div><div className="text-[#64748B]">Surgeries / Bed</div><div className="font-medium text-gray-900">{d.surgeries.length} · {bed ? `${bed.ward || "Ward"} ${bed.bed_no || ""}` : "No bed"}</div></div>
          </div>
          {d.invoices.length > 0 && <ul className="mt-2 space-y-0.5 border-t border-[#E2E8F0] pt-2">{d.invoices.slice(0, 2).map((i: Row) => <li key={i.id} className="flex justify-between"><span>{i.invoice_no} · {pretty(i.module)}</span><span>{money(i.total_amount)} <Badge status={i.payment_status} /></span></li>)}</ul>}
        </Panel>
      </div>
    </div>
  )
}

function MoreVisits({ d }: { d: Row }) {
  return (
    <div className="mt-3 space-y-3">
      <Card title="Emergency visits">
        {d.er_visits?.length ? <Table head={["Arrived", "Visit no", "Mode", "Condition", "Doctor", "Status"]}>{d.er_visits.map((v: Row) => <tr key={v.id}><td className="px-2 py-1.5">{fmtDateTime(v.arrival_at)}</td><td className="px-2 py-1.5">{v.visit_no}</td><td className="px-2 py-1.5">{pretty(v.arrival_mode)}</td><td className="px-2 py-1.5">{v.condition_at_arrival || "—"}</td><td className="px-2 py-1.5">{v.assigned_doctor_name || "—"}</td><td className="px-2 py-1.5"><Badge status={v.status} /></td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
      <div className="grid gap-3 md:grid-cols-2">
        <Card title="Bed allocations">
          {d.beds.length ? <Table head={["Ward / bed", "Allocated", "Released", "Status"]}>{d.beds.map((b: Row) => <tr key={b.id}><td className="px-2 py-1.5">{b.ward || "—"} {b.room_no || ""} {b.bed_no || ""}</td><td className="px-2 py-1.5">{fmtDate(b.allocated_at)}</td><td className="px-2 py-1.5">{b.released_at ? fmtDate(b.released_at) : "—"}</td><td className="px-2 py-1.5"><Badge status={b.status} /></td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
        </Card>
        <Card title="Surgeries">
          {d.surgeries.length ? <Table head={["Procedure", "Surgeon", "Scheduled", "Status"]}>{d.surgeries.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{x.procedure_name}</td><td className="px-2 py-1.5">{x.surgeon_name || "—"}</td><td className="px-2 py-1.5">{fmtDateTime(x.scheduled_start)}</td><td className="px-2 py-1.5"><Badge status={x.status} /></td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
        </Card>
      </div>
    </div>
  )
}

const num = (v: any) => { const n = parseFloat(String(v ?? "").replace(/[^\d.]/g, "")); return Number.isFinite(n) ? n : null }

function Spark({ title, unit, pts, color }: { title: string; unit: string; pts: Array<{ at: string; v: number }>; color: string }) {
  const W = 220, H = 56, P = 4
  const vs = pts.map((p) => p.v), lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1
  const xy = pts.map((p, i) => [P + (pts.length === 1 ? (W - 2 * P) / 2 : (i * (W - 2 * P)) / (pts.length - 1)), H - P - ((p.v - lo) / span) * (H - 2 * P)])
  const last = pts[pts.length - 1]
  return (
    <div className="rounded-lg border border-[#E2E8F0] bg-white p-3">
      <div className="flex items-baseline justify-between"><span className="text-[11.5px] font-medium text-[#475569]">{title}</span><span className="text-[15px] font-semibold text-gray-900">{last.v}<span className="ml-0.5 text-[10.5px] font-normal text-[#94A3B8]">{unit}</span></span></div>
      {pts.length > 1 ? (
        <svg viewBox={`0 0 ${W} ${H}`} className="mt-1 h-14 w-full" role="img" aria-label={`${title} trend`}>
          <polyline points={xy.map((q) => q.join(",")).join(" ")} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
          {xy.map((q, i) => <circle key={i} cx={q[0]} cy={q[1]} r="2.5" fill={color}><title>{`${pts[i].v} ${unit} · ${fmtDateTime(pts[i].at)}`}</title></circle>)}
        </svg>
      ) : <p className="mt-2 text-[10.5px] text-[#94A3B8]">Only one reading so far.</p>}
      <div className="flex justify-between text-[10px] text-[#94A3B8]"><span>{pts.length > 1 ? `min ${lo} · max ${hi}` : ""}</span><span>{fmtDate(last.at)}</span></div>
    </div>
  )
}

function VitalsTab({ d }: { d: Row }) {
  const rows: Row[] = [...d.vitals].reverse() // oldest → newest for the charts
  const series = (get: (v: Row) => number | null) => rows.map((v) => ({ at: v.created_at, v: get(v) })).filter((p): p is { at: string; v: number } => p.v !== null)
  const sys = (v: Row) => num(String(v.bp ?? "").split("/")[0])
  const dia = (v: Row) => num(String(v.bp ?? "").split("/")[1])
  const charts = [
    { title: "Systolic BP", unit: "mmHg", color: "#DC2626", pts: series(sys) },
    { title: "Diastolic BP", unit: "mmHg", color: "#F97316", pts: series(dia) },
    { title: "Pulse", unit: "bpm", color: "#2563EB", pts: series((v) => num(v.pulse)) },
    { title: "Temperature", unit: "°", color: "#D97706", pts: series((v) => num(v.temperature)) },
    { title: "SpO₂", unit: "%", color: "#0891B2", pts: series((v) => num(v.spo2)) },
    { title: "Blood glucose", unit: "mg/dL", color: "#7C3AED", pts: series((v) => num(v.blood_glucose)) },
    { title: "Weight", unit: "kg", color: "#059669", pts: series((v) => num(v.weight)) },
  ].filter((c) => c.pts.length)
  if (!d.vitals.length) return <Card title="Vitals"><Empty>No vitals recorded.</Empty></Card>
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{charts.map((c) => <Spark key={c.title} {...c} />)}</div>
      <Card title="Vitals history">
        <Table head={["When", "BP", "Pulse", "Temp", "SpO₂", "Glucose", "Weight"]}>
          {d.vitals.map((v: Row) => <tr key={v.id}><td className="px-2 py-1.5">{fmtDateTime(v.created_at)}</td><td className="px-2 py-1.5">{v.bp || "—"}</td><td className="px-2 py-1.5">{v.pulse || "—"}</td><td className="px-2 py-1.5">{v.temperature || "—"}</td><td className="px-2 py-1.5">{v.spo2 ?? "—"}</td><td className="px-2 py-1.5">{v.blood_glucose ?? "—"}</td><td className="px-2 py-1.5">{v.weight ?? "—"}</td></tr>)}
        </Table>
      </Card>
    </div>
  )
}

function NotesTab({ patientId }: { patientId: string }) {
  const { data, error, loading, reload } = useAsync(() => p360.notes(patientId), [patientId])
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const save = async () => {
    if (!text.trim()) return
    setBusy(true); setMsg("")
    try { await p360.addNote(patientId, text); setText(""); reload() } catch (e) { setMsg(errMsg(e)) } finally { setBusy(false) }
  }
  return (
    <div className="space-y-3">
      <Card title="Add coordinator note">
        <textarea aria-label="Coordinator note" className={cx(inputCls, "min-h-[84px] w-full")} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)}
          placeholder="Internal note for the care team — not visible to the patient and not sent to them." />
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[11px] text-[#94A3B8]">{text.length}/2000 · Notes can't be edited or deleted once saved.</span>
          <button className={btnCls.primary} disabled={busy || !text.trim()} onClick={save}>{busy ? "Saving…" : "Save note"}</button>
        </div>
        {msg && <p className="mt-2 text-[12px] text-[#B91C1C]">{msg}</p>}
      </Card>
      <Card title="Notes">
        {error && <ErrorBox error={error} onRetry={reload} />}
        {loading && !data ? <Loading /> : data?.notes?.length ? (
          <ul className="space-y-2">{data.notes.map((n: Row) => (
            <li key={n.id} className="rounded-lg border border-[#E2E8F0] bg-white p-3">
              <p className="whitespace-pre-wrap text-[12.5px] text-gray-900">{n.note}</p>
              <p className="mt-1 text-[10.5px] text-[#94A3B8]">{n.author || "Unknown"} · {fmtDateTime(n.created_at)}</p>
            </li>
          ))}</ul>
        ) : <Empty>No notes yet.</Empty>}
      </Card>
    </div>
  )
}

/** Every symptom report the patient sent in, not just the latest. */
function SymptomHistory({ patientId }: { patientId: string }) {
  const { data, error, loading, reload } = useAsync(() => p360.symptomHistory(patientId), [patientId])
  return (
    <Card title="Symptom history (reported by the patient)">
      {error && <ErrorBox error={error} onRetry={reload} />}
      {loading && !data ? <Loading /> : data?.symptoms?.length ? (
        <ul className="space-y-2">{data.symptoms.map((s: Row) => (
          <li key={s.id} className="border-l-2 border-emerald-500 pl-3 text-[12.5px]"><span className="italic text-gray-900">"{s.symptoms}"</span><span className="text-[#94A3B8]"> · {pretty(s.channel)} · {fmtDateTime(s.created_at)}</span></li>
        ))}</ul>
      ) : <Empty>The patient hasn't reported any symptoms.</Empty>}
    </Card>
  )
}

const PRINT_CSS = `
@page { size: A4; margin: 14mm }
* { box-sizing: border-box }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11.5px; color: #111; margin: 0 }
h1 { font-size: 19px; margin: 0 0 2px; color: #12285A }
h2 { font-size: 13px; margin: 16px 0 5px; padding: 3px 6px; background: #E8F0FE; color: #12285A; border-left: 3px solid #1B4FD8; page-break-after: avoid }
.sub { color: #555; margin: 0 0 4px }
.head { border-bottom: 2px solid #1B4FD8; padding-bottom: 8px; margin-bottom: 4px }
table { width: 100%; border-collapse: collapse; margin-bottom: 2px }
th { text-align: left; background: #F1F5F9; font-weight: 700 }
th, td { border: 1px solid #CBD5E1; padding: 3px 6px; vertical-align: top }
tr { page-break-inside: avoid }
td.k { width: 22%; background: #F8FAFC; font-weight: 600; color: #334155 }
.alert { display: inline-block; margin: 0 6px 4px 0; padding: 2px 8px; border: 1px solid #FCA5A5; background: #FEF2F2; color: #991B1B; border-radius: 4px; font-weight: 600 }
.note { margin: 0 0 6px; white-space: pre-wrap }
.muted { color: #666; font-size: 10px }
.foot { margin-top: 18px; padding-top: 6px; border-top: 1px solid #CBD5E1; color: #666; font-size: 10px }
`

/** The printable document: every section that has data, as tables. Rendered to a standalone HTML page, so the app layout can't affect it. */
const LABEL = (k: string) => k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
const WHEN_KEY = /(_at|_date|_time|timestamp|^date|^time)$/i
const cellText = (k: string, v: any): string => {
  if (v === null || v === undefined || v === "") return ""
  if (typeof v === "boolean") return v ? "Yes" : "No"
  if (typeof v === "object") { const j = JSON.stringify(v); return j.length > 300 ? j.slice(0, 300) + "…" : j }
  if (WHEN_KEY.test(k) && !Number.isNaN(Date.parse(String(v)))) return /\d{2}:\d{2}/.test(String(v)) ? fmtDateTime(String(v)) : fmtDate(String(v))
  return String(v)
}

/** A module's records printed generically: every column that has at least one value, so no stored data is dropped. */
function GenericTable({ rows }: { rows: Row[] }) {
  const cols = Object.keys(Object.assign({}, ...rows)).filter((k) => rows.some((r) => cellText(k, r[k]) !== ""))
  if (!cols.length) return null
  return <table><thead><tr>{cols.map((c) => <th key={c}>{LABEL(c)}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{cellText(c, r[c]) || "—"}</td>)}</tr>)}</tbody></table>
}

function PrintSummary({ d, notes, symptoms, record }: { d: Row; notes: Row[]; symptoms: Row[]; record: Row | null }) {
  const p = d.patient, h = d.clinical_history
  const dash = (v: any) => (v === null || v === undefined || v === "" ? "—" : String(v))
  const T = (head: string[], rows: any[][]) => (
    <table><thead><tr>{head.map((x) => <th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody></table>
  )
  const S = (title: string, count: number, body: React.ReactNode) => (count > 0 ? <><h2>{title}</h2>{body}</> : null)
  const allergy = p.allergies || h?.allergies
  const admitted = d.admissions.some((a: Row) => !a.discharge_date)
  const due = d.invoices.reduce((n: number, i: Row) => n + Number(i.due_amount || 0), 0)
  const sym = firstSymptoms(d.whatsapp_symptoms?.symptoms, p.symptoms)
  const bed = d.beds.find((b: Row) => !b.released_at)
  const kv = (rows: Array<[string, any]>) => (
    <table><tbody>{[0, 2, 4, 6].map((i) => rows.slice(i, i + 2)).filter((r) => r.length).map((r, i) => <tr key={i}>{r.map(([k, v]) => <Fragment key={k}><td className="k">{k}</td><td>{dash(v)}</td></Fragment>)}{r.length === 1 && <><td className="k" /><td /></>}</tr>)}</tbody></table>
  )
  const pending = d.checkpoints.filter((c: Row) => ["scheduled", "queued", "overdue"].includes(c.status))
  const openTasks = d.tasks.filter((t: Row) => !["resolved", "cancelled"].includes(t.status))
  const hist: Array<[string, any]> = h ? [["Allergies", h.allergies], ["Existing conditions", h.existing_diseases], ["Chronic conditions", h.chronic_conditions], ["Previous surgeries", h.previous_surgeries], ["Family history", h.family_history]] : []
  const histRows = hist.filter(([, v]) => v)
  const alerts = [allergy && `Allergies: ${allergy}`, admitted && `Currently admitted${bed ? ` — ${bed.ward || "Ward"} ${bed.bed_no || ""}` : ""}`, due > 0 && `Outstanding balance ${money(due)}`].filter(Boolean) as string[]

  return (
    <div>
      <div className="head">
        <h1>Patient Summary</h1>
        <p className="sub"><b>{fullName(p)}</b> · {p.patient_id}</p>
        <p className="muted">Printed {fmtDateTime(new Date().toISOString())} from Patient 360°. Read from every module that holds this patient's data; sections with no data are omitted.</p>
      </div>

      {kv([["Name", fullName(p)], ["Patient ID", p.patient_id], ["Age / Gender", `${dash(p.age)} yrs / ${dash(p.gender)}`], ["Mobile", p.phone], ["Blood group", p.blood_group], ["Registered", p.created_at ? fmtDate(p.created_at) : "—"], ["Treating doctor", d.appointments[0]?.doctor_name], ["Department", d.appointments[0]?.department]])}

      {record && record.modules.length > 0 && <>
        <h2>Modules visited — patient pathway</h2>
        <p className="note">{record.modules.map((m: Row) => m.module).join("  →  ")}</p>
        {T(["#", "Module", "Records", "Source data", "First seen", "Latest"], record.modules.map((m: Row, i: number) => [i + 1, m.module, m.records, (m.sources || []).join(", "), m.first ? fmtDateTime(m.first) : "—", m.last ? fmtDateTime(m.last) : "—"]))}
      </>}
      {!record && <p className="muted">Cross-module records could not be loaded; only the Patient 360 profile data is shown.</p>}

      {alerts.length > 0 && <><h2>Alerts</h2><div>{alerts.map((a) => <span key={a} className="alert">{a}</span>)}</div></>}

      {S("Reported symptoms", sym || symptoms.length ? 1 : 0, <>
        {sym && <p className="note"><b>Current:</b> {sym}</p>}
        {symptoms.length > 0 && T(["When", "Channel", "Symptoms reported"], symptoms.map((s) => [fmtDateTime(s.created_at), pretty(s.channel), s.symptoms]))}
      </>)}

      {S("Medical history", histRows.length, T(["Item", "Details"], histRows.map(([k, v]) => [k, v])))}
      {S("Diagnoses", d.diagnoses.length, T(["Diagnosis", "Recorded"], d.diagnoses.map((x: Row) => [x.diagnosis_name, fmtDate(x.created_at)])))}
      {S("Vitals", d.vitals.length, T(["When", "BP", "Pulse", "Temp", "SpO₂", "Glucose", "Weight"], d.vitals.slice(0, 12).map((v: Row) => [fmtDateTime(v.created_at), dash(v.bp), dash(v.pulse), dash(v.temperature), dash(v.spo2), dash(v.blood_glucose), dash(v.weight)])))}

      {S("Admissions", d.admissions.length, T(["Admitted", "Discharged"], d.admissions.map((a: Row) => [fmtDateTime(a.admission_date), a.discharge_date ? fmtDate(a.discharge_date) : "Currently admitted"])))}
      {S("OP / IP encounters", d.encounters.length, T(["Arrived", "Type", "Complaint", "Status"], d.encounters.map((e: Row) => [fmtDateTime(e.arrival_at), dash(e.encounter_type), dash(e.chief_complaint), pretty(e.status)])))}
      {S("Emergency visits", d.er_visits.length, T(["Arrived", "Visit no", "Condition", "Doctor", "Status"], d.er_visits.map((v: Row) => [fmtDateTime(v.arrival_at), dash(v.visit_no), dash(v.condition_at_arrival), dash(v.assigned_doctor_name), pretty(v.status)])))}
      {S("Surgeries", d.surgeries.length, T(["Procedure", "Surgeon", "Scheduled", "Status"], d.surgeries.map((x: Row) => [x.procedure_name, dash(x.surgeon_name), fmtDateTime(x.scheduled_start), pretty(x.status)])))}
      {S("Bed allocations", d.beds.length, T(["Ward / bed", "Allocated", "Released"], d.beds.map((b: Row) => [`${b.ward || "—"} ${b.room_no || ""} ${b.bed_no || ""}`, fmtDate(b.allocated_at), b.released_at ? fmtDate(b.released_at) : "—"])))}

      {S("Medications", d.medications.length, T(["Prescribed", "Medicines", "Doctor", "Status"], d.medications.map((m: Row) => [fmtDate(m.created_at), (m.medicines || []).map((x: Row) => [x.name, x.dosage, x.quantity ? `× ${x.quantity}` : ""].filter(Boolean).join(" ")).join("; ") || "—", dash(m.doctor_username), pretty(m.status)])))}
      {S("Labs & imaging", d.investigations.length, T(["Test", "Ordered", "Ordered by", "Status"], d.investigations.map((x: Row) => [x.test_name, fmtDate(x.created_at), dash(x.doctor_name), `${labState(x)} (${pretty(x.order_status)})`])))}
      {S("Appointments", d.appointments.length, T(["When", "Department", "Doctor", "Type", "Status"], d.appointments.map((a: Row) => [fmtDateTime(a.appointment_date), dash(a.department), dash(a.doctor_name), dash(a.visit_type), pretty(a.status)])))}

      {S("Care journey", d.journeys.length, <>
        {T(["Journey", "State", "Started"], d.journeys.map((j: Row) => [pretty(j.template_key), pretty(j.state), fmtDate(j.started_at)]))}
        {pending.length > 0 && <><p className="muted" style={{ marginTop: 6 }}>Pending follow-up steps</p>{T(["Step", "Due", "Status"], pending.map((c: Row) => [c.label, fmtDate(c.due_at), pretty(c.status)]))}</>}
      </>)}
      {S("Open tasks", openTasks.length, T(["Task", "Priority", "Status", "Due"], openTasks.map((t: Row) => [dash(t.title), pretty(t.priority), pretty(t.status), t.due_at ? fmtDate(t.due_at) : "—"])))}

      {S("Invoices", d.invoices.length, T(["Invoice", "Module", "Date", "Total", "Paid", "Due", "Status"], d.invoices.map((i: Row) => [i.invoice_no, pretty(i.module), fmtDate(i.created_at), money(i.total_amount), money(i.paid_amount), money(i.due_amount), pretty(i.payment_status)])))}
      {S("Insurance claims", d.claims.length, T(["Insurer", "Claimed", "Approved", "Status"], d.claims.map((c: Row) => [dash(c.insurer_name), money(c.claim_amount), money(c.approved_amount), pretty(c.claim_status)])))}
      {S("Documents", d.documents.length, T(["Type", "File", "Uploaded"], d.documents.map((x: Row) => [pretty(x.doc_type), dash(x.file_name), fmtDate(x.created_at)])))}
      {S("Communication log", d.communications.length, T(["When", "Channel", "Kind", "Status"], d.communications.map((m: Row) => [fmtDateTime(m.created_at), String(m.channel || "").toUpperCase(), pretty(m.kind), pretty(m.status)])))}
      {S("Coordinator notes", notes.length, notes.map((n) => <div key={n.id} style={{ pageBreakInside: "avoid" }}><p className="note">{n.note}</p><p className="muted">{dash(n.author)} · {fmtDateTime(n.created_at)}</p></div>))}

      {record && record.sections.length > 0 && <h2 style={{ background: "#12285A", color: "#fff", borderLeft: "none" }}>Detailed module records</h2>}
      {record?.sections.map((s: Row, i: number) => (
        <section key={`${s.title}${i}`} style={{ marginTop: 8 }}>
          <p style={{ fontWeight: 700, margin: "6px 0 3px" }}>{s.title} <span className="muted">· {s.module} · {s.rows.length} record{s.rows.length > 1 ? "s" : ""}</span></p>
          <GenericTable rows={s.rows} />
        </section>
      ))}

      <p className="foot">Confidential patient information. Data reflects the source modules at the time of printing and may change.</p>
    </div>
  )
}

/** Print in a hidden iframe holding only the summary, so the app's scroll containers and sidebar never affect the output. */
async function printSummary(d: Row) {
  const id = d.patient.patient_id
  const [notes, symptoms, record] = await Promise.all([
    p360.notes(id).then((r: Row) => (r.notes || []) as Row[]).catch(() => [] as Row[]),
    p360.symptomHistory(id).then((r: Row) => (r.symptoms || []) as Row[]).catch(() => [] as Row[]),
    p360.fullRecord(id).then((r: Row) => r as Row).catch(() => null),
  ])
  const html = renderToStaticMarkup(<PrintSummary d={d} notes={notes} symptoms={symptoms} record={record} />)
  const frame = document.createElement("iframe")
  frame.setAttribute("aria-hidden", "true")
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0"
  document.body.appendChild(frame)
  const doc = frame.contentDocument!
  doc.open()
  doc.write(`<!doctype html><html><head><meta charset="utf-8"><title>Patient summary - ${fullName(d.patient).replace(/[<>&]/g, "")}</title><style>${PRINT_CSS}</style></head><body>${html}</body></html>`)
  doc.close()
  frame.contentWindow!.addEventListener("afterprint", () => frame.remove())
  setTimeout(() => { frame.contentWindow!.focus(); frame.contentWindow!.print() }, 150)
}


/** Everyone who has treated this patient, derived from records already loaded (no new data source). */
function CareTeam({ d }: { d: Row }) {
  const team = new Map<string, { name: string; dept: Set<string>; roles: Set<string>; n: number; last: string }>()
  const add = (name: any, dept: any, role: string, at: any) => {
    const nm = String(name || "").trim()
    if (!nm) return
    const t = team.get(nm.toLowerCase()) || { name: nm, dept: new Set<string>(), roles: new Set<string>(), n: 0, last: "" }
    if (dept) t.dept.add(String(dept))
    t.roles.add(role); t.n++
    if (at && (!t.last || +new Date(at) > +new Date(t.last))) t.last = String(at)
    team.set(nm.toLowerCase(), t)
  }
  d.appointments.forEach((a: Row) => add(a.doctor_name, a.department, "Consultant", a.appointment_date))
  d.encounters.forEach((e: Row) => add(e.attending_doctor || e.doctor_name, e.department, "Attending", e.arrival_at))
  d.admissions.forEach((a: Row) => add(a.attending_doctor, a.department, "Attending (IP)", a.admission_date))
  d.er_visits.forEach((v: Row) => add(v.assigned_doctor_name, v.assigned_specialty, "Emergency", v.arrival_at))
  d.surgeries.forEach((s: Row) => add(s.surgeon_name, "Surgery", "Surgeon", s.scheduled_start))
  d.investigations.forEach((x: Row) => add(x.doctor_name, x.department, "Ordering doctor", x.created_at))
  const list = [...team.values()].sort((a, b) => b.n - a.n)
  return (
    <Card title="Care team">
      {list.length ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {list.map((t) => (
            <div key={t.name} className="flex items-start gap-3 rounded-lg border border-[#E2E8F0] bg-white p-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[#DCE6FB] text-[12px] font-bold text-[#1E3A6E]">{initials({ name: t.name })}</span>
              <div className="min-w-0 text-[12px]">
                <div className="truncate text-[13px] font-semibold text-gray-900">{t.name}</div>
                <div className="text-[#64748B]">{[...t.dept].join(", ") || "—"}</div>
                <div className="mt-1 flex flex-wrap gap-1">{[...t.roles].map((r) => <Badge key={r} tone="blue">{r}</Badge>)}</div>
                <div className="mt-1 text-[10.5px] text-[#94A3B8]">{t.n} record{t.n > 1 ? "s" : ""} · last {fmtDate(t.last)}</div>
              </div>
            </div>
          ))}
        </div>
      ) : <Empty>No treating clinicians on record yet.</Empty>}
    </Card>
  )
}

function BillingTab({ d }: { d: Row }) {
  const sum = (k: string) => d.invoices.reduce((n: number, i: Row) => n + Number(i[k] || 0), 0)
  const claimed = d.claims.reduce((n: number, c: Row) => n + Number(c.claim_amount || 0), 0)
  const approved = d.claims.reduce((n: number, c: Row) => n + Number(c.approved_amount || 0), 0)
  const stat = (l: string, v: string, warn = false) => <div className="rounded-lg bg-[#EEF3FF] px-3 py-2"><div className="text-[11px] text-[#64748B]">{l}</div><div className={cx("text-[16px] font-semibold", warn ? "text-[#B91C1C]" : "text-[#1E3A6E]")}>{v}</div></div>
  const invNo = (id: any) => d.invoices.find((i: Row) => i.id === id)?.invoice_no
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {stat("Billed", money(sum("total_amount")))}{stat("Paid", money(sum("paid_amount")))}{stat("Outstanding", money(sum("due_amount")), sum("due_amount") > 0)}{stat("Claimed", money(claimed))}{stat("Approved", money(approved))}
      </div>
      <Card title="Invoices">
        {d.invoices.length ? <Table head={["Invoice", "Module", "Date", "Total", "Paid", "Due", "Status"]}>{d.invoices.map((i: Row) => <tr key={i.id}><td className="px-2 py-1.5">{i.invoice_no}</td><td className="px-2 py-1.5">{pretty(i.module)}</td><td className="px-2 py-1.5">{fmtDate(i.created_at)}</td><td className="px-2 py-1.5">{money(i.total_amount)}</td><td className="px-2 py-1.5">{money(i.paid_amount)}</td><td className="px-2 py-1.5">{money(i.due_amount)}</td><td className="px-2 py-1.5"><Badge status={i.payment_status} /></td></tr>)}</Table> : <Empty>No invoices.</Empty>}
      </Card>
      <Card title="Insurance claims">
        {d.claims.length ? <Table head={["Insurer", "Invoice", "Submitted", "Claimed", "Approved", "Shortfall", "Status"]}>{d.claims.map((c: Row) => { const gap = c.approved_amount == null ? null : Number(c.claim_amount || 0) - Number(c.approved_amount || 0); return <tr key={c.id}><td className="px-2 py-1.5">{c.insurer_name || "—"}</td><td className="px-2 py-1.5">{invNo(c.invoice_id) || "—"}</td><td className="px-2 py-1.5">{c.submitted_at ? fmtDate(c.submitted_at) : "—"}</td><td className="px-2 py-1.5">{money(c.claim_amount)}</td><td className="px-2 py-1.5">{money(c.approved_amount)}</td><td className={cx("px-2 py-1.5", gap !== null && gap > 0 && "text-[#B91C1C]")}>{gap === null ? "—" : money(gap)}</td><td className="px-2 py-1.5"><Badge status={c.claim_status} /></td></tr> })}</Table> : <Empty>No insurance claims.</Empty>}
      </Card>
    </div>
  )
}

/** Critical flags pinned in the profile header, all derived from the loaded profile. */
function AlertsStrip({ d, admitted }: { d: Row; admitted: boolean }) {
  const allergy = d.patient.allergies || d.clinical_history?.allergies
  const due = d.invoices.reduce((n: number, i: Row) => n + Number(i.due_amount || 0), 0)
  const overdue = d.checkpoints.filter((c: Row) => c.status === "overdue").length
  const bed = d.beds.find((b: Row) => !b.released_at)
  const flags: Array<{ tone: string; text: string }> = [
    allergy && { tone: "border-red-300 bg-red-50 text-red-800", text: `⚠ Allergies: ${allergy}` },
    admitted && { tone: "border-blue-200 bg-blue-50 text-blue-800", text: `🛏 Admitted${bed ? ` · ${bed.ward || "Ward"} ${bed.bed_no || ""}` : ""}` },
    due > 0 && { tone: "border-amber-300 bg-amber-50 text-amber-900", text: `💳 Outstanding ${money(due)}` },
    overdue > 0 && { tone: "border-amber-300 bg-amber-50 text-amber-900", text: `⏰ ${overdue} overdue checkpoint${overdue > 1 ? "s" : ""}` },
  ].filter(Boolean) as Array<{ tone: string; text: string }>
  if (!flags.length) return null
  return <div role="alert" className="mt-3 flex flex-wrap gap-2">{flags.map((f) => <span key={f.text} className={cx("rounded-md border px-2.5 py-1 text-[12px] font-medium", f.tone)}>{f.text}</span>)}</div>
}

function FollowUpSummary({ d }: { d: Row }) {
  const now = Date.now()
  const lastDischarge = d.admissions.filter((a: Row) => a.discharge_date).sort((a: Row, b: Row) => +new Date(b.discharge_date) - +new Date(a.discharge_date))[0]
  const upcoming = d.appointments.filter((a: Row) => ["scheduled", "checked_in", "confirmed", "booked", "pending"].includes(a.status) && +new Date(a.appointment_date) >= now - 864e5)
  const missed = d.appointments.filter((a: Row) => ["no_show", "missed", "cancelled"].includes(String(a.status).toLowerCase()) || (["scheduled", "booked", "pending"].includes(a.status) && +new Date(a.appointment_date) < now - 864e5))
  const pending = d.checkpoints.filter((c: Row) => ["scheduled", "queued", "overdue"].includes(c.status))
  const box = (title: string, tone: string, body: React.ReactNode) => <div className={cx("rounded-lg border p-3 text-[12px]", tone)}><div className="mb-1 text-[11px] font-semibold uppercase tracking-wide opacity-70">{title}</div>{body}</div>
  return (
    <div className="mb-3 grid gap-3 md:grid-cols-4">
      {box("Last discharge", "border-[#DDE2EC] bg-white", lastDischarge ? <><div className="font-semibold text-gray-900">{fmtDate(lastDischarge.discharge_date)}</div><div className="text-[#64748B]">{lastDischarge.discharge_diagnosis || lastDischarge.diagnosis || "Admitted " + fmtDate(lastDischarge.admission_date)}</div></> : <span className="text-[#64748B]">No discharge on record.</span>)}
      {box("Upcoming", "border-blue-200 bg-blue-50", upcoming.length ? <><div className="text-[18px] font-semibold text-[#1E3A6E]">{upcoming.length}</div><div className="text-[#475569]">Next: {fmtDateTime(upcoming[upcoming.length - 1].appointment_date)}</div></> : <span className="text-[#64748B]">Nothing scheduled.</span>)}
      {box("Missed / cancelled", missed.length ? "border-amber-300 bg-amber-50" : "border-[#DDE2EC] bg-white", <div className={cx("text-[18px] font-semibold", missed.length ? "text-amber-900" : "text-gray-900")}>{missed.length}</div>)}
      {box("Pending follow-up steps", pending.length ? "border-amber-300 bg-amber-50" : "border-[#DDE2EC] bg-white", pending.length ? <><div className="text-[18px] font-semibold text-amber-900">{pending.length}</div><div className="truncate text-[#475569]">Next: {pending[0].label}</div></> : <span className="text-[#64748B]">None pending.</span>)}
    </div>
  )
}

function Appointments({ d }: { d: Row }) {
  return (
    <>
    <FollowUpSummary d={d} />
    <Card title="Appointments (read from the scheduler)">
      {d.appointments.length ? <Table head={["When", "Department", "Doctor", "Type", "Token", "Status", "Consultation"]}>{d.appointments.map((a: Row) => <tr key={a.id} className="align-top"><td className="px-2 py-1.5">{fmtDateTime(a.appointment_date)}</td><td className="px-2 py-1.5">{a.department || "—"}</td><td className="px-2 py-1.5">{a.doctor_name || "—"}</td><td className="px-2 py-1.5">{a.visit_type}</td><td className="px-2 py-1.5">{a.token_no ?? "—"}</td><td className="px-2 py-1.5"><Badge status={a.status} /></td><td className="px-2 py-1.5 text-[11.5px]">{a.chief_complaint && <div>Complaint: {a.chief_complaint}</div>}{a.diagnosis && <div>Dx: {a.diagnosis}</div>}{a.medicines && <div>Rx: {typeof a.medicines === "string" ? a.medicines : JSON.stringify(a.medicines)}</div>}{a.advice && <div>Advice: {a.advice}</div>}{a.follow_up_date && <div>Follow-up: {fmtDate(a.follow_up_date)}</div>}{!a.chief_complaint && !a.diagnosis && !a.medicines && !a.advice && "—"}</td></tr>)}</Table> : <Empty>No appointments.</Empty>}
    </Card>
    </>
  )
}

function Labs({ d }: { d: Row }) {
  return (
    <Card title="Labs & Imaging orders">
      {d.investigations.length ? <Table head={["Test", "Ordered", "By", "Status"]}>{d.investigations.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{x.test_name}</td><td className="px-2 py-1.5">{fmtDate(x.created_at)}</td><td className="px-2 py-1.5">{x.doctor_name || "—"}</td><td className="px-2 py-1.5"><Badge tone={labState(x) === "Available" ? "green" : "amber"}>{labState(x)}</Badge> <span className="text-[10.5px] text-[#94A3B8]">{pretty(x.order_status)}</span></td></tr>)}</Table> : <Empty>No investigations ordered.</Empty>}
      <p className="mt-2 text-[10.5px] text-[#94A3B8]">Results and reports are opened from the Laboratory / Radiology module; Patient 360° does not duplicate them.</p>
    </Card>
  )
}

function Documents({ d }: { d: Row }) {
  return (
    <Card title="Documents">
      {d.documents.length ? <Table head={["Type", "File", "Uploaded", "OCR"]}>{d.documents.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{pretty(x.doc_type)}</td><td className="px-2 py-1.5">{x.file_name || "—"}</td><td className="px-2 py-1.5">{fmtDate(x.created_at)}</td><td className="px-2 py-1.5">{x.ocr_extracted ? <Badge tone="amber">Extracted — unverified</Badge> : "—"}</td></tr>)}</Table> : <Empty>No documents uploaded.</Empty>}
    </Card>
  )
}

function Profile({ patientId, onClose, navigate }: { patientId: string; onClose: () => void; navigate: (m: string) => void }) {
  const [tab, setTab] = useState<Tab>("overview")
  const [showTask, setShowTask] = useState(false)
  const [copied, setCopied] = useState("")
  const [printing, setPrinting] = useState(false)
  /** Those modules take no patient input, so hand the ID over via the clipboard to paste into their search. */
  const openFor = async (module: string) => {
    try { await navigator.clipboard.writeText(patientId); setCopied(patientId) } catch { /* clipboard blocked: still navigate */ }
    navigate(module)
  }
  const { data: raw, error, loading, reload } = useAsync(() => p360.profile(patientId), [patientId])
  // Older backends omit some sections; treat a missing list as empty instead of crashing the page.
  const d = raw ? { ...Object.fromEntries(LISTS.map((k) => [k, []])), ...raw } : null

  if (loading && !d) return <Loading />
  if (error && !d) return <ErrorBox error={error} onRetry={reload} />
  if (!d) return null

  const p = d.patient
  const visit = d.appointments[0]
  const nextAppt = (d.further_appointments && d.further_appointments[0]) || d.appointments.filter((a: Row) => ["scheduled", "checked_in", "confirmed", "booked", "pending"].includes(a.status))[0] || d.appointments[0]
  const latestVisit = d.encounters?.[0] || d.admissions?.[0]
  const isAwaitingTriage = latestVisit && (
    latestVisit.status === "Awaiting Triage" ||
    latestVisit.status === "Registered" ||
    latestVisit.status === "In Queue" ||
    latestVisit.status === "Symptoms Captured" ||
    latestVisit.department === "Awaiting Triage" ||
    (latestVisit.status || "").toLowerCase().includes("triage")
  )
  const whatsappSymptomText = firstSymptoms(d.whatsapp_symptoms?.symptoms, d.patient?.symptoms, latestVisit?.symptoms, latestVisit?.chief_complaint)
  const whatsappSymptomDate = d.whatsapp_symptoms?.received_at || d.patient?.updated_at
  const openTasks = d.tasks.filter((t: Row) => !["resolved", "cancelled"].includes(t.status)).length
  const admitted = d.admissions.some((a: Row) => !a.discharge_date)
  const facts: Array<[string, string, string]> = [
    ["🎂", `${p.age ?? "—"} Years`, `(${p.gender || "—"})`],
    ["📞", p.phone || "No phone on record", "(Mobile)"],
    ["🩸", p.blood_group || "—", "(Blood group)"],
    ["📅", fmtDate(p.created_at), "(Registration)"],
    ["👨‍⚕️", visit?.doctor_name || "—", "(Treating Doctor)"],
    ["🏥", visit?.department || "—", "(Department)"],
  ]

  return (
    <div className="space-y-3">
      <section className="rounded-xl border border-[#DDE2EC] bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start gap-5">
          <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-full bg-[#DCE6FB] text-3xl font-bold text-[#1E3A6E]">{initials(p)}</div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-2xl font-semibold text-gray-900">{fullName(p)}</h2>
              <Badge tone="green">● Active</Badge>
              <Badge tone="blue">{admitted ? "Inpatient" : p.patient_type && p.patient_type !== "regular" ? pretty(p.patient_type) : "OPD Patient"}</Badge>
              {isAwaitingTriage && (
                <span className="inline-flex items-center gap-1 rounded bg-amber-100 px-2.5 py-0.5 text-[11px] font-bold text-amber-900 border border-amber-300">
                  ⏳ Awaiting Triage
                </span>
              )}
            </div>
            <div className="mt-0.5 text-[13px] text-[#475569]">{p.patient_id}</div>
            <dl className="mt-4 grid grid-cols-2 gap-y-3 md:grid-cols-3 xl:flex xl:divide-x xl:divide-[#E2E8F0]">
              {facts.map(([icon, v, l]) => (
                <div key={l} className="flex items-center gap-2.5 text-[12.5px] xl:px-4 xl:first:pl-0">
                  <span aria-hidden className="text-lg">{icon}</span>
                  <div className="min-w-0"><dd className="truncate font-medium text-gray-900">{v}</dd><dt className="text-[11px] text-[#64748B]">{l}</dt></div>
                </div>
              ))}
            </dl>

            {/* Prominent Banner when Awaiting Triage or displaying Further Appointments & WhatsApp Symptoms */}
            <div className="mt-3.5 flex flex-wrap items-center gap-3 rounded-lg border border-amber-200 bg-amber-50/80 px-3.5 py-2 text-[12px]">
              <div className="flex items-center gap-1.5 font-bold text-amber-900">
                <span className="animate-pulse">⏳</span>
                <span>Awaiting Triage</span>
              </div>
              <span className="text-amber-300">|</span>
              <div className="flex items-center gap-1.5 text-gray-800">
                <span className="font-semibold text-gray-700">📅 Further Appointment:</span>
                {nextAppt ? (
                  <span className="font-bold text-[#1B4FD8]">
                    {fmtDateTime(nextAppt.appointment_date)} · {nextAppt.doctor_name || "Doctor"} ({nextAppt.department || "General"})
                  </span>
                ) : (
                  <span className="text-[#64748B]">None scheduled</span>
                )}
              </div>
              <span className="text-amber-300">|</span>
              <div className="flex items-center gap-1.5 text-gray-800">
                <span className="inline-flex items-center gap-1 rounded bg-emerald-100 px-2 py-0.5 text-[11px] font-bold text-emerald-800 border border-emerald-300">
                  Symptoms:
                </span>
                <span className="font-medium text-gray-900 italic">
                  {whatsappSymptomText ? `"${whatsappSymptomText}"` : "None reported"}
                </span>
                {whatsappSymptomText && whatsappSymptomDate && <span className="text-[10.5px] text-[#64748B]">({fmtDate(whatsappSymptomDate)})</span>}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button className={btnCls.ghost} onClick={() => window.open(patientPageUrl(p.patient_id), "_blank", "noopener")}>✎ Open Full Record</button>
            <button className={btnCls.ghost} onClick={() => setShowTask(true)}>+ Task</button>
            <button className={btnCls.ghost} disabled={printing} onClick={async () => { setPrinting(true); try { await printSummary(d) } finally { setPrinting(false) } }}>{printing ? "Preparing…" : "🖨 Print summary"}</button>
            <button className={btnCls.ghost} onClick={reload}>⟳ Refresh</button>
            <button className={btnCls.ghost} onClick={onClose}>✕ Close</button>
          </div>
        </div>
        <AlertsStrip d={d} admitted={admitted} />
        <div className="mt-3 flex flex-wrap gap-2 border-t border-[#EEF1F6] pt-3">
          <span className="self-center text-[11px] font-semibold uppercase tracking-wide text-[#94A3B8]">Quick actions</span>
          {copied && <span className="self-center text-[11px] text-emerald-700">Patient ID {copied} copied</span>}
          <button className={btnCls.ghost} onClick={() => openFor("patient_messages")} title="Copies the patient ID, then opens Patient Messages">💬 Send message</button>
          <button className={btnCls.ghost} onClick={() => openFor("appointments")} title="Copies the patient ID, then opens Appointments">📅 Book appointment</button>
          <button className={btnCls.ghost} onClick={() => setShowTask(true)}>✅ Create task</button>
          <button className={btnCls.ghost} onClick={() => setTab("consent")}>🔒 Consent</button>
        </div>
      </section>

      <nav role="tablist" className="flex gap-1.5 overflow-x-auto rounded-xl border border-[#DDE2EC] bg-white p-1.5 shadow-sm">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}
            className={cx("flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-2 text-[12px] font-medium", tab === t.key ? "bg-[#1B4FD8] text-white shadow" : "text-[#475569] hover:bg-[#EEF3FF]")}>
            <span aria-hidden>{t.icon}</span>{t.label}{t.key === "tasks" && openTasks > 0 ? ` (${openTasks})` : ""}
          </button>
        ))}
      </nav>

      {tab === "overview" && <Overview d={d} go={setTab} />}
      {tab === "timeline" && <TimelineTab patientId={p.patient_id} />}
      {tab === "clinical" && <ClinicalTab d={d} />}
      {tab === "vitals" && <VitalsTab d={d} />}
      {tab === "care_team" && <CareTeam d={d} />}
      {tab === "notes" && <NotesTab patientId={p.patient_id} />}
      {tab === "billing" && <BillingTab d={d} />}
      {tab === "encounters" && <><EncountersTab d={d} /><div className="mt-3"><SymptomHistory patientId={p.patient_id} /></div><MoreVisits d={d} /></>}
      {tab === "medications" && <MedicationsTab d={d} />}
      {tab === "labs" && <Labs d={d} />}
      {tab === "appointments" && <Appointments d={d} />}
      {tab === "journey" && <JourneyTab d={d} />}
      {tab === "surveys" && <SurveysTab d={d} />}
      {tab === "communications" && <CommsTab d={d} navigate={navigate} />}
      {tab === "tasks" && <TasksView patientId={p.patient_id} />}
      {tab === "documents" && <Documents d={d} />}
      {tab === "consent" && <ConsentTab patientId={p.patient_id} prefs={d.preferences} onChanged={reload} />}
      <p className="text-[10.5px] text-[#94A3B8]">{d.freshness_note} Loaded {fmtDateTime(d.generated_at)}.</p>
      {showTask && <NewTask patientId={p.patient_id} onClose={() => setShowTask(false)} onCreated={() => { setShowTask(false); reload() }} />}
    </div>
  )
}

// --- Hero landing page circular diagram ---
const DATA_NODES = [
  { label: "Medical History",  angle: 315, bg: "#FFE4EF", iconBg: "#F472B6", svg: "M12 21.593c-5.63-5.539-11-10.297-11-14.402 0-3.791 3.068-5.191 5.281-5.191 1.312 0 4.151.501 5.719 4.457 1.59-3.968 4.464-4.447 5.726-4.447 2.54 0 5.274 1.621 5.274 5.181 0 4.069-5.136 8.625-11 14.402z" },
  { label: "Clinical Records", angle: 0,  bg: "#E0F2FE", iconBg: "#38BDF8", svg: "M4.5 6.375a4.125 4.125 0 1 1 8.25 0 4.125 4.125 0 0 1-8.25 0ZM14.25 8.625a3.375 3.375 0 1 1 6.75 0 3.375 3.375 0 0 1-6.75 0ZM1.5 19.125a7.125 7.125 0 0 1 14.25 0v.003l-.001.119a.75.75 0 0 1-.363.63 13.067 13.067 0 0 1-6.761 1.873c-2.472 0-4.786-.684-6.76-1.873a.75.75 0 0 1-.364-.63l-.001-.122ZM17.25 19.128l-.001.144a2.25 2.25 0 0 1-.233.96 10.088 10.088 0 0 0 5.06-1.01.75.75 0 0 0 .42-.643 4.875 4.875 0 0 0-6.957-4.611 8.586 8.586 0 0 1 1.71 5.157v.003Z" },
  { label: "Lab & Imaging",    angle: 45,  bg: "#FEF3C7", iconBg: "#F59E0B", svg: "M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" },
  { label: "Medications",      angle: 90,    bg: "#F3E8FF", iconBg: "#A855F7", svg: "M15.75 10.5V6a3.75 3.75 0 1 0-7.5 0v4.5m11.356-1.993 1.263 12c.07.665-.45 1.243-1.119 1.243H4.25a1.125 1.125 0 0 1-1.12-1.243l1.264-12A1.125 1.125 0 0 1 5.513 7.5h12.974c.576 0 1.059.435 1.119 1.007ZM8.625 10.5a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm7.5 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" },
  { label: "Appointments",     angle: 135,   bg: "#DCFCE7", iconBg: "#22C55E", svg: "M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 0 1 2.25-2.25h13.5A2.25 2.25 0 0 1 21 7.5v11.25m-18 0A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75m-18 0v-7.5A2.25 2.25 0 0 1 5.25 9h13.5A2.25 2.25 0 0 1 21 11.25v7.5m-9-6h.008v.008H12v-.008ZM12 15h.008v.008H12V15Zm0 2.25h.008v.008H12v-.008ZM9.75 15h.008v.008H9.75V15Zm0 2.25h.008v.008H9.75v-.008ZM7.5 15h.008v.008H7.5V15Zm0 2.25h.008v.008H7.5v-.008Zm6.75-4.5h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V15Zm0 2.25h.008v.008h-.008v-.008Zm2.25-4.5h.008v.008H16.5v-.008Zm0 2.25h.008v.008H16.5V15Z" },
  { label: "Care Journey",     angle: 180,   bg: "#E0F2FE", iconBg: "#0EA5E9", svg: "M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 0 0 2.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 0 0-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75 2.25 2.25 0 0 0-.1-.664m-5.8 0A2.251 2.251 0 0 1 13.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25ZM6.75 12h.008v.008H6.75V12Zm0 3h.008v.008H6.75V15Zm0 3h.008v.008H6.75V18Z" },
  { label: "Communications",   angle: 225,  bg: "#E0F2FE", iconBg: "#3B82F6", svg: "M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V20.25a.75.75 0 0 0 1.28.53l3.58-3.58A48.52 48.52 0 0 0 11.25 17c2.115 0 4.198-.137 6.24-.402 1.129-.146 2.112-.717 2.76-1.635Z" },
  { label: "Documents",        angle: 270,  bg: "#FEF9C3", iconBg: "#EAB308", svg: "M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" },
]

/** Hosp AI → Patient 360°: hero landing page + search. */
export default function PatientSearch360({ navigate }: { navigate: (module: string) => void }) {
  const [query, setQuery] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [matches, setMatches] = useState<Row[] | null>(null)
  const [total, setTotal] = useState(0)
  const [openId, setOpenId] = useState<string | null>(null)

  const search = async (e?: React.FormEvent) => {
    e?.preventDefault()
    const q = normalise(query)
    if (!q) return
    setBusy(true); setError(""); setMatches(null); setOpenId(null)
    try {
      const r = await p360.registry({ q, limit: 10 })
      setTotal(r.total)
      if (r.patients.length === 1) setOpenId(r.patients[0].patient_id)
      else setMatches(r.patients)
    } catch (err) { setError(errMsg(err)) } finally { setBusy(false) }
  }
  const clear = () => { setQuery(""); setMatches(null); setOpenId(null); setError("") }

  // ── When a patient is open show slim header + profile ──
  if (openId) {
    return (
      <div className="flex-1 overflow-y-auto bg-[#F0F2F5]">
        <div className="space-y-4 p-6">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <h1 className="text-2xl font-semibold text-gray-900">Patient 360°</h1>
              <p className="text-[12.5px] text-[#64748B]">Unified patient profile and care coordination</p>
            </div>
            <form onSubmit={search} role="search" className="flex min-w-[320px] flex-1 gap-2 rounded-xl border border-[#CBD5E1] bg-white p-1.5 shadow-sm md:max-w-2xl">
              <span aria-hidden className="flex items-center pl-2 text-[#64748B]">🔍</span>
              <input aria-label="Search patient" className="w-full bg-transparent px-1 text-[14px] text-gray-900 outline-none"
                value={query} maxLength={80} placeholder="Search by patient ID, name or mobile number"
                onChange={(e) => setQuery(e.target.value)} />
              {query && <button type="button" className="px-2 text-[#64748B] hover:text-gray-900" onClick={clear}>✕</button>}
              <button type="submit" disabled={busy || !query.trim()}
                className="rounded-lg bg-[#1B4FD8] px-6 py-2 text-[13px] font-medium text-white hover:bg-[#1741B0] disabled:opacity-50">
                {busy ? "Searching…" : "Search"}
              </button>
            </form>
          </div>
          {error && <ErrorBox error={error} onRetry={search} />}
          <Profile key={openId} patientId={openId} navigate={navigate} onClose={() => { setOpenId(null); clear() }} />
        </div>
      </div>
    )
  }

  // ── Hero landing page (no patient selected) ──
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="relative min-h-screen bg-gradient-to-br from-[#E6F0FD] via-[#F1F6FE] to-[#EAF2FD] overflow-hidden">

        {/* Decorative "+" marks */}
        {[
          { top: "8%",  left: "4%",  sz: 20, op: 0.18 },
          { top: "20%", left: "48%", sz: 15, op: 0.12 },
          { top: "55%", left: "2%",  sz: 14, op: 0.11 },
          { top: "72%", left: "62%", sz: 18, op: 0.10 },
          { top: "85%", left: "32%", sz: 12, op: 0.09 },
          { top: "38%", left: "92%", sz: 16, op: 0.11 },
        ].map((p, i) => (
          <span key={i} aria-hidden style={{
            position: "absolute", top: p.top, left: p.left,
            fontSize: p.sz, opacity: p.op, color: "#1B4FD8",
            fontWeight: 900, lineHeight: 1, userSelect: "none", pointerEvents: "none",
          }}>+</span>
        ))}

        {/* Two-column hero */}
        <div className="flex flex-col lg:flex-row min-h-[calc(100vh-56px)]">

          {/* LEFT: title, tagline, pills, search */}
          <div className="flex-1 flex flex-col justify-center px-8 py-10 lg:px-14 lg:py-16 max-w-2xl">
            <h1 className="text-5xl lg:text-6xl font-extrabold text-[#12285A] leading-tight tracking-tight">
              Patient <span className="text-[#0E9FBF]">360°</span>
            </h1>
            <p className="mt-3 text-xl font-bold text-[#12285A]">
              One patient. Complete view. Better care.
            </p>
            <p className="mt-3 text-[#475569] text-[13.5px] leading-relaxed max-w-md">
              Get a unified view of your patient's journey — from registration to follow-up — with clinical data, care coordination and PatientReach AI.
            </p>

            {/* Feature cards */}
            <div className="mt-7 grid grid-cols-4 gap-3 max-w-xl">
              {[
                { icon: "M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.5 20.118a7.5 7.5 0 0 1 15 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.5-1.632Z", label: "Unified\nPatient Profile" },
                { icon: "M3 12h4.5l2.25-6 4.5 12 2.25-6H21", label: "Clinical\nTimeline" },
                { icon: "M18 18.72a9.094 9.094 0 0 0 3.741-.479 3 3 0 0 0-4.682-2.72m.94 3.198.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0 1 12 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 0 1 6 18.719m12 0a5.971 5.971 0 0 0-.941-3.197m0 0A5.995 5.995 0 0 0 12 12.75a5.995 5.995 0 0 0-5.058 2.772m0 0a3 3 0 0 0-4.681 2.72 8.986 8.986 0 0 0 3.74.477m.94-3.197a5.971 5.971 0 0 0-.94 3.197M15 6.75a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm6 3a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Zm-13.5 0a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Z", label: "Care\nCoordination" },
                { icon: "M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V20.25a.75.75 0 0 0 1.28.53l3.58-3.58A48.52 48.52 0 0 0 11.25 17c2.115 0 4.198-.137 6.24-.402 1.129-.146 2.112-.717 2.76-1.635Z", label: "PatientReach\nAI" },
              ].map((f) => (
                <div key={f.label} className="flex flex-col items-center gap-2 rounded-xl bg-[#E4EEFC] px-2 py-4 text-center text-[11px] font-medium leading-tight text-[#1E3A6E] whitespace-pre-line">
                  <svg viewBox="0 0 24 24" className="h-7 w-7 text-[#2563EB]" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={f.icon} /></svg>
                  {f.label}
                </div>
              ))}
            </div>

            {/* Search bar */}
            <form onSubmit={search} role="search"
              className="mt-8 flex items-center gap-2 rounded-xl border border-[#CBD5E1] bg-white p-1.5 shadow-md max-w-xl">
              <span aria-hidden className="flex items-center pl-2 text-[#94A3B8] text-lg">🔍</span>
              <input
                autoFocus
                aria-label="Search patient by ID, name or mobile number"
                className="w-full bg-transparent px-2 text-[14px] text-gray-900 outline-none placeholder:text-[#94A3B8]"
                placeholder="Search by Patient ID, name or mobile number..."
                value={query} maxLength={80}
                onChange={(e) => setQuery(e.target.value)}
              />
              {query && (
                <button type="button" className="px-2 text-[#64748B] hover:text-gray-900 text-sm" onClick={clear}>✕</button>
              )}
              <button type="submit" disabled={busy || !query.trim()}
                className="flex items-center gap-1.5 rounded-lg bg-[#1B4FD8] px-5 py-2 text-[13px] font-semibold text-white hover:bg-[#1741B0] disabled:opacity-50 transition-colors">
                {busy ? "Searching…" : <><span>Search</span><span aria-hidden className="text-base font-bold">›</span></>}
              </button>
            </form>

            {error && <div className="mt-4"><ErrorBox error={error} onRetry={search} /></div>}
            {!openId && matches && matches.length === 0 && (
              <div className="mt-4"><Empty>No registered patient matches "{query.trim()}". Check the ID, name spelling or mobile number.</Empty></div>
            )}
            {!openId && matches && matches.length > 1 && (
              <section className="mt-4 rounded-lg border border-[#DDE2EC] bg-white shadow-sm">
                <header className="border-b border-[#EEF1F6] px-4 py-2 text-[12.5px] font-semibold">
                  {total} patients match — choose one
                  {total > matches.length ? ` (showing first ${matches.length}; refine to narrow)` : ""}
                </header>
                <ul className="divide-y divide-[#F1F5F9]">
                  {matches.map((m) => (
                    <li key={m.patient_id}>
                      <button className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-[#EEF3FF]"
                        onClick={() => { setOpenId(m.patient_id); setMatches(null) }}>
                        <span className="flex h-9 w-9 items-center justify-center rounded-full bg-[#DCE6FB] text-[12px] font-bold text-[#1E3A6E]">{initials(m)}</span>
                        <span className="flex-1">
                          <span className="block text-[13px] font-medium text-gray-900">{fullName(m)}</span>
                          <span className="block text-[11.5px] text-[#64748B]">{m.patient_id} · {m.age ?? "—"} yrs · {m.gender || "—"} · {m.phone || "no phone"}</span>
                        </span>
                        {m.admitted ? <Badge tone="blue">Admitted</Badge> : null}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>

          {/* RIGHT: Circular data-source diagram */}
          <div className="flex-1 flex items-center justify-center p-6 relative">
            <div style={{ position: "relative", width: 480, height: 480, maxWidth: "100%" }}>
              <svg viewBox="0 0 480 480" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
                <circle cx="240" cy="240" r="190" fill="none" stroke="#BFD6F6" strokeWidth="1.5" strokeDasharray="3 5" />
                <circle cx="240" cy="240" r="105" fill="rgba(255,255,255,0.55)" stroke="#D3E3FA" strokeWidth="1.5" />
                {DATA_NODES.map((node) => {
                  const rad = (node.angle * Math.PI) / 180
                  const mx = 240 + 190 * Math.sin(rad + 0.4), my = 240 - 190 * Math.cos(rad + 0.4)
                  return <circle key={node.label} cx={mx} cy={my} r="3" fill="#93C5FD" />
                })}
              </svg>

              {/* Center - patient avatar with double ring */}
              <div style={{ position: "absolute", top: "50%", left: "50%", transform: "translate(-50%,-50%)", width: 190, height: 190, borderRadius: "50%", background: "rgba(255,255,255,0.7)", border: "2px solid #DCE9FB", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 8px 30px rgba(37,99,235,0.15)", zIndex: 10 }}>
                <div style={{ width: 150, height: 150, borderRadius: "50%", overflow: "hidden", background: "linear-gradient(180deg,#EAF3FF,#CFE3FB)", border: "4px solid #fff", boxShadow: "0 0 0 2px #93C5FD" }}>
                  <svg viewBox="0 0 150 150" style={{ width: "100%", height: "100%" }}>
                    <path d="M18 150 C18 112 46 100 75 100 C104 100 132 112 132 150Z" fill="#4FB3C8" />
                    <path d="M58 100 L75 124 L92 100Z" fill="#fff" />
                    <rect x="64" y="82" width="22" height="22" rx="8" fill="#F2C9A5" />
                    <ellipse cx="75" cy="62" rx="25" ry="28" fill="#F7D6B8" />
                    <path d="M49 58 C48 30 70 26 82 30 C98 33 104 46 101 60 C96 48 86 44 74 46 C62 48 54 50 49 58Z" fill="#2B2F3A" />
                    <circle cx="66" cy="64" r="2.4" fill="#2B2F3A" /><circle cx="84" cy="64" r="2.4" fill="#2B2F3A" />
                    <path d="M67 77 Q75 83 83 77" stroke="#B5705A" strokeWidth="2" fill="none" strokeLinecap="round" />
                  </svg>
                </div>
              </div>

              {/* Node circles: icon + label inside */}
              {DATA_NODES.map((node) => {
                const rad = (node.angle * Math.PI) / 180
                const left = `${((240 + 190 * Math.sin(rad)) / 480) * 100}%`
                const top = `${((240 - 190 * Math.cos(rad)) / 480) * 100}%`
                return (
                  <div key={node.label} style={{ position: "absolute", left, top, transform: "translate(-50%,-50%)", zIndex: 5, width: 88, height: 88, borderRadius: "50%", background: "#fff", border: "2px solid #E3EDFB", boxShadow: "0 6px 18px rgba(27,79,216,0.14)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 3 }}>
                    <svg viewBox="0 0 24 24" style={{ width: 28, height: 28 }} fill="none" stroke={node.iconBg} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={node.svg} /></svg>
                    <span style={{ fontSize: 10.5, fontWeight: 600, color: "#1E3A6E", textAlign: "center", lineHeight: 1.15, maxWidth: 68 }}>{node.label}</span>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* Footer bar */}
        <div className="border-t border-[#DCE6FB] bg-white/80 backdrop-blur-sm px-8 py-3 flex items-center justify-between text-[12px] text-[#475569]">
          <span className="flex items-center gap-2">
            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#1B4FD8] text-white text-[10px] font-bold">✓</span>
            Better Patient Engagement.&nbsp;&nbsp;Smarter Care Coordination.&nbsp;&nbsp;Improved Health Outcomes.
          </span>
          <span className="flex items-center gap-1.5 font-semibold text-[#1B4FD8]">
            <span className="rounded bg-[#1B4FD8] text-white px-1.5 py-0.5 text-[10px] font-bold leading-none">H</span>
            Powered by HospAI
          </span>
        </div>
      </div>
    </div>
  )
}