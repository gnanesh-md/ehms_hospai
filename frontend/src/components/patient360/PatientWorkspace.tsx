import { useState } from "react"
import { patientPageUrl } from "../patientRecord/patientRecord"
import { p360, type Row } from "./api"
import TasksView from "./TasksView"
import { Badge, btnCls, Card, Empty, ErrorBox, errMsg, Field, fmtDate, fmtDateTime, fullName, inputCls, Loading, pretty, Table, Tabs, useAsync, firstSymptoms } from "./ui"

type Tab = "overview" | "timeline" | "clinical" | "encounters" | "medications" | "investigations" | "journey" | "surveys" | "communications" | "tasks" | "consent"

/** Where a value came from and whether it is verified (spec 1.4: every aggregated value keeps source, time and status). */
const Source = ({ r }: { r: Row }) =>
  r.source_module ? <span className="ml-1.5 text-[10px] text-[#94A3B8]">{r.source_module} · {pretty(r.verification_state)}</span> : null

export default function PatientWorkspace({ patientId, onBack, navigate }: { patientId: string; onBack: () => void; navigate: (m: string) => void }) {
  const [tab, setTab] = useState<Tab>("overview")
  const [showTask, setShowTask] = useState(false)
  const { data, error, loading, reload } = useAsync(() => p360.profile(patientId), [patientId])

  if (loading && !data) return <Loading />
  if (error && !data) return (<div className="space-y-3"><button className={btnCls.ghost} onClick={onBack}>← Back</button><ErrorBox error={error} onRetry={reload} /></div>)
  if (!data) return null

  const p = data.patient
  const openTasks = data.tasks.filter((t: Row) => !["resolved", "cancelled"].includes(t.status)).length
  const activeJourney = data.journeys.find((j: Row) => j.state === "active")
  const whatsapp = data.authorizations.find((a: Row) => a.channel === "whatsapp" && a.purpose === "clinical_followup")
  const sms = data.authorizations.find((a: Row) => a.channel === "sms" && a.purpose === "clinical_followup")

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <button className={btnCls.ghost} onClick={onBack}>← Registry</button>
        <div className="flex gap-2">
          <button className={btnCls.ghost} onClick={() => setShowTask(true)}>+ Coordination task</button>
          <button className={btnCls.ghost} onClick={() => window.open(patientPageUrl(p.patient_id), "_blank", "noopener")}>Open full record ↗</button>
          <button className={btnCls.ghost} onClick={() => navigate("patient_messages")}>Patient Messages</button>
          <button className={btnCls.ghost} onClick={reload}>Refresh</button>
        </div>
      </div>

      <Card>
        <div className="grid gap-4 md:grid-cols-4">
          <div>
            <div className="text-[15px] font-semibold text-gray-900">{fullName(p)}</div>
            <div className="text-[12px] text-[#475569]">{p.patient_id}</div>
            <div className="text-[12px] text-[#475569]">{p.age ?? "—"} yrs · {p.gender || "—"} · {p.blood_group || "blood group n/a"}</div>
          </div>
          <div className="text-[12px] text-[#334155]">
            <div className="text-[10.5px] uppercase text-[#94A3B8]">Contact</div>
            <div>{p.phone || "No phone on record"}</div>
            <div className="text-[#64748B]">{p.address || "No address"}</div>
          </div>
          <div className="text-[12px] text-[#334155]">
            <div className="text-[10.5px] uppercase text-[#94A3B8]">Coordination</div>
            <div>{activeJourney ? <>Journey: <Badge status={activeJourney.state} /> {pretty(activeJourney.template_key)}</> : "No active journey"}</div>
            <div>{openTasks} open task(s)</div>
            <div className="text-[#64748B]">Language: {data.preferences?.preferred_language || "not set"} · Channel: {data.preferences?.preferred_channel || "not set"}</div>
          </div>
          <div className="text-[12px] text-[#334155]">
            <div className="text-[10.5px] uppercase text-[#94A3B8]">Clinical follow-up authorization</div>
            <div>WhatsApp: <Badge status={whatsapp?.status} /></div>
            <div>SMS: <Badge status={sms?.status} /></div>
            <div className="text-[10.5px] text-[#94A3B8]">Updated {fmtDateTime(whatsapp?.updated_at || sms?.updated_at)}</div>
          </div>
        </div>
        {p.allergies && <p className="mt-3 rounded border border-[#FDE68A] bg-[#FFFBEB] px-2 py-1 text-[12px] text-[#92400E]">Allergies on registration: {p.allergies}</p>}
      </Card>

      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { key: "overview", label: "Overview" },
          { key: "timeline", label: "Timeline" },
          { key: "clinical", label: "Clinical history" },
          { key: "encounters", label: "Encounters & appointments" },
          { key: "medications", label: "Medications", badge: data.medications.length },
          { key: "investigations", label: "Investigations & documents" },
          { key: "journey", label: "Care journey" },
          { key: "surveys", label: "Surveys", badge: data.survey_responses.length },
          { key: "communications", label: "Communications" },
          { key: "tasks", label: "Tasks", badge: openTasks },
          { key: "consent", label: "Consent & audit" },
        ]}
      />

      {tab === "overview" && <OverviewTab d={data} />}
      {tab === "timeline" && <TimelineTab patientId={p.patient_id} />}
      {tab === "clinical" && <ClinicalTab d={data} />}
      {tab === "encounters" && <EncountersTab d={data} />}
      {tab === "medications" && <MedicationsTab d={data} />}
      {tab === "investigations" && <InvestigationsTab d={data} />}
      {tab === "journey" && <JourneyTab d={data} />}
      {tab === "surveys" && <SurveysTab d={data} />}
      {tab === "communications" && <CommsTab d={data} navigate={navigate} />}
      {tab === "tasks" && <TasksView patientId={p.patient_id} />}
      {tab === "consent" && <ConsentTab patientId={p.patient_id} prefs={data.preferences} onChanged={reload} />}
      <p className="text-[10.5px] text-[#94A3B8]">{data.freshness_note} Loaded {fmtDateTime(data.generated_at)}.</p>

      {showTask && <NewTask patientId={p.patient_id} onClose={() => setShowTask(false)} onCreated={() => { setShowTask(false); reload() }} />}
    </div>
  )
}

