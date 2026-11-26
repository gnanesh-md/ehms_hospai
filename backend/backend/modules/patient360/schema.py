"""Patient 360 coordination tables.

Patient 360 is a coordination layer over the existing HMS: it stores *coordination state* (enrollments,
journeys, checkpoints, tasks, surveys, authorizations, segments) and links to source records by their canonical
`patients.patient_id` / admission id. It deliberately does NOT copy the clinical record and does not add a second
patient master. Things the HMS already has are reused, not recreated:

  * audit trail          -> audit_logs (module_name = 'patient360'), written with shared.audit.log_audit_event
  * communication log    -> patient_messages (modules/messaging)
  * timeline sources     -> utils.database.get_patient_journey + admissions + the tables below
  * NL filtering/files   -> modules/bulk_import (PatientReach AI screen)

Called from utils.database.init_database(). Every statement is idempotent (IF NOT EXISTS).
"""

PATIENT360_DDL = [
    # ── Enrollment: one row per finalized discharge event (idempotent on the event key, AC-01) ─────────
    """
    CREATE TABLE IF NOT EXISTS patient360_enrollments (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        admission_id INTEGER,
        discharge_event_key TEXT NOT NULL,
        source_module TEXT NOT NULL DEFAULT 'beds',
        ward TEXT,
        discharged_at TIMESTAMP NOT NULL,
        stay_days REAL,
        status TEXT NOT NULL DEFAULT 'eligible'
            CHECK (status IN ('eligible', 'pending_review', 'enrolled', 'excluded', 'cancelled', 'completed')),
        exclusion_reason TEXT,
        journey_id INTEGER,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (hospital_id, discharge_event_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_enroll_patient ON patient360_enrollments (hospital_id, patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_p360_enroll_status ON patient360_enrollments (hospital_id, status, discharged_at DESC)",
    # ── Journey templates: versioned, approval-gated ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS care_journey_templates (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        template_key TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        name TEXT NOT NULL,
        pathway TEXT NOT NULL DEFAULT 'general_discharge',
        trigger_event TEXT NOT NULL DEFAULT 'discharge.finalized',
        eligibility TEXT NOT NULL DEFAULT '{}',
        checkpoints TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'pending_approval', 'approved', 'published', 'retired')),
        change_log TEXT,
        created_by TEXT,
        submitted_at TIMESTAMP,
        approved_by TEXT,
        approved_at TIMESTAMP,
        published_at TIMESTAMP,
        retired_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (hospital_id, template_key, version)
    )
    """,
    # ── Journey instances / checkpoints ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS care_journey_instances (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        enrollment_id INTEGER NOT NULL,
        template_id INTEGER NOT NULL,
        template_key TEXT NOT NULL,
        template_version INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'active', 'paused', 'completed', 'cancelled', 'closed', 'failed', 'needs_review')),
        assigned_to TEXT,
        started_at TIMESTAMP,
        paused_at TIMESTAMP,
        closed_at TIMESTAMP,
        state_reason TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (hospital_id, enrollment_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_journey_patient ON care_journey_instances (hospital_id, patient_id, state)",
    """
    CREATE TABLE IF NOT EXISTS care_checkpoints (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        journey_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        checkpoint_key TEXT NOT NULL,
        label TEXT NOT NULL,
        action_type TEXT NOT NULL,
        spec TEXT NOT NULL DEFAULT '{}',
        due_at TIMESTAMP NOT NULL,
        status TEXT NOT NULL DEFAULT 'scheduled'
            CHECK (status IN ('scheduled', 'queued', 'sent', 'delivered', 'completed', 'skipped', 'failed', 'cancelled', 'overdue')),
        eligibility_at_send TEXT,
        task_id INTEGER,
        completed_at TIMESTAMP,
        completion_note TEXT,
        UNIQUE (journey_id, checkpoint_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_cp_due ON care_checkpoints (hospital_id, status, due_at)",
    # ── Tasks (the accountable follow-up work) + their history ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS care_tasks (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        journey_id INTEGER,
        checkpoint_id INTEGER,
        source_type TEXT NOT NULL DEFAULT 'manual',
        source_ref TEXT,
        source_key TEXT,
        category TEXT NOT NULL DEFAULT 'follow_up',
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
        title TEXT NOT NULL,
        description TEXT,
        owner_team TEXT,
        assigned_to TEXT,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'assigned', 'acknowledged', 'in_progress', 'escalated', 'snoozed', 'resolved', 'cancelled')),
        due_at TIMESTAMP,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        acknowledged_at TIMESTAMP,
        resolved_at TIMESTAMP,
        resolution_action TEXT,
        resolution_note TEXT,
        UNIQUE (hospital_id, source_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_task_patient ON care_tasks (hospital_id, patient_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_p360_task_queue ON care_tasks (hospital_id, status, due_at)",
    """
    CREATE TABLE IF NOT EXISTS care_task_events (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        actor TEXT,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_task_events ON care_task_events (task_id, created_at)",
    # ── Surveys: immutable published versions; responses are append-only ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS survey_templates (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        survey_key TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        language TEXT NOT NULL DEFAULT 'en',
        title TEXT NOT NULL,
        purpose TEXT,
        pathway TEXT,
        questions TEXT NOT NULL DEFAULT '[]',
        escalation_rules TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'pending_approval', 'approved', 'published', 'retired')),
        created_by TEXT,
        submitted_at TIMESTAMP,
        approved_by TEXT,
        approved_at TIMESTAMP,
        published_at TIMESTAMP,
        retired_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (hospital_id, survey_key, version, language)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS survey_responses (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        journey_id INTEGER,
        checkpoint_id INTEGER,
        survey_id INTEGER NOT NULL,
        survey_key TEXT NOT NULL,
        survey_version INTEGER NOT NULL,
        answers TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'staff_entry',
        provider_message_id TEXT,
        captured_by TEXT,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'under_review', 'reviewed')),
        reviewed_by TEXT,
        reviewed_at TIMESTAMP,
        review_note TEXT,
        triggered_task_ids TEXT,
        ai_summary TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_resp_patient ON survey_responses (hospital_id, patient_id, submitted_at DESC)",
    # ── Communication authorization (append-only history) + preferences ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS communication_authorizations (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        purpose TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('active', 'withdrawn', 'expired', 'pending_verification', 'not_granted')),
        source TEXT NOT NULL DEFAULT 'staff_recorded',
        policy_version TEXT,
        note TEXT,
        recorded_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_auth ON communication_authorizations (hospital_id, patient_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS patient360_preferences (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        preferred_language TEXT,
        preferred_channel TEXT,
        contact_from_hour INTEGER,
        contact_to_hour INTEGER,
        updated_by TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (hospital_id, patient_id)
    )
    """,
    # ── Segments (versioned) and snapshot memberships ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS patient_segments (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        segment_key TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        name TEXT NOT NULL,
        description TEXT,
        criteria TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'dynamic' CHECK (mode IN ('dynamic', 'snapshot')),
        owner TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        archived_at TIMESTAMP,
        UNIQUE (hospital_id, segment_key, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS segment_memberships (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        segment_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        reasons TEXT,
        evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_seg_members ON segment_memberships (segment_id, patient_id)",
    # ── Coordinator notes: internal, append-only free text about a patient (separate from tasks) ─────────
    """
    CREATE TABLE IF NOT EXISTS care_notes (
        id SERIAL PRIMARY KEY,
        hospital_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        note TEXT NOT NULL,
        author TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_p360_notes_patient ON care_notes (hospital_id, patient_id, created_at)",
]


def ensure_patient360_tables(conn):
    """Create the Patient 360 tables. `conn` is the init_database connection (autocommit)."""
    cursor = conn.cursor()
    for statement in PATIENT360_DDL:
        cursor.execute(statement)
    conn.commit()
