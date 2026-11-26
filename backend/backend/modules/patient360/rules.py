"""Patient 360 rules -- pure functions, no database, no Flask.

Everything that decides *whether something is allowed or valid* lives here so it can be unit-tested
without a database: state machines, template/survey validation, deterministic escalation rules,
send-time eligibility and the allow-listed segment compiler.

Safety stance (spec sections 1.4, 12, 16):
  * anything that cannot be verified fails closed (no consent record == not eligible);
  * the model never writes SQL -- it proposes (field, op, value) tuples that are validated here against an
    allow-list and compiled into a parameterised WHERE clause;
  * escalation is decided by clinician-approved deterministic rules, never by an LLM.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_HOSPITAL_TEMPLATE = re.compile(r"you are registered at|\bUMR\b.{0,60}\bOP No\b|OP timings\s*:", re.I | re.S)
_BOILERPLATE = re.compile(r"\bHelpline\b[^.\n]*\.?|Your health record\s*:?\s*\S*|https?://\S+", re.I)


def clean_symptom_text(text):
    """Symptoms must be what the patient reported. Returns None when `text` is really the hospital's own registration
    message (it can be echoed back through WhatsApp), and strips helpline / health-record-link boilerplate otherwise."""
    if not text or not str(text).strip():
        return None
    s = str(text)
    if _HOSPITAL_TEMPLATE.search(s):
        return None
    s = re.sub(r"\s{2,}", " ", _BOILERPLATE.sub("", s)).strip(" .,-\n")
    return s or None

# ── Enums (spec Appendix A) ─────────────────────────────────────────────────────────────────────
CHANNELS = ("whatsapp", "sms")
# "all" is only ever *recorded* (a STOP reply / a blanket withdrawal); it is not a purpose you send for.
PURPOSES = ("clinical_followup", "appointment_reminder", "survey", "marketing")
AUTH_STATUSES = ("active", "withdrawn", "expired", "pending_verification", "not_granted")
CHECKPOINT_ACTIONS = ("survey", "message", "appointment_reminder", "call_task", "staff_review")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")
TASK_OPEN_STATUSES = ("open", "assigned", "acknowledged", "in_progress", "escalated", "snoozed")
TASK_STATUSES = TASK_OPEN_STATUSES + ("resolved", "cancelled")
QUESTION_TYPES = ("yes_no", "single_choice", "multiple_choice", "rating", "free_text")

# ── State machines ──────────────────────────────────────────────────────────────────────────────
# Templates and surveys share one approval workflow: nobody publishes their own draft unreviewed.
APPROVAL_TRANSITIONS = {
    "draft": {"pending_approval"},
    "pending_approval": {"approved", "draft"},  # approve, or send back
    "approved": {"published", "draft"},
    "published": {"retired"},
    "retired": set(),
}

JOURNEY_TRANSITIONS = {
    "pending": {"active", "cancelled"},
    "active": {"paused", "completed", "cancelled", "closed", "needs_review"},
    "paused": {"active", "cancelled", "closed", "needs_review"},
    "needs_review": {"active", "cancelled", "closed"},
    "completed": {"closed"},
    "cancelled": {"closed"},
    "closed": set(),
}
# Pausing, cancelling and closing stop patient-facing activity, so they always carry a written reason.
JOURNEY_REASON_REQUIRED = {"paused", "cancelled", "closed", "needs_review"}

TASK_TRANSITIONS = {
    "open": {"assigned", "acknowledged", "in_progress", "escalated", "snoozed", "resolved", "cancelled"},
    "assigned": {"acknowledged", "in_progress", "escalated", "snoozed", "resolved", "cancelled"},
    "acknowledged": {"in_progress", "escalated", "snoozed", "resolved", "cancelled"},
    "in_progress": {"escalated", "snoozed", "resolved", "cancelled"},
    "escalated": {"acknowledged", "in_progress", "resolved", "cancelled"},
    "snoozed": {"open", "acknowledged", "in_progress", "resolved", "cancelled"},
    "resolved": set(),
    "cancelled": set(),
}


def can_transition(table: dict, current: str, target: str) -> bool:
    return target in table.get(current, set())


# ── Small validators ────────────────────────────────────────────────────────────────────────────
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")


def clean_text(value, max_len: int, field: str, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return text


def slug(value, field: str = "key") -> str:
    key = str(value or "").strip().lower()
    if not _KEY_RE.match(key):
        raise ValueError(f"{field} must be 1-40 characters: lowercase letters, digits, '-' or '_'")
    return key


def slugify(value) -> str:
    """'Surgical discharge (v2)' -> 'surgical-discharge-v2': a stable key derived from a human name."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")[:40].strip("-")


