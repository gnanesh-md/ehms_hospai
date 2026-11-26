"""Patient 360 rules: the decisions that must hold regardless of the database (spec section 19 acceptance criteria)."""
from datetime import datetime

import pytest

from modules.patient360 import rules as r


# ── State machines ──────────────────────────────────────────────────────────────────────────────
def test_approval_cannot_skip_review():
    assert r.can_transition(r.APPROVAL_TRANSITIONS, "draft", "pending_approval")
    assert not r.can_transition(r.APPROVAL_TRANSITIONS, "draft", "published")
    assert not r.can_transition(r.APPROVAL_TRANSITIONS, "draft", "approved")
    assert not r.can_transition(r.APPROVAL_TRANSITIONS, "retired", "published")


def test_closed_journey_is_final_and_pause_resume_works():
    assert r.can_transition(r.JOURNEY_TRANSITIONS, "active", "paused")
    assert r.can_transition(r.JOURNEY_TRANSITIONS, "paused", "active")
    assert not r.can_transition(r.JOURNEY_TRANSITIONS, "closed", "active")
    assert not r.can_transition(r.JOURNEY_TRANSITIONS, "completed", "active")


def test_resolved_task_cannot_reopen():
    assert not r.can_transition(r.TASK_TRANSITIONS, "resolved", "open")
    assert r.can_transition(r.TASK_TRANSITIONS, "snoozed", "open")


# ── Checkpoints ─────────────────────────────────────────────────────────────────────────────────
def test_checkpoints_validate_and_compute_due_time():
    cps = r.validate_checkpoints([
        {"key": "day1", "label": "Day 1", "action_type": "message", "offset_days": 1, "message_text": "How are you feeling?"},
        {"key": "day14", "action_type": "call_task", "offset_days": 14, "offset_hours": 6},
    ])
    assert r.compute_due_at(datetime(2026, 11, 1, 9), cps[0]) == datetime(2026, 11, 2, 9)
    assert r.compute_due_at(datetime(2026, 11, 1, 9), cps[1]) == datetime(2026, 11, 15, 15)


@pytest.mark.parametrize("bad", [
    [],
    [{"key": "a", "action_type": "nope"}],
    [{"key": "a", "action_type": "call_task"}, {"key": "a", "action_type": "call_task"}],
    [{"key": "a", "action_type": "message", "offset_days": 1}],  # message needs approved text
    [{"key": "a", "action_type": "survey"}],  # survey needs a survey_key
    [{"key": "a", "action_type": "call_task", "offset_days": 999}],
    [{"key": "Bad Key!", "action_type": "call_task"}],
])
def test_bad_checkpoints_rejected(bad):
    with pytest.raises(ValueError):
        r.validate_checkpoints(bad)


def test_ward_scoped_template_never_matches_unknown_ward():
    elig = r.validate_eligibility({"wards": ["Surgical Ward"], "min_stay_days": 1})
    assert r.template_matches(elig, "surgical ward", 3)[0]
    assert not r.template_matches(elig, None, 3)[0]
    assert not r.template_matches(elig, "Surgical Ward", 0.2)[0]


# ── Surveys ─────────────────────────────────────────────────────────────────────────────────────
QUESTIONS = r.validate_questions([
    {"id": "fever", "text": "Do you have a fever?", "type": "yes_no"},
    {"id": "pain", "text": "Pain 0-10", "type": "rating", "min": 0, "max": 10},
    {"id": "detail", "text": "Tell us more", "type": "free_text", "required": True, "show_if": {"question_id": "fever", "equals": "yes"}},
])


def test_branching_hides_and_drops_answers():
    out = r.validate_answers(QUESTIONS, {"fever": "no", "pain": 3})
    assert out == {"fever": "no", "pain": 3}
    with pytest.raises(ValueError):  # shown + required once fever == yes
        r.validate_answers(QUESTIONS, {"fever": "yes", "pain": 3})


def test_answers_reject_unknown_ids_and_out_of_range():
    with pytest.raises(ValueError):
        r.validate_answers(QUESTIONS, {"fever": "no", "pain": 3, "injected": "x"})
    with pytest.raises(ValueError):
        r.validate_answers(QUESTIONS, {"fever": "no", "pain": 11})


