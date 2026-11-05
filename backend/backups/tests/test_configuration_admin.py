"""Unfold changes the daily schedule through the audited service only."""
from unittest.mock import patch
import uuid
from django.test import TransactionTestCase
from django.urls import reverse
from backups.tests import test_configuration as fixtures
from backups.models import BackupConfiguration,BackupConfigurationChange,BackupJob,SendAttempt


class BackupConfigurationAdminTests(fixtures.BackupConfigurationFixtures,TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)

    def admin_url(self):
        return reverse("admin:backups_backupconfiguration_configure")

    def test_default_form_is_localized_and_changes_hour_or_disables_without_execution(self):
        for language,label in (("fr","Configuration de la sauvegarde quotidienne"),("en","Daily backup configuration")):
            with self.subTest(language=language):
                page=self.client.get(self.admin_url(),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(page.status_code,200)
                self.assertContains(page,label)
                self.assertContains(page,"Africa/Douala")
                self.assertContains(page,"02:00")
        self.assertFalse(BackupConfiguration.objects.exists())
        data={"operation_id":str(uuid.uuid4()),"expected_version":0,"enabled":"on","local_hour":6,"local_minute":15,"reason":"Horaire local confirmé"}
        saved=self.client.post(self.admin_url(),data)
        self.assertEqual(saved.status_code,302)
        self.assertEqual(self.client.post(self.admin_url(),data).status_code,302)
        configuration=BackupConfiguration.objects.get()
        self.assertEqual((configuration.local_hour,configuration.local_minute,configuration.version),(6,15,1))
        self.assertEqual(BackupConfigurationChange.objects.count(),1)
        disabled=self.client.post(self.admin_url(),{"operation_id":str(uuid.uuid4()),"expected_version":1,
            "local_hour":6,"local_minute":15,"reason":"Désactivation confirmée"})
        self.assertEqual(disabled.status_code,302)
        configuration.refresh_from_db()
        self.assertFalse(configuration.enabled)
        self.assertEqual(configuration.version,2)
        self.assertFalse(BackupJob.objects.exists())
        self.assertFalse(SendAttempt.objects.exists())

    def test_stale_form_has_visible_conflict_without_overwriting_current_schedule(self):
        self.assertEqual(self.change(local_hour=7).status_code,200)
        response=self.client.post(self.admin_url(),{"operation_id":str(uuid.uuid4()),"expected_version":0,
            "enabled":"on","local_hour":4,"local_minute":0,"reason":"Ancien formulaire"},HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"La configuration a changé")
        self.assertEqual(BackupConfiguration.objects.get().local_hour,7)
        self.assertEqual(BackupConfigurationChange.objects.count(),1)

    def test_owner_is_refused_and_current_role_is_checked_again_by_the_service(self):
        from identity.models import User
        from backups.services.configuration import change_configuration
        self.owner.is_staff=True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.admin_url()).status_code,403)
        self.client.force_login(self.operator)
        def removed(*args,**kwargs):
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return change_configuration(*args,**kwargs)
        with patch("backups.configuration_admin.change_configuration",side_effect=removed):
            response=self.client.post(self.admin_url(),{"operation_id":str(uuid.uuid4()),"expected_version":0,
                "enabled":"on","local_hour":2,"local_minute":0,"reason":"Contrôle des droits"})
        self.assertEqual(response.status_code,403)
        self.assertFalse(BackupConfiguration.objects.exists())

    def test_configuration_history_is_readonly_and_csrf_is_required(self):
        from django.test import Client
        protected=Client(enforce_csrf_checks=True)
        protected.force_login(self.operator)
        self.assertEqual(protected.get(self.admin_url()).status_code,200)
        data={"operation_id":str(uuid.uuid4()),"expected_version":0,"enabled":"on","local_hour":2,"local_minute":0,"reason":"protégé"}
        self.assertEqual(protected.post(self.admin_url(),data).status_code,403)
        data["csrfmiddlewaretoken"]=protected.cookies["csrftoken"].value
        self.assertEqual(protected.post(self.admin_url(),data).status_code,302)
        history=self.client.get(reverse("admin:backups_backupconfigurationchange_changelist"))
        self.assertEqual(history.status_code,200)
        self.assertContains(history,"protégé")
        self.assertEqual(self.client.get(reverse("admin:backups_backupconfigurationchange_add")).status_code,403)

    def test_configuration_link_is_available_from_both_empty_changelists(self):
        for name in ("admin:backups_backupjob_changelist","admin:backups_backupconfiguration_changelist"):
            with self.subTest(name=name):
                response=self.client.get(reverse(name))
                self.assertEqual(response.status_code,200)
                self.assertContains(response,f'href="{self.admin_url()}"')
        self.assertFalse(BackupConfiguration.objects.exists())
