# -*- coding: utf-8 -*-
# Copyright 2026
# License OPL-1
{
    "name": "Rental Management - Legacy HR Integration",
    "summary": "Keeps Odoo employees in sync with the legacy HR system",
    "description": """
    Client-specific integration (Bahwan Group Oman).

    A daily cron walks the employees already present in Odoo and refreshes
    each one from the legacy HR service, calling the per-employee endpoint
    /api/Employee/{company_code},{employee_code}.

    The legacy company code is set on the Odoo company form; the service URL
    and timeout live in Settings > Real Estate. The sync never creates,
    archives or deletes employees: employees the legacy system does not know
    are flagged on the record instead.

    The Occupy wizard additionally offers an on-demand lookup by company code
    and employee code, which does create the employee when Odoo lacks them.
    """,
    "author": "",
    "website": "",
    "category": "Realestate",
    "version": "1.0.0",
    "depends": [
        "rental_management",
        "hr",
    ],
    "data": [
        "data/ir_cron.xml",
        "views/res_company_views.xml",
        "views/hr_employee_views.xml",
        "views/res_config_settings_views.xml",
        "wizard/occupy_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