def test_branching_cannot_look_forward():
    with pytest.raises(ValueError):
        r.validate_questions([
            {"id": "a", "text": "A", "type": "yes_no", "show_if": {"question_id": "b", "equals": "yes"}},
            {"id": "b", "text": "B", "type": "yes_no"},
        ])


def test_escalation_is_deterministic_and_keeps_original_answer():
    rules = r.validate_escalation_rules([
        {"question_id": "fever", "operator": "eq", "value": "yes", "priority": "urgent", "due_hours": 4},
        {"question_id": "pain", "operator": "gte", "value": 8, "priority": "high"},
    ], QUESTIONS)
    answers = r.validate_answers(QUESTIONS, {"fever": "yes", "pain": 9, "detail": "since last night"})
    hit = r.evaluate_escalations(rules, answers)
    assert [h["priority"] for h in hit] == ["urgent", "high"]
    assert answers["pain"] == 9  # untouched
    calm = r.validate_answers(QUESTIONS, {"fever": "no", "pain": 2})
    assert r.evaluate_escalations(rules, calm) == []


def test_escalation_rule_must_reference_a_real_question():
    with pytest.raises(ValueError):
        r.validate_escalation_rules([{"question_id": "ghost", "operator": "eq", "value": "yes"}], QUESTIONS)


# ── Consent / eligibility (AC-08, AC-11) ────────────────────────────────────────────────────────
def A(status, channel="whatsapp", purpose="clinical_followup", at="2026-11-01T10:00:00", i=1):
    return {"id": i, "channel": channel, "purpose": purpose, "status": status, "created_at": at}


def test_fail_closed_without_active_authorization():
    d = r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=[])
    assert not d["eligible"] and d["reason"] == "no_active_authorization"


def test_active_authorization_allows_and_is_purpose_separated():
    rows = [A("active")]
    assert r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=rows)["eligible"]
    # clinical consent is NOT marketing consent (spec 8.5)
    assert not r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="marketing", auth_rows=rows)["eligible"]
    assert not r.check_send_eligibility(phone="9876543210", channel="sms", purpose="clinical_followup", auth_rows=rows)["eligible"]


def test_latest_decision_wins_and_withdrawal_blocks():
    rows = [A("active", i=1, at="2026-11-01T10:00:00"), A("withdrawn", i=2, at="2026-11-02T10:00:00")]
    assert r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=rows)["reason"] == "opted_out"
    rows.append(A("active", i=3, at="2026-11-03T10:00:00"))
    assert r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=rows)["eligible"]


def test_blanket_stop_blocks_every_purpose():
    rows = [A("active"), A("withdrawn", purpose="all", i=2, at="2026-11-05T10:00:00")]
    for purpose in r.PURPOSES:
        assert r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose=purpose, auth_rows=rows, require_active=False)["reason"] == "opted_out"


def test_desk_mode_only_blocks_explicit_withdrawal():
    ok = r.check_send_eligibility(phone="9876543210", channel="sms", purpose="clinical_followup", auth_rows=[], require_active=False)
    assert ok["eligible"]  # no record: existing desk behaviour unchanged


def test_bad_phone_and_contact_window():
    assert r.check_send_eligibility(phone="12", channel="sms", purpose="survey", auth_rows=[A("active", channel="sms", purpose="survey")])["reason"] == "missing_or_invalid_contact"
    rows = [A("active")]
    night = datetime(2026, 11, 24, 18, 30)  # 00:00 IST
    d = r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=rows, contact_window=(9, 20), now_utc=night)
    assert d["reason"] == "outside_contact_window" and d["deferrable"]
    noon = datetime(2026, 11, 24, 6, 30)  # 12:00 IST
    assert r.check_send_eligibility(phone="9876543210", channel="whatsapp", purpose="clinical_followup", auth_rows=rows, contact_window=(9, 20), now_utc=noon)["eligible"]


