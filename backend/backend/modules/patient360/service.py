"""Patient 360 data access and workflows.

Every query is scoped by hospital_id (there is no automatic tenant scoping in this codebase) and every
patient-scoped write first proves the patient is a canonical record of that hospital -- rows bulk-imported from an
uploaded file (patients.source = 'bulk_import') are *not* canonical and are never enrolled, messaged or profiled
from here (spec 9.3 / AC-05).

Business decisions live in modules.patient360.rules; this file only moves data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from utils.database import (
    current_utc_naive,
    get_admissions,
    get_connection,
    get_patient_journey,
    list_appointments,
    list_encounters,
)
from shared.responses import row_to_dict, rows_to_dicts
from modules.patient360 import rules

logger = logging.getLogger(__name__)

CANONICAL = "p.deleted_at IS NULL AND COALESCE(p.source, 'registration') <> 'bulk_import'"
PRIORITY_DUE_HOURS = {"urgent": 4, "high": 24, "normal": 72, "low": 168}


class Patient360Error(Exception):
    """A request the caller can fix; the route turns it into an HTTP error."""

    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


def _j(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dump(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _one(cur, sql, params=()):
    cur.execute(sql, params)
    return row_to_dict(cur.fetchone())


def _all(cur, sql, params=()):
    cur.execute(sql, params)
    return rows_to_dicts(cur.fetchall())


def _canonical_patient(cur, hospital_id, patient_id):
    patient_id = str(patient_id or "").strip()
    if not patient_id:
        raise Patient360Error("patient_id is required")
    row = _one(
        cur,
        f"SELECT p.patient_id, p.name, p.last_name, p.phone FROM patients p "
        f"WHERE p.hospital_id = ? AND p.patient_id = ? AND {CANONICAL}",
        (hospital_id, patient_id),
    )
    if not row:
        raise Patient360Error("Patient not found", 404)
    return row


def _has_col(cur, table, column) -> bool:
    """Tables here were created by different versions of the app over time, so a column that exists in a fresh database
    can be missing in an upgraded one (and vice versa). Ask the database instead of assuming."""
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?", (table, column))
    return cur.fetchone() is not None


def _vitals(cur, pid):
    """Latest vitals under either schema: the original (created_at, encounter_id) or the upgraded one (recorded_at, admission_id,
    spo2, ...). The timestamp is always returned as created_at so the screen needs no change."""
    when = "recorded_at" if _has_col(cur, "patient_vitals", "recorded_at") else "created_at"
    extra = [c for c in ("spo2", "respiratory_rate", "blood_glucose", "weight", "height") if _has_col(cur, "patient_vitals", c)]
    cols = ", ".join(["id", "bp", "pulse", "temperature"] + extra)
    return _all(cur, f"SELECT {cols}, {when} AS created_at FROM patient_vitals WHERE patient_id = ? ORDER BY {when} DESC LIMIT 30", (pid,))


# ── Registry ────────────────────────────────────────────────────────────────────────────────────
_ADMITTED = "EXISTS (SELECT 1 FROM admissions a WHERE a.hospital_id = p.hospital_id AND a.patient_id = p.patient_id AND a.discharge_date IS NULL)"
_REGISTRY_STATES = {
    "admitted": _ADMITTED,
    "discharged_30d": "EXISTS (SELECT 1 FROM admissions a WHERE a.hospital_id = p.hospital_id AND a.patient_id = p.patient_id "
    "AND a.discharge_date >= CURRENT_DATE - 30)",
    "active_journey": "EXISTS (SELECT 1 FROM care_journey_instances j WHERE j.hospital_id = p.hospital_id AND j.patient_id = p.patient_id AND j.state = 'active')",
    "open_tasks": "EXISTS (SELECT 1 FROM care_tasks t WHERE t.hospital_id = p.hospital_id AND t.patient_id = p.patient_id AND t.status NOT IN ('resolved', 'cancelled'))",
}


def list_registry(hospital_id, q="", state="", limit=25, offset=0):
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    where, params = [f"p.hospital_id = ?", CANONICAL], [hospital_id]
    q = " ".join(str(q or "").split())[:80]
    if q:
        like = f"%{q.lower()}%"
        # Phones are stored as typed ("+91 98765 43210"), so compare digits only; a "+91" country prefix on the query is ignored.
        digits = "".join(ch for ch in q if ch.isdigit())
        phone_like = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
        phone_sql = "REGEXP_REPLACE(COALESCE(p.phone, ''), '[^0-9]', '', 'g') LIKE ?"
        where.append(f"(LOWER(p.patient_id) LIKE ? OR LOWER(COALESCE(p.name, '') || ' ' || COALESCE(p.last_name, '')) LIKE ? OR COALESCE(p.phone, '') LIKE ?"
                     + (f" OR {phone_sql})" if len(digits) >= 4 else ")"))
        params += [like, like, f"%{q}%"] + ([f"%{phone_like}%"] if len(digits) >= 4 else [])
    if state:
        if state not in _REGISTRY_STATES:
            raise Patient360Error(f"state must be one of {', '.join(_REGISTRY_STATES)}")
        where.append(_REGISTRY_STATES[state])
    clause = " AND ".join(where)
    with get_connection() as conn:
        cur = conn.cursor()
        total = _one(cur, f"SELECT COUNT(*) AS n FROM patients p WHERE {clause}", tuple(params))["n"]
        rows = _all(
            cur,
            f"""
            SELECT p.patient_id, p.name, p.last_name, p.age, p.gender, p.phone, p.created_at,
                   CASE WHEN {_ADMITTED} THEN 1 ELSE 0 END AS admitted,
                   (SELECT MAX(a.discharge_date) FROM admissions a WHERE a.hospital_id = p.hospital_id AND a.patient_id = p.patient_id) AS last_discharge,
                   (SELECT j.state FROM care_journey_instances j WHERE j.hospital_id = p.hospital_id AND j.patient_id = p.patient_id ORDER BY j.id DESC LIMIT 1) AS journey_state,
                   (SELECT COUNT(*) FROM care_tasks t WHERE t.hospital_id = p.hospital_id AND t.patient_id = p.patient_id AND t.status NOT IN ('resolved', 'cancelled')) AS open_tasks
            FROM patients p WHERE {clause}
            ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?
            """,
            tuple(params + [limit, offset]),
        )
    return {"patients": rows, "total": total, "limit": limit, "offset": offset}


# ── Profile ─────────────────────────────────────────────────────────────────────────────────────
_PROFILE_FIELDS = (
    "patient_id, name, middle_name, last_name, dob, age, gender, phone, address, blood_group, "
    "emergency_contact, allergies, symptoms, patient_type, created_at, updated_at"
)  # no Aadhaar / identity hash: those never leave the patient module


def _source(module, record_type, record_id=None, state="recorded"):
    return {"source_module": module, "source_record_type": record_type, "source_record_id": record_id, "verification_state": state}


def get_profile(hospital_id, patient_id):
    with get_connection() as conn:
        cur = conn.cursor()
        patient = _one(
            cur,
            f"SELECT {_PROFILE_FIELDS} FROM patients p WHERE p.hospital_id = ? AND p.patient_id = ? AND {CANONICAL}",
            (hospital_id, str(patient_id or "").strip()),
        )
        if not patient:
            return None
        pid = patient["patient_id"]
        # Child tables without their own hospital_id are scoped through the patient proven above.
        history = _one(cur, "SELECT allergies, existing_diseases, chronic_conditions, previous_surgeries, family_history, updated_at "
                            "FROM medical_history WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        diagnoses = _all(cur, "SELECT id, diagnosis_name, encounter_id, created_at FROM diagnosis_records WHERE patient_id = ? ORDER BY created_at DESC LIMIT 10", (pid,))
        vitals = _vitals(cur, pid)
        rx_rows = _all(cur, "SELECT id, status, doctor_username, medicines_json, created_at, fulfilled_at FROM pharmacy_prescriptions "
                            "WHERE hospital_id = ? AND patient_id = ? ORDER BY created_at DESC LIMIT 10", (hospital_id, pid))
        investigations = _all(cur, "SELECT id, test_name, doctor_name, created_at, order_status, status, reported_at, department FROM diagnostics WHERE hospital_id = ? AND patient_id = ? "
                                   + ("AND deleted_at IS NULL " if _has_col(cur, "diagnostics", "deleted_at") else "")
                                   + "ORDER BY created_at DESC LIMIT 10", (hospital_id, pid))
        documents = _all(cur, "SELECT id, doc_type, file_name, mime_type, created_at, (ocr_text IS NOT NULL AND ocr_text <> '') AS ocr_extracted "
                              "FROM documents WHERE patient_id = ? " + ("AND deleted_at IS NULL " if _has_col(cur, "documents", "deleted_at") else "") +
                              "ORDER BY created_at DESC LIMIT 20", (pid,))
        live = lambda t, a="": ("AND " + (a + "." if a else "") + "deleted_at IS NULL ") if _has_col(cur, t, "deleted_at") else ""
        invoices = _all(cur, "SELECT id, invoice_no, module, total_amount, paid_amount, due_amount, payment_status, created_at FROM invoices "
                             "WHERE hospital_id = ? AND patient_id = ? " + live("invoices") + "ORDER BY created_at DESC LIMIT 10", (hospital_id, pid))
        claims = _all(cur, "SELECT id, invoice_id, insurer_name, claim_amount, approved_amount, claim_status, submitted_at FROM insurance_claims "
                           "WHERE patient_id = ? " + live("insurance_claims") + "ORDER BY id DESC LIMIT 10", (pid,))
        surgeries = _all(cur, "SELECT id, procedure_name, surgeon_name, scheduled_start, status FROM ot_surgeries "
                              "WHERE patient_id = ? " + live("ot_surgeries") + "ORDER BY scheduled_start DESC NULLS LAST LIMIT 10", (pid,))
        beds = _all(cur, "SELECT id, ward, room_no, bed_no, allocated_at, released_at, status FROM bed_allocations "
                         "WHERE hospital_id = ? AND patient_id = ? " + live("bed_allocations") + "ORDER BY allocated_at DESC NULLS LAST LIMIT 5", (hospital_id, pid))
        er_visits = _all(cur, "SELECT id, visit_no, arrival_at, status, arrival_mode, condition_at_arrival, assigned_doctor_name, assigned_specialty FROM er_visits "
                              "WHERE hospital_id = ? AND patient_id = ? " + live("er_visits") + "ORDER BY arrival_at DESC LIMIT 10", (hospital_id, pid))
        lab_orders = _all(cur, "SELECT id, order_no, encounter_type, priority, status, clinical_indication, created_at FROM diag_orders "
                               "WHERE patient_id = ? ORDER BY created_at DESC LIMIT 10", (pid,))
        journeys = _all(cur, "SELECT id, template_key, template_version, state, started_at, state_reason, assigned_to FROM care_journey_instances "
                             "WHERE hospital_id = ? AND patient_id = ? ORDER BY id DESC LIMIT 5", (hospital_id, pid))
        checkpoints = _all(cur, "SELECT id, journey_id, checkpoint_key, label, action_type, due_at, status, task_id FROM care_checkpoints "
                                "WHERE hospital_id = ? AND patient_id = ? ORDER BY due_at ASC LIMIT 60", (hospital_id, pid))
        tasks = _all(cur, "SELECT id, title, category, priority, status, owner_team, assigned_to, due_at, created_at FROM care_tasks "
                          "WHERE hospital_id = ? AND patient_id = ? ORDER BY (status IN ('resolved','cancelled')), due_at ASC NULLS LAST, id DESC LIMIT 30",
                     (hospital_id, pid))
        responses = _all(cur, "SELECT id, survey_key, survey_version, review_status, submitted_at, channel FROM survey_responses "
                              "WHERE hospital_id = ? AND patient_id = ? ORDER BY submitted_at DESC LIMIT 10", (hospital_id, pid))
        messages = _all(cur, "SELECT id, channel, kind, body, status, created_at FROM patient_messages WHERE hospital_id = ? AND patient_id = ? "
                             "ORDER BY created_at DESC LIMIT 15", (hospital_id, pid))
        prefs = _one(cur, "SELECT preferred_language, preferred_channel, contact_from_hour, contact_to_hour, updated_by, updated_at "
                          "FROM patient360_preferences WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        phone_digits = "".join(ch for ch in (patient.get("phone") or "") if ch.isdigit())
        whatsapp_fb = None
        if phone_digits:
            whatsapp_fb = _one(
                cur,
                """
                SELECT comment, received_at, whatsapp_message_id, rating
                FROM patient_feedback
                WHERE RIGHT(regexp_replace(phone_number, '[^0-9]', '', 'g'), 10) = ?
                  AND comment IS NOT NULL AND comment <> ''
                ORDER BY received_at DESC LIMIT 1
                """,
                (phone_digits[-10:],),
            )
        whatsapp_msg = _one(
            cur,
            """
            SELECT body, created_at, kind
            FROM patient_messages
            WHERE hospital_id = ? AND patient_id = ? AND channel = 'whatsapp'
              AND kind = 'symptoms'  -- only what the patient reported, never the hospital's own outbound messages
              AND body IS NOT NULL AND body <> ''
            ORDER BY created_at DESC LIMIT 1
            """,
            (hospital_id, pid),
        )
    medications = []
    for rx in rx_rows:
        items = _j(rx.pop("medicines_json", None), [])
        rx["medicines"] = [
            {"name": m.get("name"), "dosage": m.get("dosage"), "quantity": m.get("quantity")} for m in items if isinstance(m, dict)
        ]
        rx.update(_source("pharmacy", "prescription", rx["id"]))
        medications.append(rx)
    for key, rows, mod, rtype in (
        ("diagnoses", diagnoses, "clinical", "diagnosis"),
        ("vitals", vitals, "clinical", "vitals"),
        ("investigations", investigations, "laboratory", "diagnostic_order"),
        ("documents", documents, "documents", "document"),
        ("invoices", invoices, "billing", "invoice"),
        ("claims", claims, "insurance", "claim"),
        ("surgeries", surgeries, "surgery", "surgery"),
        ("beds", beds, "beds", "bed_allocation"),
        ("er_visits", er_visits, "emergency", "er_visit"),
        ("lab_orders", lab_orders, "laboratory", "lab_order"),
    ):
        for r in rows:
            r.update(_source(mod, rtype, r["id"]))
    admissions = rows_to_dicts(get_admissions(pid, hospital_id=hospital_id))[:10]
    encounters = rows_to_dicts(list_encounters(patient_id=pid, hospital_id=hospital_id))[:10]
    appointments = rows_to_dicts(list_appointments(patient_id=pid, hospital_id=hospital_id))[:15]
    for r in admissions:
        r.update(_source("inpatient", "admission", r.get("id")))
    for r in encounters:
        r.update(_source("outpatient" if r.get("encounter_type") == "OP" else "inpatient", "encounter", r.get("id")))
    for r in appointments:
        r.update(_source("appointments", "appointment", r.get("id")))

    # Never show the hospital's own registration message (or its helpline / link boilerplate) as the patient's symptoms.
    clean = rules.clean_symptom_text
    patient["symptoms"] = clean(patient.get("symptoms"))
    for e in encounters:
        e["symptoms"] = clean(e.get("symptoms"))
    whatsapp_symptom_text = (
        clean(whatsapp_msg.get("body") if whatsapp_msg else None)
        or patient.get("symptoms")
        or (encounters[0].get("symptoms") if encounters else None)
        or clean(whatsapp_fb.get("comment") if whatsapp_fb else None)
    )
    whatsapp_symptom_at = (
        (whatsapp_msg.get("created_at") if whatsapp_msg else None)
        or (whatsapp_fb.get("received_at") if whatsapp_fb else None)
        or patient.get("updated_at")
    )
    whatsapp_symptoms = {
        "symptoms": whatsapp_symptom_text,
        "received_at": whatsapp_symptom_at,
        "channel": "whatsapp" if (whatsapp_fb or whatsapp_msg) else "intake",
        "has_whatsapp_source": bool(whatsapp_fb or whatsapp_msg),
    } if whatsapp_symptom_text else None

    now_dt = current_utc_naive()
    further_appointments = []
    for a in appointments:
        st = str(a.get("status") or "").lower()
        dt = a.get("appointment_date")
        if st in ("scheduled", "checked_in", "confirmed", "booked", "pending") or (dt and dt >= now_dt):
            further_appointments.append(a)
    further_appointments.sort(key=lambda x: str(x.get("appointment_date") or ""), reverse=False)

    return {
        "patient": patient,
        "clinical_history": history,
        "diagnoses": diagnoses,
        "vitals": vitals,
        "medications": medications,
        "investigations": investigations,
        "documents": documents,
        "invoices": invoices,
        "claims": claims,
        "surgeries": surgeries,
        "beds": beds,
        "er_visits": er_visits,
        "lab_orders": lab_orders,
        "admissions": admissions,
        "encounters": encounters,
        "appointments": appointments,
        "further_appointments": further_appointments,
        "whatsapp_symptoms": whatsapp_symptoms,
        "journeys": journeys,
        "checkpoints": checkpoints,
        "tasks": tasks,
        "survey_responses": responses,
        "communications": messages,
        "preferences": prefs,
        "authorizations": current_authorizations(hospital_id, pid),
        "generated_at": current_utc_naive(),
        "freshness_note": "Read live from each source module at request time; nothing here is a cached copy.",
    }


# ── Timeline ────────────────────────────────────────────────────────────────────────────────────
_STAGE = {  # get_patient_journey stage -> (category, source module)
    "registration": ("registration", "patients"),
    "er": ("emergency", "er"),
    "queue": ("opd", "appointments"),
    "consultation": ("opd", "op"),
    "bed": ("ipd", "beds"),
    "clinical": ("opd", "clinical"),
    "pharmacy": ("pharmacy", "pharmacy"),
    "lab": ("laboratory", "laboratory"),
    "documents": ("documents", "documents"),
    "billing": ("finance", "billing"),
    "icu": ("ot_icu", "icu"),
    "ot": ("ot_icu", "ot"),
    "orders": ("clinical", "clinical"),
    "care_plan": ("clinical", "clinical"),
}


def _event(category, module, rtype, rid, label, when, actor=None, state="recorded", detail=None, key=None):
    stamp = rules.parse_dt(when) if when else None
    ident = key or hashlib.sha1(f"{module}|{rtype}|{rid}|{label}|{stamp}".encode()).hexdigest()[:16]
    return {
        "event_id": ident, "event_type": rtype, "category": category, "display_summary": label, "event_timestamp": stamp,
        "source_module": module, "source_record_type": rtype, "source_record_id": rid, "actor_id": actor,
        "verification_state": state, "detail": detail, "correlation_id": ident,
    }


def get_timeline(hospital_id, patient_id, category="", limit=50, offset=0):
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    with get_connection() as conn:
        cur = conn.cursor()
        patient = _canonical_patient(cur, hospital_id, patient_id)
        pid = patient["patient_id"]
        msgs = _all(cur, "SELECT id, channel, kind, status, created_by, created_at FROM patient_messages WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        tasks = _all(cur, "SELECT id, title, status, priority, created_by, created_at, resolved_at FROM care_tasks WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        responses = _all(cur, "SELECT id, survey_key, survey_version, channel, submitted_at, review_status FROM survey_responses WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        journeys = _all(cur, "SELECT id, template_key, template_version, state, state_reason, started_at, closed_at, created_by FROM care_journey_instances WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        enrollments = _all(cur, "SELECT id, status, discharged_at, created_at FROM patient360_enrollments WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
        auths = _all(cur, "SELECT id, channel, purpose, status, recorded_by, created_at FROM communication_authorizations WHERE hospital_id = ? AND patient_id = ?", (hospital_id, pid))
    events = []
    try:
        for ev in (get_patient_journey(pid, hospital_id=hospital_id) or {}).get("events", []):
            cat, mod = _STAGE.get(ev.get("stage"), ("clinical", "clinical"))
            if ev.get("timestamp"):
                events.append(_event(cat, mod, str(ev.get("stage")), None, ev.get("label") or "", ev["timestamp"], detail=ev.get("detail")))
    except Exception:  # a broken source must not blank the whole timeline
        logger.exception("patient360: clinical timeline source failed for %s", pid)
    for e in enrollments:
        events.append(_event("discharge", "beds", "discharge_finalized", e["id"], "Discharge finalized -- follow-up eligibility evaluated", e["discharged_at"], key=f"enr{e['id']}"))
    for m in msgs:
        events.append(_event("engagement", "messaging", "message", m["id"], f"{m['channel'].upper()} message ({m['kind']}) -- {m['status']}", m["created_at"], m.get("created_by"), key=f"msg{m['id']}"))
    for r in responses:
        events.append(_event("engagement", "patient360", "survey_response", r["id"], f"Survey response received ({r['survey_key']} v{r['survey_version']}, {r['channel']})", r["submitted_at"], state="patient_reported", key=f"resp{r['id']}"))
    for j in journeys:
        events.append(_event("coordination", "patient360", "journey_started", j["id"], f"Care journey started ({j['template_key']} v{j['template_version']})", j["started_at"] or None, j.get("created_by"), key=f"jst{j['id']}"))
        if j["closed_at"]:
            events.append(_event("coordination", "patient360", "journey_closed", j["id"], f"Care journey {j['state']}" + (f": {j['state_reason']}" if j["state_reason"] else ""), j["closed_at"], key=f"jcl{j['id']}"))
    for t in tasks:
        events.append(_event("coordination", "patient360", "task_created", t["id"], f"Task created: {t['title']} ({t['priority']})", t["created_at"], t.get("created_by"), key=f"tc{t['id']}"))
        if t["resolved_at"]:
            events.append(_event("coordination", "patient360", "task_resolved", t["id"], f"Task {t['status']}: {t['title']}", t["resolved_at"], key=f"tr{t['id']}"))
    for a in auths:
        events.append(_event("engagement", "patient360", "authorization", a["id"], f"{a['channel']} / {a['purpose']} authorization: {a['status']}", a["created_at"], a.get("recorded_by"), key=f"auth{a['id']}"))
    events = [e for e in events if e["event_timestamp"] is not None]
    if category:
        events = [e for e in events if e["category"] == category]
    events.sort(key=lambda e: (e["event_timestamp"], e["event_id"]), reverse=True)
    categories = sorted({e["category"] for e in events})
    return {"events": events[offset:offset + limit], "total": len(events), "limit": limit, "offset": offset, "categories": categories}


# ── Authorization & preferences ─────────────────────────────────────────────────────────────────
def record_authorization(hospital_id, patient_id, channel, purpose, status, actor, source="staff_recorded", note="", policy_version=None):
    if channel not in rules.CHANNELS + ("all",):
        raise Patient360Error("channel must be whatsapp, sms or all")
    if purpose not in rules.PURPOSES + ("all",):
        raise Patient360Error(f"purpose must be one of {', '.join(rules.PURPOSES)} or all")
    if status not in rules.AUTH_STATUSES:
        raise Patient360Error(f"status must be one of {', '.join(rules.AUTH_STATUSES)}")
    note = rules.clean_text(note, 300, "note")
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        row = _one(
            cur,
            "INSERT INTO communication_authorizations (hospital_id, patient_id, channel, purpose, status, source, policy_version, note, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id, created_at",
            (hospital_id, patient_id, channel, purpose, status, source, policy_version, note or None, actor),
        )
        conn.commit()
    return row


def _auth_rows(cur, hospital_id, patient_ids):
    if not patient_ids:
        return {}
    marks = ", ".join("?" for _ in patient_ids)
    rows = _all(cur, f"SELECT id, patient_id, channel, purpose, status, source, note, recorded_by, created_at FROM communication_authorizations "
                     f"WHERE hospital_id = ? AND patient_id IN ({marks}) ORDER BY created_at DESC, id DESC", tuple([hospital_id] + list(patient_ids)))
    grouped = {}
    for r in rows:
        grouped.setdefault(r["patient_id"], []).append(r)
    return grouped


def current_authorizations(hospital_id, patient_id):
    """The decision in force for every channel x purpose, plus the last-updated stamp (spec 4.1)."""
    with get_connection() as conn:
        rows = _auth_rows(conn.cursor(), hospital_id, [patient_id]).get(patient_id, [])
    out = []
    for ch in rules.CHANNELS:
        for purpose in rules.PURPOSES:
            latest = rules.latest_authorization(rows, ch, purpose)
            out.append({"channel": ch, "purpose": purpose, "status": (latest or {}).get("status") or "not_granted",
                        "recorded": bool(latest), "updated_at": (latest or {}).get("created_at"), "recorded_by": (latest or {}).get("recorded_by"),
                        "via_blanket_record": bool(latest and (latest["purpose"] == "all" or latest["channel"] == "all"))})
    return out


def authorization_history(hospital_id, patient_id, limit=100):
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        return _all(cur, "SELECT id, channel, purpose, status, source, policy_version, note, recorded_by, created_at FROM communication_authorizations "
                         "WHERE hospital_id = ? AND patient_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (hospital_id, patient_id, max(1, min(int(limit), 500))))


def save_preferences(hospital_id, patient_id, actor, language=None, channel=None, from_hour=None, to_hour=None):
    if channel not in (None, "", *rules.CHANNELS):
        raise Patient360Error("preferred_channel must be whatsapp or sms")
    hours = []
    for h in (from_hour, to_hour):
        if h in (None, ""):
            hours.append(None)
            continue
        try:
            h = int(h)
        except (TypeError, ValueError):
            raise Patient360Error("contact hours must be whole numbers 0-24") from None
        if not 0 <= h <= 24:
            raise Patient360Error("contact hours must be 0-24")
        hours.append(h)
    if (hours[0] is None) != (hours[1] is None):
        raise Patient360Error("set both contact_from_hour and contact_to_hour, or neither")
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        cur.execute(
            """
            INSERT INTO patient360_preferences (hospital_id, patient_id, preferred_language, preferred_channel, contact_from_hour, contact_to_hour, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (hospital_id, patient_id) DO UPDATE SET preferred_language = EXCLUDED.preferred_language,
                preferred_channel = EXCLUDED.preferred_channel, contact_from_hour = EXCLUDED.contact_from_hour,
                contact_to_hour = EXCLUDED.contact_to_hour, updated_by = EXCLUDED.updated_by, updated_at = CURRENT_TIMESTAMP
            """,
            (hospital_id, patient_id, rules.clean_text(language, 20, "language") or None, channel or None, hours[0], hours[1], actor),
        )
        conn.commit()


def check_eligibility(hospital_id, patient_id, channel, purpose, require_active=True, now=None):
    """Send-time check against the live record. Fails closed when it cannot establish eligibility."""
    with get_connection() as conn:
        cur = conn.cursor()
        patient = _one(cur, f"SELECT p.patient_id, p.phone FROM patients p WHERE p.hospital_id = ? AND p.patient_id = ? AND {CANONICAL}", (hospital_id, patient_id))
        if not patient:
            return {"eligible": False, "reason": "patient_not_found_or_not_canonical", "deferrable": False}
        auth = _auth_rows(cur, hospital_id, [patient_id]).get(patient_id, [])
        prefs = _one(cur, "SELECT contact_from_hour, contact_to_hour FROM patient360_preferences WHERE hospital_id = ? AND patient_id = ?", (hospital_id, patient_id))
    window = None
    if prefs and prefs.get("contact_from_hour") is not None and prefs.get("contact_to_hour") is not None:
        window = (prefs["contact_from_hour"], prefs["contact_to_hour"])
    return rules.check_send_eligibility(phone=patient["phone"], channel=channel, purpose=purpose, auth_rows=auth,
                                        contact_window=window, now_utc=now, require_active=require_active)


def is_opted_out(hospital_id, patient_id, channel, purpose="clinical_followup"):
    """Used by the existing desk-messaging path: only an *explicit* withdrawal blocks (no record = unchanged behaviour)."""
    if not patient_id:
        return False
    try:
        with get_connection() as conn:
            rows = _auth_rows(conn.cursor(), hospital_id, [patient_id]).get(patient_id, [])
        latest = rules.latest_authorization(rows, channel, purpose)
        return bool(latest and latest["status"] == "withdrawn")
    except Exception:
        logger.exception("patient360: opt-out lookup failed; not blocking an existing desk send")
        return False


def record_opt_out_by_phone(phone, channel="whatsapp", source="inbound_stop_reply"):
    """A STOP reply identifies a sender number, not a hospital: withdraw for every canonical patient on that number."""
    digits = rules.normalize_phone(phone)
    if not digits:
        return 0
    last10 = digits[-10:]
    with get_connection() as conn:
        cur = conn.cursor()
        matches = _all(cur, f"SELECT p.hospital_id, p.patient_id FROM patients p WHERE {CANONICAL} AND RIGHT(regexp_replace(COALESCE(p.phone, ''), '[^0-9]', '', 'g'), 10) = ?", (last10,))
        for m in matches:
            cur.execute("INSERT INTO communication_authorizations (hospital_id, patient_id, channel, purpose, status, source, note, recorded_by) "
                        "VALUES (?, ?, ?, 'all', 'withdrawn', ?, 'Patient replied STOP', 'patient')", (m["hospital_id"], m["patient_id"], channel, source))
        conn.commit()
    return len(matches)


# ── Approval workflow shared by journey templates and surveys ───────────────────────────────────
def _self_approval_allowed():
    return os.getenv("PATIENT360_ALLOW_SELF_APPROVAL", "").strip().lower() == "true"


def _advance_approval(cur, table, hospital_id, row_id, target, actor, group_cols):
    row = _one(cur, f"SELECT * FROM {table} WHERE id = ? AND hospital_id = ?", (row_id, hospital_id))
    if not row:
        raise Patient360Error("Not found", 404)
    if not rules.can_transition(rules.APPROVAL_TRANSITIONS, row["status"], target):
        raise Patient360Error(f"Cannot move from {row['status']} to {target}", 409)
    sets, params = ["status = ?"], [target]
    if target == "pending_approval":
        sets += ["submitted_at = CURRENT_TIMESTAMP"]
    if target == "approved":
        # Two-person rule: whoever drafted/submitted it may not be the one who clinically approves it.
        if row.get("created_by") and row["created_by"] == actor and not _self_approval_allowed():
            raise Patient360Error("A different authorized person must approve this (two-person rule).", 403)
        sets += ["approved_by = ?", "approved_at = CURRENT_TIMESTAMP"]
        params.append(actor)
    if target == "published":
        sets += ["published_at = CURRENT_TIMESTAMP"]
        # Publishing a new version retires the previous one; journeys already running keep the version they started on.
        where = " AND ".join(f"{c} = ?" for c in group_cols)
        cur.execute(f"UPDATE {table} SET status = 'retired', retired_at = CURRENT_TIMESTAMP WHERE hospital_id = ? AND {where} AND status = 'published' AND id <> ?",
                    tuple([hospital_id] + [row[c] for c in group_cols] + [row_id]))
    if target == "retired":
        sets += ["retired_at = CURRENT_TIMESTAMP"]
    cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ? AND hospital_id = ?", tuple(params + [row_id, hospital_id]))
    return _one(cur, f"SELECT * FROM {table} WHERE id = ? AND hospital_id = ?", (row_id, hospital_id))  # what was actually saved


# ── Journey templates ───────────────────────────────────────────────────────────────────────────
def _template_out(row):
    if row:
        row["eligibility"] = _j(row.get("eligibility"), {})
        row["checkpoints"] = _j(row.get("checkpoints"), [])
    return row


def list_templates(hospital_id, status=""):
    with get_connection() as conn:
        rows = _all(conn.cursor(), "SELECT * FROM care_journey_templates WHERE hospital_id = ?" + (" AND status = ?" if status else "") + " ORDER BY template_key, version DESC",
                    (hospital_id, status) if status else (hospital_id,))
    return [_template_out(r) for r in rows]


def create_template(hospital_id, actor, data):
    key = rules.slug(data.get("template_key") or rules.slugify(data.get("name")), "template_key")
    name = rules.clean_text(data.get("name"), 120, "name", required=True)
    checkpoints = rules.validate_checkpoints(data.get("checkpoints"))
    eligibility = rules.validate_eligibility(data.get("eligibility"))
    pathway = rules.clean_text(data.get("pathway") or "general_discharge", 60, "pathway")
    with get_connection() as conn:
        cur = conn.cursor()
        latest = _one(cur, "SELECT COALESCE(MAX(version), 0) AS v FROM care_journey_templates WHERE hospital_id = ? AND template_key = ?", (hospital_id, key))["v"]
        row = _one(cur, "INSERT INTO care_journey_templates (hospital_id, template_key, version, name, pathway, eligibility, checkpoints, change_log, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                   (hospital_id, key, latest + 1, name, pathway, _dump(eligibility), _dump(checkpoints), rules.clean_text(data.get("change_log"), 300, "change_log") or None, actor))
        conn.commit()
    return _template_out(row)


def update_template(hospital_id, template_id, data):
    with get_connection() as conn:
        cur = conn.cursor()
        row = _one(cur, "SELECT * FROM care_journey_templates WHERE id = ? AND hospital_id = ?", (template_id, hospital_id))
        if not row:
            raise Patient360Error("Template not found", 404)
        if row["status"] != "draft":
            raise Patient360Error("Only a draft can be edited -- create a new version instead.", 409)
        name = rules.clean_text(data.get("name") or row["name"], 120, "name", required=True)
        checkpoints = rules.validate_checkpoints(data["checkpoints"]) if "checkpoints" in data else _j(row["checkpoints"], [])
        eligibility = rules.validate_eligibility(data["eligibility"]) if "eligibility" in data else _j(row["eligibility"], {})
        cur.execute("UPDATE care_journey_templates SET name = ?, pathway = ?, eligibility = ?, checkpoints = ?, change_log = ? WHERE id = ?",
                    (name, rules.clean_text(data.get("pathway") or row["pathway"], 60, "pathway"), _dump(eligibility), _dump(checkpoints),
                     rules.clean_text(data.get("change_log") or row.get("change_log"), 300, "change_log") or None, template_id))
        conn.commit()
        return _template_out(_one(cur, "SELECT * FROM care_journey_templates WHERE id = ?", (template_id,)))


def new_template_version(hospital_id, template_id, actor):
    with get_connection() as conn:
        cur = conn.cursor()
        src = _one(cur, "SELECT * FROM care_journey_templates WHERE id = ? AND hospital_id = ?", (template_id, hospital_id))
        if not src:
            raise Patient360Error("Template not found", 404)
        latest = _one(cur, "SELECT MAX(version) AS v FROM care_journey_templates WHERE hospital_id = ? AND template_key = ?", (hospital_id, src["template_key"]))["v"]
        row = _one(cur, "INSERT INTO care_journey_templates (hospital_id, template_key, version, name, pathway, eligibility, checkpoints, change_log, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                   (hospital_id, src["template_key"], latest + 1, src["name"], src["pathway"], src["eligibility"], src["checkpoints"], f"Copied from v{src['version']}", actor))
        conn.commit()
    return _template_out(row)


def template_transition(hospital_id, template_id, target, actor):
    with get_connection() as conn:
        cur = conn.cursor()
        row = _advance_approval(cur, "care_journey_templates", hospital_id, template_id, target, actor, ["template_key"])
        conn.commit()
    return row


# ── Enrollment & journeys ───────────────────────────────────────────────────────────────────────
def _published_templates(cur, hospital_id):
    rows = _all(cur, "SELECT * FROM care_journey_templates WHERE hospital_id = ? AND status = 'published' AND trigger_event = 'discharge.finalized' ORDER BY template_key", (hospital_id,))
    return [_template_out(r) for r in rows]


def _start_journey(cur, hospital_id, enrollment, template, actor):
    """Create the journey + its checkpoints for an enrollment. Idempotent: the enrollment can only ever have one."""
    existing = _one(cur, "SELECT * FROM care_journey_instances WHERE hospital_id = ? AND enrollment_id = ?", (hospital_id, enrollment["id"]))
    if existing:
        return existing, False
    others = _one(cur, "SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND patient_id = ? AND state IN ('active', 'paused')", (hospital_id, enrollment["patient_id"]))["n"]
    if others:
        # Two journeys would send overlapping check-ins to the same patient: hold for a human to decide.
        cur.execute("UPDATE patient360_enrollments SET status = 'pending_review', exclusion_reason = 'overlapping_active_journey', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (enrollment["id"],))
        return None, False
    journey = _one(cur, "INSERT INTO care_journey_instances (hospital_id, patient_id, enrollment_id, template_id, template_key, template_version, state, started_at, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, ?) RETURNING *",
                   (hospital_id, enrollment["patient_id"], enrollment["id"], template["id"], template["template_key"], template["version"], actor))
    anchor = rules.parse_dt(enrollment["discharged_at"])
    for cp in template["checkpoints"]:
        cur.execute("INSERT INTO care_checkpoints (hospital_id, journey_id, patient_id, checkpoint_key, label, action_type, spec, due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (journey_id, checkpoint_key) DO NOTHING",
                    (hospital_id, journey["id"], enrollment["patient_id"], cp["key"], cp["label"], cp["action_type"], _dump(cp), rules.compute_due_at(anchor, cp)))
    cur.execute("UPDATE patient360_enrollments SET status = 'enrolled', journey_id = ?, exclusion_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (journey["id"], enrollment["id"]))
    return journey, True


def enroll_from_discharge(hospital_id, bed_id=None, admission_id=None, actor=None, source_module="beds"):
    """Called after a discharge is finalized. Never raises into the discharge itself (the caller wraps it) and is
    idempotent on the discharge event key, so a retried or duplicated event can never create a second enrollment."""
    with get_connection() as conn:
        cur = conn.cursor()
        ward = None
        patient_id = None
        released_at = None
        if bed_id is not None:
            alloc = _one(cur, "SELECT patient_id, admission_id, ward, released_at FROM bed_allocations WHERE hospital_id = ? AND bed_id = ? AND status = 'released' "
                              "ORDER BY released_at DESC NULLS LAST, id DESC LIMIT 1", (hospital_id, bed_id))
            if not alloc:
                return {"status": "ignored", "reason": "no_released_allocation"}
            patient_id, admission_id, ward, released_at = alloc["patient_id"], alloc["admission_id"], alloc["ward"], alloc["released_at"]
        if admission_id is None:
            raise Patient360Error("admission_id or bed_id is required")
        adm = _one(cur, "SELECT id, patient_id, admission_date, discharge_date FROM admissions WHERE id = ? AND hospital_id = ?", (admission_id, hospital_id))
        if not adm or not adm.get("discharge_date"):
            return {"status": "ignored", "reason": "discharge_not_finalized"}
        patient_id = patient_id or adm["patient_id"]
        if not _one(cur, f"SELECT 1 AS ok FROM patients p WHERE p.hospital_id = ? AND p.patient_id = ? AND {CANONICAL}", (hospital_id, patient_id)):
            return {"status": "ignored", "reason": "patient_not_canonical"}
        discharged_at = rules.parse_dt(released_at) or rules.parse_dt(adm["discharge_date"])
        admitted_at = rules.parse_dt(adm["admission_date"])
        stay_days = round((discharged_at - admitted_at).total_seconds() / 86400, 2) if admitted_at and discharged_at else None
        event_key = f"admission:{adm['id']}:discharge"
        created = _one(cur, "INSERT INTO patient360_enrollments (hospital_id, patient_id, admission_id, discharge_event_key, source_module, ward, discharged_at, stay_days, created_by) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (hospital_id, discharge_event_key) DO NOTHING RETURNING *",
                       (hospital_id, patient_id, adm["id"], event_key, source_module, ward, discharged_at, stay_days, actor))
        if not created:
            conn.commit()
            return {"status": "duplicate", "enrollment": _one(cur, "SELECT * FROM patient360_enrollments WHERE hospital_id = ? AND discharge_event_key = ?", (hospital_id, event_key))}
        journey = None
        for template in _published_templates(cur, hospital_id):
            ok, _why = rules.template_matches(template["eligibility"], ward, stay_days)
            if ok:
                journey, _ = _start_journey(cur, hospital_id, created, template, actor or "system")
                break
        conn.commit()
        return {"status": "enrolled" if journey else "eligible", "enrollment": _one(cur, "SELECT * FROM patient360_enrollments WHERE id = ?", (created["id"],)), "journey": journey}


def list_enrollments(hospital_id, status="", limit=50, offset=0):
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    with get_connection() as conn:
        cur = conn.cursor()
        where, params = ["e.hospital_id = ?"], [hospital_id]
        if status:
            where.append("e.status = ?")
            params.append(status)
        clause = " AND ".join(where)
        total = _one(cur, f"SELECT COUNT(*) AS n FROM patient360_enrollments e WHERE {clause}", tuple(params))["n"]
        rows = _all(cur, f"SELECT e.*, p.name, p.last_name, p.phone FROM patient360_enrollments e LEFT JOIN patients p ON p.hospital_id = e.hospital_id AND p.patient_id = e.patient_id "
                         f"WHERE {clause} ORDER BY e.discharged_at DESC LIMIT ? OFFSET ?", tuple(params + [limit, offset]))
    return {"enrollments": rows, "total": total}


def start_journey_for_enrollment(hospital_id, enrollment_id, template_id, actor):
    with get_connection() as conn:
        cur = conn.cursor()
        enrollment = _one(cur, "SELECT * FROM patient360_enrollments WHERE id = ? AND hospital_id = ?", (enrollment_id, hospital_id))
        template = _template_out(_one(cur, "SELECT * FROM care_journey_templates WHERE id = ? AND hospital_id = ?", (template_id, hospital_id)))
        if not enrollment or not template:
            raise Patient360Error("Enrollment or template not found", 404)
        if template["status"] != "published":
            raise Patient360Error("Only a published (clinically approved) template can start a journey.", 409)
        if enrollment["status"] in ("cancelled", "completed", "excluded"):
            raise Patient360Error(f"This enrollment is {enrollment['status']}.", 409)
        existing = _one(cur, "SELECT id FROM care_journey_instances WHERE hospital_id = ? AND enrollment_id = ?", (hospital_id, enrollment_id))
        if existing:
            raise Patient360Error("This enrollment already has a journey.", 409)
        # A person deciding overrides the overlap hold: take the held enrollment out of review first.
        cur.execute("UPDATE patient360_enrollments SET status = 'eligible', exclusion_reason = NULL WHERE id = ?", (enrollment_id,))
        others = _one(cur, "SELECT id FROM care_journey_instances WHERE hospital_id = ? AND patient_id = ? AND state IN ('active', 'paused') LIMIT 1", (hospital_id, enrollment["patient_id"]))
        if others:
            raise Patient360Error("The patient already has a running journey -- close or cancel it first.", 409)
        journey, _ = _start_journey(cur, hospital_id, enrollment, template, actor)
        conn.commit()
    return journey


def list_journeys(hospital_id, state="", patient_id="", limit=50, offset=0):
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    where, params = ["j.hospital_id = ?"], [hospital_id]
    if state:
        where.append("j.state = ?")
        params.append(state)
    if patient_id:
        where.append("j.patient_id = ?")
        params.append(patient_id)
    clause = " AND ".join(where)
    with get_connection() as conn:
        cur = conn.cursor()
        total = _one(cur, f"SELECT COUNT(*) AS n FROM care_journey_instances j WHERE {clause}", tuple(params))["n"]
        rows = _all(cur, f"""
            SELECT j.*, p.name, p.last_name,
                   (SELECT COUNT(*) FROM care_checkpoints c WHERE c.journey_id = j.id) AS checkpoint_total,
                   (SELECT COUNT(*) FROM care_checkpoints c WHERE c.journey_id = j.id AND c.status IN ('completed', 'skipped', 'cancelled')) AS checkpoint_done,
                   (SELECT MIN(c.due_at) FROM care_checkpoints c WHERE c.journey_id = j.id AND c.status IN ('scheduled', 'queued', 'overdue')) AS next_due_at
            FROM care_journey_instances j LEFT JOIN patients p ON p.hospital_id = j.hospital_id AND p.patient_id = j.patient_id
            WHERE {clause} ORDER BY j.id DESC LIMIT ? OFFSET ?""", tuple(params + [limit, offset]))
    return {"journeys": rows, "total": total}


def get_journey(hospital_id, journey_id):
    with get_connection() as conn:
        cur = conn.cursor()
        journey = _one(cur, "SELECT * FROM care_journey_instances WHERE id = ? AND hospital_id = ?", (journey_id, hospital_id))
        if not journey:
            return None
        cps = _all(cur, "SELECT * FROM care_checkpoints WHERE journey_id = ? AND hospital_id = ? ORDER BY due_at, id", (journey_id, hospital_id))
    for c in cps:
        c["spec"] = _j(c.get("spec"), {})
        c["eligibility_at_send"] = _j(c.get("eligibility_at_send"), None)
    journey["checkpoints"] = cps
    return journey


def transition_journey(hospital_id, journey_id, target, reason, actor):
    reason = rules.clean_text(reason, 300, "reason")
    if target in rules.JOURNEY_REASON_REQUIRED and not reason:
        raise Patient360Error(f"A reason is required to move a journey to '{target}'.")
    with get_connection() as conn:
        cur = conn.cursor()
        j = _one(cur, "SELECT * FROM care_journey_instances WHERE id = ? AND hospital_id = ?", (journey_id, hospital_id))
        if not j:
            raise Patient360Error("Journey not found", 404)
        if not rules.can_transition(rules.JOURNEY_TRANSITIONS, j["state"], target):
            raise Patient360Error(f"A {j['state']} journey cannot become {target}.", 409)
        sets = ["state = ?", "state_reason = ?"]
        params = [target, reason or None]
        if target == "paused":
            sets.append("paused_at = CURRENT_TIMESTAMP")
        if target in ("closed", "cancelled", "completed"):
            sets.append("closed_at = CURRENT_TIMESTAMP")
        cur.execute(f"UPDATE care_journey_instances SET {', '.join(sets)} WHERE id = ?", tuple(params + [journey_id]))
        if target in ("closed", "cancelled"):
            cur.execute("UPDATE care_checkpoints SET status = 'cancelled' WHERE journey_id = ? AND status IN ('scheduled', 'queued', 'overdue')", (journey_id,))
        enrollment_status = {"cancelled": "cancelled", "closed": "completed", "completed": "completed"}.get(target)
        if enrollment_status:
            cur.execute("UPDATE patient360_enrollments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (enrollment_status, j["enrollment_id"]))
        conn.commit()
    return get_journey(hospital_id, journey_id)


def _maybe_complete_journey(cur, hospital_id, journey_id):
    open_n = _one(cur, "SELECT COUNT(*) AS n FROM care_checkpoints WHERE journey_id = ? AND status NOT IN ('completed', 'skipped', 'cancelled')", (journey_id,))["n"]
    if open_n == 0:
        cur.execute("UPDATE care_journey_instances SET state = 'completed', closed_at = CURRENT_TIMESTAMP, state_reason = 'All checkpoints completed' "
                    "WHERE id = ? AND hospital_id = ? AND state = 'active'", (journey_id, hospital_id))
        cur.execute("UPDATE patient360_enrollments SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE journey_id = ? AND hospital_id = ?", (journey_id, hospital_id))


def resolve_checkpoint(hospital_id, checkpoint_id, outcome, note, actor):
    if outcome not in ("completed", "skipped"):
        raise Patient360Error("outcome must be completed or skipped")
    note = rules.clean_text(note, 300, "note", required=(outcome == "skipped"))
    with get_connection() as conn:
        cur = conn.cursor()
        cp = _one(cur, "SELECT * FROM care_checkpoints WHERE id = ? AND hospital_id = ?", (checkpoint_id, hospital_id))
        if not cp:
            raise Patient360Error("Checkpoint not found", 404)
        if cp["status"] in ("completed", "skipped", "cancelled"):
            raise Patient360Error(f"This checkpoint is already {cp['status']}.", 409)
        cur.execute("UPDATE care_checkpoints SET status = ?, completed_at = CURRENT_TIMESTAMP, completion_note = ? WHERE id = ?",
                    (outcome, f"{note} (by {actor})" if note else f"by {actor}", checkpoint_id))
        _maybe_complete_journey(cur, hospital_id, cp["journey_id"])
        conn.commit()
        return _one(cur, "SELECT * FROM care_checkpoints WHERE id = ?", (checkpoint_id,))


def process_due_checkpoints(hospital_id, actor="system", now=None, limit=200):
    """Turn due checkpoints into accountable work. Nothing is sent automatically: for a message or survey it
    records the send-time eligibility decision and queues a task for the coordinator, who sends it through the
    existing Patient Messages tool -- so an approved wording and a human always sit between a journey and a patient.
    Safe to run repeatedly (tasks are keyed on the checkpoint)."""
    now = now or current_utc_naive()
    summary = {"examined": 0, "queued": 0, "skipped": 0, "deferred": 0, "marked_overdue": 0}
    with get_connection() as conn:
        cur = conn.cursor()
        due = _all(cur, "SELECT c.* FROM care_checkpoints c JOIN care_journey_instances j ON j.id = c.journey_id AND j.hospital_id = c.hospital_id "
                        "WHERE c.hospital_id = ? AND j.state = 'active' AND c.status = 'scheduled' AND c.due_at <= ? ORDER BY c.due_at LIMIT ?", (hospital_id, now, limit))
        for cp in due:
            summary["examined"] += 1
            spec = _j(cp["spec"], {})
            action = cp["action_type"]
            if action in ("call_task", "staff_review"):
                task_id = _ensure_task(cur, hospital_id, cp["patient_id"], actor, title=f"{cp['label']}", category="journey_checkpoint",
                                       priority="normal", description=spec.get("instructions") or "", owner_team=spec.get("owner_role"),
                                       source_type="checkpoint", source_ref=str(cp["id"]), source_key=f"cp:{cp['id']}", journey_id=cp["journey_id"], checkpoint_id=cp["id"])
                cur.execute("UPDATE care_checkpoints SET status = 'queued', task_id = ? WHERE id = ?", (task_id, cp["id"]))
                summary["queued"] += 1
                continue
            channel = "whatsapp"
            prefs = _one(cur, "SELECT preferred_channel FROM patient360_preferences WHERE hospital_id = ? AND patient_id = ?", (hospital_id, cp["patient_id"]))
            if prefs and prefs.get("preferred_channel") in rules.CHANNELS:
                channel = prefs["preferred_channel"]
            # Everything a post-discharge journey sends (check-ins, surveys, reminders) is clinical follow-up; it needs the
            # patient's clinical-follow-up authorization, never the marketing one.
            purpose = "clinical_followup"
            conn.commit()  # check_eligibility opens its own connection
            decision = check_eligibility(hospital_id, cp["patient_id"], channel, purpose, require_active=True, now=now)
            decision["channel"], decision["purpose"], decision["checked_at"] = channel, purpose, now
            if decision["eligible"]:
                body = spec.get("message_text") or (f"Send survey '{spec.get('survey_key')}'" if action == "survey" else cp["label"])
                task_id = _ensure_task(cur, hospital_id, cp["patient_id"], actor, title=f"Send: {cp['label']}", category="outreach_due",
                                       priority="normal", description=f"{channel.upper()} / {purpose}. {body}", owner_team=spec.get("owner_role"),
                                       source_type="checkpoint", source_ref=str(cp["id"]), source_key=f"cp:{cp['id']}", journey_id=cp["journey_id"], checkpoint_id=cp["id"])
                cur.execute("UPDATE care_checkpoints SET status = 'queued', task_id = ?, eligibility_at_send = ? WHERE id = ?", (task_id, _dump(decision), cp["id"]))
                summary["queued"] += 1
            elif decision["deferrable"]:
                cur.execute("UPDATE care_checkpoints SET eligibility_at_send = ? WHERE id = ?", (_dump(decision), cp["id"]))
                summary["deferred"] += 1
            else:
                task_id = _ensure_task(cur, hospital_id, cp["patient_id"], actor, title=f"Cannot contact for: {cp['label']}", category="contact_verification",
                                       priority="normal", description=f"Automated outreach blocked ({decision['reason']}). Verify the contact and authorization, or call the patient.",
                                       owner_team="care_coordinator", source_type="checkpoint", source_ref=str(cp["id"]), source_key=f"cp:{cp['id']}",
                                       journey_id=cp["journey_id"], checkpoint_id=cp["id"])
                cur.execute("UPDATE care_checkpoints SET status = 'skipped', task_id = ?, eligibility_at_send = ?, completion_note = ? WHERE id = ?",
                            (task_id, _dump(decision), f"Blocked: {decision['reason']}", cp["id"]))
                summary["skipped"] += 1
        cur.execute("UPDATE care_checkpoints SET status = 'overdue' WHERE hospital_id = ? AND status = 'queued' AND due_at < ?", (hospital_id, now - timedelta(hours=48)))
        summary["marked_overdue"] = cur.rowcount or 0
        conn.commit()
    return summary


# ── Tasks ───────────────────────────────────────────────────────────────────────────────────────
def _task_event(cur, hospital_id, task_id, event_type, actor, from_status=None, to_status=None, note=None):
    cur.execute("INSERT INTO care_task_events (hospital_id, task_id, event_type, from_status, to_status, actor, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hospital_id, task_id, event_type, from_status, to_status, actor, note))


def _ensure_task(cur, hospital_id, patient_id, actor, *, title, category="follow_up", priority="normal", description="", owner_team=None,
                 assigned_to=None, due_at=None, source_type="manual", source_ref=None, source_key=None, journey_id=None, checkpoint_id=None):
    """Create a task, or return the existing one for the same source_key (idempotent: retries never duplicate work)."""
    if source_key:
        found = _one(cur, "SELECT id FROM care_tasks WHERE hospital_id = ? AND source_key = ?", (hospital_id, source_key))
        if found:
            return found["id"]
    if priority not in rules.TASK_PRIORITIES:
        raise Patient360Error(f"priority must be one of {', '.join(rules.TASK_PRIORITIES)}")
    due_at = rules.parse_dt(due_at) or (current_utc_naive() + timedelta(hours=PRIORITY_DUE_HOURS[priority]))
    status = "assigned" if assigned_to else "open"
    row = _one(cur, "INSERT INTO care_tasks (hospital_id, patient_id, journey_id, checkpoint_id, source_type, source_ref, source_key, category, priority, title, description, "
                    "owner_team, assigned_to, status, due_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
               (hospital_id, patient_id, journey_id, checkpoint_id, source_type, source_ref, source_key, category, priority,
                rules.clean_text(title, 160, "title", required=True), rules.clean_text(description, 1000, "description") or None,
                owner_team, assigned_to, status, due_at, actor))
    _task_event(cur, hospital_id, row["id"], "created", actor, None, status, source_type)
    return row["id"]


def create_task(hospital_id, actor, data):
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, data.get("patient_id"))
        task_id = _ensure_task(cur, hospital_id, data["patient_id"], actor, title=data.get("title"), category=rules.clean_text(data.get("category") or "follow_up", 60, "category"),
                               priority=data.get("priority") or "normal", description=data.get("description"), owner_team=rules.clean_text(data.get("owner_team"), 60, "owner_team") or None,
                               assigned_to=rules.clean_text(data.get("assigned_to"), 80, "assigned_to") or None, due_at=data.get("due_at"),
                               source_type="manual", source_ref=data.get("source_ref"))
        conn.commit()
    return get_task(hospital_id, task_id)


def list_tasks(hospital_id, status="open", patient_id="", assigned_to="", priority="", overdue=False, limit=50, offset=0):
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    where, params = ["t.hospital_id = ?"], [hospital_id]
    if status == "open":
        where.append("t.status NOT IN ('resolved', 'cancelled')")
    elif status and status != "all":
        if status not in rules.TASK_STATUSES:
            raise Patient360Error("unknown status")
        where.append("t.status = ?")
        params.append(status)
    for col, val in (("patient_id", patient_id), ("assigned_to", assigned_to), ("priority", priority)):
        if val:
            where.append(f"t.{col} = ?")
            params.append(val)
    if overdue:
        where.append("t.due_at < CURRENT_TIMESTAMP AND t.status NOT IN ('resolved', 'cancelled')")
    clause = " AND ".join(where)
    with get_connection() as conn:
        cur = conn.cursor()
        total = _one(cur, f"SELECT COUNT(*) AS n FROM care_tasks t WHERE {clause}", tuple(params))["n"]
        rows = _all(cur, f"SELECT t.*, p.name AS patient_name, p.last_name AS patient_last_name FROM care_tasks t "
                         f"LEFT JOIN patients p ON p.hospital_id = t.hospital_id AND p.patient_id = t.patient_id WHERE {clause} "
                         f"ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.due_at ASC NULLS LAST, t.id DESC LIMIT ? OFFSET ?",
                    tuple(params + [limit, offset]))
    return {"tasks": rows, "total": total}


def get_task(hospital_id, task_id):
    with get_connection() as conn:
        cur = conn.cursor()
        task = _one(cur, "SELECT * FROM care_tasks WHERE id = ? AND hospital_id = ?", (task_id, hospital_id))
        if not task:
            return None
        task["history"] = _all(cur, "SELECT event_type, from_status, to_status, actor, note, created_at FROM care_task_events WHERE task_id = ? AND hospital_id = ? ORDER BY id", (task_id, hospital_id))
    return task


_TASK_ACTIONS = {"acknowledge": "acknowledged", "start": "in_progress", "escalate": "escalated", "snooze": "snoozed", "reopen": "open", "resolve": "resolved", "cancel": "cancelled"}


def task_action(hospital_id, task_id, action, actor, data):
    data = data or {}
    note = rules.clean_text(data.get("note"), 500, "note")
    with get_connection() as conn:
        cur = conn.cursor()
        task = _one(cur, "SELECT * FROM care_tasks WHERE id = ? AND hospital_id = ?", (task_id, hospital_id))
        if not task:
            raise Patient360Error("Task not found", 404)
        if action == "assign":
            if task["status"] in ("resolved", "cancelled"):
                raise Patient360Error("A closed task cannot be reassigned.", 409)
            assignee = rules.clean_text(data.get("assigned_to"), 80, "assigned_to", required=True)
            team = rules.clean_text(data.get("owner_team") or task["owner_team"], 60, "owner_team") or None
            new_status = "assigned" if task["status"] == "open" else task["status"]
            cur.execute("UPDATE care_tasks SET assigned_to = ?, owner_team = ?, status = ? WHERE id = ?", (assignee, team, new_status, task_id))
            _task_event(cur, hospital_id, task_id, "reassigned" if task["assigned_to"] else "assigned", actor, task["status"], new_status, f"to {assignee}" + (f": {note}" if note else ""))
        elif action in _TASK_ACTIONS:
            target = _TASK_ACTIONS[action]
            if not rules.can_transition(rules.TASK_TRANSITIONS, task["status"], target):
                raise Patient360Error(f"A {task['status']} task cannot be {target}.", 409)
            if target in ("resolved", "cancelled", "escalated") and not note:
                raise Patient360Error("A note explaining the action taken is required.")
            sets, params = ["status = ?"], [target]
            if target == "acknowledged" or (target in ("in_progress", "resolved") and not task["acknowledged_at"]):
                sets.append("acknowledged_at = COALESCE(acknowledged_at, CURRENT_TIMESTAMP)")
            if target in ("resolved", "cancelled"):
                sets += ["resolved_at = CURRENT_TIMESTAMP", "resolution_action = ?", "resolution_note = ?"]
                params += [rules.clean_text(data.get("resolution_action") or action, 80, "resolution_action"), note]
            if target == "snoozed":
                sets.append("due_at = ?")
                params.append(rules.parse_dt(data.get("until")) or (current_utc_naive() + timedelta(hours=24)))
            if target == "acknowledged" and not task["assigned_to"]:
                sets.append("assigned_to = ?")
                params.append(actor)
            cur.execute(f"UPDATE care_tasks SET {', '.join(sets)} WHERE id = ?", tuple(params + [task_id]))
            _task_event(cur, hospital_id, task_id, action, actor, task["status"], target, note)
            if target in ("resolved", "cancelled") and task.get("checkpoint_id"):
                cp_status = "completed" if target == "resolved" else "skipped"
                cur.execute("UPDATE care_checkpoints SET status = ?, completed_at = CURRENT_TIMESTAMP, completion_note = ? WHERE id = ? AND status IN ('queued', 'overdue', 'sent', 'delivered')",
                            (cp_status, f"Task #{task_id} {target} by {actor}", task["checkpoint_id"]))
                cp = _one(cur, "SELECT journey_id FROM care_checkpoints WHERE id = ?", (task["checkpoint_id"],))
                if cp:
                    _maybe_complete_journey(cur, hospital_id, cp["journey_id"])
        else:
            raise Patient360Error("unknown action")
        conn.commit()
    return get_task(hospital_id, task_id)


# ── Surveys ─────────────────────────────────────────────────────────────────────────────────────
def _survey_out(row):
    if row:
        row["questions"] = _j(row.get("questions"), [])
        row["escalation_rules"] = _j(row.get("escalation_rules"), [])
    return row


def list_surveys(hospital_id, status=""):
    with get_connection() as conn:
        rows = _all(conn.cursor(), "SELECT * FROM survey_templates WHERE hospital_id = ?" + (" AND status = ?" if status else "") + " ORDER BY survey_key, version DESC, language",
                    (hospital_id, status) if status else (hospital_id,))
    return [_survey_out(r) for r in rows]


def create_survey(hospital_id, actor, data):
    key = rules.slug(data.get("survey_key") or rules.slugify(data.get("title")), "survey_key")
    questions = rules.validate_questions(data.get("questions"))
    escalations = rules.validate_escalation_rules(data.get("escalation_rules"), questions)
    language = rules.clean_text(data.get("language") or "en", 10, "language")
    with get_connection() as conn:
        cur = conn.cursor()
        latest = _one(cur, "SELECT COALESCE(MAX(version), 0) AS v FROM survey_templates WHERE hospital_id = ? AND survey_key = ? AND language = ?", (hospital_id, key, language))["v"]
        row = _one(cur, "INSERT INTO survey_templates (hospital_id, survey_key, version, language, title, purpose, pathway, questions, escalation_rules, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                   (hospital_id, key, latest + 1, language, rules.clean_text(data.get("title"), 120, "title", required=True), rules.clean_text(data.get("purpose"), 300, "purpose") or None,
                    rules.clean_text(data.get("pathway"), 60, "pathway") or None, _dump(questions), _dump(escalations), actor))
        conn.commit()
    return _survey_out(row)


def update_survey(hospital_id, survey_id, data):
    with get_connection() as conn:
        cur = conn.cursor()
        row = _one(cur, "SELECT * FROM survey_templates WHERE id = ? AND hospital_id = ?", (survey_id, hospital_id))
        if not row:
            raise Patient360Error("Survey not found", 404)
        if row["status"] != "draft":
            raise Patient360Error("A published or approved survey is immutable -- create a new version.", 409)
        questions = rules.validate_questions(data["questions"]) if "questions" in data else _j(row["questions"], [])
        escalations = rules.validate_escalation_rules(data["escalation_rules"], questions) if "escalation_rules" in data else rules.validate_escalation_rules(_j(row["escalation_rules"], []), questions)
        cur.execute("UPDATE survey_templates SET title = ?, purpose = ?, pathway = ?, questions = ?, escalation_rules = ? WHERE id = ?",
                    (rules.clean_text(data.get("title") or row["title"], 120, "title", required=True), rules.clean_text(data.get("purpose", row.get("purpose")), 300, "purpose") or None,
                     rules.clean_text(data.get("pathway", row.get("pathway")), 60, "pathway") or None, _dump(questions), _dump(escalations), survey_id))
        conn.commit()
        return _survey_out(_one(cur, "SELECT * FROM survey_templates WHERE id = ?", (survey_id,)))


def new_survey_version(hospital_id, survey_id, actor):
    with get_connection() as conn:
        cur = conn.cursor()
        src = _one(cur, "SELECT * FROM survey_templates WHERE id = ? AND hospital_id = ?", (survey_id, hospital_id))
        if not src:
            raise Patient360Error("Survey not found", 404)
        latest = _one(cur, "SELECT MAX(version) AS v FROM survey_templates WHERE hospital_id = ? AND survey_key = ? AND language = ?", (hospital_id, src["survey_key"], src["language"]))["v"]
        row = _one(cur, "INSERT INTO survey_templates (hospital_id, survey_key, version, language, title, purpose, pathway, questions, escalation_rules, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                   (hospital_id, src["survey_key"], latest + 1, src["language"], src["title"], src["purpose"], src["pathway"], src["questions"], src["escalation_rules"], actor))
        conn.commit()
    return _survey_out(row)


def survey_transition(hospital_id, survey_id, target, actor):
    with get_connection() as conn:
        cur = conn.cursor()
        row = _advance_approval(cur, "survey_templates", hospital_id, survey_id, target, actor, ["survey_key", "language"])
        conn.commit()
    return row


def submit_response(hospital_id, actor, data):
    """Persist a patient's answers against the exact published survey version, then apply the survey's
    clinician-approved escalation rules. The original answers are never edited afterwards."""
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, data.get("patient_id"))
        survey = _survey_out(_one(cur, "SELECT * FROM survey_templates WHERE id = ? AND hospital_id = ?", (data.get("survey_id"), hospital_id)))
        if not survey:
            raise Patient360Error("Survey not found", 404)
        if survey["status"] != "published":
            raise Patient360Error("Responses can only be recorded against a published survey version.", 409)
        try:
            answers = rules.validate_answers(survey["questions"], data.get("answers"))
        except ValueError as exc:
            raise Patient360Error(str(exc)) from None
        checkpoint = journey_id = None
        if data.get("checkpoint_id"):
            checkpoint = _one(cur, "SELECT * FROM care_checkpoints WHERE id = ? AND hospital_id = ? AND patient_id = ?", (data["checkpoint_id"], hospital_id, data["patient_id"]))
            if not checkpoint:
                raise Patient360Error("Checkpoint not found for this patient", 404)
            journey_id = checkpoint["journey_id"]
        channel = rules.clean_text(data.get("channel") or "staff_entry", 30, "channel")
        resp = _one(cur, "INSERT INTO survey_responses (hospital_id, patient_id, journey_id, checkpoint_id, survey_id, survey_key, survey_version, answers, channel, provider_message_id, captured_by) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                    (hospital_id, data["patient_id"], journey_id, checkpoint["id"] if checkpoint else None, survey["id"], survey["survey_key"], survey["version"],
                     _dump(answers), channel, data.get("provider_message_id"), actor))
        task_ids = []
        for i, rule in enumerate(rules.evaluate_escalations(survey["escalation_rules"], answers)):
            task_ids.append(_ensure_task(
                cur, hospital_id, data["patient_id"], actor, title=rule["title"], category=rule["category"], priority=rule["priority"],
                description=f"Patient-reported (not a verified finding). Survey '{survey['survey_key']}' v{survey['version']}, question '{rule['question_id']}': "
                            f"{json.dumps(answers.get(rule['question_id']), default=str)}",
                owner_team=rule["team"], due_at=current_utc_naive() + timedelta(hours=rule["due_hours"]), source_type="survey_response",
                source_ref=str(resp["id"]), source_key=f"resp:{resp['id']}:{i}", journey_id=journey_id, checkpoint_id=checkpoint["id"] if checkpoint else None))
        if task_ids:
            cur.execute("UPDATE survey_responses SET triggered_task_ids = ? WHERE id = ?", (_dump(task_ids), resp["id"]))
        if checkpoint and checkpoint["status"] not in ("completed", "skipped", "cancelled"):
            cur.execute("UPDATE care_checkpoints SET status = 'completed', completed_at = CURRENT_TIMESTAMP, completion_note = ? WHERE id = ?",
                        (f"Survey response #{resp['id']} received", checkpoint["id"]))
            _maybe_complete_journey(cur, hospital_id, checkpoint["journey_id"])
        conn.commit()
    resp["answers"] = answers
    resp["triggered_task_ids"] = task_ids
    return resp


def list_responses(hospital_id, patient_id="", review_status="", limit=50, offset=0):
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    where, params = ["r.hospital_id = ?"], [hospital_id]
    if patient_id:
        where.append("r.patient_id = ?")
        params.append(patient_id)
    if review_status:
        where.append("r.review_status = ?")
        params.append(review_status)
    clause = " AND ".join(where)
    with get_connection() as conn:
        cur = conn.cursor()
        total = _one(cur, f"SELECT COUNT(*) AS n FROM survey_responses r WHERE {clause}", tuple(params))["n"]
        rows = _all(cur, f"SELECT r.*, p.name AS patient_name, p.last_name AS patient_last_name FROM survey_responses r "
                         f"LEFT JOIN patients p ON p.hospital_id = r.hospital_id AND p.patient_id = r.patient_id WHERE {clause} ORDER BY r.submitted_at DESC LIMIT ? OFFSET ?",
                    tuple(params + [limit, offset]))
    for r in rows:
        r["answers"] = _j(r.get("answers"), {})
        r["triggered_task_ids"] = _j(r.get("triggered_task_ids"), [])
    return {"responses": rows, "total": total}


def review_response(hospital_id, response_id, actor, status, note):
    if status not in ("under_review", "reviewed"):
        raise Patient360Error("status must be under_review or reviewed")
    with get_connection() as conn:
        cur = conn.cursor()
        if not _one(cur, "SELECT id FROM survey_responses WHERE id = ? AND hospital_id = ?", (response_id, hospital_id)):
            raise Patient360Error("Response not found", 404)
        # Only the review columns change; the patient's answers are immutable.
        cur.execute("UPDATE survey_responses SET review_status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP, review_note = ? WHERE id = ?",
                    (status, actor, rules.clean_text(note, 500, "note") or None, response_id))
        conn.commit()


# ── Segments ────────────────────────────────────────────────────────────────────────────────────
def _segment_sql(criteria, extra_fields=()):
    where, params, used = rules.compile_criteria(criteria)
    return where, params, used


def evaluate_segment(hospital_id, criteria, limit=25, offset=0):
    """Matched patients with *why* each matched. Executes only the compiled, parameterised allow-listed clause."""
    where, params, used = _segment_sql(criteria)
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    fields = list(dict.fromkeys(used + ["has_phone"]))
    cols = ", ".join(f"({rules.SEGMENT_FIELDS[f]['sql']}) AS f_{f}" for f in fields)
    base = f"FROM patients p WHERE p.hospital_id = ? AND {CANONICAL} AND {where}"
    with get_connection() as conn:
        cur = conn.cursor()
        scope = _one(cur, f"SELECT COUNT(*) AS n FROM patients p WHERE p.hospital_id = ? AND {CANONICAL}", (hospital_id,))["n"]
        matched = _one(cur, f"SELECT COUNT(*) AS n {base}", tuple([hospital_id] + params))["n"]
        rows = _all(cur, f"SELECT p.patient_id, p.name, p.last_name, p.age, p.gender, p.phone, {cols} {base} ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?",
                    tuple([hospital_id] + params + [limit, offset]))
    conditions = rules.flatten_conditions(criteria)
    for r in rows:
        reasons = []
        for c in conditions:
            actual = r.get(f"f_{c['field']}")
            try:
                if rules.condition_holds(c["field"], c["op"], actual, c.get("value")):
                    reasons.append(rules.describe_condition(c, actual))
            except (ValueError, TypeError):
                continue
        r["match_reasons"] = reasons
        for f in fields:
            r.pop(f"f_{f}", None)
    return {"patients": rows, "matched": matched, "total_in_scope": scope, "not_matched": scope - matched, "limit": limit, "offset": offset,
            "interpreted": [{"field": c["field"], "label": rules.SEGMENT_FIELDS[c["field"]]["label"], "op": c["op"], "value": c.get("value")} for c in conditions],
            "logic": (criteria.get("logic") or "and").lower() if "conditions" in criteria else "and"}


def segment_recipients(hospital_id, criteria, channel, purpose, cap=2000):
    """Who an outreach could actually go to, and exactly why everyone else is excluded -- recomputed each time,
    never trusted from an earlier preview (spec 8.3 steps 4-5). Sending itself stays in the existing messaging tools."""
    where, params, _ = _segment_sql(criteria)
    with get_connection() as conn:
        cur = conn.cursor()
        rows = _all(cur, f"SELECT p.patient_id, p.name, p.last_name, p.phone FROM patients p WHERE p.hospital_id = ? AND {CANONICAL} AND {where} "
                         f"ORDER BY p.id LIMIT ?", tuple([hospital_id] + params + [cap + 1]))
        truncated = len(rows) > cap
        rows = rows[:cap]
        auth = _auth_rows(cur, hospital_id, [r["patient_id"] for r in rows]) if rows else {}
        prefs = {r["patient_id"]: r for r in _all(cur, "SELECT patient_id, contact_from_hour, contact_to_hour FROM patient360_preferences WHERE hospital_id = ?", (hospital_id,))}
    eligible, excluded, seen_phones = [], [], set()
    for r in rows:
        pref = prefs.get(r["patient_id"]) or {}
        window = (pref["contact_from_hour"], pref["contact_to_hour"]) if pref.get("contact_from_hour") is not None and pref.get("contact_to_hour") is not None else None
        decision = rules.check_send_eligibility(phone=r["phone"], channel=channel, purpose=purpose, auth_rows=auth.get(r["patient_id"], []), contact_window=window)
        phone = rules.normalize_phone(r["phone"])
        if decision["eligible"] and phone in seen_phones:
            decision = {"eligible": False, "reason": "duplicate_phone_number", "deferrable": False}
        if decision["eligible"]:
            seen_phones.add(phone)
            eligible.append({"patient_id": r["patient_id"], "name": " ".join(filter(None, [r["name"], r["last_name"]]))})
        else:
            excluded.append({"patient_id": r["patient_id"], "name": " ".join(filter(None, [r["name"], r["last_name"]])), "reason": decision["reason"]})
    reasons = {}
    for e in excluded:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    return {"eligible": eligible, "excluded": excluded, "eligible_count": len(eligible), "excluded_count": len(excluded), "excluded_by_reason": reasons,
            "truncated": truncated, "channel": channel, "purpose": purpose, "evaluated_at": current_utc_naive()}


def _segment_out(row):
    if row:
        row["criteria"] = _j(row.get("criteria"), {})
    return row


def _write_snapshot(cur, hospital_id, segment_id, criteria):
    where, params, _ = _segment_sql(criteria)
    cur.execute("DELETE FROM segment_memberships WHERE segment_id = ? AND hospital_id = ?", (segment_id, hospital_id))
    cur.execute(f"INSERT INTO segment_memberships (hospital_id, segment_id, patient_id) SELECT p.hospital_id, ?, p.patient_id FROM patients p "
                f"WHERE p.hospital_id = ? AND {CANONICAL} AND {where}", tuple([segment_id, hospital_id] + params))
    return cur.rowcount or 0


def save_segment(hospital_id, actor, data, parent_id=None):
    criteria = data.get("criteria")
    rules.compile_criteria(criteria)  # rejects anything outside the allow-list before it is stored
    mode = data.get("mode") or "dynamic"
    if mode not in ("dynamic", "snapshot"):
        raise Patient360Error("mode must be dynamic or snapshot")
    name = rules.clean_text(data.get("name"), 120, "name", required=True)
    with get_connection() as conn:
        cur = conn.cursor()
        if parent_id:
            parent = _one(cur, "SELECT * FROM patient_segments WHERE id = ? AND hospital_id = ?", (parent_id, hospital_id))
            if not parent:
                raise Patient360Error("Segment not found", 404)
            key = parent["segment_key"]
            version = _one(cur, "SELECT MAX(version) AS v FROM patient_segments WHERE hospital_id = ? AND segment_key = ?", (hospital_id, key))["v"] + 1
            cur.execute("UPDATE patient_segments SET status = 'archived', archived_at = CURRENT_TIMESTAMP WHERE hospital_id = ? AND segment_key = ? AND status = 'active'", (hospital_id, key))
        else:
            key, version = hashlib.sha1(f"{hospital_id}|{name}|{current_utc_naive()}".encode()).hexdigest()[:12], 1
        row = _one(cur, "INSERT INTO patient_segments (hospital_id, segment_key, version, name, description, criteria, mode, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                   (hospital_id, key, version, name, rules.clean_text(data.get("description"), 300, "description") or None, _dump(criteria), mode, actor))
        if mode == "snapshot":
            row["member_count"] = _write_snapshot(cur, hospital_id, row["id"], criteria)
        conn.commit()
    return _segment_out(row)


def list_segments(hospital_id, include_archived=False):
    with get_connection() as conn:
        rows = _all(conn.cursor(), "SELECT s.*, (SELECT COUNT(*) FROM segment_memberships m WHERE m.segment_id = s.id) AS snapshot_count FROM patient_segments s "
                                   "WHERE s.hospital_id = ?" + ("" if include_archived else " AND s.status = 'active'") + " ORDER BY s.created_at DESC", (hospital_id,))
    return [_segment_out(r) for r in rows]


def get_segment(hospital_id, segment_id):
    with get_connection() as conn:
        return _segment_out(_one(conn.cursor(), "SELECT * FROM patient_segments WHERE id = ? AND hospital_id = ?", (segment_id, hospital_id)))


def refresh_snapshot(hospital_id, segment_id):
    seg = get_segment(hospital_id, segment_id)
    if not seg:
        raise Patient360Error("Segment not found", 404)
    if seg["mode"] != "snapshot":
        raise Patient360Error("Only snapshot segments are refreshed; a dynamic segment is evaluated live.", 409)
    with get_connection() as conn:
        count = _write_snapshot(conn.cursor(), hospital_id, segment_id, seg["criteria"])
        conn.commit()
    return {"segment_id": segment_id, "member_count": count, "evaluated_at": current_utc_naive()}


def archive_segment(hospital_id, segment_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE patient_segments SET status = 'archived', archived_at = CURRENT_TIMESTAMP WHERE id = ? AND hospital_id = ? AND status = 'active'", (segment_id, hospital_id))
        if not cur.rowcount:
            raise Patient360Error("Segment not found or already archived", 404)
        conn.commit()


def interpret_query(prompt):
    """Natural-language -> validated criteria, via the existing Keppler/Qwen filter translator (ai.service). The model only
    proposes (field, op, value); nothing it returns is executed until rules.criteria_from_ai has checked it against the
    allow-list. When the model is unavailable the answer says so and manual filters keep working."""
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        raise Patient360Error("prompt is required")
    if len(prompt) > 400:
        raise Patient360Error("prompt must be at most 400 characters")
    from ai.service import translate_patient_filter_prompt

    hints = "; ".join(f"{name} = {f['help']} ({f['type']}{', values: ' + '/'.join(f['values']) if f.get('values') else ''})" for name, f in rules.SEGMENT_FIELDS.items())
    proposal = translate_patient_filter_prompt(f"{prompt}\nField meanings: {hints}. For true/false fields use value true or false.", list(rules.SEGMENT_FIELDS))
    if proposal is None:
        return {"available": False, "criteria": None, "rejected": [], "message": "The AI model is not available. Use the manual filter builder."}
    criteria, rejected = rules.criteria_from_ai(proposal)
    return {"available": True, "criteria": criteria, "rejected": rejected, "valid": criteria is not None and not rejected, "provisional": True,
            "message": "AI-proposed filter -- review the interpreted criteria before relying on it."}


# ── Overview & analytics ────────────────────────────────────────────────────────────────────────
def get_overview(hospital_id):
    with get_connection() as conn:
        cur = conn.cursor()
        one = lambda sql, p=(hospital_id,): _one(cur, sql, p)["n"]  # noqa: E731
        data = {
            "active_journeys": one("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND state = 'active'"),
            "paused_journeys": one("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND state = 'paused'"),
            "journeys_needing_review": one("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND state IN ('needs_review', 'failed')"),
            "enrollments_pending_review": one("SELECT COUNT(*) AS n FROM patient360_enrollments WHERE hospital_id = ? AND status = 'pending_review'"),
            "enrollments_awaiting_template": one("SELECT COUNT(*) AS n FROM patient360_enrollments WHERE hospital_id = ? AND status = 'eligible'"),
            "templates_awaiting_approval": one("SELECT COUNT(*) AS n FROM care_journey_templates WHERE hospital_id = ? AND status = 'pending_approval'"),
            "surveys_awaiting_approval": one("SELECT COUNT(*) AS n FROM survey_templates WHERE hospital_id = ? AND status = 'pending_approval'"),
            "checkpoints_due_now": one("SELECT COUNT(*) AS n FROM care_checkpoints c JOIN care_journey_instances j ON j.id = c.journey_id WHERE c.hospital_id = ? AND j.state = 'active' AND c.status = 'scheduled' AND c.due_at <= CURRENT_TIMESTAMP"),
            "checkpoints_overdue": one("SELECT COUNT(*) AS n FROM care_checkpoints WHERE hospital_id = ? AND status = 'overdue'"),
            "responses_awaiting_review": one("SELECT COUNT(*) AS n FROM survey_responses WHERE hospital_id = ? AND review_status = 'pending'"),
            "open_tasks": one("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled')"),
            "overdue_tasks": one("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled') AND due_at < CURRENT_TIMESTAMP"),
            "concerns_awaiting_acknowledgement": one("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND category = 'patient_reported_concern' AND status IN ('open', 'assigned')"),
            "communication_failures_7d": one("SELECT COUNT(*) AS n FROM patient_messages WHERE hospital_id = ? AND status IN ('failed', 'undelivered') AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'"),
        }
        data["workload"] = _all(cur, "SELECT COALESCE(assigned_to, owner_team, 'unassigned') AS owner, COUNT(*) AS open_tasks, "
                                     "SUM(CASE WHEN due_at < CURRENT_TIMESTAMP THEN 1 ELSE 0 END) AS overdue FROM care_tasks "
                                     "WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled') GROUP BY 1 ORDER BY 2 DESC LIMIT 15", (hospital_id,))
        data["recent_tasks"] = _all(cur, "SELECT id, patient_id, title, priority, status, due_at FROM care_tasks WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled') "
                                         "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, due_at ASC NULLS LAST LIMIT 8", (hospital_id,))
    data["generated_at"] = current_utc_naive()
    return data


def get_analytics(hospital_id, days=30):
    days = max(1, min(int(days), 365))
    since = current_utc_naive() - timedelta(days=days)
    with get_connection() as conn:
        cur = conn.cursor()
        n = lambda sql, p: _one(cur, sql, p)["n"]  # noqa: E731
        enrolled = n("SELECT COUNT(*) AS n FROM patient360_enrollments WHERE hospital_id = ? AND created_at >= ?", (hospital_id, since))
        journeyed = n("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND created_at >= ?", (hospital_id, since))
        done = n("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND created_at >= ? AND state IN ('completed')", (hospital_id, since))
        closed_due = n("SELECT COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? AND created_at >= ? AND state IN ('completed', 'closed', 'cancelled')", (hospital_id, since))
        sent = n("SELECT COUNT(*) AS n FROM patient_messages WHERE hospital_id = ? AND created_at >= ? AND status IN ('sent', 'delivered', 'read', 'undelivered', 'failed')", (hospital_id, since))
        delivered = n("SELECT COUNT(*) AS n FROM patient_messages WHERE hospital_id = ? AND created_at >= ? AND status IN ('delivered', 'read')", (hospital_id, since))
        invited = n("SELECT COUNT(*) AS n FROM care_checkpoints WHERE hospital_id = ? AND action_type = 'survey' AND status IN ('queued', 'sent', 'delivered', 'completed', 'overdue') AND due_at >= ?", (hospital_id, since))
        answered = n("SELECT COUNT(*) AS n FROM survey_responses WHERE hospital_id = ? AND checkpoint_id IS NOT NULL AND submitted_at >= ?", (hospital_id, since))
        tasks_created = n("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND created_at >= ?", (hospital_id, since))
        tasks_resolved = n("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND created_at >= ? AND status = 'resolved'", (hospital_id, since))
        open_now = n("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled')", (hospital_id,))
        overdue_now = n("SELECT COUNT(*) AS n FROM care_tasks WHERE hospital_id = ? AND status NOT IN ('resolved', 'cancelled') AND due_at < CURRENT_TIMESTAMP", (hospital_id,))
        ack = _one(cur, "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (acknowledged_at - created_at))) AS p50, "
                        "percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (acknowledged_at - created_at))) AS p90, COUNT(*) AS n "
                        "FROM care_tasks WHERE hospital_id = ? AND created_at >= ? AND acknowledged_at IS NOT NULL", (hospital_id, since))
        by_priority = _all(cur, "SELECT priority, COUNT(*) AS created, SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved, "
                                "percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (resolved_at - created_at))) FILTER (WHERE resolved_at IS NOT NULL) AS median_resolution_seconds "
                                "FROM care_tasks WHERE hospital_id = ? AND created_at >= ? GROUP BY priority", (hospital_id, since))
        journeys_by_state = _all(cur, "SELECT state, COUNT(*) AS n FROM care_journey_instances WHERE hospital_id = ? GROUP BY state ORDER BY n DESC", (hospital_id,))
        checkpoints_by_status = _all(cur, "SELECT status, COUNT(*) AS n FROM care_checkpoints WHERE hospital_id = ? GROUP BY status ORDER BY n DESC", (hospital_id,))

    def metric(key, label, num, den, definition, unit="ratio"):
        return {"key": key, "label": label, "numerator": num, "denominator": den, "value": (round(num / den, 4) if den else None), "unit": unit, "definition": definition}

    window = f"last {days} days (UTC)"
    return {
        "window_days": days, "since": since, "refreshed_at": current_utc_naive(), "timezone": "UTC",
        "metrics": [
            metric("delivery_rate", "Message delivery rate", delivered, sent, f"Messages delivered or read / messages accepted for sending, from the Patient Messages log, {window}. Exclusions: not_configured (never sent)."),
            metric("survey_response_rate", "Survey response rate", answered, invited, f"Survey responses linked to a checkpoint / survey checkpoints issued (queued or later), {window}. Walk-in responses without a checkpoint are not counted."),
            metric("journey_completion_rate", "Journey completion rate", done, closed_due, f"Journeys that completed / journeys that ended (completed, closed or cancelled), for journeys started in the {window}."),
            metric("task_resolution_rate", "Task resolution rate", tasks_resolved, tasks_created, f"Resolved / created, for tasks created in the {window}."),
            metric("overdue_task_rate", "Overdue task rate", overdue_now, open_now, "Open tasks past due / all open tasks, measured now."),
        ],
        "counts": {"enrollments": enrolled, "journeys_started": journeyed, "messages_accepted": sent, "tasks_created": tasks_created},
        "task_acknowledgement_seconds": {"median": ack["p50"], "p90": ack["p90"], "tasks": ack["n"], "definition": f"Task creation to first acknowledgement, tasks created in the {window}."},
        "task_by_priority": by_priority, "journeys_by_state": journeys_by_state, "checkpoints_by_status": checkpoints_by_status,
        "caveat": "These are operational engagement metrics. They do not show that outcomes improved.",
    }


def patient_audit(hospital_id, patient_id, limit=100):
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        return _all(cur, "SELECT id, actor_username, action, module_name, entity_key, payload, created_at FROM audit_logs "
                         "WHERE hospital_id = ? AND module_name = 'patient360' AND entity_key = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (hospital_id, patient_id, max(1, min(int(limit), 500))))


# ── Coordinator notes & symptom history ─────────────────────────────────────────────────────────
NOTE_MAX = 2000


def list_notes(hospital_id, patient_id, limit=100):
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        return _all(cur, "SELECT id, note, author, created_at FROM care_notes WHERE hospital_id = ? AND patient_id = ? "
                         "ORDER BY created_at DESC, id DESC LIMIT ?", (hospital_id, patient_id, max(1, min(int(limit), 500))))


def add_note(hospital_id, actor, patient_id, text):
    note = str(text or "").strip()  # newlines are kept; only the ends are trimmed
    if not note:
        raise Patient360Error("note is required")
    if len(note) > NOTE_MAX:
        raise Patient360Error(f"note must be at most {NOTE_MAX} characters")
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        cur.execute("INSERT INTO care_notes (hospital_id, patient_id, note, author) VALUES (?, ?, ?, ?) "
                    "RETURNING id, note, author, created_at", (hospital_id, patient_id, note, actor))
        row = row_to_dict(cur.fetchone())
        conn.commit()
    return row


def symptom_history(hospital_id, patient_id, limit=50):
    """Every symptom report the patient sent in, newest first (hospital messages and registration echoes excluded)."""
    with get_connection() as conn:
        cur = conn.cursor()
        _canonical_patient(cur, hospital_id, patient_id)
        rows = _all(cur, "SELECT id, channel, body, created_at FROM patient_messages WHERE hospital_id = ? AND patient_id = ? "
                         "AND kind = 'symptoms' AND body IS NOT NULL AND body <> '' ORDER BY created_at DESC, id DESC LIMIT ?",
                    (hospital_id, patient_id, max(1, min(int(limit), 200))))
    out = []
    for r in rows:
        text = rules.clean_symptom_text(r["body"])
        if text:
            out.append({"id": r["id"], "channel": r["channel"], "symptoms": text, "created_at": r["created_at"]})
    return out


# ── Full cross-module record (print summary) ────────────────────────────────────────────────────
# (table, module, title, curated). `curated` tables are already laid out by the profile, so they only feed the module trail;
# the rest are printed generically. Table names come from this constant list only -- never from request input.
_RECORD_TABLES = [
    ("patients", "Registration", "Registration", True),
    ("appointments", "Appointments", "Appointments", True),
    ("opd_queue", "OPD", "OPD queue", False),
    ("op_timeline", "OPD", "OP timeline", False),
    ("encounters", "OPD / Inpatient", "Encounters", True),
    ("er_visits", "Emergency", "Emergency visits", True),
    ("admissions", "Inpatient", "Admissions", True),
    ("bed_allocations", "Bed management", "Bed allocations", True),
    ("patient_movements", "Bed management", "Patient movements", False),
    ("clinical_notes", "Clinical", "Clinical notes", False),
    ("diagnosis_records", "Clinical", "Diagnoses", True),
    ("medical_history", "Clinical", "Medical history", True),
    ("care_plans", "Clinical", "Care plans", False),
    ("clinical_orders", "Clinical", "Clinical orders", False),
    ("certificates", "Clinical", "Certificates", False),
    ("patient_vitals", "Nursing", "Vitals", True),
    ("nursing_notes", "Nursing", "Nursing notes", False),
    ("observation_notes", "Nursing", "Observation notes", False),
    ("medication_schedules", "Nursing", "Medication administration schedule", False),
    ("icu_consults", "ICU", "ICU consults", False),
    ("icu_flowsheets", "ICU", "ICU flowsheets", False),
    ("icu_monitoring", "ICU", "ICU monitoring", False),
    ("icu_ventilator_settings", "ICU", "ICU ventilator settings", False),
    ("icu_infusions", "ICU", "ICU infusions", False),
    ("icu_io_records", "ICU", "ICU intake / output", False),
    ("icu_lab_results", "ICU", "ICU lab results", False),
    ("icu_rass_scores", "ICU", "ICU sedation (RASS) scores", False),
    ("diagnostics", "Laboratory / Radiology", "Diagnostic orders", True),
    ("ot_cases", "Surgery / OT", "OT cases", False),
    ("ot_surgeries", "Surgery / OT", "Surgeries", True),
    ("pharmacy_prescriptions", "Pharmacy", "Prescriptions", True),
    ("pharmacy_sales", "Pharmacy", "Pharmacy sales", False),
    ("invoices", "Billing", "Invoices", True),
    ("insurance_claims", "Insurance", "Insurance claims", True),
    ("insurance_verifications", "Insurance", "Insurance verifications", False),
    ("insurance_records", "Insurance", "Insurance records", False),
    ("ambulance_dispatch", "Ambulance", "Ambulance dispatch", False),
    ("transfer_notices", "Transfers", "Transfer notices", False),
    ("patient_consents", "Consent", "Consents", False),
    ("documents", "Documents", "Documents", True),
    ("patient_messages", "Patient messages", "Messages", True),
    ("care_journey_instances", "Patient 360", "Care journeys", True),
    ("care_tasks", "Patient 360", "Tasks", True),
    ("care_notes", "Patient 360", "Coordinator notes", True),
    ("survey_responses", "Patient 360", "Survey responses", True),
]
_SKIP_EXACT = {"id", "hospital_id", "patient_id", "deleted_at", "password", "token", "secret", "hash", "embedding", "ocr_text", "file_data", "file_content", "blob"}
_SKIP_SUFFIX = ("_token", "_hash", "_secret", "_password", "_embedding", "_blob", "_ocr_text")


def _skip_col(name):
    return name in _SKIP_EXACT or name.endswith(_SKIP_SUFFIX)


_WHEN_COLS = ("created_at", "recorded_at", "arrival_at", "admission_date", "appointment_date", "scheduled_start", "allocated_at", "timestamp",
              "event_time", "started_at", "submitted_at", "noted_at", "ordered_at", "moved_at", "dispatched_at")
_CELL_MAX = 1500


def _record_cell(v):
    if v is None or isinstance(v, (bytes, bytearray, memoryview)):
        return None
    if isinstance(v, str) and len(v) > _CELL_MAX:
        return v[:_CELL_MAX] + "…"
    return v


def full_record(hospital_id, patient_id, per_table=200):
    """Every module's rows for one patient plus the trail of modules the patient has been through (oldest first)."""
    with get_connection() as conn:
        cur = conn.cursor()
        pid = _canonical_patient(cur, hospital_id, patient_id)["patient_id"]
    modules, sections = {}, []
    for table, module, title, curated in _RECORD_TABLES:
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                if not _has_col(cur, table, "patient_id"):
                    continue
                where, params = ["patient_id = ?"], [pid]
                if _has_col(cur, table, "hospital_id"):
                    where.append("hospital_id = ?"); params.append(hospital_id)
                if _has_col(cur, table, "deleted_at"):
                    where.append("deleted_at IS NULL")
                when = next((c for c in _WHEN_COLS if _has_col(cur, table, c)), None)
                order = f"ORDER BY {when} DESC" if when else ""
                rows = _all(cur, f"SELECT * FROM {table} WHERE {' AND '.join(where)} {order} LIMIT {int(per_table)}", tuple(params))
        except Exception:  # one unreadable source must not blank the whole record
            logger.exception("patient360: full record source %s failed for %s", table, pid)
            continue
        if not rows:
            continue
        stamps = [str(r[when]) for r in rows if when and r.get(when)]
        m = modules.setdefault(module, {"module": module, "records": 0, "first": None, "last": None, "sources": []})
        m["records"] += len(rows)
        m["sources"].append(title)
        if stamps:
            m["first"] = min([x for x in (m["first"], min(stamps)) if x])
            m["last"] = max([x for x in (m["last"], max(stamps)) if x])
        if not curated:
            cleaned = [{k: _record_cell(v) for k, v in r.items() if not _skip_col(k)} for r in rows]
            sections.append({"module": module, "title": title, "rows": cleaned})
    trail = sorted(modules.values(), key=lambda x: (x["first"] is None, x["first"] or ""))
    return {"modules": trail, "sections": sections}
