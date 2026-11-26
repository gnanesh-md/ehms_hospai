"""Comprehensive test suite for Centralized Doctor Availability & Assignment Engine.

Covers all 14 required business scenarios:
1. Doctor logs in and is scheduled for duty: assignment allowed.
2. Doctor is on approved leave: assignment blocked.
3. Doctor is logged out: assignment blocked.
4. Doctor's session expires: assignment blocked.
5. Doctor logs in but is not scheduled for duty: assignment blocked.
6. Doctor's shift ends: new assignments blocked.
7. Doctor returns from leave and logs in on a scheduled working day: assignment allowed.
8. Doctor has reached workload capacity: assignment blocked.
9. Multiple doctors are available: workload-balancing strategy (least assigned).
10. No doctors are available: patient remains in unassigned queue.
11. Date changes at midnight: dynamic recalculation across midnight.
12. Overnight shift: doctor remains eligible across midnight during shift.
13. Doctor logs out with existing patients: existing assignments remain intact.
14. Doctor is available in one department but not another: department restrictions enforced.
"""

import datetime
import unittest
from zoneinfo import ZoneInfo

from utils.database import (
    get_connection,
    ensure_doctor_availability_tables,
)
from services.doctor_availability_service import (
    is_time_within_shift,
    get_hospital_now,
    is_doctor_eligible_for_assignment,
    get_eligible_doctors_for_department,
    HOSPITAL_TIMEZONE,
)


class TestShiftCalculation(unittest.TestCase):
    """Tests for normal, multi-day, and overnight shifts crossing midnight."""

    def test_same_day_shift_within_window(self):
        check_time = datetime.time(12, 30)
        self.assertTrue(is_time_within_shift(check_time, "09:00", "17:00", is_overnight=False))

    def test_same_day_shift_before_window(self):
        check_time = datetime.time(8, 30)
        self.assertFalse(is_time_within_shift(check_time, "09:00", "17:00", is_overnight=False))

    def test_same_day_shift_after_window(self):
        check_time = datetime.time(17, 30)
        self.assertFalse(is_time_within_shift(check_time, "09:00", "17:00", is_overnight=False))

    def test_overnight_shift_before_midnight(self):
        check_time = datetime.time(22, 30)
        self.assertTrue(is_time_within_shift(check_time, "20:00", "04:00", is_overnight=True))

    def test_overnight_shift_after_midnight(self):
        check_time = datetime.time(2, 15)
        self.assertTrue(is_time_within_shift(check_time, "20:00", "04:00", is_overnight=True))

    def test_overnight_shift_outside_window(self):
        check_time = datetime.time(10, 0)
        self.assertFalse(is_time_within_shift(check_time, "20:00", "04:00", is_overnight=True))