def normalize_phone(raw) -> str:
    """Digits only; an Indian 10-digit number gets its country code. '' when unusable."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 else ""


def parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        raise ValueError("invalid date/time") from None


# ── Journey templates ───────────────────────────────────────────────────────────────────────────
def validate_checkpoints(raw) -> list:
    """Normalise a template's checkpoint list. Timing, wording and owners are clinical decisions made by the
    approving team -- this only checks the shape, it never invents a schedule."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one checkpoint is required")
    if len(raw) > 30:
        raise ValueError("a journey can have at most 30 checkpoints")
    seen, out = set(), []
    for i, cp in enumerate(raw, start=1):
        if not isinstance(cp, dict):
            raise ValueError(f"checkpoint {i} is not an object")
        key = slug(cp.get("key"), f"checkpoint {i} key")
        if key in seen:
            raise ValueError(f"duplicate checkpoint key '{key}'")
        seen.add(key)
        action = str(cp.get("action_type") or "")
        if action not in CHECKPOINT_ACTIONS:
            raise ValueError(f"checkpoint '{key}': action_type must be one of {', '.join(CHECKPOINT_ACTIONS)}")
        try:
            days = int(cp.get("offset_days", 0))
            hours = int(cp.get("offset_hours", 0))
        except (TypeError, ValueError):
            raise ValueError(f"checkpoint '{key}': offset_days/offset_hours must be whole numbers") from None
        if not 0 <= days <= 365 or not 0 <= hours <= 23:
            raise ValueError(f"checkpoint '{key}': offset_days must be 0-365 and offset_hours 0-23")
        item = {
            "key": key,
            "label": clean_text(cp.get("label") or key, 120, f"checkpoint '{key}' label", required=True),
            "action_type": action,
            "offset_days": days,
            "offset_hours": hours,
            "owner_role": clean_text(cp.get("owner_role") or "care_coordinator", 60, "owner_role"),
            "survey_key": "",
            "message_text": "",
            "instructions": clean_text(cp.get("instructions"), 500, f"checkpoint '{key}' instructions"),
        }
        if action == "survey":
            item["survey_key"] = slug(cp.get("survey_key"), f"checkpoint '{key}' survey_key")
        if action in ("message", "appointment_reminder"):
            item["message_text"] = clean_text(cp.get("message_text"), 600, f"checkpoint '{key}' message_text", required=True)
        out.append(item)
    return out


def validate_eligibility(raw) -> dict:
    """Which discharges a template applies to. Empty lists mean "any"."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"departments": [], "wards": [], "min_stay_days": 0}
    for name in ("departments", "wards"):
        vals = raw.get(name) or []
        if not isinstance(vals, list) or len(vals) > 30:
            raise ValueError(f"eligibility.{name} must be a list")
        out[name] = [clean_text(v, 80, f"eligibility.{name}") for v in vals if str(v).strip()]
    try:
        out["min_stay_days"] = max(0, min(365, int(raw.get("min_stay_days") or 0)))
    except (TypeError, ValueError):
        raise ValueError("eligibility.min_stay_days must be a whole number") from None
    return out


def compute_due_at(anchor: datetime, checkpoint: dict) -> datetime:
    """Due time = discharge time + the checkpoint's relative offset. Stored UTC-naive like every other timestamp."""
    return anchor + timedelta(days=int(checkpoint.get("offset_days", 0)), hours=int(checkpoint.get("offset_hours", 0)))


