# -*- coding: utf-8 -*-
# Copyright 2026
# License OPL-1
"""Refresh Odoo employees from the legacy (Bahwan) HR system.

The legacy service answers one employee at a time, keyed by the pair
``companyCode,empCode`` — so this integration is a reconciliation pass over
the employees Odoo already holds, not a discovery mechanism. Employees are
never created, archived or deleted here; an employee the legacy system does
not know is flagged on the record for someone to look at.
"""
import logging
import base64
import binascii
import hashlib
import logging
import re
import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Placeholders are filled from the employee's company code and employee code.
DEFAULT_EMPLOYEE_PATH = "/api/Employee/{company_code},{employee_code}"
DEFAULT_TIMEOUT = 30

# Legacy payload key -> hr.employee field. empCode is deliberately absent:
# it is the key the employee was looked up by, so a response must never
# rewrite it.
LEGACY_FIELD_MAP = {
    "companyCode": "emp_company_code",
    "empDeptCode": "emp_dept_code",
    "empDeptName": "emp_dept_name",
    "locationCode": "location_code",
    "locationName": "location_name",
    "empName": "name",
    "eMail": "work_email",
    "phone": "mobile_phone",
    "civilCardNo": "identification_id",
    "grade": "emp_grade",
    "designation": "emp_designation",
}
# Base64 photo; handled separately so it is only rewritten when it changes.
LEGACY_IMAGE_KEY = "empImage"
_DATA_URI_PREFIX = re.compile(r"^data:image/[\w.+-]+;base64,", re.IGNORECASE)

