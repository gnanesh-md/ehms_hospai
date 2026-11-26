"""Patient 360 API (spec section 15).

Permissions (core/auth.py SUB_MODULES["patient360"]):
  patient360.read               view registry, profiles, timelines, tasks, segments, analytics
  patient360.coordination.write enrol, run journeys, work tasks, record survey responses, draft templates/surveys
  patient360.consent.write      record communication authorization / opt-outs / contact preferences
  patient360.approve            clinically approve and publish journey templates and surveys
  patient360.segments.write     save / refresh / archive segments

Sending messages and bulk broadcast stay in the existing Patient Messages / PatientReach AI tools; this module
decides *eligibility* and coordinates the work, it does not run a second messaging stack.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, g, jsonify, request

from shared.audit import log_audit_event
from shared.hospital import current_hospital_id
from shared.session_auth import require_permissions
from modules.patient360 import rules, service
from modules.patient360.service import Patient360Error

patient360_bp = Blueprint("patient360", __name__)

READ = "patient360.read"
COORD = "patient360.coordination.write"
CONSENT = "patient360.consent.write"
APPROVE = "patient360.approve"
SEGMENTS = "patient360.segments.write"


def _errors(view):
    """Turn a rejected request into a clean JSON error instead of a 500."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Patient360Error as exc:
            return jsonify({"error": str(exc), **exc.extra}), exc.status
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return wrapper


def _actor():
    return (getattr(g, "current_user", None) or {}).get("username") or "unknown"


def _hid():
    return current_hospital_id()


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _audit(action, entity, payload=None):
    log_audit_event(action, "patient360", str(entity) if entity is not None else None, payload)


def _page():
    return request.args.get("limit", default=25, type=int), request.args.get("offset", default=0, type=int)


# ── Meta, overview, analytics ───────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/meta")
@require_permissions(READ)
def meta():
    return jsonify({
        "channels": rules.CHANNELS, "purposes": rules.PURPOSES, "authorization_statuses": rules.AUTH_STATUSES,
        "checkpoint_actions": rules.CHECKPOINT_ACTIONS, "task_priorities": rules.TASK_PRIORITIES, "task_statuses": rules.TASK_STATUSES,
        "question_types": rules.QUESTION_TYPES, "escalation_operators": rules.ESCALATION_OPERATORS,
        "segment_fields": {k: {"label": v["label"], "type": v["type"], "values": v.get("values"), "help": v["help"], "ops": rules.SEGMENT_OPS[v["type"]]}
                           for k, v in rules.SEGMENT_FIELDS.items()},
    })


@patient360_bp.get("/api/patient360/overview")
@require_permissions(READ)
def overview():
    return jsonify(service.get_overview(_hid()))


@patient360_bp.get("/api/patient360/analytics")
@require_permissions(READ)
def analytics():
    return jsonify(service.get_analytics(_hid(), request.args.get("days", default=30, type=int)))


# ── Registry, profile, timeline ─────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/patients")
@require_permissions(READ)
@_errors
def registry():
    limit, offset = _page()
    return jsonify(service.list_registry(_hid(), request.args.get("q", ""), request.args.get("state", ""), limit, offset))


@patient360_bp.get("/api/patient360/patients/<patient_id>")
@require_permissions(READ)
@_errors
def profile(patient_id):
    data = service.get_profile(_hid(), patient_id)
    if data is None:
        return jsonify({"error": "Patient not found"}), 404
    _audit("profile_view", data["patient"]["patient_id"])  # spec 12.4: sensitive record access is audited
    return jsonify(data)


@patient360_bp.get("/api/patient360/patients/<patient_id>/timeline")
@require_permissions(READ)
@_errors
def timeline(patient_id):
    limit, offset = request.args.get("limit", default=50, type=int), request.args.get("offset", default=0, type=int)
    return jsonify(service.get_timeline(_hid(), patient_id, request.args.get("category", ""), limit, offset))