def test_stop_words():
    assert r.is_opt_out_message("STOP") and r.is_opt_out_message(" stop. ")
    assert not r.is_opt_out_message("please don't stop the medicine")
    assert r.normalize_phone("98765 43210") == "919876543210"


# ── Segment compiler (AC-04): allow-list, parameters, no model SQL ──────────────────────────────
def test_compiles_to_parameterised_sql():
    where, params, used = r.compile_criteria({"logic": "and", "conditions": [
        {"field": "days_since_discharge", "op": "lte", "value": 7},
        {"field": "has_active_journey", "op": "eq", "value": False},
    ]})
    assert params == [7, 0] and used == ["days_since_discharge", "has_active_journey"]
    assert "7" not in where  # values are bound, never interpolated


@pytest.mark.parametrize("bad", [
    {"field": "password", "op": "eq", "value": "x"},
    {"field": "age", "op": "contains", "value": "5"},
    {"field": "age", "op": "gt", "value": "1; DROP TABLE patients"},
    {"field": "gender", "op": "eq", "value": "robot"},
    {"field": "age", "op": "gt", "value": 10 ** 9},
    {"field": "has_phone", "op": "eq", "value": "maybe"},
])
def test_rejects_unknown_fields_ops_and_values(bad):
    with pytest.raises(ValueError):
        r.compile_criteria({"logic": "and", "conditions": [bad]})


def test_limits_on_depth_and_size():
    node = {"field": "age", "op": "gt", "value": 1}
    deep = node
    for _ in range(5):
        deep = {"logic": "and", "conditions": [deep]}
    with pytest.raises(ValueError):
        r.compile_criteria(deep)
    with pytest.raises(ValueError):
        r.compile_criteria({"logic": "or", "conditions": [node] * 21})


def test_or_groups_and_in_operator():
    where, params, _ = r.compile_criteria({"logic": "or", "conditions": [
        {"field": "gender", "op": "in", "value": ["male", "female"]}, {"field": "age", "op": "gte", "value": 60}]})
    assert " OR " in where and params == ["male", "female", 60]


def test_ai_proposal_with_any_invalid_condition_is_flagged():
    criteria, rejected = r.criteria_from_ai({"logic": "AND", "conditions": [
        {"field": "age", "op": "gt", "value": 50}, {"field": "salary", "op": "gt", "value": 1}]})
    assert criteria["conditions"] == [{"field": "age", "op": "gt", "value": 50}]
    assert rejected and rejected[0]["condition"]["field"] == "salary"  # caller must not execute: AC-04
    assert r.criteria_from_ai({"conditions": []})[0] is None
    assert r.criteria_from_ai("DROP TABLE patients")[0] is None


def test_match_reasons_explain_each_condition():
    crit = {"logic": "and", "conditions": [{"field": "age", "op": "gte", "value": 50}, {"field": "has_phone", "op": "eq", "value": True}]}
    conds = r.flatten_conditions(crit)
    assert r.condition_holds("age", "gte", 54, 50) and not r.condition_holds("age", "gte", 40, 50)
    assert not r.condition_holds("age", "gte", None, 50)
    assert "Age >= 50 (patient: 54)" == r.describe_condition(conds[0], 54)
    assert r.describe_condition(conds[1], 1).startswith("Has a phone number is yes")


# ── Symptom text must be what the patient reported ──────────────────────────────────────────────
REGISTRATION_ECHO = ("Dear A B, you are registered at Imperial Hospitals. UMR PAT-1, OP No. OP034, Department. OP timings: Mon-Sat, 9:00 AM - 5:00 PM. "
                     "Please wait to be called. Helpline 08816-279999. Your health record: http://192.168.1.235:8443/#/card/abc")


def test_registration_message_is_not_a_symptom():
    assert r.clean_symptom_text(REGISTRATION_ECHO) is None


def test_boilerplate_is_stripped_from_real_symptoms():
    assert r.clean_symptom_text("fever and cough since 2 days. Helpline 08816-279999. Your health record: http://x/y") == "fever and cough since 2 days"
    assert r.clean_symptom_text("chest pain") == "chest pain"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_symptoms_are_none(empty):
    assert r.clean_symptom_text(empty) is None
