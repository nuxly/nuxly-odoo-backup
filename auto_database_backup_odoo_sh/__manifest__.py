# -*- coding: utf-8 -*-
{
    'name': "Odoo.sh Backup to Cloud (Google Drive / OneDrive)",
    'version': '17.0.1.3.0',
    'summary': "Send Odoo.sh automatic backups to Google Drive / OneDrive",
    'description': """
      This module extends `auto_database_backup` to support additional cloud destinations for Odoo.sh:
        - Google Drive (Odoo.sh > Google Drive)
        - OneDrive (Odoo.sh > OneDrive)

        It removes the need for a master password, hides unnecessary options, and uses token-based authentication for automatic uploads. 
        Backups are taken from the `/backup.daily` directory, zipped, and uploaded to the selected cloud storage. 
        The module also supports automatic cleanup of older backups and provides detailed logging for traceability.
    """,
    'author': "Nuxly",
    'category': 'Tools',
    'website': "https://www.nuxly.com",
    'depends': ['auto_database_backup'],
    'external_dependencies': {
        'python': ['dropbox', 'pyncclient', 'boto3', 'nextcloud-api-wrapper','paramiko']},
    'data': [
        'views/db_backup_configure_view.xml',
        'data/ir_cron_data.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'license': 'LGPL-3',
    'application': True,
}
