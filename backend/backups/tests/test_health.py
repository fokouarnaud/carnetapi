"""Q145: incomplete archives block generation; full SMTP acceptance has an age."""
from datetime import datetime, timedelta, timezone
from html import unescape
from unittest.mock import patch
import uuid
from django.test import TestCase
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied
from backups.models import BackupConfiguration, BackupEvent, BackupJob, BackupPart, BackupSchedulerState, SendAttempt
from identity.models import User


class BackupHealthFixtures:
    at = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)

    def setUp(self):
        self.operator = User.objects.create_superuser(username="backup-health-technical", password=None)
        self.owner = User.objects.create_user(username="backup-health-owner", is_staff=True)

    def archive(self, state="ready", parts=2):
        values = {"operation_id": uuid.uuid4(), "requested_by": self.operator, "reason": "health fixture",
            "recipient": "backup@example.test", "key_id": "test", "state": state}
        if state == "ready":
            values.update(part_count=parts, snapshot_bytes=4096, archive_bytes=100, prepared_at=self.at-timedelta(days=3))
        elif state == "failed":
            values["failure_code"] = "snapshot_failed"
        job = BackupJob.objects.create(**values)
        if state == "ready":
            for number in range(1, parts+1):
                BackupPart.objects.create(job=job, number=number, filename=f"fixture-{job.id}-{number}", size_bytes=100, sha256="a"*64)
        return job

    def acceptance(self, part, at, recipient=None, number=1):
        return SendAttempt.objects.create(part=part, operation_id=uuid.uuid4(), requested_by=self.operator,
            number=number, state="accepted", sender="sender@example.test", recipient=recipient or part.job.recipient,
            encoded_bytes=512, wire_sha256="b"*64, completed_at=at)


class BackupHealthTests(BackupHealthFixtures, TestCase):
    def overview(self, actor=None, at=None):
        from backups.services.health import backup_health
        return backup_health(actor or self.operator, at=at or self.at)

    def test_empty_history_is_unknown_without_a_fictitious_success(self):
        health = self.overview()
        self.assertEqual(health["checked_at"], self.at)
        self.assertFalse(health["blocked"])
        self.assertEqual(health["blocked_count"], 0)
        self.assertIsNone(health["blocking_job"])
        self.assertIsNone(health["last_accepted_job"])
        self.assertIsNone(health["last_accepted_at"])
        self.assertIsNone(health["last_accepted_age_seconds"])
        self.assertFalse(BackupConfiguration.objects.exists())
        self.assertFalse(BackupSchedulerState.objects.exists())

    def test_preparing_failed_and_partly_accepted_archives_all_block_generation(self):
        partial = self.archive()
        self.acceptance(partial.parts.first(), self.at-timedelta(days=2))
        failed = self.archive("failed")
        self.archive("preparing")
        BackupJob.objects.filter(id=failed.id).update(created_at=self.at-timedelta(days=5))
        health = self.overview()
        self.assertTrue(health["blocked"])
        self.assertEqual(health["blocked_count"], 3)
        self.assertEqual(health["blocking_job"].id, failed.id)
        self.assertIsNone(health["last_accepted_at"])

    def test_complete_age_uses_first_acceptance_of_each_part_even_after_late_duplicates(self):
        complete = self.archive()
        first, second = list(complete.parts.all())
        self.acceptance(first, self.at-timedelta(days=3))
        completed_at = self.at-timedelta(days=2)
        self.acceptance(second, completed_at)
        self.acceptance(first, self.at-timedelta(hours=1), number=2)
        newer_incomplete = self.archive()
        health = self.overview()
        self.assertEqual(health["last_accepted_job"].id, complete.id)
        self.assertEqual(health["last_accepted_at"], completed_at)
        self.assertEqual(health["last_accepted_age_seconds"], 2*86400)
        self.assertEqual(health["blocking_job"].id, newer_incomplete.id)
        self.assertEqual(complete.cleanup_state, "waiting")

    def test_abandonment_removes_blockage_without_creating_a_success_or_deleting_history(self):
        from backups.services.abandonment import abandon_backup
        abandoned = self.archive()
        self.acceptance(abandoned.parts.first(), self.at-timedelta(days=1))
        abandon_backup(self.operator, abandoned.id, uuid.uuid4(), "Abandon confirmé")
        health = self.overview()
        self.assertFalse(health["blocked"])
        self.assertIsNone(health["last_accepted_at"])
        self.assertEqual(SendAttempt.objects.count(), 1)
        self.assertEqual(BackupJob.objects.count(), 1)
        self.assertEqual(BackupEvent.objects.filter(kind="abandoned").count(), 1)

    def test_owner_and_revoked_technical_user_cannot_read_global_health(self):
        with self.assertRaises(PermissionDenied):
            self.overview(actor=self.owner)
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):
            self.overview()

    def test_missing_sparse_or_wrong_recipient_parts_never_look_complete(self):
        from backups.services.delivery import all_parts_accepted, jobs_with_delivery_status
        for variant in ("missing", "sparse", "wrong_recipient", "no_parts"):
            with self.subTest(variant=variant):
                job = self.archive()
                parts = list(job.parts.all())
                for part in parts:
                    self.acceptance(part, self.at-timedelta(days=1),
                        recipient="someone-else@example.test" if variant == "wrong_recipient" else None)
                if variant in {"missing", "no_parts"}:
                    SendAttempt.objects.filter(part=parts[-1]).delete()
                    parts[-1].delete()
                    if variant == "no_parts":
                        SendAttempt.objects.filter(part=parts[0]).delete()
                        parts[0].delete()
                elif variant == "sparse":
                    BackupPart.objects.filter(id=parts[-1].id).update(number=3)
                self.assertFalse(all_parts_accepted(job))
                self.assertFalse(jobs_with_delivery_status().get(id=job.id).delivery_complete)
        health = self.overview()
        self.assertEqual(health["blocked_count"], 4)
        self.assertIsNone(health["last_accepted_at"])

    def test_future_acceptance_preserves_timestamp_but_has_no_fictitious_zero_age(self):
        job = self.archive()
        future = self.at+timedelta(hours=2)
        for part in job.parts.all():
            self.acceptance(part, future)
        health = self.overview()
        self.assertFalse(health["blocked"])
        self.assertEqual(health["last_accepted_at"], future)
        self.assertTrue(health["clock_inconsistent"])
        self.assertIsNone(health["last_accepted_age_seconds"])
        with self.assertRaisesRegex(ValueError, "aware_datetime_required"):
            self.overview(at=self.at.replace(tzinfo=None))

    def test_overview_uses_bounded_sql_queries_across_history(self):
        for _ in range(24):
            self.archive("failed")
        job = self.archive()
        for part in job.parts.all():
            self.acceptance(part, self.at-timedelta(days=1))
        # Four SELECTs (current rights, global count, first blocker, last complete)
        # plus the savepoint pair used inside this TestCase's outer transaction.
        with self.assertNumQueries(6):
            health = self.overview()
        self.assertEqual(health["blocked_count"], 24)
        self.assertEqual(health["last_accepted_job"].id, job.id)