class TestDoctorEligibilityRules(unittest.TestCase):
    """Integration tests for all 14 assignment engine scenarios against database."""

    @classmethod
    def setUpClass(cls):
        try:
            with get_connection(autocommit=True) as conn:
                ensure_doctor_availability_tables(conn)
        except Exception as exc:
            raise unittest.SkipTest(f"needs a reachable PostgreSQL (DATABASE_URL): {exc}")

    def setUp(self):
        """Creates an isolated test doctor with user account and active schedule."""
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            unique_id = int(datetime.datetime.now().timestamp() * 1000) % 10000000
            self.username = f"test_doc_{unique_id}"
            self.doc_name = f"Dr. Test {unique_id}"
            
            # Create user
            cursor.execute(
                """
                INSERT INTO users (username, password_hash, full_name, role, access_role, status)
                VALUES (%s, 'dummy_hash', %s, 'staff', 'clinician', 'active')
                RETURNING id;
                """,
                (self.username, self.doc_name),
            )
            self.user_id = cursor.fetchone()[0]

            # Create doctor
            cursor.execute(
                """
                INSERT INTO doctors (
                    doctor_name, department, user_id, status,
                    max_workload, is_manually_unavailable, working_days,
                    duty_start_time, duty_end_time, schedule_active
                )
                VALUES (%s, %s, %s, 'available', 10, FALSE,
                        'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday', '00:00', '23:59', TRUE)
                RETURNING id;
                """,
                (self.doc_name, "Cardiology", self.user_id),
            )
            self.doctor_id = cursor.fetchone()[0]

    def tearDown(self):
        """Cleans up created test records."""
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE user_id = %s;", (self.user_id,))
            cursor.execute("DELETE FROM leave_requests WHERE employee_id = %s OR doctor_id = %s;", (str(self.user_id), self.doctor_id))
            cursor.execute("DELETE FROM appointments WHERE doctor_name = %s;", (self.doc_name,))
            cursor.execute("DELETE FROM doctors WHERE id = %s;", (self.doctor_id,))
            cursor.execute("DELETE FROM users WHERE id = %s;", (self.user_id,))

    def _create_active_session(self, user_id, expires_in_hours=4):
        now = datetime.datetime.now(ZoneInfo("UTC"))
        expires_at = now + datetime.timedelta(hours=expires_in_hours)
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            token = f"test_token_{user_id}_{int(now.timestamp())}"
            cursor.execute(
                """
                INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, expires_at)
                VALUES (%s, %s, '127.0.0.1', 'pytest', %s)
                RETURNING id;
                """,
                (user_id, token, expires_at),
            )
            return token

    def _expire_sessions(self, user_id):
        past = datetime.datetime.now(ZoneInfo("UTC")) - datetime.timedelta(hours=2)
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE sessions SET expires_at = %s WHERE user_id = %s;", (past, user_id))

    def _delete_sessions(self, user_id):
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE user_id = %s;", (user_id,))

    # Scenario 1: Doctor logs in and is scheduled for duty: assignment allowed.
    def test_scenario_1_logged_in_and_scheduled_allowed(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()
        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertTrue(is_eligible, f"Expected doctor to be eligible, but got: {reason}")
        self.assertEqual(details["status"], "available")

    # Scenario 2: Doctor is on approved leave: assignment blocked.
    def test_scenario_2_approved_leave_blocked(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()
        today_str = now_dt.strftime("%Y-%m-%d")

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO leave_requests (doctor_id, employee_id, leave_type, start_date, end_date, status)
                VALUES (%s, %s, 'Casual Leave', %s, %s, 'approved');
                """,
                (self.doctor_id, str(self.user_id), today_str, today_str),
            )

        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "on_leave")
        self.assertIn("leave", reason.lower())

    # Scenario 3: Doctor is logged out: assignment blocked.
    def test_scenario_3_logged_out_blocked(self):
        self._delete_sessions(self.user_id)
        now_dt = get_hospital_now()
        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "offline")
        self.assertTrue("offline" in reason.lower() or "logged out" in reason.lower() or "session" in reason.lower())

    # Scenario 4: Doctor's session expires: assignment blocked.
    def test_scenario_4_expired_session_blocked(self):
        self._create_active_session(self.user_id)
        self._expire_sessions(self.user_id)

        now_dt = get_hospital_now()
        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "offline")
        self.assertTrue("session" in reason.lower() or "expired" in reason.lower() or "offline" in reason.lower())

    # Scenario 5: Doctor logs in but is not scheduled for duty: assignment blocked.
    def test_scenario_5_logged_in_not_scheduled_day_blocked(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()
        current_day = now_dt.strftime("%A")
        other_day = "Sunday" if current_day != "Sunday" else "Monday"

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE doctors SET working_days = %s WHERE id = %s;",
                (other_day, self.doctor_id),
            )

        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "off_duty")
        self.assertTrue("scheduled" in reason.lower() or "off duty" in reason.lower())

    # Scenario 6: Doctor's shift ends: new assignments blocked.
    def test_scenario_6_shift_ended_blocked(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()
        shift_start = (now_dt - datetime.timedelta(hours=2)).strftime("%H:%M")
        shift_end = (now_dt - datetime.timedelta(minutes=30)).strftime("%H:%M")

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE doctors
                SET duty_start_time = %s, duty_end_time = %s,
                    working_days = 'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday'
                WHERE id = %s;
                """,
                (shift_start, shift_end, self.doctor_id),
            )

        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "off_duty")
        self.assertTrue("outside scheduled duty hours" in reason.lower() or "off duty" in reason.lower())

    # Scenario 7: Doctor returns from leave and logs in on a scheduled working day: assignment allowed.
    def test_scenario_7_return_from_leave_allowed(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()
        yesterday_str = (now_dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO leave_requests (doctor_id, employee_id, leave_type, start_date, end_date, status)
                VALUES (%s, %s, 'Sick Leave', %s, %s, 'approved');
                """,
                (self.doctor_id, str(self.user_id), yesterday_str, yesterday_str),
            )

        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertTrue(is_eligible, f"Expected doctor to be eligible, but got: {reason}")
        self.assertEqual(details["status"], "available")

    # Scenario 8: Doctor has reached workload capacity: assignment blocked.
    def test_scenario_8_workload_capacity_reached_blocked(self):
        self._create_active_session(self.user_id)

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE doctors SET max_workload = 1 WHERE id = %s;", (self.doctor_id,))
            now_dt = get_hospital_now()
            cursor.execute(
                """
                INSERT INTO appointments (
                    patient_name, visit_type, department, doctor_name,
                    appointment_date, token_no, status, hospital_id
                )
                VALUES ('Patient Cap Test', 'OP', 'Cardiology', %s, %s, 1, 'scheduled', 1);
                """,
                (self.doc_name, now_dt),
            )

        is_eligible, reason, details = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=now_dt)
        self.assertFalse(is_eligible)
        self.assertEqual(details["status"], "busy")
        self.assertTrue("busy" in reason.lower() or "workload" in reason.lower() or "capacity" in reason.lower())

    # Scenario 9: Multiple doctors are available: lowest workload doctor chosen.
    def test_scenario_9_workload_balancing(self):
        self._create_active_session(self.user_id)

        unique_id2 = int(datetime.datetime.now().timestamp() * 1000) % 10000000 + 1
        username2 = f"test_doc2_{unique_id2}"
        doc2_name = f"Dr. Doc2 Test {unique_id2}"

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (username, password_hash, full_name, role, access_role, status)
                VALUES (%s, 'hash', %s, 'staff', 'clinician', 'active')
                RETURNING id;
                """,
                (username2, doc2_name),
            )
            user2_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO doctors (
                    doctor_name, department, user_id, status,
                    max_workload, working_days, duty_start_time, duty_end_time, schedule_active
                )
                VALUES (%s, 'Cardiology', %s, 'available', 20,
                        'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday', '00:00', '23:59', TRUE)
                RETURNING id;
                """,
                (doc2_name, user2_id),
            )
            doc2_id = cursor.fetchone()[0]

        try:
            self._create_active_session(user2_id)
            now_dt = get_hospital_now()
            with get_connection(autocommit=True) as conn:
                cursor = conn.cursor()
                # Assign 2 appointments to doctor 1, none to doctor 2
                cursor.execute(
                    """
                    INSERT INTO appointments (patient_name, visit_type, department, doctor_name, appointment_date, token_no, status, hospital_id)
                    VALUES ('Patient 1', 'OP', 'Cardiology', %s, %s, 101, 'scheduled', 1),
                           ('Patient 2', 'OP', 'Cardiology', %s, %s, 102, 'checked_in', 1);
                    """,
                    (self.doc_name, now_dt, self.doc_name, now_dt),
                )

            eligible = get_eligible_doctors_for_department("Cardiology")
            eligible_ids = [d["doctor_id"] for d in eligible]
            self.assertIn(doc2_id, eligible_ids)
            self.assertIn(self.doctor_id, eligible_ids)
            # Doctor 2 has current_workload = 0 while Doctor 1 has current_workload = 2
            first_doctor = eligible[0]
            self.assertEqual(first_doctor["doctor_id"], doc2_id)
            self.assertEqual(first_doctor["current_workload"], 0)
        finally:
            with get_connection(autocommit=True) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM sessions WHERE user_id = %s;", (user2_id,))
                cursor.execute("DELETE FROM appointments WHERE doctor_name = %s;", (doc2_name,))
                cursor.execute("DELETE FROM doctors WHERE id = %s;", (doc2_id,))
                cursor.execute("DELETE FROM users WHERE id = %s;", (user2_id,))

    # Scenario 10: No doctors are available: retained in unassigned queue.
    def test_scenario_10_no_doctors_available_unassigned(self):
        eligible = get_eligible_doctors_for_department("NonExistentDepartment_XYZ")
        self.assertEqual(len(eligible), 0)

    # Scenario 11: Date changes at midnight: dynamic recalculation.
    def test_scenario_11_dynamic_recalculation_on_date_change(self):
        self._create_active_session(self.user_id)

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE doctors SET working_days = 'Wednesday' WHERE id = %s;",
                (self.doctor_id,),
            )

        wednesday_dt = datetime.datetime(2026, 11, 25, 10, 0, 0, tzinfo=HOSPITAL_TIMEZONE)
        is_wed_ok, _, details_wed = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=wednesday_dt)
        self.assertTrue(is_wed_ok)
        self.assertEqual(details_wed["status"], "available")

        thursday_dt = datetime.datetime(2026, 11, 26, 0, 1, 0, tzinfo=HOSPITAL_TIMEZONE)
        is_thu_ok, _, details_thu = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=thursday_dt)
        self.assertFalse(is_thu_ok)
        self.assertEqual(details_thu["status"], "off_duty")

    # Scenario 12: Overnight shift: doctor remains eligible during scheduled shift across midnight.
    def test_scenario_12_overnight_shift_across_midnight(self):
        self._create_active_session(self.user_id)

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE doctors
                SET duty_start_time = '20:00', duty_end_time = '04:00',
                    working_days = 'Wednesday', schedule_active = TRUE
                WHERE id = %s;
                """,
                (self.doctor_id,),
            )

        # Wednesday 22:00 -> Eligible
        wed_night = datetime.datetime(2026, 11, 25, 22, 0, 0, tzinfo=HOSPITAL_TIMEZONE)
        is_ok_pre, _, details_pre = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=wed_night)
        self.assertTrue(is_ok_pre)
        self.assertEqual(details_pre["status"], "available")

        # Thursday 02:30 -> Still Eligible
        thu_early = datetime.datetime(2026, 11, 26, 2, 30, 0, tzinfo=HOSPITAL_TIMEZONE)
        is_ok_post, _, details_post = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=thu_early)
        self.assertTrue(is_ok_post)
        self.assertEqual(details_post["status"], "available")

        # Thursday 05:00 -> Shift ended
        thu_after = datetime.datetime(2026, 11, 26, 5, 0, 0, tzinfo=HOSPITAL_TIMEZONE)
        is_ok_end, _, details_end = is_doctor_eligible_for_assignment(self.doctor_id, target_datetime=thu_after)
        self.assertFalse(is_ok_end)
        self.assertEqual(details_end["status"], "off_duty")

    # Scenario 13: Doctor logs out with existing patients: existing assignments remain intact.
    def test_scenario_13_logout_preserves_existing_assignments(self):
        self._create_active_session(self.user_id)
        now_dt = get_hospital_now()

        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO appointments (
                    patient_name, visit_type, department, doctor_name,
                    appointment_date, token_no, status, hospital_id
                )
                VALUES ('Patient Preserved', 'OP', 'Cardiology', %s, %s, 99, 'scheduled', 1)
                RETURNING id;
                """,
                (self.doc_name, now_dt),
            )
            appt_id = cursor.fetchone()[0]

        # Doctor logs out
        self._delete_sessions(self.user_id)

        # Verify existing patient assignment is intact
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT doctor_name, status FROM appointments WHERE id = %s;", (appt_id,))
            row = cursor.fetchone()
            self.assertEqual(row[0], self.doc_name)
            self.assertEqual(row[1], "scheduled")

        # Verify new assignments are blocked
        is_eligible, _, _ = is_doctor_eligible_for_assignment(self.doctor_id)
        self.assertFalse(is_eligible)

    # Scenario 14: Doctor is available in one department but not another: department restrictions enforced.
    def test_scenario_14_department_restrictions(self):
        self._create_active_session(self.user_id)

        cardio_eligible = get_eligible_doctors_for_department("Cardiology")
        cardio_ids = [d["doctor_id"] for d in cardio_eligible]
        self.assertIn(self.doctor_id, cardio_ids)

        neuro_eligible = get_eligible_doctors_for_department("Neurology")
        neuro_ids = [d["doctor_id"] for d in neuro_eligible]
        self.assertNotIn(self.doctor_id, neuro_ids)


if __name__ == "__main__":
    unittest.main()
