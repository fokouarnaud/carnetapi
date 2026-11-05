"""Technical operators can prepare backups from a protected Unfold form."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from backups.models import BackupJob
from backups.services.archive import build_archive
from identity.models import User


class BackupAdministrationTests(TransactionTestCase):
    def setUp(self):
        self.technical = User.objects.create_superuser(username="backup-admin", password=None)
        self.owner = User.objects.create_user(username="backup-owner", is_staff=True)
        self.directory = TemporaryDirectory(prefix="backup-admin-test-")
        self.addCleanup(self.directory.cleanup)
        self.options = override_settings(BACKUP_ROOT=Path(self.directory.name), BACKUP_ENCRYPTION_KEY=Fernet.generate_key(),
            BACKUP_KEY_ID="test-primary", BACKUP_PART_MAX_BYTES=32768)
        self.options.enable()
        self.addCleanup(self.options.disable)

    def test_unfold_preparation_and_followup_are_localized_and_do_not_imply_smtp_delivery(self):
        self.client.force_login(self.technical)
        for language, labels in (("fr", ["Préparer une sauvegarde serveur", "Destinataire", "Motif"]),
                ("en", ["Prepare server backup", "Recipient", "Reason"])):
            with self.subTest(language=language):
                response = self.client.get(reverse("admin:backups_backupjob_prepare"), HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code, 200)
                for label in labels:
                    self.assertContains(response, label)
        response = self.client.post(reverse("admin:backups_backupjob_prepare"), {"operation_id": str(uuid.uuid4()),
            "recipient": "backup@example.test", "reason": "Sauvegarde de contrôle"}, HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BackupJob.objects.get().state, "ready")
        listing = self.client.get(reverse("admin:backups_backupjob_changelist"), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(listing, "Préparée")

    def test_staff_business_owner_cannot_prepare_or_read_global_backup_metadata(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:backups_backupjob_prepare")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:backups_backupjob_changelist")).status_code, 403)
        self.assertFalse(BackupJob.objects.exists())

    def test_role_removed_during_preparation_denies_the_admin_response_but_keeps_the_archive(self):
        self.client.force_login(self.technical)

        def removed(*args, **kwargs):
            User.objects.filter(id=self.technical.id).update(is_superuser=False)
            return build_archive(*args, **kwargs)

        with patch("backups.services.jobs.build_archive", side_effect=removed):
            response = self.client.post(reverse("admin:backups_backupjob_prepare"), {"operation_id": str(uuid.uuid4()),
                "recipient": "backup@example.test", "reason": "Droits recontrôlés"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BackupJob.objects.get().state, "ready")