function OverviewTab({ d }: { d: Row }) {
  const upcoming = d.appointments.filter((a: Row) => ["scheduled", "checked_in"].includes(a.status)).slice(0, 4)
  const nextAppt = (d.further_appointments && d.further_appointments[0]) || upcoming[0] || d.appointments[0]
  const whatsappSymptomText = firstSymptoms(d.whatsapp_symptoms?.symptoms, d.patient?.symptoms, d.encounters?.[0]?.symptoms, d.encounters?.[0]?.chief_complaint)
  const nextCp = d.checkpoints.find((c: Row) => ["scheduled", "queued", "overdue"].includes(c.status))
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card title="Latest encounter / admission">
        {d.admissions[0] ? (
          <p className="text-[12px]">Admitted {fmtDate(d.admissions[0].admission_date)} · {d.admissions[0].discharge_date ? `discharged ${fmtDate(d.admissions[0].discharge_date)}` : <Badge tone="blue">Currently admitted</Badge>}<Source r={d.admissions[0]} /></p>
        ) : d.encounters[0] ? (
          <div className="space-y-2 text-[12px]">
            <p className="flex items-center gap-2 flex-wrap">
              <span>{d.encounters[0].encounter_type} visit {fmtDateTime(d.encounters[0].arrival_at)} · {d.encounters[0].chief_complaint || "no complaint recorded"}</span>
              <Badge status={d.encounters[0].status} />
              <Source r={d.encounters[0]} />
            </p>
            <div className="rounded border border-[#DCE6FB] bg-[#F4F7FE] p-1.5 text-[11px]">
              <span className="font-semibold text-gray-800">📅 Further Appointment: </span>
              {nextAppt ? (
                <span className="font-medium text-[#1B4FD8]">
                  {fmtDateTime(nextAppt.appointment_date)} · {nextAppt.doctor_name || "Doctor"} ({nextAppt.department || "General"})
                </span>
              ) : <span className="text-[#64748B]">None scheduled</span>}
            </div>
            <div className="rounded border border-emerald-200 bg-emerald-50/70 p-1.5 text-[11px]">
              <span className="font-semibold text-emerald-900">Symptoms: </span>
              <span className="font-medium text-gray-900 italic">
                {whatsappSymptomText ? `"${whatsappSymptomText}"` : "None reported"}
              </span>
            </div>
          </div>
        ) : <p className="text-[12px] text-[#64748B]">No encounters recorded.</p>}
      </Card>
      <Card title="Diagnoses on record">
        {d.diagnoses.length ? <ul className="space-y-1 text-[12px]">{d.diagnoses.slice(0, 5).map((x: Row) => <li key={x.id}>{x.diagnosis_name} <span className="text-[#94A3B8]">{fmtDate(x.created_at)}</span><Source r={x} /></li>)}</ul> : <p className="text-[12px] text-[#64748B]">None recorded.</p>}
      </Card>
      <Card title="Upcoming appointments">
        {upcoming.length ? <ul className="space-y-1 text-[12px]">{upcoming.map((a: Row) => <li key={a.id}>{fmtDateTime(a.appointment_date)} · {a.department || "—"} · {a.doctor_name || "—"} <Badge status={a.status} /></li>)}</ul> : <p className="text-[12px] text-[#64748B]">No upcoming appointments.</p>}
      </Card>
      <Card title="Next journey checkpoint">
        {nextCp ? <p className="text-[12px]">{nextCp.label} · due {fmtDateTime(nextCp.due_at)} <Badge status={nextCp.status} /></p> : <p className="text-[12px] text-[#64748B]">No pending checkpoint.</p>}
      </Card>
      <Card title="Current medications (latest prescriptions)">
        {d.medications.length ? <ul className="space-y-1 text-[12px]">{d.medications.slice(0, 3).map((m: Row) => <li key={m.id}>{m.medicines.map((x: Row) => x.name).join(", ") || "—"} <Badge status={m.status} /></li>)}</ul> : <p className="text-[12px] text-[#64748B]">None on record.</p>}
      </Card>
      <Card title="Latest vitals">
        {d.vitals[0] ? <p className="text-[12px]">BP {d.vitals[0].bp || "—"} · Pulse {d.vitals[0].pulse || "—"} · Temp {d.vitals[0].temperature || "—"} <span className="text-[#94A3B8]">{fmtDateTime(d.vitals[0].created_at)}</span></p> : <p className="text-[12px] text-[#64748B]">None recorded.</p>}
      </Card>
    </div>
  )
}

