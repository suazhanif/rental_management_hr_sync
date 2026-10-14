# -*- coding: utf-8 -*-
# Copyright 2026
# License OPL-1
"""Look an employee up in the legacy HR system from the Occupy wizard.

``occupy.wizard`` already owns an ``employee_code`` field — a read-only mirror
of the chosen employee's code — so the two lookup inputs added here are named
``legacy_company_code`` / ``legacy_employee_code`` to leave that field alone.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class OccupyWizard(models.TransientModel):
    _inherit = "occupy.wizard"

    legacy_company_code = fields.Char(
        string="Legacy Company Code",
        default=lambda self: self.env.company.legacy_company_code,
        help="Legacy HR company code, e.g. 'B'.",
    )
    legacy_employee_code = fields.Char(
        string="Legacy Employee Code",
        help="Employee code in the legacy HR system, e.g. '9483'.",
    )

    # The Sync button saves the wizard before it runs, and the occupant is what
    # the lookup fills in (from the employee's work contact) — so it cannot
    # also be mandatory at save time. Booking is unaffected: action_book still
    # refuses an empty occupant, and the view keeps the field mandatory for
    # non-company bookings, where no employee lookup fills it.
    occupant_id = fields.Many2one(required=False)

    def action_fetch_legacy_employee(self):
        """Pull the employee from the legacy system and select them below.

        Creates the employee when Odoo does not have them yet, updates them
        when it does, then fills in the wizard's Employee field.
        """
        self.ensure_one()
        company_code = (self.legacy_company_code or "").strip()
        employee_code = (self.legacy_employee_code or "").strip()
        if not company_code or not employee_code:
            raise UserError(
                _("Enter both the company code and the employee code before syncing.")
            )
        employee = self.env["hr.employee"]._upsert_from_legacy(
            company_code, employee_code
        )
        self.employee_id = employee
        # _onchange_employee_id does this when a user picks the employee by
        # hand; a write does not fire onchanges, so mirror it here. Only when
        # the employee has a work contact, so a legacy record without one does
        # not silently clear an occupant already chosen.
        if employee.work_contact_id:
            self.occupant_id = employee.work_contact_id
        return self._reopen_occupy_wizard()

    def _reopen_occupy_wizard(self):
        """Re-render the still-open dialog with the fetched values."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Accommodation Book"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": dict(self.env.context),
        }
