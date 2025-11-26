# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime
import json
import requests
import logging
import os
import math
import tempfile
import shutil
import odoo.tools.osutil
from odoo.exceptions import UserError
import zipfile
_logger = logging.getLogger(__name__)
CHUNK_SIZE = 62914560  # 60 MiB = 60 * 1024 * 1024

class DbBackupConfigure(models.Model):
    _inherit = 'db.backup.configure'
    # Extend available backup destinations to add 2 destinations for Odoo.sh
    backup_destination = fields.Selection(
    selection_add=[
        ('odoo_sh_gdrive', 'Odoo.sh + Google Drive'),
        ('odoo_sh_onedrive', 'Odoo.sh + OneDrive')])
    db_name = fields.Char(required=False, default="database_backup")
    master_pwd = fields.Char(required=False)

    @api.constrains('db_name')
    def _check_db_credentials(self):
        """
        Override constraint to bypass DB credential check
        when using Odoo.sh (where it's unnecessary).
        """
        _logger.debug("Skipping DB name check for backup config.")
        return

    def _schedule_auto_backup(self):
        """
        Override Odoo's base method to add support for:
        - Creating and zipping Odoo.sh backups from /backup.daily
        - Sending them to Google Drive or OneDrive
        - Auto-removing old backups from cloud if enabled
        """
        super()._schedule_auto_backup() 
        _logger.debug("========= SCHEDULE BACKUP CALL =========")
        records = self.search([])
        try:
            for rec in records:
                if rec.backup_destination not in ['odoo_sh_gdrive', 'odoo_sh_onedrive']:
                    continue
                
                # ========= PRE-CHECK DISK SPACE =========
                if not rec._check_disk_space_before_backup():
                    continue
                # ========================================

                try:
                    filename, content = rec._extract_daily_backup_zip()
                except Exception as e:
                    _logger.warning("Error extracting daily backup for '%s': %s", rec.name, str(e))
                    rec.generated_exception = f"Backup extraction error: {e}"
                    if rec.notify_user:
                        self.env.ref('auto_database_backup.mail_template_data_db_backup_failed').send_mail(rec.id, force_send=True)
                    continue
                if not filename:
                    continue
                try:
                    # Google Drive backup Odoo.sh
                    if rec.backup_destination == 'odoo_sh_gdrive':
                        rec._send_to_gdrive(filename, content)
                        if rec.auto_remove:
                            headers = {"Authorization": f"Bearer {rec.gdrive_access_token}"}
                            query = f"parents = '{rec.google_drive_folder_key}'"
                            files_req = requests.get(
                                f"https://www.googleapis.com/drive/v3/files?q={query}",
                                headers=headers)
                            for file in files_req.json().get('files', []):
                                meta = requests.get(
                                    f"https://www.googleapis.com/drive/v3/files/{file['id']}?fields=createdTime",
                                    headers=headers)
                                created = meta.json().get('createdTime', '')[:19].replace('T', ' ')
                                days = (fields.Datetime.now() - fields.datetime.strptime(created, '%Y-%m-%d %H:%M:%S')).days
                                if days >= rec.days_to_remove:
                                    requests.delete(f"https://www.googleapis.com/drive/v3/files/{file['id']}", headers=headers)
                    # Onedrive Backup Odoo.sh
                    elif rec.backup_destination == 'odoo_sh_onedrive':
                        rec._send_to_onedrive(filename, content)
                        if rec.auto_remove:
                            headers = {'Authorization': f"Bearer {rec.onedrive_access_token}"}
                            list_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{rec.onedrive_folder_key}/children"
                            response = requests.get(list_url, headers=headers)
                            for file in response.json().get('value', []):
                                created = file['createdDateTime'][:19].replace('T', ' ')
                                days = (fields.Datetime.now() - fields.datetime.strptime(created, '%Y-%m-%d %H:%M:%S')).days
                                if days >= rec.days_to_remove:
                                    delete_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{file['id']}"
                                    requests.delete(delete_url, headers=headers)
                    if rec.notify_user:
                        self.env.ref('auto_database_backup.mail_template_data_db_backup_successful').send_mail(rec.id, force_send=True)
                except Exception as e:
                    rec.generated_exception = str(e)
                    _logger.exception("ODoo.sh Backup failed: %s", e)
                    if rec.notify_user:
                        self.env.ref('auto_database_backup.mail_template_data_db_backup_failed').send_mail(rec.id, force_send=True)
        finally:
            # Clean /tmp folder after all backups (even if failed)
            os.system("rm -rf /tmp/* || true")
            _logger.debug("Temporary folder /tmp cleaned up after backup process.")

    def _extract_daily_backup_zip(self):
        """
        Scan folder /backup.daily in Odoo.sh and zip any folder or file containing 'daily'
        into a ZIP archive for cloud upload.
        """
        base_path = "backup.daily"
        today = datetime.today().strftime('%Y-%m-%d')
        zip_filepath = f"/tmp/backup_{today}.zip"
        _logger.debug("Scanning directory: %s", base_path)
        if not os.path.isdir(base_path):
            raise UserError(_("The folder '%s' was not found.") % base_path)
        entries = os.listdir(base_path)
        _logger.debug("Entries found: %s", entries)
        found = False
        with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as z:
            for entry in entries:
                if "daily" not in entry.lower():
                    continue
                abs_daily = os.path.join(base_path, entry)
                if not os.path.isdir(abs_daily):
                    continue
                # Search ONLY for filestore
                for root, dirs, files in os.walk(abs_daily):
                    if "filestore" not in dirs:
                        continue
                    filestore_path = os.path.join(root, "filestore")
                    _logger.debug("Found filestore: %s", filestore_path)

                    # Add all files inside the filestore
                    for r, dd, ff in os.walk(filestore_path):
                        for f in ff:
                            abs_file = os.path.join(r, f)
                            # Path inside zip (keep the daily folder name)
                            rel_file = os.path.relpath(abs_file, base_path)
                            z.write(abs_file, arcname=rel_file)
                    found = True
                    break  # stop scanning after filestore found
        if not found:
            _logger.debug("No filestore found in any 'daily' folder.")
            return None, None
        _logger.debug("Created zip archive: %s", zip_filepath)
        # Log final size
        final_size = os.path.getsize(zip_filepath)
        _logger.debug("Final ZIP size: %.2f MB (%.2f GB)",
                    final_size / (1024**2),
                    final_size / (1024**3))
        return os.path.basename(zip_filepath), zip_filepath

    # Upload a ZIP backup to Google Drive
    def _send_to_gdrive(self, filename, filepath):
        _logger.debug("Preparing to upload to Google Drive: %s", filename)
        if self.gdrive_token_validity <= fields.Datetime.now():
            _logger.debug("Google token expired, refreshing...")
            self.generate_gdrive_refresh_token()
        headers = {
            "Authorization": f"Bearer {self.gdrive_access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/zip",}
        metadata = {
            "name": filename,
            "parents": [self.google_drive_folder_key],}
        # 1. Create upload session "resumable" for large files
        session = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
            headers=headers,
            json=metadata)
        if session.status_code not in [200, 201]:
            _logger.debug("Error initiating upload session: %s", session.text)
            raise UserError(f"Google Drive error: {session.text}")
        upload_url = session.headers.get("Location")
        if not upload_url:
            raise UserError(_("Google Drive did not return an upload URL."))
        # 2. Upload the ZIP file via PUT request
        with open(filepath, 'rb') as f:
            upload = requests.put(
                upload_url,
                headers={"Content-Type": "application/zip"},
                data=f
            )
            _logger.debug("Upload to Google Drive response code: %s", upload.status_code)
            _logger.debug("Upload response content: %s", upload.text)
            upload.raise_for_status()
        os.remove(filepath)
        _logger.debug("Deleted temp zip: %s", filepath)

    # Upload a ZIP backup to OneDrive
    def _send_to_onedrive(self, filename, filepath):
        self.ensure_one()
        if not os.path.isfile(filepath):
            raise UserError(_("Backup file not found: %s") % filepath)
        file_size = os.path.getsize(filepath)
        _logger.debug("Starting OneDrive upload for file '%s' (%s bytes)", filename, file_size)
        if self.onedrive_token_validity <= fields.Datetime.now():
            _logger.debug("Refreshing OneDrive token...")
            self.generate_onedrive_refresh_token()
        headers = {
            'Authorization': f'Bearer {self.onedrive_access_token}',
            'Content-Type': 'application/json'}
        session_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{self.onedrive_folder_key}:/{filename}:/createUploadSession"
        session_body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "rename",
                "name": filename}}
        response = requests.post(session_url, headers=headers, json=session_body)
        if response.status_code != 200:
            _logger.error("Failed to create OneDrive session: %s", response.text)
            raise UserError(_("Failed to create OneDrive upload session."))
        upload_url = response.json().get('uploadUrl')
        if not upload_url:
            raise UserError(_("Upload URL not returned by OneDrive."))
        _logger.debug("Upload session created. Starting chunked upload...")
        # Chunked upload is required by OneDrive for files larger than 4 MB.
        # This allows us to split large files (like backup ZIPs) into smaller parts,
        # upload them sequentially, and avoid memory or timeout issues during transfer.
        with open(filepath, 'rb') as f:
            num_chunks = math.ceil(file_size / CHUNK_SIZE)
            for i in range(num_chunks):
                start = i * CHUNK_SIZE
                end = min(start + CHUNK_SIZE, file_size) - 1
                chunk_length = end - start + 1
                f.seek(start)
                chunk_data = f.read(chunk_length)
                chunk_headers = {
                    'Content-Length': str(chunk_length),
                    'Content-Range': f"bytes {start}-{end}/{file_size}"}
                _logger.debug("Uploading chunk %d/%d (%s-%s)...", i + 1, num_chunks, start, end)
                res = requests.put(upload_url, headers=chunk_headers, data=chunk_data)
                if res.status_code not in (200, 201, 202):
                    _logger.error("Chunk upload failed (%s-%s): %s", start, end, res.text)
                    raise UserError(_("Upload failed for chunk %s/%s.") % (i + 1, num_chunks))
        _logger.debug("Upload to OneDrive completed for file '%s'", filename)



    def _estimate_daily_backup_size(self):
        """
        Estimate the total size based STRICTLY on what _extract_daily_backup_zip()
        will copy:
        - Any file containing 'daily' in backup.daily
        - The 'filestore' folder inside any 'daily' directory
        """
        base_path = "backup.daily"
        total_size = 0
        _logger.debug("Estimating backup size from '%s'...", base_path)
        if not os.path.isdir(base_path):
            raise UserError(_("Backup folder '%s' not found.") % base_path)
        entries = os.listdir(base_path)
        _logger.debug("Entries found: %s", entries)
        for entry in entries:
            if 'daily' not in entry.lower():
                continue
            abs_src = os.path.join(base_path, entry)
            # === CASE 1: Simple file ===
            if os.path.isfile(abs_src):
                size = os.path.getsize(abs_src)
                total_size += size
                _logger.debug("Including daily file: %s (%.2f MB)", abs_src, size / (1024**2))
            # === CASE 2: Directory ===
            elif os.path.isdir(abs_src):
                _logger.debug("Scanning directory for filestore: %s", abs_src)
                for root, dirs, files in os.walk(abs_src):

                    # Look only for 'filestore'
                    for d in dirs:
                        if d == 'filestore':
                            filestore_path = os.path.join(root, d)
                            _logger.debug("Found filestore: %s", filestore_path)

                            for r, dd, ff in os.walk(filestore_path):
                                for f in ff:
                                    fp = os.path.join(r, f)
                                    size = os.path.getsize(fp)
                                    total_size += size
                            break  # stop scanning inside this directory
        _logger.debug("Estimated total backup size: %.2f MB (%.2f GB)", total_size / (1024**2), total_size / (1024**3))
        return total_size

    def _check_disk_space_before_backup(self):
        """
        Checks estimated backup size + required free space BEFORE generating ZIP.
        Returns True if OK, False if backup must be skipped.
        """
        try:
            estimated = self._estimate_daily_backup_size()
            stat = shutil.disk_usage("/tmp")
            free_space = stat.free
            # Convert byte → GB
            estimated_gb = estimated / (1024**3)
            # Round required space to next full GB
            required_gb = math.ceil(estimated_gb)
            required = required_gb * (1024**3)
            _logger.debug("Disk space pre-check — estimated=%.2fGB free=%.2fGB required=%dGB", estimated_gb, free_space / (1024**3), required_gb)
            if free_space < required:
                missing = required - free_space
                msg = (
                    "Insufficient disk space to generate the backup: "
                    "Estimated backup size: %.2f GB / "
                    "Available space: %.2f GB. "
                    "Backup generation required %d GB."
                ) % (estimated_gb, free_space / (1024**3), required_gb)
                _logger.error("Backup aborted for %s. Missing %.2fGB.",self.name, missing / (1024**3))
                self.generated_exception = msg
                if self.notify_user:
                    self.env.ref('auto_database_backup.mail_template_data_db_backup_failed').send_mail(self.id, force_send=True)
                return False
            return True
        except Exception as e:
            _logger.exception("Error during disk space pre-check: %s", e)
            self.generated_exception = str(e)
            if self.notify_user:
                self.env.ref('auto_database_backup.mail_template_data_db_backup_failed').send_mail(self.id, force_send=True)
            return False