@patient360_bp.get("/api/patient360/patients/<patient_id>/audit")
@require_permissions(READ)
@_errors
def patient_audit(patient_id):
    return jsonify({"events": service.patient_audit(_hid(), patient_id, request.args.get("limit", default=100, type=int))})


@patient360_bp.get("/api/patient360/patients/<patient_id>/notes")
@require_permissions(READ)
@_errors
def notes_list(patient_id):
    return jsonify({"notes": service.list_notes(_hid(), patient_id)})


@patient360_bp.post("/api/patient360/patients/<patient_id>/notes")
@require_permissions(COORD)
@_errors
def notes_add(patient_id):
    n = service.add_note(_hid(), _actor(), patient_id, _body().get("note"))
    _audit("note_added", patient_id, {"note_id": n["id"]})  # the note text itself is not copied into the audit log
    return jsonify(n), 201


@patient360_bp.get("/api/patient360/patients/<patient_id>/full-record")
@require_permissions(READ)
@_errors
def full_record(patient_id):
    data = service.full_record(_hid(), patient_id)
    _audit("full_record_print", patient_id, {"modules": len(data["modules"])})  # a cross-module read of the whole record is audited
    return jsonify(data)


@patient360_bp.get("/api/patient360/patients/<patient_id>/symptoms")
@require_permissions(READ)
@_errors
def symptoms_history(patient_id):
    return jsonify({"symptoms": service.symptom_history(_hid(), patient_id)})


# ── Consent, preferences, eligibility ───────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/patients/<patient_id>/consent")
@require_permissions(READ)
@_errors
def consent_get(patient_id):
    return jsonify({"current": service.current_authorizations(_hid(), patient_id), "history": service.authorization_history(_hid(), patient_id)})


@patient360_bp.post("/api/patient360/patients/<patient_id>/consent")
@require_permissions(CONSENT)
@_errors
def consent_set(patient_id):
    d = _body()
    row = service.record_authorization(_hid(), patient_id, d.get("channel"), d.get("purpose"), d.get("status"), _actor(),
                                       source=rules.clean_text(d.get("source") or "staff_recorded", 40, "source"), note=d.get("note"), policy_version=d.get("policy_version"))
    _audit("consent_recorded", patient_id, {"channel": d.get("channel"), "purpose": d.get("purpose"), "status": d.get("status")})
    return jsonify({"recorded": row, "current": service.current_authorizations(_hid(), patient_id)}), 201


@patient360_bp.put("/api/patient360/patients/<patient_id>/preferences")
@require_permissions(CONSENT)
@_errors
def preferences_set(patient_id):
    d = _body()
    service.save_preferences(_hid(), patient_id, _actor(), d.get("preferred_language"), d.get("preferred_channel"), d.get("contact_from_hour"), d.get("contact_to_hour"))
    _audit("preferences_updated", patient_id, d)
    return jsonify({"saved": True})


@patient360_bp.post("/api/patient360/eligibility")
@require_permissions(READ)
@_errors
def eligibility():
    d = _body()
    return jsonify(service.check_eligibility(_hid(), str(d.get("patient_id") or ""), d.get("channel") or "whatsapp", d.get("purpose") or "clinical_followup",
                                             require_active=bool(d.get("require_active", True))))


# ── Enrollments & journeys ──────────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/enrollments")
@require_permissions(READ)
@_errors
def enrollments():
    limit, offset = request.args.get("limit", default=50, type=int), request.args.get("offset", default=0, type=int)
    return jsonify(service.list_enrollments(_hid(), request.args.get("status", ""), limit, offset))


@patient360_bp.post("/api/patient360/enrollments/from-discharge")
@require_permissions(COORD)
@_errors
def enroll_manual():
    d = _body()
    result = service.enroll_from_discharge(_hid(), admission_id=d.get("admission_id"), actor=_actor(), source_module="patient360_manual")
    _audit("enrollment_created", d.get("admission_id"), {"result": result.get("status")})
    return jsonify(result), (201 if result["status"] in ("enrolled", "eligible") else 200)


