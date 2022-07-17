# -*- coding: utf-8 -*-

{
    'name': 'Windcave Payment Acquirer',
    'category': 'Accounting/Payment Acquirers',
    'sequence': 380,
    'summary': 'Payment Acquirer: Windcave Implementation',
    'version': '1.0',
    'description': """Windcave Payment Acquirer""",
    'depends': ['payment'],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_views.xml',
        'views/payment_windcave_templates.xml',
        'views/portal_templates.xml',
        'data/payment_acquirer_data.xml',
        'data/windcave_method_data.xml',
        'wizard/windcave_transaction_refund_wizard_views.xml',
    ],
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': True,
    'post_init_hook': 'create_missing_journal_for_acquirers',
    'uninstall_hook': 'uninstall_hook',
}