export function TimelineTab({ patientId }: { patientId: string }) {
  const [category, setCategory] = useState("")
  const [limit, setLimit] = useState(30)
  const { data, error, loading, reload } = useAsync(() => p360.timeline(patientId, { category, limit }), [patientId, category, limit])
  return (
    <Card
      title="Timeline"
      actions={
        <select aria-label="Timeline category" className={inputCls + " !w-44"} value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">All categories</option>
          {(data?.categories || []).map((c: string) => <option key={c} value={c}>{pretty(c)}</option>)}
        </select>
      }
    >
      {error && <ErrorBox error={error} onRetry={reload} />}
      {loading && !data ? <Loading /> : !data || data.events.length === 0 ? <Empty>No events.</Empty> : (
        <>
          <ol className="space-y-2">
            {data.events.map((e: Row) => (
              <li key={e.event_id} className="flex gap-3 border-l-2 border-[#DDE2EC] pl-3">
                <div className="w-36 shrink-0 text-[11px] text-[#64748B]">{fmtDateTime(e.event_timestamp)}</div>
                <div className="text-[12px] text-[#1E293B]">
                  <Badge tone="gray">{pretty(e.category)}</Badge> {e.display_summary}
                  <span className="ml-1.5 text-[10px] text-[#94A3B8]">{e.source_module} · {pretty(e.verification_state)}</span>
                </div>
              </li>
            ))}
          </ol>
          {data.total > data.events.length && <button className={btnCls.ghost + " mt-3"} onClick={() => setLimit(limit + 30)}>Show more ({data.total - data.events.length} older)</button>}
        </>
      )}
    </Card>
  )
}

