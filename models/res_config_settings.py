# -*- coding: utf-8 -*-
# Copyright 2026
# License OPL-1
from odoo import _, fields, models
from odoo.exceptions import UserError

from .hr_employee import DEFAULT_EMPLOYEE_PATH, DEFAULT_TIMEOUT


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    hr_sync_enabled = fields.Boolean(
        string="Sync Employees with Legacy HR System",
        config_parameter="rental_management_hr_sync.enabled",
    )
    hr_sync_base_url = fields.Char(
        string="API Base URL",
        help="Root of the legacy HR service, e.g. http://5.37.56.75:8082",
        config_parameter="rental_management_hr_sync.base_url",
    )
    hr_sync_employee_path = fields.Char(
        string="Employee Path",
        default=DEFAULT_EMPLOYEE_PATH,
        help="Path appended to the base URL. {company_code} and {employee_code} "
        "are replaced per employee.",
        config_parameter="rental_management_hr_sync.employee_path",
    )
    hr_sync_timeout = fields.Integer(
        string="Request Timeout (Seconds)",
        default=DEFAULT_TIMEOUT,
        config_parameter="rental_management_hr_sync.timeout",
    )
    hr_sync_batch_limit = fields.Integer(
        string="Employees per Run",
        help="Cap on how many employees one daily run refreshes, oldest sync "
        "first. Leave at 0 to sync every employee each run.",
        config_parameter="rental_management_hr_sync.batch_limit",
    )
    def action_sync_legacy_employees_now(self):
        """Run the daily sync immediately, using the values just entered."""
        self.ensure_one()
        self.execute()
        Employee = self.env["hr.employee"].sudo()
        settings = Employee._legacy_sync_settings()
        if not settings["base_url"]:
            raise UserError(_("Set the legacy HR API base URL first."))
        employees = Employee.search(Employee._legacy_sync_domain())
        if not employees:
            raise UserError(
                _(
                    "No employee is eligible for the sync. Employees need an "
                    "Employee Code, and their company needs a Legacy HR Company "
                    "Code."
                )
            )
        return Employee._legacy_sync_notification(employees._run_legacy_sync(settings))