def template_matches(eligibility: dict, ward: str | None, stay_days: float | None) -> tuple[bool, str]:
    """(eligible, reason). A discharge whose ward is unknown never matches a ward-restricted template."""
    wards = [w.lower() for w in eligibility.get("wards") or []]
    if wards and (ward or "").strip().lower() not in wards:
        return False, "ward_not_in_template_scope"
    min_stay = eligibility.get("min_stay_days") or 0
    if min_stay and (stay_days is None or stay_days < min_stay):
        return False, "stay_shorter_than_template_minimum"
    return True, "eligible"


# ── Surveys ─────────────────────────────────────────────────────────────────────────────────────
def validate_questions(raw) -> list:
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one question is required")
    if len(raw) > 40:
        raise ValueError("a survey can have at most 40 questions")
    ids, out = set(), []
    for i, q in enumerate(raw, start=1):
        if not isinstance(q, dict):
            raise ValueError(f"question {i} is not an object")
        qid = slug(q.get("id"), f"question {i} id")
        if qid in ids:
            raise ValueError(f"duplicate question id '{qid}'")
        qtype = str(q.get("type") or "")
        if qtype not in QUESTION_TYPES:
            raise ValueError(f"question '{qid}': type must be one of {', '.join(QUESTION_TYPES)}")
        item = {
            "id": qid,
            "text": clean_text(q.get("text"), 300, f"question '{qid}' text", required=True),
            "type": qtype,
            "required": bool(q.get("required", True)),
            "options": [],
            "min": 1,
            "max": 5,
            "show_if": None,
        }
        if qtype in ("single_choice", "multiple_choice"):
            options = [clean_text(o, 80, f"question '{qid}' option") for o in (q.get("options") or []) if str(o).strip()]
            if not 2 <= len(options) <= 12 or len(set(options)) != len(options):
                raise ValueError(f"question '{qid}': provide 2-12 distinct options")
            item["options"] = options
        if qtype == "rating":
            try:
                lo, hi = int(q.get("min", 1)), int(q.get("max", 5))
            except (TypeError, ValueError):
                raise ValueError(f"question '{qid}': min/max must be whole numbers") from None
            if not 0 <= lo < hi <= 10:
                raise ValueError(f"question '{qid}': rating scale must satisfy 0 <= min < max <= 10")
            item["min"], item["max"] = lo, hi
        cond = q.get("show_if")
        if cond:
            # Branching may only look *backwards*, so a survey can never loop or deadlock.
            if not isinstance(cond, dict) or cond.get("question_id") not in ids:
                raise ValueError(f"question '{qid}': show_if must reference an earlier question")
            item["show_if"] = {"question_id": cond["question_id"], "equals": cond.get("equals")}
        ids.add(qid)
        out.append(item)
    return out


def _norm(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return str(value).strip().lower() if value is not None else ""


def is_question_visible(question: dict, answers: dict) -> bool:
    cond = question.get("show_if")
    if not cond:
        return True
    return _norm(answers.get(cond["question_id"])) == _norm(cond.get("equals"))


def validate_answers(questions: list, answers) -> dict:
    """Check a submission against the exact survey version and return the answers as stored. Answers to
    questions the branching hid are dropped; unknown question ids are rejected, never silently kept."""
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object keyed by question id")
    known = {q["id"] for q in questions}
    unknown = sorted(set(answers) - known)
    if unknown:
        raise ValueError(f"unknown question id(s): {', '.join(unknown)}")
    out = {}
    for q in questions:
        if not is_question_visible(q, answers):
            continue
        qid, value = q["id"], answers.get(q["id"])
        empty = value is None or value == "" or value == []
        if empty:
            if q["required"]:
                raise ValueError(f"question '{qid}' is required")
            continue
        if q["type"] == "yes_no":
            if _norm(value) not in ("yes", "no"):
                raise ValueError(f"question '{qid}': answer must be yes or no")
            out[qid] = _norm(value)
        elif q["type"] == "single_choice":
            if value not in q["options"]:
                raise ValueError(f"question '{qid}': not one of the options")
            out[qid] = value
        elif q["type"] == "multiple_choice":
            if not isinstance(value, list) or any(v not in q["options"] for v in value):
                raise ValueError(f"question '{qid}': choose from the options")
            out[qid] = list(dict.fromkeys(value))
        elif q["type"] == "rating":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"question '{qid}': rating must be a whole number") from None
            if not q["min"] <= number <= q["max"]:
                raise ValueError(f"question '{qid}': rating must be {q['min']}-{q['max']}")
            out[qid] = number
        else:
            out[qid] = clean_text(value, 2000, f"question '{qid}' answer")
    return out


