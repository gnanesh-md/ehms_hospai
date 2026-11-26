import { apiFetch } from "../../lib/api"

/** Rows come straight from the Patient 360 API; the screens read the fields they need. */
export type Row = Record<string, any>

const SLOW = 20000 // aggregate endpoints read several source modules

const qs = (params: Record<string, unknown> = {}) => {
  const p = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v))
  })
  const s = p.toString()
  return s ? `?${s}` : ""
}

const get = <R = Row>(path: string, params?: Record<string, unknown>) =>
  apiFetch<R>(`/api/patient360${path}${qs(params)}`, { timeoutMs: SLOW })

const send = <R = Row>(method: "POST" | "PUT" | "DELETE", path: string, body?: unknown) =>
  apiFetch<R>(`/api/patient360${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body), timeoutMs: SLOW })

export const p360 = {
  meta: () => get("/meta"),
  overview: () => get("/overview"),
  analytics: (days: number) => get("/analytics", { days }),

  registry: (p: { q?: string; state?: string; limit?: number; offset?: number }) => get("/patients", p),
  profile: (id: string) => get(`/patients/${encodeURIComponent(id)}`),
  timeline: (id: string, p: { category?: string; limit?: number; offset?: number }) => get(`/patients/${encodeURIComponent(id)}/timeline`, p),
  patientAudit: (id: string) => get(`/patients/${encodeURIComponent(id)}/audit`),
  notes: (id: string) => get(`/patients/${encodeURIComponent(id)}/notes`),
  addNote: (id: string, note: string) => send("POST", `/patients/${encodeURIComponent(id)}/notes`, { note }),
  fullRecord: (id: string) => get(`/patients/${encodeURIComponent(id)}/full-record`),
  symptomHistory: (id: string) => get(`/patients/${encodeURIComponent(id)}/symptoms`),

  consent: (id: string) => get(`/patients/${encodeURIComponent(id)}/consent`),
  recordConsent: (id: string, b: Row) => send("POST", `/patients/${encodeURIComponent(id)}/consent`, b),
  savePreferences: (id: string, b: Row) => send("PUT", `/patients/${encodeURIComponent(id)}/preferences`, b),
  eligibility: (b: Row) => send("POST", "/eligibility", b),

  enrollments: (p: Row) => get("/enrollments", p),
  enrollFromDischarge: (admission_id: number) => send("POST", "/enrollments/from-discharge", { admission_id }),
  startJourney: (enrollmentId: number, template_id: number) => send("POST", `/enrollments/${enrollmentId}/start-journey`, { template_id }),
  journeys: (p: Row) => get("/journeys", p),
  journey: (id: number) => get(`/journeys/${id}`),
  journeyState: (id: number, state: string, reason: string) => send("POST", `/journeys/${id}/state`, { state, reason }),
  resolveCheckpoint: (id: number, outcome: string, note: string) => send("POST", `/checkpoints/${id}/resolve`, { outcome, note }),
  runDue: () => send("POST", "/checkpoints/run-due", {}),

  templates: () => get("/journey-templates"),
  createTemplate: (b: Row) => send("POST", "/journey-templates", b),
  updateTemplate: (id: number, b: Row) => send("PUT", `/journey-templates/${id}`, b),
  newTemplateVersion: (id: number) => send("POST", `/journey-templates/${id}/new-version`, {}),
  submitTemplate: (id: number) => send("POST", `/journey-templates/${id}/submit`, {}),
  reviewTemplate: (id: number, action: string) => send("POST", `/journey-templates/${id}/review/${action}`, {}),

  tasks: (p: Row) => get("/tasks", p),
  task: (id: number) => get(`/tasks/${id}`),
  createTask: (b: Row) => send("POST", "/tasks", b),
  taskAction: (id: number, b: Row) => send("POST", `/tasks/${id}/action`, b),

  surveys: () => get("/surveys"),
  createSurvey: (b: Row) => send("POST", "/surveys", b),
  updateSurvey: (id: number, b: Row) => send("PUT", `/surveys/${id}`, b),
  newSurveyVersion: (id: number) => send("POST", `/surveys/${id}/new-version`, {}),
  submitSurvey: (id: number) => send("POST", `/surveys/${id}/submit`, {}),
  reviewSurvey: (id: number, action: string) => send("POST", `/surveys/${id}/review/${action}`, {}),
  responses: (p: Row) => get("/responses", p),
  submitResponse: (b: Row) => send("POST", "/responses", b),
  reviewResponse: (id: number, status: string, note: string) => send("POST", `/responses/${id}/review`, { status, note }),

  interpret: (prompt: string) => send("POST", "/queries/interpret", { prompt }),
  evaluate: (criteria: Row, limit = 25, offset = 0) => send("POST", "/segments/evaluate", { criteria, limit, offset }),
  recipients: (criteria: Row, channel: string, purpose: string) => send("POST", "/segments/recipients", { criteria, channel, purpose }),
  segments: () => get("/segments"),
  saveSegment: (b: Row) => send("POST", "/segments", b),
  versionSegment: (id: number, b: Row) => send("PUT", `/segments/${id}`, b),
  refreshSegment: (id: number) => send("POST", `/segments/${id}/refresh`, {}),
  archiveSegment: (id: number) => send("DELETE", `/segments/${id}`),
}