@patient360_bp.post("/api/patient360/enrollments/<int:enrollment_id>/start-journey")
@require_permissions(COORD)
@_errors
def start_journey(enrollment_id):
    journey = service.start_journey_for_enrollment(_hid(), enrollment_id, _body().get("template_id"), _actor())
    _audit("journey_started", enrollment_id, {"journey_id": (journey or {}).get("id")})
    return jsonify({"journey": journey}), 201


@patient360_bp.get("/api/patient360/journeys")
@require_permissions(READ)
@_errors
def journeys():
    limit, offset = request.args.get("limit", default=50, type=int), request.args.get("offset", default=0, type=int)
    return jsonify(service.list_journeys(_hid(), request.args.get("state", ""), request.args.get("patient_id", ""), limit, offset))


@patient360_bp.get("/api/patient360/journeys/<int:journey_id>")
@require_permissions(READ)
@_errors
def journey_detail(journey_id):
    j = service.get_journey(_hid(), journey_id)
    return (jsonify(j), 200) if j else (jsonify({"error": "Journey not found"}), 404)


@patient360_bp.post("/api/patient360/journeys/<int:journey_id>/state")
@require_permissions(COORD)
@_errors
def journey_state(journey_id):
    d = _body()
    journey = service.transition_journey(_hid(), journey_id, d.get("state"), d.get("reason"), _actor())
    _audit("journey_state_changed", journey["patient_id"], {"journey_id": journey_id, "state": d.get("state"), "reason": d.get("reason")})
    return jsonify(journey)


@patient360_bp.post("/api/patient360/checkpoints/<int:checkpoint_id>/resolve")
@require_permissions(COORD)
@_errors
def checkpoint_resolve(checkpoint_id):
    d = _body()
    cp = service.resolve_checkpoint(_hid(), checkpoint_id, d.get("outcome"), d.get("note"), _actor())
    _audit("checkpoint_resolved", cp["patient_id"], {"checkpoint_id": checkpoint_id, "outcome": d.get("outcome")})
    return jsonify(cp)


@patient360_bp.post("/api/patient360/checkpoints/run-due")
@require_permissions(COORD)
@_errors
def checkpoints_run_due():
    summary = service.process_due_checkpoints(_hid(), _actor())
    _audit("checkpoints_processed", None, summary)
    return jsonify(summary)


# ── Journey templates ───────────────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/journey-templates")
@require_permissions(READ)
def templates_list():
    return jsonify({"templates": service.list_templates(_hid(), request.args.get("status", ""))})


@patient360_bp.post("/api/patient360/journey-templates")
@require_permissions(COORD)
@_errors
def templates_create():
    t = service.create_template(_hid(), _actor(), _body())
    _audit("template_created", t["id"], {"key": t["template_key"], "version": t["version"]})
    return jsonify(t), 201


@patient360_bp.put("/api/patient360/journey-templates/<int:template_id>")
@require_permissions(COORD)
@_errors
def templates_update(template_id):
    t = service.update_template(_hid(), template_id, _body())
    _audit("template_edited", template_id)
    return jsonify(t)


@patient360_bp.post("/api/patient360/journey-templates/<int:template_id>/new-version")
@require_permissions(COORD)
@_errors
def templates_new_version(template_id):
    t = service.new_template_version(_hid(), template_id, _actor())
    _audit("template_versioned", t["id"], {"key": t["template_key"], "version": t["version"]})
    return jsonify(t), 201


@patient360_bp.post("/api/patient360/journey-templates/<int:template_id>/submit")
@require_permissions(COORD)
@_errors
def templates_submit(template_id):
    row = service.template_transition(_hid(), template_id, "pending_approval", _actor())
    _audit("template_submitted", template_id)
    return jsonify(row)


_REVIEW = {"approve": "approved", "reject": "draft", "publish": "published", "retire": "retired"}