ESCALATION_OPERATORS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "contains")


def validate_escalation_rules(raw, questions: list) -> list:
    """Rules a clinician approves with the survey: answer pattern -> task. Deterministic by design."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > 40:
        raise ValueError("escalation_rules must be a list of at most 40 rules")
    qids = {q["id"]: q for q in questions}
    out = []
    for i, rule in enumerate(raw, start=1):
        if not isinstance(rule, dict) or rule.get("question_id") not in qids:
            raise ValueError(f"escalation rule {i}: question_id must be a question in this survey")
        op = rule.get("operator")
        if op not in ESCALATION_OPERATORS:
            raise ValueError(f"escalation rule {i}: operator must be one of {', '.join(ESCALATION_OPERATORS)}")
        if op == "in" and not isinstance(rule.get("value"), list):
            raise ValueError(f"escalation rule {i}: 'in' needs a list value")
        priority = rule.get("priority", "high")
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"escalation rule {i}: priority must be one of {', '.join(TASK_PRIORITIES)}")
        try:
            due_hours = max(1, min(24 * 14, int(rule.get("due_hours", 24))))
        except (TypeError, ValueError):
            raise ValueError(f"escalation rule {i}: due_hours must be a whole number") from None
        out.append(
            {
                "question_id": rule["question_id"],
                "operator": op,
                "value": rule.get("value"),
                "priority": priority,
                "team": clean_text(rule.get("team") or "care_coordinator", 60, "team"),
                "due_hours": due_hours,
                "category": clean_text(rule.get("category") or "patient_reported_concern", 60, "category"),
                "title": clean_text(rule.get("title") or "Patient-reported concern", 120, "title"),
            }
        )
    return out


def _rule_holds(op: str, actual, expected) -> bool:
    if actual is None:
        return False
    if op == "contains":
        return str(expected).strip().lower() in str(actual).strip().lower()
    if op == "in":
        pool = {_norm(v) for v in expected}
        return any(_norm(a) in pool for a in (actual if isinstance(actual, list) else [actual]))
    if op in ("eq", "ne"):
        same = _norm(actual) == _norm(expected) if not isinstance(actual, list) else _norm(expected) in _norm(actual)
        return same if op == "eq" else not same
    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"lt": a < e, "lte": a <= e, "gt": a > e, "gte": a >= e}[op]


def evaluate_escalations(rules: list, answers: dict) -> list:
    """Every rule whose answer pattern matches. The patient's original answer is never altered."""
    return [r for r in rules if _rule_holds(r["operator"], answers.get(r["question_id"]), r["value"])]


# ── Communication eligibility (spec 8.5, 12.1) ──────────────────────────────────────────────────
def latest_authorization(rows, channel: str, purpose: str):
    """The decision in force: the newest record for this channel+purpose, or the newest blanket ('all') record,
    whichever is newer. `rows` are dicts with channel, purpose, status, created_at (ISO or datetime), newest first
    is not assumed."""
    best, best_key = None, None
    for r in rows:
        if r.get("channel") not in (channel, "all") or r.get("purpose") not in (purpose, "all"):
            continue
        stamp = parse_dt(r.get("created_at")) or datetime.min
        key = (stamp, r.get("id") or 0)
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


