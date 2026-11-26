-- One-off cleanup: the hospital's own registration WhatsApp message ("Dear X, you are registered at ...
-- Helpline ... Your health record: <link>") was saved as the patient's symptoms by the WhatsApp webhook.
-- The webhook no longer does this and Patient 360 hides it, but the stored text is still in these columns.
--
-- Run in psql. Step 1 only previews. Step 2 clears the text inside a transaction; check the counts, then COMMIT.

-- Step 1: preview what would change
SELECT 'patients' AS tbl, id::text, symptoms FROM patients
 WHERE symptoms ~* 'you are registered at|OP timings\s*:'
UNION ALL
SELECT 'encounters', id::text, symptoms FROM encounters
 WHERE symptoms ~* 'you are registered at|OP timings\s*:'
UNION ALL
SELECT 'patient_messages', id::text, body FROM patient_messages
 WHERE kind = 'symptoms' AND body ~* 'you are registered at|OP timings\s*:'
UNION ALL
SELECT 'patient_feedback', id::text, comment FROM patient_feedback
 WHERE comment ~* 'you are registered at|OP timings\s*:';

-- Step 2: clear it (patient_messages / patient_feedback rows are removed from the symptom feed by re-kinding,
-- not deleted, so the communication log keeps a record)
BEGIN;
UPDATE patients   SET symptoms = NULL WHERE symptoms ~* 'you are registered at|OP timings\s*:';
UPDATE encounters SET symptoms = NULL WHERE symptoms ~* 'you are registered at|OP timings\s*:';
UPDATE patient_messages SET kind = 'registration_echo'
 WHERE kind = 'symptoms' AND body ~* 'you are registered at|OP timings\s*:';
UPDATE patient_feedback SET comment = NULL WHERE comment ~* 'you are registered at|OP timings\s*:';
-- Review the row counts above, then:
-- COMMIT;   -- or ROLLBACK;