@patient360_bp.post("/api/patient360/journey-templates/<int:template_id>/review/<action>")
@require_permissions(APPROVE)
@_errors
def templates_review(template_id, action):
    if action not in _REVIEW:
        raise Patient360Error("action must be approve, reject, publish or retire")
    row = service.template_transition(_hid(), template_id, _REVIEW[action], _actor())
    _audit(f"template_{action}", template_id, {"key": row["template_key"], "version": row["version"]})
    return jsonify(row)


# ── Tasks ───────────────────────────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/tasks")
@require_permissions(READ)
@_errors
def tasks_list():
    a = request.args
    return jsonify(service.list_tasks(_hid(), a.get("status", "open"), a.get("patient_id", ""), a.get("assigned_to", ""), a.get("priority", ""),
                                      a.get("overdue") == "true", a.get("limit", default=50, type=int), a.get("offset", default=0, type=int)))


@patient360_bp.post("/api/patient360/tasks")
@require_permissions(COORD)
@_errors
def tasks_create():
    t = service.create_task(_hid(), _actor(), _body())
    _audit("task_created", t["patient_id"], {"task_id": t["id"], "priority": t["priority"]})
    return jsonify(t), 201


@patient360_bp.get("/api/patient360/tasks/<int:task_id>")
@require_permissions(READ)
def tasks_detail(task_id):
    t = service.get_task(_hid(), task_id)
    return (jsonify(t), 200) if t else (jsonify({"error": "Task not found"}), 404)


@patient360_bp.post("/api/patient360/tasks/<int:task_id>/action")
@require_permissions(COORD)
@_errors
def tasks_action(task_id):
    d = _body()
    t = service.task_action(_hid(), task_id, d.get("action"), _actor(), d)
    _audit(f"task_{d.get('action')}", t["patient_id"], {"task_id": task_id, "status": t["status"]})
    return jsonify(t)


# ── Surveys & responses ─────────────────────────────────────────────────────────────────────────
@patient360_bp.get("/api/patient360/surveys")
@require_permissions(READ)
def surveys_list():
    return jsonify({"surveys": service.list_surveys(_hid(), request.args.get("status", ""))})


@patient360_bp.post("/api/patient360/surveys")
@require_permissions(COORD)
@_errors
def surveys_create():
    s = service.create_survey(_hid(), _actor(), _body())
    _audit("survey_created", s["id"], {"key": s["survey_key"], "version": s["version"]})
    return jsonify(s), 201


@patient360_bp.put("/api/patient360/surveys/<int:survey_id>")
@require_permissions(COORD)
@_errors
def surveys_update(survey_id):
    s = service.update_survey(_hid(), survey_id, _body())
    _audit("survey_edited", survey_id)
    return jsonify(s)


@patient360_bp.post("/api/patient360/surveys/<int:survey_id>/new-version")
@require_permissions(COORD)
@_errors
def surveys_new_version(survey_id):
    s = service.new_survey_version(_hid(), survey_id, _actor())
    _audit("survey_versioned", s["id"], {"key": s["survey_key"], "version": s["version"]})
    return jsonify(s), 201


@patient360_bp.post("/api/patient360/surveys/<int:survey_id>/submit")
@require_permissions(COORD)
@_errors
def surveys_submit(survey_id):
    row = service.survey_transition(_hid(), survey_id, "pending_approval", _actor())
    _audit("survey_submitted", survey_id)
    return jsonify(row)


@patient360_bp.post("/api/patient360/surveys/<int:survey_id>/review/<action>")
@require_permissions(APPROVE)
@_errors
def surveys_review(survey_id, action):
    if action not in _REVIEW:
        raise Patient360Error("action must be approve, reject, publish or retire")
    row = service.survey_transition(_hid(), survey_id, _REVIEW[action], _actor())
    _audit(f"survey_{action}", survey_id, {"key": row["survey_key"], "version": row["version"]})
    return jsonify(row)