def check_send_eligibility(
    *,
    phone,
    channel: str,
    purpose: str,
    auth_rows,
    contact_window: tuple | None = None,
    now_utc: datetime | None = None,
    require_active: bool = True,
) -> dict:
    """Decide whether a message may go to a patient *right now*. Returns {eligible, reason, deferrable}.

    require_active=True  (journey checkpoints, campaigns): fail closed -- no active authorization, no send.
    require_active=False (existing desk messaging):        only an explicit withdrawal blocks, so today's
                                                           front-desk workflows keep working unchanged."""
    if channel not in CHANNELS:
        return {"eligible": False, "reason": "unsupported_channel", "deferrable": False}
    if purpose not in PURPOSES:
        return {"eligible": False, "reason": "unsupported_purpose", "deferrable": False}
    if not normalize_phone(phone):
        return {"eligible": False, "reason": "missing_or_invalid_contact", "deferrable": False}
    decision = latest_authorization(auth_rows, channel, purpose)
    status = (decision or {}).get("status")
    if status == "withdrawn":
        return {"eligible": False, "reason": "opted_out", "deferrable": False}
    if require_active and status != "active":
        return {"eligible": False, "reason": "no_active_authorization" if status is None else f"authorization_{status}", "deferrable": False}
    if contact_window:
        start_h, end_h = contact_window
        local_hour = ((now_utc or datetime.utcnow()).replace(tzinfo=timezone.utc).astimezone(IST)).hour
        inside = start_h <= local_hour < end_h if start_h < end_h else (local_hour >= start_h or local_hour < end_h)
        if not inside:
            return {"eligible": False, "reason": "outside_contact_window", "deferrable": True}
    return {"eligible": True, "reason": "eligible", "deferrable": False}


STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt-out"}


def is_opt_out_message(text) -> bool:
    return str(text or "").strip().lower().strip(".!") in STOP_WORDS


# ── Segments: allow-listed fields -> parameterised SQL (spec 9.1, 9.4) ──────────────────────────
# Every expression is evaluated per patient row aliased `p` and references only that row's hospital_id and
# patient_id. No expression takes user input; values are bound as parameters.
def _exists(sql: str) -> str:
    return f"(CASE WHEN EXISTS ({sql}) THEN 1 ELSE 0 END)"


_LATEST_AUTH = (
    "(SELECT ca.status FROM communication_authorizations ca WHERE ca.hospital_id = p.hospital_id "
    "AND ca.patient_id = p.patient_id AND ca.purpose IN ('clinical_followup', 'all') AND ca.channel IN ('{ch}', 'all') "
    "ORDER BY ca.created_at DESC, ca.id DESC LIMIT 1)"
)

