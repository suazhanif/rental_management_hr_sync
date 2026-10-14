# -*- coding: utf-8 -*-
# Copyright 2026
# License OPL-1
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    legacy_company_code = fields.Char(
        string="Legacy HR Company Code",
        help="Company code this Odoo company is known by in the legacy HR "
        "system, e.g. 'B'. Employees of a company without a code are left "
        "out of the daily sync.",
    )
