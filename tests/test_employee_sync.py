"""Tests for the daily reconciliation against the legacy Bahwan HR system.

The legacy service lives on the customer's internal network, so the tests
stub the HTTP layer and exercise the eligibility domain, the per-employee
mapping and the outcome flagging on top of a canned payload.
"""
from unittest.mock import patch

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

EMPLOYEE_PATH = (
    "odoo.addons.rental_management_hr_sync.models.hr_employee.HrEmployee"
)

PAYLOAD = {
    "companyCode": "B",
    "empCode": 9483,
    "empName": "MOHAMED NAINAR BILAL KAREEM",
    "empDeptCode": "10",
    "empDeptName": "IT",
    "locationCode": "01",
    "locationName": "WATTAYAH",
}


@tagged("employee_sync", "rental_management_hr_sync")
class TestLegacyEmployeeSync(TransactionCase):
    """Reconciliation behaviour for employees held in both systems."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Employee = cls.env["hr.employee"]
        cls.company = cls.env.company
        cls.company.legacy_company_code = "B"
        cls._configure()
        cls.employee = cls.Employee.create(
            {
                "name": "Stale Name",
                "employee_code": "9483",
                "company_id": cls.company.id,
            }
        )

    @classmethod
    def _configure(cls, **overrides):
        values = {
            "enabled": "True",
            "base_url": "http://5.37.56.75:8082",
            "employee_path": "/api/Employee/{company_code},{employee_code}",
            "timeout": "30",
            "batch_limit": "0",
        }
        values.update(overrides)
        params = cls.env["ir.config_parameter"].sudo()
        for key, value in values.items():
            params.set_param(f"rental_management_hr_sync.{key}", value)

    def _cron(self, payload=PAYLOAD, side_effect=None):
        kwargs = (
            {"side_effect": side_effect}
            if side_effect is not None
            else {"return_value": payload}
        )
        with patch(f"{EMPLOYEE_PATH}._fetch_legacy_employee", **kwargs) as fetch:
            counters = self.Employee.cron_sync_legacy_employees()
        return counters, fetch

    # -- mapping ----------------------------------------------------------
    def test_existing_employee_is_refreshed_from_the_api(self):
        counters, _fetch = self._cron()
        self.assertEqual(counters["synced"], 1)
        self.employee.invalidate_recordset()
        self.assertEqual(self.employee.name, "MOHAMED NAINAR BILAL KAREEM")
        self.assertEqual(self.employee.emp_company_code, "B")
        self.assertEqual(self.employee.emp_dept_code, "10")
        self.assertEqual(self.employee.emp_dept_name, "IT")
        self.assertEqual(self.employee.location_code, "01")
        self.assertEqual(self.employee.location_name, "WATTAYAH")
        self.assertEqual(self.employee.legacy_sync_status, "synced")
        self.assertFalse(self.employee.legacy_sync_message)
        self.assertTrue(self.employee.legacy_sync_date)

    def test_blank_legacy_values_do_not_wipe_odoo_data(self):
        self._cron()
        sparse = dict(PAYLOAD, empDeptName="", locationName=None)
        self._cron(payload=sparse)
        self.employee.invalidate_recordset()
        self.assertEqual(self.employee.emp_dept_name, "IT")
        self.assertEqual(self.employee.location_name, "WATTAYAH")

    def test_the_cron_never_creates_employees(self):
        before = self.Employee.search_count([])
        self._cron()
        self.assertEqual(self.Employee.search_count([]), before)

    # -- outcome flagging -------------------------------------------------
    def test_unknown_employee_is_flagged_not_archived(self):
        counters, _fetch = self._cron(payload=None)
        self.assertEqual(counters["not_found"], 1)
        self.employee.invalidate_recordset()
        self.assertEqual(self.employee.legacy_sync_status, "not_found")
        self.assertTrue(self.employee.legacy_sync_message)
        self.assertTrue(self.employee.active)
        self.assertEqual(self.employee.name, "Stale Name")

    def test_transport_failure_is_flagged_as_error(self):
        counters, _fetch = self._cron(
            side_effect=requests.exceptions.ConnectTimeout("boom")
        )
        self.assertEqual(counters["error"], 1)
        self.employee.invalidate_recordset()
        self.assertEqual(self.employee.legacy_sync_status, "error")
        self.assertIn("boom", self.employee.legacy_sync_message)

    def test_lookup_uses_the_code_from_the_employees_company(self):
        self.company.legacy_company_code = "X"
        self.addCleanup(self.company.write, {"legacy_company_code": "B"})
        counters, fetch = self._cron()
        self.assertEqual(counters["synced"], 1)
        self.assertEqual(fetch.call_args.args[-2], "X")

    def test_a_stored_mirror_code_does_not_override_the_company(self):
        # emp_company_code records what the API returned; it must not be used
        # as the lookup key.
        self.employee.emp_company_code = "OLD"
        _counters, fetch = self._cron()
        self.assertEqual(fetch.call_args.args[-2], "B")

    # -- eligibility ------------------------------------------------------
    def test_employees_without_a_code_are_not_eligible(self):
        self.Employee.create({"name": "No Code", "company_id": self.company.id})
        counters, fetch = self._cron()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(counters["synced"], 1)

    def test_company_without_a_legacy_code_is_skipped(self):
        self.company.legacy_company_code = False
        self.addCleanup(self.company.write, {"legacy_company_code": "B"})
        counters, fetch = self._cron()
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(counters, {"synced": 0, "not_found": 0, "error": 0})

    def test_batch_limit_takes_the_least_recently_synced_first(self):
        second = self.Employee.create(
            {
                "name": "Second",
                "employee_code": "9484",
                "company_id": self.company.id,
            }
        )
        self._configure(batch_limit="1")
        self.addCleanup(self._configure, batch_limit="0")

        _counters, fetch = self._cron()
        first_pass = fetch.call_args.args[-1]
        _counters, fetch = self._cron()
        second_pass = fetch.call_args.args[-1]

        self.assertEqual(fetch.call_count, 1)
        self.assertNotEqual(first_pass, second_pass)
        self.assertEqual({first_pass, second_pass}, {"9483", second.employee_code})

    # -- switches and URLs ------------------------------------------------
    def test_cron_is_a_no_op_while_disabled(self):
        self._configure(enabled="False")
        self.addCleanup(self._configure, enabled="True")
        _counters, fetch = self._cron()
        self.assertEqual(fetch.call_count, 0)

    def test_cron_is_a_no_op_without_a_base_url(self):
        self._configure(base_url="")
        self.addCleanup(self._configure, base_url="http://5.37.56.75:8082")
        _counters, fetch = self._cron()
        self.assertEqual(fetch.call_count, 0)

    def test_endpoint_url_matches_the_legacy_format(self):
        settings = self.Employee._legacy_sync_settings()
        self.assertEqual(
            self.Employee._legacy_endpoint_url(settings, "B", "9483"),
            "http://5.37.56.75:8082/api/Employee/B,9483",
        )

    # -- manual button ----------------------------------------------------
    def test_manual_button_raises_for_an_unknown_employee(self):
        with patch(f"{EMPLOYEE_PATH}._fetch_legacy_employee", return_value=None):
            with self.assertRaises(UserError):
                self.employee.action_sync_from_legacy_hr()

    def test_manual_button_refreshes_one_employee(self):
        with patch(f"{EMPLOYEE_PATH}._fetch_legacy_employee", return_value=PAYLOAD):
            action = self.employee.action_sync_from_legacy_hr()
        self.assertEqual(action["tag"], "display_notification")
        self.employee.invalidate_recordset()
        self.assertEqual(self.employee.legacy_sync_status, "synced")


@tagged("employee_sync", "rental_management_hr_sync")
class TestOccupyWizardLegacyLookup(TransactionCase):
    """The Occupy wizard's Sync button: find-or-create, then select."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Employee = cls.env["hr.employee"]
        cls.company = cls.env.company
        cls.company.legacy_company_code = "B"
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param(
            "rental_management_hr_sync.base_url", "http://5.37.56.75:8082"
        )
        params.set_param(
            "rental_management_hr_sync.employee_path",
            "/api/Employee/{company_code},{employee_code}",
        )
        # occupy.wizard requires a property to book.
        cls.property_id = cls.env["property.details"].create(
            {
                "name": "Compound 1-101",
                "sale_lease": "employee_family",
                "stage": "available",
                "type": "residential",
            }
        )

    def _wizard(self, **values):
        return self.env["occupy.wizard"].create(
            dict(
                {
                    "property_id": self.property_id.id,
                    "legacy_company_code": "B",
                    "legacy_employee_code": "9483",
                },
                **values,
            )
        )

    def _sync(self, wizard, payload=PAYLOAD):
        with patch(f"{EMPLOYEE_PATH}._fetch_legacy_employee", return_value=payload):
            return wizard.action_fetch_legacy_employee()

    def _existing(self, code="9483"):
        return self.Employee.with_context(active_test=False).search(
            [("employee_code", "=", code), ("company_id", "=", self.company.id)]
        )

    def test_unknown_employee_is_created_and_selected(self):
        wizard = self._wizard()
        self.assertFalse(self._existing())

        action = self._sync(wizard)

        employee = self._existing()
        self.assertEqual(len(employee), 1)
        self.assertEqual(employee.name, "MOHAMED NAINAR BILAL KAREEM")
        self.assertEqual(employee.employee_code, "9483")
        self.assertEqual(employee.emp_dept_name, "IT")
        self.assertEqual(employee.location_name, "WATTAYAH")
        self.assertEqual(employee.company_id, self.company)
        self.assertEqual(employee.legacy_sync_status, "synced")
        # The wizard's Employee field is filled in with the fetched employee.
        self.assertEqual(wizard.employee_id, employee)
        # The dialog stays open on the same wizard record.
        self.assertEqual(action["res_model"], "occupy.wizard")
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(action["target"], "new")

    def test_known_employee_is_updated_not_duplicated(self):
        existing = self.Employee.create(
            {
                "name": "Stale Name",
                "employee_code": "9483",
                "company_id": self.company.id,
            }
        )
        wizard = self._wizard()

        self._sync(wizard)

        self.assertEqual(self._existing(), existing)
        self.assertEqual(existing.name, "MOHAMED NAINAR BILAL KAREEM")
        self.assertEqual(existing.emp_dept_name, "IT")
        self.assertEqual(wizard.employee_id, existing)

    def test_occupant_follows_the_employees_work_contact(self):
        contact = self.env["res.partner"].create({"name": "Bilal Work Contact"})
        self.Employee.create(
            {
                "name": "Stale Name",
                "employee_code": "9483",
                "company_id": self.company.id,
                "work_contact_id": contact.id,
            }
        )
        wizard = self._wizard()

        self._sync(wizard)

        self.assertEqual(wizard.occupant_id, contact)

    def test_employee_without_work_contact_keeps_the_chosen_occupant(self):
        employee = self.Employee.create(
            {
                "name": "Stale Name",
                "employee_code": "9483",
                "company_id": self.company.id,
            }
        )
        employee.work_contact_id = False
        occupant = self.env["res.partner"].create({"name": "Chosen By Hand"})
        wizard = self._wizard(occupant_id=occupant.id)

        self._sync(wizard)

        self.assertEqual(wizard.occupant_id, occupant)

    def test_employee_code_is_not_rewritten_by_the_response(self):
        """empCode from the payload must never overwrite the matching key."""
        existing = self.Employee.create(
            {
                "name": "Stale Name",
                "employee_code": "9484",
                "company_id": self.company.id,
            }
        )
        wizard = self._wizard(legacy_employee_code="9484")

        self._sync(wizard)

        self.assertEqual(existing.employee_code, "9484")
        self.assertEqual(wizard.employee_id, existing)

    def test_missing_codes_are_refused(self):
        for values in ({"legacy_company_code": False}, {"legacy_employee_code": False}):
            wizard = self._wizard(**values)
            with self.assertRaises(UserError):
                wizard.action_fetch_legacy_employee()

    def test_unmapped_company_code_is_refused(self):
        wizard = self._wizard(legacy_company_code="ZZ")
        with self.assertRaises(UserError):
            self._sync(wizard)
        self.assertFalse(self._existing())

    def test_employee_unknown_to_the_legacy_system_is_refused(self):
        wizard = self._wizard()
        with self.assertRaises(UserError):
            self._sync(wizard, payload=None)
        self.assertFalse(self._existing())

    def test_transport_failure_is_reported_to_the_user(self):
        wizard = self._wizard()
        with patch(
            f"{EMPLOYEE_PATH}._fetch_legacy_employee",
            side_effect=requests.exceptions.ConnectTimeout("boom"),
        ):
            with self.assertRaises(UserError):
                wizard.action_fetch_legacy_employee()
        self.assertFalse(self._existing())

    def test_archived_employee_is_reported_rather_than_reused(self):
        existing = self.Employee.create(
            {
                "name": "Gone",
                "employee_code": "9483",
                "company_id": self.company.id,
            }
        )
        existing.action_archive()
        wizard = self._wizard()
        with self.assertRaises(UserError):
            self._sync(wizard)
        self.assertFalse(wizard.employee_id)

    def test_booking_still_refuses_an_empty_occupant(self):
        """Relaxing the required flag must not let a booking through."""
        wizard = self._wizard()
        with patch(f"{EMPLOYEE_PATH}._fetch_legacy_employee", return_value=PAYLOAD):
            wizard.action_fetch_legacy_employee()
        # hr.employee gives a new employee a work contact, which becomes the
        # occupant; clear it to reach the empty-occupant case.
        wizard.occupant_id = False
        with self.assertRaises(ValidationError):
            wizard.action_book()

    def test_company_code_defaults_from_the_current_company(self):
        defaults = self.env["occupy.wizard"].default_get(["legacy_company_code"])
        self.assertEqual(defaults.get("legacy_company_code"), "B")