class HrEmployee(models.Model):
    _inherit = "hr.employee"

    # Mirrored legacy values, refreshed by the sync. These are a record of
    # what the legacy system returned; the lookup key is the company's own
    # legacy_company_code plus employee_code.
    emp_company_code = fields.Char("Legacy Company Code", copy=False, index=True)
    emp_dept_code = fields.Char("Legacy Department Code", copy=False)
    emp_dept_name = fields.Char("Legacy Department", copy=False)
    location_code = fields.Char("Legacy Location Code", copy=False)
    location_name = fields.Char("Legacy Location", copy=False)

    legacy_sync_date = fields.Datetime("Last Legacy Sync", readonly=True, copy=False)
    legacy_sync_status = fields.Selection(
        [
            ("synced", "Synced"),
            ("not_found", "Not Found"),
            ("error", "Error"),
        ],
        string="Legacy Sync Status",
        readonly=True,
        copy=False,
    )
    legacy_sync_message = fields.Char("Legacy Sync Message", readonly=True, copy=False)
    emp_grade = fields.Char("Legacy Grade", copy=False)
    emp_designation = fields.Char("Legacy Designation", copy=False)
    legacy_image_checksum = fields.Char(copy=False, readonly=True)

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------
    @api.model
    def _legacy_sync_settings(self):
        """Read the global settings driving the legacy HR sync."""
        params = self.env["ir.config_parameter"].sudo()

        def _int_param(key, fallback):
            try:
                return int(params.get_param(key) or 0) or fallback
            except (TypeError, ValueError):
                return fallback

        return {
            "enabled": params.get_param("rental_management_hr_sync.enabled") == "True",
            "base_url": (
                params.get_param("rental_management_hr_sync.base_url") or ""
            ).strip().rstrip("/"),
            "employee_path": (
                params.get_param("rental_management_hr_sync.employee_path") or ""
            ).strip()
            or DEFAULT_EMPLOYEE_PATH,
            "timeout": _int_param("rental_management_hr_sync.timeout", DEFAULT_TIMEOUT),
            # 0 means "no cap": sync every eligible employee in one run.
            "batch_limit": _int_param("rental_management_hr_sync.batch_limit", 0),
        }

    # -------------------------------------------------------------------------
    # Which employees take part
    # -------------------------------------------------------------------------
    def _legacy_company_code(self):
        """Legacy company code to query this employee under.

        Always the code carried by the employee's company: the company form is
        the single place this mapping is maintained.
        """
        self.ensure_one()
        return (self.company_id.legacy_company_code or "").strip()

    @api.model
    def _legacy_sync_domain(self):
        """Employees eligible for the daily sync.

        Any active employee carrying an employee code and belonging to a
        company that has a legacy HR company code.
        """
        return [
            ("employee_code", "!=", False),
            ("company_id.legacy_company_code", "!=", False),
        ]

    # -------------------------------------------------------------------------
    # HTTP
    # -------------------------------------------------------------------------
    @api.model
    def _legacy_endpoint_url(self, settings, company_code, employee_code):
        path = settings["employee_path"].format(
            company_code=company_code, employee_code=employee_code
        )
        if not path.startswith("/"):
            path = "/" + path
        return settings["base_url"] + path

    @api.model
    def _fetch_legacy_employee(self, settings, company_code, employee_code):
        """Return the legacy record for one employee, or None when unknown.

        Raises for transport failures so the caller can tell "this employee is
        gone" apart from "the service is down".
        """
        url = self._legacy_endpoint_url(settings, company_code, employee_code)
        response = requests.get(
            url, timeout=settings["timeout"], headers={"Accept": "application/json"}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ValueError(_("Unexpected response format from %s", url))
        records = [record for record in payload if isinstance(record, dict)]
        return records[0] if records else None

    # -------------------------------------------------------------------------
    # Mapping
    # -------------------------------------------------------------------------
    @api.model
    def _legacy_value(self, record, key):
        value = record.get(key)
        return "" if value is None else str(value).strip()

    @api.model
    def _legacy_image(self, record):
        """(base64, checksum) of the legacy photo, or None when absent/invalid."""
        value = self._legacy_value(record, LEGACY_IMAGE_KEY)
        if not value:
            return None
        value = _DATA_URI_PREFIX.sub("", value).replace("\n", "").replace("\r", "")
        try:
            if not base64.b64decode(value, validate=True):
                return None
        except (binascii.Error, ValueError):
            _logger.warning("Legacy HR sync: ignoring invalid employee image data.")
            return None
        return value, hashlib.sha1(value.encode()).hexdigest()

    @api.model
    def _legacy_employee_values(self, record):
        """Map a legacy payload onto writable hr.employee values.

        Blank legacy values are dropped rather than written, so a sparse
        response never wipes data already held in Odoo.
        """
        values = {}
        for key, field_name in LEGACY_FIELD_MAP.items():
            value = self._legacy_value(record, key)
            if value:
                values[field_name] = value
        if values.get("work_email") and "@" not in values["work_email"]:
            values.pop("work_email")
        if values.get("emp_designation"):
            values["job_title"] = values["emp_designation"]
        image = self._legacy_image(record)
        if image:
            values["image_1920"], values["legacy_image_checksum"] = image
        return values

    def _legacy_changes(self, values):
        """Subset of ``values`` that differs from this employee.

        The photo is compared through its checksum so an unchanged image is
        not re-written (and re-resized) on every run.
        """
        self.ensure_one()
        changes = {}
        for field_name, value in values.items():
            if field_name == "image_1920":
                continue
            if field_name == "legacy_image_checksum":
                if value != self.legacy_image_checksum or not self.image_1920:
                    changes["image_1920"] = values["image_1920"]
                    changes["legacy_image_checksum"] = value
                continue
            if (self[field_name] or "") != value:
                changes[field_name] = value
        return changes

    # -------------------------------------------------------------------------
    # Sync
    # -------------------------------------------------------------------------
    def _sync_one_legacy_employee(self, settings):
        """Refresh a single employee. Returns 'synced', 'not_found' or 'error'."""
        self.ensure_one()
        company_code = self._legacy_company_code()
        if not company_code:
            return "error", _("No legacy company code for this employee's company.")
        try:
            record = self._fetch_legacy_employee(
                settings, company_code, self.employee_code
            )
        except (requests.exceptions.RequestException, ValueError) as error:
            return "error", str(error)
        if not record:
            return "not_found", _(
                "The legacy HR system has no employee %(code)s under company %(company)s.",
                code=self.employee_code,
                company=company_code,
            )
        changes = self._legacy_changes(self._legacy_employee_values(record))
        if changes:
            self.write(changes)

    @api.model
    def _company_for_legacy_code(self, company_code):
        return (
            self.env["res.company"]
            .sudo()
            .search([("legacy_company_code", "=ilike", company_code)], limit=1)
        )

    @api.model
    def _find_by_legacy_key(self, company, employee_code):
        """Existing employee for a legacy key, archived ones included."""
        return (
            self.with_context(active_test=False)
            .sudo()
            .search(
                [
                    ("employee_code", "=", employee_code),
                    ("company_id", "=", company.id),
                ],
                limit=1,
            )
        )

    @api.model
    def _upsert_from_legacy(self, company_code, employee_code):
        """Fetch one employee from the legacy system, creating it if needed.

        This is the on-demand path used by the Occupy wizard: unlike the daily
        cron it may create an employee. Raises a UserError the caller can show
        rather than flagging the record, since there is nothing to flag yet.
        """
        settings = self._legacy_sync_settings()
        if not settings["base_url"]:
            raise UserError(
                _(
                    "The legacy HR API base URL is not set. Configure it in "
                    "Settings > Real Estate > Legacy HR Integration."
                )
            )
        company = self._company_for_legacy_code(company_code)
        if not company:
            raise UserError(
                _(
                    "No Odoo company carries the legacy HR company code "
                    "'%(code)s'. Set it on the company form.",
                    code=company_code,
                )
            )
        try:
            record = self._fetch_legacy_employee(settings, company_code, employee_code)
        except (requests.exceptions.RequestException, ValueError) as error:
            raise UserError(
                _("Could not reach the legacy HR system: %s", error)
            ) from error
        if not record:
            raise UserError(
                _(
                    "The legacy HR system has no employee %(code)s under company "
                    "%(company)s.",
                    code=employee_code,
                    company=company_code,
                )
            )

        values = self._legacy_employee_values(record)
        employee = self._find_by_legacy_key(company, employee_code)
        if employee and not employee.active:
            raise UserError(
                _(
                    "Employee %(name)s (%(code)s) exists in Odoo but is archived. "
                    "Restore the employee before booking an accommodation.",
                    name=employee.name,
                    code=employee_code,
                )
            )
        if employee:
            changes = employee._legacy_changes(values)
            if changes:
                employee.write(changes)
        else:
            if not values.get("name"):
                raise UserError(
                    _(
                        "The legacy HR system returned no name for employee "
                        "%(code)s, so the employee cannot be created.",
                        code=employee_code,
                    )
                )
            employee = (
                self.sudo()
                .with_company(company)
                .create(
                    dict(
                        values,
                        employee_code=employee_code,
                        company_id=company.id,
                    )
                )
            )
        employee.write(
            {
                "legacy_sync_date": fields.Datetime.now(),
                "legacy_sync_status": "synced",
                "legacy_sync_message": False,
            }
        )
        return employee

    def action_sync_from_legacy_hr(self):
        """Refresh the selected employees now, from the employee form/list."""
        settings = self._legacy_sync_settings()
        if not settings["base_url"]:
            raise UserError(
                _(
                    "The legacy HR API base URL is not set. Configure it in "
                    "Settings > Real Estate > Legacy HR Integration."
                )
            )
        counters = self._run_legacy_sync(settings)
        if len(self) == 1 and self.legacy_sync_status != "synced":
            raise UserError(self.legacy_sync_message)
        return self._legacy_sync_notification(counters)

    def _run_legacy_sync(self, settings):
        """Sync every employee in ``self``, recording the outcome on each."""
        counters = {"synced": 0, "not_found": 0, "error": 0}
        now = fields.Datetime.now()
        for employee in self:
            status, message = employee._sync_one_legacy_employee(settings)
            counters[status] += 1
            employee.write(
                {
                    "legacy_sync_date": now,
                    "legacy_sync_status": status,
                    "legacy_sync_message": message or False,
                }
            )
            if status != "synced":
                _logger.warning(
                    "Legacy HR sync: employee %s (code %s): %s",
                    employee.id,
                    employee.employee_code,
                    message,
                )
        return counters

    @api.model
    def _legacy_sync_notification(self, counters):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Legacy HR Sync"),
                "message": _(
                    "%(synced)s synced, %(not_found)s not found, %(error)s failed.",
                    synced=counters["synced"],
                    not_found=counters["not_found"],
                    error=counters["error"],
                ),
                "type": "success" if not counters["error"] else "warning",
                "sticky": False,
            },
        }

    @api.model
    def cron_sync_legacy_employees(self):
        """Daily entry point; a no-op while the integration is switched off."""
        settings = self._legacy_sync_settings()
        if not settings["enabled"]:
            _logger.info("Legacy HR sync is disabled, skipping.")
            return False
        if not settings["base_url"]:
            _logger.warning("Legacy HR sync has no base URL configured, skipping.")
            return False
        # Oldest first, so a capped run works through everyone in rotation
        # instead of re-syncing the same head of the list every day.
        employees = self.sudo().search(
            self._legacy_sync_domain(),
            order="legacy_sync_date ASC NULLS FIRST, id ASC",
            limit=settings["batch_limit"] or None,
        )
        counters = employees._run_legacy_sync(settings)
        _logger.info(
            "Legacy HR sync finished: %(synced)s synced, %(not_found)s not found, "
            "%(error)s failed.",
            counters,
        )
        return counters
