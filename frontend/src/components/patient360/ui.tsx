import React, { useCallback, useEffect, useRef, useState } from "react"

/** Small shared pieces for the Patient 360 screens, styled like the rest of the HMS (white cards, #DDE2EC borders). */

export const cx = (...c: Array<string | false | null | undefined>) => c.filter(Boolean).join(" ")

const IST = "Asia/Kolkata"
export const fmtDateTime = (v: any) => {
  if (!v) return "—"
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString("en-IN", { timeZone: IST, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })
}
export const fmtDate = (v: any) => {
  if (!v) return "—"
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleDateString("en-IN", { timeZone: IST, day: "2-digit", month: "short", year: "numeric" })
}
export const fmtDuration = (seconds?: number | null) => {
  if (seconds === null || seconds === undefined) return "—"
  const m = Math.round(seconds / 60)
  if (m < 60) return `${m} min`
  const h = m / 60
  return h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} d`
}
export const fullName = (r: { name?: string; last_name?: string; patient_name?: string; patient_last_name?: string }) =>
  [r.name ?? r.patient_name, r.last_name ?? r.patient_last_name].filter(Boolean).join(" ") || "—"
/** Patient-reported symptoms only: drops the hospital's own registration message and strips helpline / link boilerplate. */
export const cleanSymptoms = (t?: string | null): string => {
  if (!t) return ""
  if (/you are registered at|\bUMR\b[\s\S]{0,60}\bOP No\b|OP timings\s*:/i.test(t)) return ""
  return t.replace(/\bHelpline\b[^.\n]*\.?|Your health record\s*:?\s*\S*|https?:\/\/\S+/gi, "").replace(/\s{2,}/g, " ").replace(/^[\s.,-]+|[\s.,-]+$/g, "")
}
/** First non-empty cleaned symptom text from the given candidates. */
export const firstSymptoms = (...c: Array<string | null | undefined>): string => { for (const x of c) { const s = cleanSymptoms(x); if (s) return s } return "" }

export const pretty = (s?: string | null) => (s ? s.replace(/_/g, " ") : "—")

type Tone = "gray" | "blue" | "green" | "amber" | "red" | "purple"
const TONES: Record<Tone, string> = {
  gray: "bg-[#F1F5F9] text-[#475569] border-[#E2E8F0]",
  blue: "bg-[#EFF6FF] text-[#1D4ED8] border-[#BFDBFE]",
  green: "bg-[#F0FDF4] text-[#15803D] border-[#BBF7D0]",
  amber: "bg-[#FFFBEB] text-[#B45309] border-[#FDE68A]",
  red: "bg-[#FEF2F2] text-[#B91C1C] border-[#FECACA]",
  purple: "bg-[#FAF5FF] text-[#7E22CE] border-[#E9D5FF]",
}
const STATUS_TONE: Record<string, Tone> = {
  active: "green", published: "green", completed: "green", resolved: "green", reviewed: "green", delivered: "green", enrolled: "green", approved: "blue",
  pending_approval: "amber", pending: "amber", paused: "amber", snoozed: "amber", queued: "blue", scheduled: "gray", open: "blue", assigned: "blue",
  acknowledged: "purple", in_progress: "purple", under_review: "purple", escalated: "red", overdue: "red", failed: "red", needs_review: "red",
  withdrawn: "red", urgent: "red", high: "amber", normal: "gray", low: "gray", cancelled: "gray", closed: "gray", retired: "gray", skipped: "gray",
  draft: "gray", eligible: "blue", pending_review: "amber", not_granted: "gray", expired: "amber", pending_verification: "amber",
}
export function Badge({ children, tone, status }: { children?: React.ReactNode; tone?: Tone; status?: string }) {
  const t = tone ?? STATUS_TONE[status ?? ""] ?? "gray"
  return <span className={cx("inline-block rounded border px-1.5 py-0.5 text-[10.5px] font-medium whitespace-nowrap", TONES[t])}>{children ?? pretty(status)}</span>
}

export function Card({ title, actions, children, className }: { title?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cx("rounded border border-[#DDE2EC] bg-white", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-[#EEF1F6] px-4 py-2.5">
          <h3 className="text-[12.5px] font-semibold text-gray-900">{title}</h3>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Stat({ label, value, tone, hint, onClick }: { label: string; value: React.ReactNode; tone?: Tone; hint?: string; onClick?: () => void }) {
  const accent = tone === "red" ? "text-[#B91C1C]" : tone === "amber" ? "text-[#B45309]" : tone === "green" ? "text-[#15803D]" : "text-gray-900"
  const Tag: any = onClick ? "button" : "div"
  return (
    <Tag onClick={onClick} className={cx("rounded border border-[#DDE2EC] bg-white px-3 py-2.5 text-left", onClick && "hover:border-[#1B4FD8]")} title={hint}>
      <div className={cx("text-xl font-semibold leading-tight", accent)}>{value}</div>
      <div className="mt-0.5 text-[11px] text-[#64748B]">{label}</div>
    </Tag>
  )
}

export const inputCls = "w-full rounded border border-[#CBD5E1] bg-white px-2 py-1.5 text-[12.5px] text-gray-900 outline-none focus:border-[#1B4FD8] disabled:bg-[#F8FAFC]"
export const btnCls = {
  primary: "rounded bg-[#1B4FD8] px-3 py-1.5 text-[12px] font-medium text-white hover:bg-[#1741B0] disabled:opacity-50",
  ghost: "rounded border border-[#CBD5E1] bg-white px-3 py-1.5 text-[12px] font-medium text-[#334155] hover:bg-[#F8FAFC] disabled:opacity-50",
  danger: "rounded border border-[#FECACA] bg-white px-3 py-1.5 text-[12px] font-medium text-[#B91C1C] hover:bg-[#FEF2F2] disabled:opacity-50",
  small: "rounded border border-[#CBD5E1] bg-white px-2 py-1 text-[11px] font-medium text-[#334155] hover:bg-[#F8FAFC] disabled:opacity-50",
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-medium text-[#475569]">{label}</span>
      {children}
      {hint && <span className="mt-0.5 block text-[10.5px] text-[#94A3B8]">{hint}</span>}
    </label>
  )
}

export const Empty = ({ children }: { children: React.ReactNode }) => <div className="rounded border border-dashed border-[#CBD5E1] px-4 py-8 text-center text-[12px] text-[#64748B]">{children}</div>
export const Loading = () => <div className="px-4 py-8 text-center text-[12px] text-[#64748B]">Loading…</div>
export const ErrorBox = ({ error, onRetry }: { error: string; onRetry?: () => void }) => (
  <div role="alert" className="flex items-center justify-between gap-3 rounded border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-[12px] text-[#B91C1C]">
    <span>{error}</span>
    {onRetry && <button className={btnCls.small} onClick={onRetry}>Retry</button>}
  </div>
)

export const errMsg = (e: any, fallback = "Request failed") => (e?.status === 403 ? "You do not have permission for this action." : e?.message || fallback)

/** Loads on mount and whenever `deps` change; keeps the previous data visible while reloading. */
export function useAsync<T>(load: () => Promise<T>, deps: React.DependencyList) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)
  const run = useCallback(() => {
    setLoading(true)
    setError("")
    load()
      .then((d) => alive.current && setData(d))
      .catch((e) => alive.current && setError(errMsg(e)))
      .finally(() => alive.current && setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    alive.current = true
    run()
    return () => {
      alive.current = false
    }
  }, [run])
  return { data, error, loading, reload: run, setData }
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: Array<{ key: T; label: string; badge?: number | string }>; value: T; onChange: (k: T) => void }) {
  return (
    <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-[#DDE2EC]">
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          aria-selected={value === t.key}
          onClick={() => onChange(t.key)}
          className={cx("whitespace-nowrap border-b-2 px-3 py-2 text-[12px] font-medium", value === t.key ? "border-[#1B4FD8] text-[#1B4FD8]" : "border-transparent text-[#64748B] hover:text-gray-900")}
        >
          {t.label}
          {t.badge !== undefined && t.badge !== 0 && <span className="ml-1.5 rounded-full bg-[#E2E8F0] px-1.5 text-[10px] text-[#334155]">{t.badge}</span>}
        </button>
      ))}
    </div>
  )
}

/** Modal that collects a written reason/note -- journey and task actions require one, so it is a first-class control. */
export function ReasonDialog({ title, label, required = true, confirmLabel = "Confirm", extra, onCancel, onConfirm }: {
  title: string
  label: string
  required?: boolean
  confirmLabel?: string
  extra?: React.ReactNode
  onCancel: () => void
  onConfirm: (text: string) => Promise<void> | void
}) {
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const submit = async () => {
    if (required && !text.trim()) return setError("Please write a reason.")
    setBusy(true)
    try {
      await onConfirm(text.trim())
    } catch (e) {
      setError(errMsg(e))
      setBusy(false)
    }
  }
  return (
    <div role="dialog" aria-modal="true" aria-label={title} className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-md rounded bg-white p-4 shadow-lg">
        <h3 className="mb-3 text-[13px] font-semibold text-gray-900">{title}</h3>
        {extra}
        <Field label={label}>
          <textarea autoFocus className={inputCls} rows={3} value={text} onChange={(e) => setText(e.target.value)} maxLength={300} />
        </Field>
        {error && <p className="mt-2 text-[11.5px] text-[#B91C1C]">{error}</p>}
        <div className="mt-3 flex justify-end gap-2">
          <button className={btnCls.ghost} onClick={onCancel} disabled={busy}>Cancel</button>
          <button className={btnCls.primary} onClick={submit} disabled={busy}>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (offset: number) => void }) {
  if (total <= limit) return null
  return (
    <div className="mt-3 flex items-center justify-between text-[11.5px] text-[#64748B]">
      <span>{offset + 1}–{Math.min(offset + limit, total)} of {total}</span>
      <div className="flex gap-2">
        <button className={btnCls.small} disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
        <button className={btnCls.small} disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
      </div>
    </div>
  )
}

export const Table = ({ head, children }: { head: string[]; children: React.ReactNode }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-left text-[12px]">
      <thead>
        <tr className="border-b border-[#E2E8F0] text-[10.5px] uppercase tracking-wide text-[#64748B]">
          {head.map((h) => <th key={h} className="px-2 py-1.5 font-medium">{h}</th>)}
        </tr>
      </thead>
      <tbody className="divide-y divide-[#F1F5F9]">{children}</tbody>
    </table>
  </div>
)