@patient360_bp.get("/api/patient360/responses")
@require_permissions(READ)
@_errors
def responses_list():
    a = request.args
    return jsonify(service.list_responses(_hid(), a.get("patient_id", ""), a.get("review_status", ""), a.get("limit", default=50, type=int), a.get("offset", default=0, type=int)))


@patient360_bp.post("/api/patient360/responses")
@require_permissions(COORD)
@_errors
def responses_create():
    r = service.submit_response(_hid(), _actor(), _body())
    _audit("survey_response_recorded", r["patient_id"], {"response_id": r["id"], "tasks": r["triggered_task_ids"]})
    return jsonify(r), 201


@patient360_bp.post("/api/patient360/responses/<int:response_id>/review")
@require_permissions(COORD)
@_errors
def responses_review(response_id):
    d = _body()
    service.review_response(_hid(), response_id, _actor(), d.get("status"), d.get("note"))
    _audit("survey_response_reviewed", response_id, {"status": d.get("status")})
    return jsonify({"saved": True})


# ── Segments, AI query, recipient eligibility ───────────────────────────────────────────────────
@patient360_bp.post("/api/patient360/queries/interpret")
@require_permissions(READ)
@_errors
def query_interpret():
    prompt = _body().get("prompt")
    result = service.interpret_query(prompt)
    _audit("ai_query_interpreted", None, {"prompt": str(prompt)[:200], "valid": result.get("valid"), "available": result.get("available")})
    return jsonify(result)


@patient360_bp.post("/api/patient360/segments/evaluate")
@require_permissions(READ)
@_errors
def segments_evaluate():
    d = _body()
    result = service.evaluate_segment(_hid(), d.get("criteria"), d.get("limit", 25), d.get("offset", 0))
    _audit("segment_evaluated", None, {"matched": result["matched"], "fields": [c["field"] for c in result["interpreted"]]})
    return jsonify(result)


@patient360_bp.post("/api/patient360/segments/recipients")
@require_permissions(READ)
@_errors
def segments_recipients():
    d = _body()
    channel, purpose = d.get("channel") or "whatsapp", d.get("purpose") or "clinical_followup"
    if channel not in rules.CHANNELS or purpose not in rules.PURPOSES:
        raise Patient360Error("unsupported channel or purpose")
    result = service.segment_recipients(_hid(), d.get("criteria"), channel, purpose)
    _audit("recipients_previewed", None, {"eligible": result["eligible_count"], "excluded": result["excluded_count"], "channel": channel, "purpose": purpose})
    return jsonify(result)


@patient360_bp.get("/api/patient360/segments")
@require_permissions(READ)
def segments_list():
    return jsonify({"segments": service.list_segments(_hid(), request.args.get("archived") == "true")})


@patient360_bp.post("/api/patient360/segments")
@require_permissions(SEGMENTS)
@_errors
def segments_create():
    s = service.save_segment(_hid(), _actor(), _body())
    _audit("segment_created", s["id"], {"name": s["name"], "mode": s["mode"]})
    return jsonify(s), 201


@patient360_bp.put("/api/patient360/segments/<int:segment_id>")
@require_permissions(SEGMENTS)
@_errors
def segments_update(segment_id):
    s = service.save_segment(_hid(), _actor(), _body(), parent_id=segment_id)
    _audit("segment_versioned", s["id"], {"name": s["name"], "version": s["version"]})
    return jsonify(s)


@patient360_bp.post("/api/patient360/segments/<int:segment_id>/refresh")
@require_permissions(SEGMENTS)
@_errors
def segments_refresh(segment_id):
    result = service.refresh_snapshot(_hid(), segment_id)
    _audit("segment_refreshed", segment_id, {"members": result["member_count"]})
    return jsonify(result)


@patient360_bp.delete("/api/patient360/segments/<int:segment_id>")
@require_permissions(SEGMENTS)
@_errors
def segments_archive(segment_id):
    service.archive_segment(_hid(), segment_id)
    _audit("segment_archived", segment_id)
    return jsonify({"archived": True})