SEGMENT_FIELDS = {
    "age": {"label": "Age", "type": "number", "sql": "p.age", "help": "age in years"},
    "gender": {"label": "Gender", "type": "enum", "values": ["male", "female", "other"], "sql": "LOWER(p.gender)", "help": "male, female or other"},
    "days_since_discharge": {
        "label": "Days since last discharge",
        "type": "number",
        "sql": "(SELECT (CURRENT_DATE - MAX(a.discharge_date)::date) FROM admissions a "
        "WHERE a.hospital_id = p.hospital_id AND a.patient_id = p.patient_id AND a.discharge_date IS NOT NULL)",
        "help": "days since the most recent inpatient discharge; 'discharged in the last 7 days' is <= 7",
    },
    "currently_admitted": {
        "label": "Currently admitted",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM admissions a WHERE a.hospital_id = p.hospital_id AND a.patient_id = p.patient_id AND a.discharge_date IS NULL"),
        "help": "true while the patient has an open admission",
    },
    "has_active_journey": {
        "label": "Has an active care journey",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM care_journey_instances j WHERE j.hospital_id = p.hospital_id AND j.patient_id = p.patient_id AND j.state = 'active'"),
        "help": "true when a post-discharge journey is running",
    },
    "has_journey_history": {
        "label": "Was ever enrolled in a journey",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM care_journey_instances j WHERE j.hospital_id = p.hospital_id AND j.patient_id = p.patient_id"),
        "help": "true when the patient has had any journey",
    },
    "open_task_count": {
        "label": "Open follow-up tasks",
        "type": "number",
        "sql": "(SELECT COUNT(*) FROM care_tasks t WHERE t.hospital_id = p.hospital_id AND t.patient_id = p.patient_id "
        "AND t.status NOT IN ('resolved', 'cancelled'))",
        "help": "number of unresolved coordination tasks",
    },
    "unresolved_concern": {
        "label": "Unresolved patient-reported concern",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM care_tasks t WHERE t.hospital_id = p.hospital_id AND t.patient_id = p.patient_id "
                       "AND t.category = 'patient_reported_concern' AND t.status NOT IN ('resolved', 'cancelled')"),
        "help": "true when a concern raised by a survey answer is still open",
    },
    "survey_pending": {
        "label": "Survey pending",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM care_checkpoints c WHERE c.hospital_id = p.hospital_id AND c.patient_id = p.patient_id "
                       "AND c.action_type = 'survey' AND c.status IN ('queued', 'sent', 'overdue')"),
        "help": "true when a survey was issued and not yet answered",
    },
    "overdue_checkpoint": {
        "label": "Has an overdue checkpoint",
        "type": "bool",
        "sql": _exists("SELECT 1 FROM care_checkpoints c WHERE c.hospital_id = p.hospital_id AND c.patient_id = p.patient_id "
                       "AND c.status IN ('scheduled', 'queued') AND c.due_at < CURRENT_TIMESTAMP"),
        "help": "true when a scheduled checkpoint is past its due time",
    },
    "has_phone": {
        "label": "Has a phone number",
        "type": "bool",
        "sql": "(CASE WHEN p.phone IS NOT NULL AND LENGTH(TRIM(p.phone)) >= 10 THEN 1 ELSE 0 END)",
        "help": "true when a contact number is on record",
    },
    "whatsapp_clinical_consent": {
        "label": "WhatsApp clinical-follow-up consent",
        "type": "enum",
        "values": ["active", "withdrawn", "expired", "pending_verification", "not_granted"],
        "sql": _LATEST_AUTH.format(ch="whatsapp"),
        "help": "status of the latest WhatsApp clinical follow-up authorization",
    },
    "sms_clinical_consent": {
        "label": "SMS clinical-follow-up consent",
        "type": "enum",
        "values": ["active", "withdrawn", "expired", "pending_verification", "not_granted"],
        "sql": _LATEST_AUTH.format(ch="sms"),
        "help": "status of the latest SMS clinical follow-up authorization",
    },
}

SEGMENT_OPS = {
    "number": ("eq", "ne", "gt", "gte", "lt", "lte"),
    "enum": ("eq", "ne", "in"),
    "bool": ("eq",),
    "text": ("eq", "ne", "contains"),
}
_SQL_OP = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
MAX_DEPTH, MAX_CONDITIONS = 3, 20


def _coerce_value(field: dict, op: str, value):
    ftype = field["type"]
    if ftype == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("expected a number") from None
        if abs(number) > 1_000_000:
            raise ValueError("number out of range")
        return int(number) if number == int(number) else number
    if ftype == "bool":
        if isinstance(value, bool):
            return 1 if value else 0
        if str(value).strip().lower() in ("true", "yes", "1"):
            return 1
        if str(value).strip().lower() in ("false", "no", "0"):
            return 0
        raise ValueError("expected true or false")
    if ftype == "enum":
        items = value if op == "in" else [value]
        if op == "in" and (not isinstance(value, list) or not value or len(value) > 10):
            raise ValueError("'in' needs a short list")
        cleaned = [str(v).strip().lower() for v in items]
        bad = [v for v in cleaned if v not in field["values"]]
        if bad:
            raise ValueError(f"not an allowed value: {', '.join(bad)}")
        return cleaned if op == "in" else cleaned[0]
    text = str(value or "").strip().lower()
    if not text or len(text) > 80:
        raise ValueError("expected a short text value")
    return text