export function ClinicalTab({ d }: { d: Row }) {
  const h = d.clinical_history
  const rows: Array<[string, any]> = h ? [["Allergies", h.allergies], ["Existing conditions", h.existing_diseases], ["Chronic conditions", h.chronic_conditions], ["Previous surgeries", h.previous_surgeries], ["Family history", h.family_history]] : []
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card title="Medical history">
        {h ? <dl className="space-y-1.5 text-[12px]">{rows.map(([k, v]) => <div key={k}><dt className="text-[10.5px] uppercase text-[#94A3B8]">{k}</dt><dd>{v || "Not recorded"}</dd></div>)}<p className="text-[10.5px] text-[#94A3B8]">Last updated {fmtDateTime(h.updated_at)}</p></dl> : <p className="text-[12px] text-[#64748B]">No history recorded.</p>}
      </Card>
      <Card title="Diagnosis history">
        {d.diagnoses.length ? <Table head={["Diagnosis", "Recorded", "Source"]}>{d.diagnoses.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{x.diagnosis_name}</td><td className="px-2 py-1.5">{fmtDate(x.created_at)}</td><td className="px-2 py-1.5 text-[10.5px] text-[#94A3B8]">{x.source_module} · {pretty(x.verification_state)}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None recorded.</p>}
      </Card>
      <p className="text-[11px] text-[#94A3B8] md:col-span-2">Vitals history and trends are on the Vitals tab.</p>
    </div>
  )
}