class BackupHealthAdminTests(BackupHealthFixtures, TestCase):
    def assert_page_text(self, response, text):
        rendered = " ".join(unescape(response.content.decode("utf-8")).split())
        self.assertTrue(text in rendered, f"Missing rendered text: {text}")

    def test_health_is_visible_from_both_technical_pages_in_french_and_english_without_writes(self):
        self.archive("failed")
        self.client.force_login(self.operator)
        pages = ("admin:backups_backupjob_changelist", "admin:backups_backupconfiguration_configure")
        for language, label in (("fr", "Nouvelles sauvegardes automatiques bloquées"),
                ("en", "New automatic backups blocked")):
            for name in pages:
                with self.subTest(language=language, page=name), patch("backups.services.sending.submit_smtp", side_effect=AssertionError("GET must not send")):
                    response = self.client.get(reverse(name), HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual(response.status_code, 200)
                    self.assert_page_text(response, label)
                    self.assert_page_text(response, "health fixture")
                    self.assertIsNone(response.context["backup_health"]["last_accepted_at"])
        self.assertEqual(BackupJob.objects.count(), 1)
        self.assertFalse(BackupConfiguration.objects.exists())
        self.assertFalse(BackupSchedulerState.objects.exists())
        self.assertFalse(SendAttempt.objects.exists())
        self.assertFalse(BackupEvent.objects.exists())

    def test_complete_acceptance_shows_date_age_and_delivery_limit_and_owner_has_no_access(self):
        job = self.archive()
        for part in job.parts.all():
            self.acceptance(part, self.at-timedelta(days=2))
        self.client.force_login(self.operator)
        with patch("django.utils.timezone.now", return_value=self.at):
            response = self.client.get(reverse("admin:backups_backupjob_changelist"), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code, 200)
        self.assert_page_text(response, "Dernière acceptation SMTP complète")
        self.assert_page_text(response, "2 jours")
        self.assert_page_text(response, "L’acceptation SMTP ne confirme pas la livraison.")
        self.assertEqual(response.context["backup_health"]["last_accepted_age_seconds"], 2*86400)
        self.client.force_login(self.owner)
        for name in ("admin:backups_backupjob_changelist", "admin:backups_backupconfiguration_configure"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_empty_history_and_future_clock_are_explained_instead_of_a_successful_age(self):
        self.client.force_login(self.operator)
        url = reverse("admin:backups_backupjob_changelist")
        response = self.client.get(url, HTTP_ACCEPT_LANGUAGE="fr")
        self.assert_page_text(response, "Aucune acceptation SMTP complète connue.")
        job = self.archive()
        for part in job.parts.all():
            self.acceptance(part, self.at+timedelta(days=1))
        with patch("django.utils.timezone.now", return_value=self.at):
            response = self.client.get(url, HTTP_ACCEPT_LANGUAGE="fr")
        self.assert_page_text(response, "Vérifiez l’horloge du serveur ; son âge est inconnu.")
        self.assertNotIn("Âge lors de ce contrôle", response.content.decode())

    def test_summary_keeps_global_blockage_when_changelist_filters_hide_it(self):
        blocker = self.archive("failed")
        job = self.archive()
        for part in job.parts.all():
            self.acceptance(part, self.at-timedelta(days=1))
        self.client.force_login(self.operator)
        response = self.client.get(reverse("admin:backups_backupjob_changelist"), {"state__exact": "ready"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)
        self.assertEqual(response.context["backup_health"]["blocked_count"], 1)
        self.assertEqual(response.context["backup_health"]["blocking_job"].id, blocker.id)

    def test_role_removed_during_health_read_is_a_403_on_both_admin_pages(self):
        from backups.services.health import backup_health
        self.client.force_login(self.operator)
        for module, name in (("backups.admin", "admin:backups_backupjob_changelist"),
                ("backups.configuration_admin", "admin:backups_backupconfiguration_configure")):
            User.objects.filter(id=self.operator.id).update(is_superuser=True)
            def revoked(actor, **kwargs):
                User.objects.filter(id=self.operator.id).update(is_superuser=False)
                return backup_health(actor, **kwargs)
            with self.subTest(page=name), patch(module+".backup_health", side_effect=revoked):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 403)