def _compile_node(node, depth: int, state: dict):
    if depth > MAX_DEPTH:
        raise ValueError(f"criteria nest more than {MAX_DEPTH} levels")
    if not isinstance(node, dict):
        raise ValueError("each condition must be an object")
    if "conditions" in node:
        logic = str(node.get("logic") or "and").lower()
        if logic not in ("and", "or"):
            raise ValueError("logic must be 'and' or 'or'")
        children = node.get("conditions")
        if not isinstance(children, list) or not children:
            raise ValueError("a group needs at least one condition")
        parts = [_compile_node(c, depth + 1, state) for c in children]
        return "(" + f" {logic.upper()} ".join(parts) + ")"
    state["count"] += 1
    if state["count"] > MAX_CONDITIONS:
        raise ValueError(f"criteria are limited to {MAX_CONDITIONS} conditions")
    name, op = node.get("field"), node.get("op")
    field = SEGMENT_FIELDS.get(name)
    if field is None:
        raise ValueError(f"unknown field '{name}'")
    if op not in SEGMENT_OPS[field["type"]]:
        raise ValueError(f"operator '{op}' is not allowed for {field['label']}")
    value = _coerce_value(field, op, node.get("value"))
    state["fields"].add(name)
    if op == "in":
        state["params"].extend(value)
        return f"({field['sql']} IN ({', '.join('?' for _ in value)}))"
    if op == "contains":
        escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        state["params"].append(f"%{escaped}%")
        return f"(LOWER({field['sql']}) LIKE ? ESCAPE '!')"
    state["params"].append(value)
    return f"({field['sql']} {_SQL_OP[op]} ?)"


def compile_criteria(criteria) -> tuple[str, list, list]:
    """(where_sql, params, fields_used). Raises ValueError on anything outside the allow-list."""
    if not isinstance(criteria, dict):
        raise ValueError("criteria must be an object")
    root = criteria if "conditions" in criteria else {"logic": "and", "conditions": [criteria]}
    state = {"count": 0, "params": [], "fields": set()}
    sql = _compile_node(root, 1, state)
    return sql, state["params"], sorted(state["fields"])


def flatten_conditions(criteria) -> list:
    root = criteria if "conditions" in criteria else {"conditions": [criteria]}
    out = []
    for node in root.get("conditions", []):
        out.extend(flatten_conditions(node) if "conditions" in node else [node])
    return out


def condition_holds(field_name: str, op: str, actual, value) -> bool:
    """Python mirror of the SQL comparison, used only to explain *why* a patient matched."""
    field = SEGMENT_FIELDS[field_name]
    if actual is None:
        return False
    if field["type"] in ("enum", "text"):
        actual = str(actual).lower()
    expected = _coerce_value(field, op, value)
    if op == "in":
        return actual in expected
    if op == "contains":
        return expected in actual
    if field["type"] == "number":
        actual = float(actual)
    elif field["type"] == "bool":
        actual = int(actual)
    return {"eq": actual == expected, "ne": actual != expected, "gt": actual > expected, "gte": actual >= expected,
            "lt": actual < expected, "lte": actual <= expected}[op]


def describe_condition(cond: dict, actual) -> str:
    field = SEGMENT_FIELDS[cond["field"]]
    words = {"eq": "is", "ne": "is not", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "is one of", "contains": "contains"}
    shown_actual = ("yes" if actual else "no") if field["type"] == "bool" and actual is not None else actual
    value = cond.get("value")
    if field["type"] == "bool":
        value = "yes" if _coerce_value(field, "eq", value) else "no"
    return f"{field['label']} {words[cond['op']]} {value} (patient: {shown_actual if shown_actual is not None else 'not recorded'})"


def criteria_from_ai(proposal) -> tuple[dict | None, list]:
    """Turn a model's {conditions: [{field, op, value}], logic} proposal into validated criteria.
    Returns (criteria, rejected). A proposal with any rejected condition must not be executed (spec AC-04)."""
    rejected = []
    if not isinstance(proposal, dict) or not isinstance(proposal.get("conditions"), list):
        return None, [{"reason": "the model returned no usable filter"}]
    accepted = []
    for cond in proposal["conditions"]:
        try:
            compile_criteria({"logic": "and", "conditions": [cond]})
            accepted.append({"field": cond["field"], "op": cond["op"], "value": cond.get("value")})
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({"condition": cond, "reason": str(exc)})
    if not accepted:
        return None, rejected or [{"reason": "no condition could be mapped to an allowed field"}]
    logic = "or" if str(proposal.get("logic") or "").lower() == "or" else "and"
    return {"logic": logic, "conditions": accepted}, rejected