export function EncountersTab({ d }: { d: Row }) {
  const nextAppt = (d.further_appointments && d.further_appointments[0]) || d.appointments?.filter((a: Row) => ["scheduled", "checked_in", "confirmed", "booked", "pending"].includes(a.status))[0] || d.appointments?.[0]
  const whatsappSymptomText = firstSymptoms(d.whatsapp_symptoms?.symptoms, d.patient?.symptoms, d.encounters?.[0]?.symptoms, d.encounters?.[0]?.chief_complaint)

  return (
    <div className="space-y-3">
      <Card title="Admissions">
        {d.admissions.length ? <Table head={["Admitted", "Discharged", "Source"]}>{d.admissions.map((a: Row) => <tr key={a.id}><td className="px-2 py-1.5">{fmtDateTime(a.admission_date)}</td><td className="px-2 py-1.5">{a.discharge_date ? fmtDate(a.discharge_date) : <Badge tone="blue">Admitted</Badge>}</td><td className="px-2 py-1.5 text-[10.5px] text-[#94A3B8]">{a.source_module}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
      <Card title="OP / IP encounters">
        {d.encounters.length ? (
          <Table head={["Arrived", "Type", "Complaint", "Status", "Further Appointment", "Symptoms"]}>
            {d.encounters.map((e: Row) => (
              <tr key={e.id}>
                <td className="px-2 py-1.5">{fmtDateTime(e.arrival_at)}</td>
                <td className="px-2 py-1.5">{e.encounter_type}</td>
                <td className="px-2 py-1.5">{e.chief_complaint || "—"}</td>
                <td className="px-2 py-1.5 whitespace-nowrap">
                  <Badge status={e.status} />
                </td>
                <td className="px-2 py-1.5 text-[11px]">
                  {nextAppt ? (
                    <div>
                      <div className="font-semibold text-gray-900">{fmtDateTime(nextAppt.appointment_date)}</div>
                      <div className="text-[10px] text-[#64748B]">{nextAppt.doctor_name || "Doctor"} · {nextAppt.department || "General"}</div>
                    </div>
                  ) : (
                    <span className="text-[#94A3B8]">—</span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-[11px]">
                  {whatsappSymptomText ? (
                    <div className="flex items-center gap-1.5">
                      <span className="inline-flex items-center gap-0.5 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-800 border border-emerald-200 shrink-0">
                        Symptoms
                      </span>
                      <span className="font-medium text-gray-900 italic max-w-xs truncate" title={whatsappSymptomText}>
                        {whatsappSymptomText}
                      </span>
                    </div>
                  ) : e.symptoms ? (
                    <span className="text-gray-800">{e.symptoms}</span>
                  ) : (
                    <span className="text-[#94A3B8]">—</span>
                  )}
                </td>
              </tr>
            ))}
          </Table>
        ) : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
      <Card title="Appointments (read from the scheduler)">
        {d.appointments.length ? <Table head={["When", "Department", "Doctor", "Type", "Status"]}>{d.appointments.map((a: Row) => <tr key={a.id}><td className="px-2 py-1.5">{fmtDateTime(a.appointment_date)}</td><td className="px-2 py-1.5">{a.department || "—"}</td><td className="px-2 py-1.5">{a.doctor_name || "—"}</td><td className="px-2 py-1.5">{a.visit_type}</td><td className="px-2 py-1.5"><Badge status={a.status} /></td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
    </div>
  )
}

export function MedicationsTab({ d }: { d: Row }) {
  return (
    <Card title="Prescriptions (from Pharmacy; read-only here)">
      {d.medications.length ? (
        <Table head={["Prescribed", "Medicines", "Prescriber", "Pharmacy status", "Fulfilled"]}>
          {d.medications.map((m: Row) => (
            <tr key={m.id} className="align-top">
              <td className="px-2 py-1.5">{fmtDateTime(m.created_at)}</td>
              <td className="px-2 py-1.5">{m.medicines.map((x: Row, i: number) => <div key={i}>{x.name} {x.dosage ? `— ${x.dosage}` : ""} {x.quantity ? `× ${x.quantity}` : ""}</div>)}</td>
              <td className="px-2 py-1.5">{m.doctor_username}</td>
              <td className="px-2 py-1.5"><Badge status={m.status} /></td>
              <td className="px-2 py-1.5">{fmtDateTime(m.fulfilled_at)}</td>
            </tr>
          ))}
        </Table>
      ) : <Empty>No prescriptions on record.</Empty>}
      <p className="mt-2 text-[10.5px] text-[#94A3B8]">Patient 360° never edits a prescription. Frequency and route are not captured by the Pharmacy module, so they are not shown.</p>
    </Card>
  )
}

export function InvestigationsTab({ d }: { d: Row }) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card title="Investigations ordered">
        {d.investigations.length ? <Table head={["Test", "Ordered", "By"]}>{d.investigations.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{x.test_name}</td><td className="px-2 py-1.5">{fmtDate(x.created_at)}</td><td className="px-2 py-1.5">{x.doctor_name || "—"}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
        <p className="mt-2 text-[10.5px] text-[#94A3B8]">Results and reports are opened from the Laboratory / Radiology source module; Patient 360° does not duplicate them.</p>
      </Card>
      <Card title="Documents">
        {d.documents.length ? <Table head={["Type", "File", "Uploaded", "OCR"]}>{d.documents.map((x: Row) => <tr key={x.id}><td className="px-2 py-1.5">{pretty(x.doc_type)}</td><td className="px-2 py-1.5">{x.file_name || "—"}</td><td className="px-2 py-1.5">{fmtDate(x.created_at)}</td><td className="px-2 py-1.5">{x.ocr_extracted ? <Badge tone="amber">Extracted — unverified</Badge> : "—"}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
    </div>
  )
}

export function JourneyTab({ d }: { d: Row }) {
  return (
    <div className="space-y-3">
      <Card title="Journeys">
        {d.journeys.length ? <Table head={["Pathway", "Version", "State", "Started", "Reason"]}>{d.journeys.map((j: Row) => <tr key={j.id}><td className="px-2 py-1.5">{pretty(j.template_key)}</td><td className="px-2 py-1.5">v{j.template_version}</td><td className="px-2 py-1.5"><Badge status={j.state} /></td><td className="px-2 py-1.5">{fmtDateTime(j.started_at)}</td><td className="px-2 py-1.5">{j.state_reason || "—"}</td></tr>)}</Table> : <Empty>Not enrolled in a care journey.</Empty>}
      </Card>
      <Card title="Checkpoints">
        {d.checkpoints.length ? <Table head={["Checkpoint", "Action", "Due", "Status"]}>{d.checkpoints.map((c: Row) => <tr key={c.id}><td className="px-2 py-1.5">{c.label}</td><td className="px-2 py-1.5">{pretty(c.action_type)}</td><td className="px-2 py-1.5">{fmtDateTime(c.due_at)}</td><td className="px-2 py-1.5"><Badge status={c.status} /></td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">None.</p>}
      </Card>
    </div>
  )
}

export function SurveysTab({ d }: { d: Row }) {
  const { data, loading } = useAsync(() => p360.responses({ patient_id: d.patient.patient_id, limit: 20 }), [d.patient.patient_id])
  return (
    <Card title="Survey responses (patient-reported — not verified clinical findings)">
      {loading && !data ? <Loading /> : !data || data.responses.length === 0 ? <Empty>No survey responses.</Empty> : (
        <div className="space-y-3">
          {data.responses.map((r: Row) => (
            <div key={r.id} className="rounded border border-[#E2E8F0] p-3 text-[12px]">
              <div className="mb-1 flex items-center gap-2"><b>{pretty(r.survey_key)} v{r.survey_version}</b><Badge status={r.review_status} /><span className="text-[#94A3B8]">{fmtDateTime(r.submitted_at)} via {pretty(r.channel)}</span></div>
              <pre className="whitespace-pre-wrap font-sans text-[#334155]">{Object.entries(r.answers).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("\n")}</pre>
              {r.triggered_task_ids?.length > 0 && <div className="mt-1 text-[#B45309]">Raised task #{r.triggered_task_ids.join(", #")}</div>}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export function CommsTab({ d, navigate }: { d: Row; navigate: (m: string) => void }) {
  return (
    <Card title="Communication history" actions={<button className={btnCls.small} onClick={() => navigate("patient_messages")}>Send via Patient Messages</button>}>
      {d.communications.length ? <Table head={["When", "Channel", "Kind", "Status"]}>{d.communications.map((m: Row) => <tr key={m.id}><td className="px-2 py-1.5">{fmtDateTime(m.created_at)}</td><td className="px-2 py-1.5">{m.channel.toUpperCase()}</td><td className="px-2 py-1.5">{pretty(m.kind)}</td><td className="px-2 py-1.5"><Badge status={m.status} /></td></tr>)}</Table> : <Empty>No messages sent to this patient.</Empty>}
      <p className="mt-2 text-[10.5px] text-[#94A3B8]">Sending uses the existing Patient Messages tool, which honours opt-outs recorded here. Message wording is never shown in this list.</p>
    </Card>
  )
}

export function ConsentTab({ patientId, prefs, onChanged }: { patientId: string; prefs: Row | null; onChanged: () => void }) {
  const { data, error, reload } = useAsync(() => p360.consent(patientId), [patientId])
  const audit = useAsync(() => p360.patientAudit(patientId), [patientId])
  const [form, setForm] = useState({ channel: "whatsapp", purpose: "clinical_followup", status: "active", note: "" })
  const [pf, setPf] = useState({ preferred_language: prefs?.preferred_language || "", preferred_channel: prefs?.preferred_channel || "", contact_from_hour: prefs?.contact_from_hour ?? "", contact_to_hour: prefs?.contact_to_hour ?? "" })
  const [msg, setMsg] = useState("")

  const record = async () => {
    setMsg("")
    try {
      await p360.recordConsent(patientId, form)
      setForm({ ...form, note: "" })
      reload(); audit.reload(); onChanged()
      setMsg("Recorded.")
    } catch (e) { setMsg(errMsg(e)) }
  }
  const savePrefs = async () => {
    setMsg("")
    try {
      await p360.savePreferences(patientId, pf)
      audit.reload(); onChanged()
      setMsg("Preferences saved.")
    } catch (e) { setMsg(errMsg(e)) }
  }
  const purposes = ["clinical_followup", "appointment_reminder", "survey", "marketing"]

  return (
    <div className="space-y-3">
      {error && <ErrorBox error={error} onRetry={reload} />}
      <Card title="Authorization in force">
        <Table head={["Purpose", "WhatsApp", "SMS"]}>
          {purposes.map((pu) => {
            const cell = (ch: string) => data?.current?.find((a: Row) => a.channel === ch && a.purpose === pu)
            return (
              <tr key={pu}>
                <td className="px-2 py-1.5">{pretty(pu)}</td>
                {["whatsapp", "sms"].map((ch) => <td key={ch} className="px-2 py-1.5"><Badge status={cell(ch)?.status} />{cell(ch)?.via_blanket_record && <span className="ml-1 text-[10px] text-[#94A3B8]">(blanket opt-out)</span>}</td>)}
              </tr>
            )
          })}
        </Table>
        <p className="mt-2 text-[10.5px] text-[#94A3B8]">Clinical follow-up consent is separate from marketing. With no record the patient is “not granted” and automated outreach is blocked. A STOP reply on WhatsApp withdraws every purpose.</p>
      </Card>

      <div className="grid gap-3 md:grid-cols-2">
        <Card title="Record authorization or opt-out">
          <div className="grid grid-cols-3 gap-2">
            <Field label="Channel"><select className={inputCls} value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>{["whatsapp", "sms", "all"].map((x) => <option key={x}>{x}</option>)}</select></Field>
            <Field label="Purpose"><select className={inputCls} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })}>{[...purposes, "all"].map((x) => <option key={x}>{x}</option>)}</select></Field>
            <Field label="Status"><select className={inputCls} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>{["active", "withdrawn", "pending_verification", "not_granted", "expired"].map((x) => <option key={x}>{x}</option>)}</select></Field>
          </div>
          <div className="mt-2"><Field label="Note (how consent was given, e.g. signed form, verbal at discharge)"><input className={inputCls} value={form.note} maxLength={300} onChange={(e) => setForm({ ...form, note: e.target.value })} /></Field></div>
          <button className={btnCls.primary + " mt-3"} onClick={record}>Record</button>
        </Card>
        <Card title="Contact preferences">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Preferred language"><input className={inputCls} value={pf.preferred_language} onChange={(e) => setPf({ ...pf, preferred_language: e.target.value })} placeholder="en, te, hi" /></Field>
            <Field label="Preferred channel"><select className={inputCls} value={pf.preferred_channel} onChange={(e) => setPf({ ...pf, preferred_channel: e.target.value })}><option value="">No preference</option><option>whatsapp</option><option>sms</option></select></Field>
            <Field label="Contact from (hour, IST)"><input className={inputCls} type="number" min={0} max={24} value={pf.contact_from_hour} onChange={(e) => setPf({ ...pf, contact_from_hour: e.target.value })} /></Field>
            <Field label="Contact until (hour, IST)"><input className={inputCls} type="number" min={0} max={24} value={pf.contact_to_hour} onChange={(e) => setPf({ ...pf, contact_to_hour: e.target.value })} /></Field>
          </div>
          <button className={btnCls.primary + " mt-3"} onClick={savePrefs}>Save preferences</button>
        </Card>
      </div>
      {msg && <p className="text-[12px] text-[#1D4ED8]">{msg}</p>}

      <Card title="Authorization history">
        {data?.history?.length ? <Table head={["When", "Channel", "Purpose", "Status", "Source", "By", "Note"]}>{data.history.map((h: Row) => <tr key={h.id}><td className="px-2 py-1.5">{fmtDateTime(h.created_at)}</td><td className="px-2 py-1.5">{h.channel}</td><td className="px-2 py-1.5">{pretty(h.purpose)}</td><td className="px-2 py-1.5"><Badge status={h.status} /></td><td className="px-2 py-1.5">{pretty(h.source)}</td><td className="px-2 py-1.5">{h.recorded_by || "—"}</td><td className="px-2 py-1.5">{h.note || ""}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">No history.</p>}
      </Card>
      <Card title="Access & action audit (Patient 360°)">
        {audit.error && <ErrorBox error={audit.error} onRetry={audit.reload} />}
        {audit.data?.events?.length ? <Table head={["When", "Who", "Action"]}>{audit.data.events.map((e: Row) => <tr key={e.id}><td className="px-2 py-1.5">{fmtDateTime(e.created_at)}</td><td className="px-2 py-1.5">{e.actor_username || "—"}</td><td className="px-2 py-1.5">{pretty(e.action)}</td></tr>)}</Table> : <p className="text-[12px] text-[#64748B]">No audited actions yet.</p>}
      </Card>
    </div>
  )
}

export function NewTask({ patientId, onClose, onCreated }: { patientId: string; onClose: () => void; onCreated: () => void }) {
  const [f, setF] = useState({ title: "", description: "", priority: "normal", owner_team: "" })
  const [error, setError] = useState("")
  return (
    <div role="dialog" aria-modal="true" aria-label="New coordination task" className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-md space-y-3 rounded bg-white p-4 shadow-lg">
        <h3 className="text-[13px] font-semibold">New coordination task for {patientId}</h3>
        <Field label="Title"><input autoFocus className={inputCls} maxLength={160} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
        <Field label="Details"><textarea className={inputCls} rows={3} maxLength={1000} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Priority"><select className={inputCls} value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })}>{["low", "normal", "high", "urgent"].map((x) => <option key={x}>{x}</option>)}</select></Field>
          <Field label="Team / role"><input className={inputCls} value={f.owner_team} onChange={(e) => setF({ ...f, owner_team: e.target.value })} placeholder="care_coordinator" /></Field>
        </div>
        {error && <p className="text-[11.5px] text-[#B91C1C]">{error}</p>}
        <div className="flex justify-end gap-2">
          <button className={btnCls.ghost} onClick={onClose}>Cancel</button>
          <button className={btnCls.primary} onClick={async () => { try { await p360.createTask({ patient_id: patientId, ...f }); onCreated() } catch (e) { setError(errMsg(e)) } }}>Create task</button>
        </div>
      </div>
    </div>
  )
}
