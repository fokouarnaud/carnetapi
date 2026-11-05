"""Technical operators drive the same durable dispatch from protected Unfold forms."""
from unittest.mock import patch
import uuid
from django.test import TransactionTestCase,override_settings
from django.urls import reverse
from backups.models import BackupDispatch,BackupEvent,SendAttempt
from backups.tests import test_dispatch as fixtures


class BackupDispatchAdminTests(fixtures.BackupDispatchFixtures,TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)

    def start_url(self):
        return reverse("admin:backups_backupjob_dispatch_start",args=[self.job.id])

    def progress_url(self,command):
        return reverse("admin:backups_backupjob_dispatch_progress",args=[self.job.id,command])

    def test_start_form_is_localized_and_idempotent_and_does_not_send_email(self):
        url=self.start_url()
        for language,label in (("fr","Envoyer cette archive"),("en","Send this archive")):
            with self.subTest(language=language):
                page=self.client.get(url,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(page.status_code,200)
                self.assertContains(page,label)
                self.assertContains(page,str(self.job.id))
                self.assertContains(page,self.job.recipient)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            invalid=self.client.post(url,{"operation_id":str(uuid.uuid4()),"reason":"   "})
            self.assertEqual(invalid.status_code,200)
            self.assertFalse(BackupDispatch.objects.exists())
            operation=str(uuid.uuid4())
            created=self.client.post(url,{"operation_id":operation,"reason":"Envoi explicite depuis Unfold"})
            self.assertEqual(created.status_code,302)
            command=BackupDispatch.objects.get()
            self.assertEqual(created.url,self.progress_url(command.id))
            replay=self.client.post(url,{"operation_id":operation,"reason":"Envoi explicite depuis Unfold"})
            self.assertEqual(replay.status_code,302)
            self.assertEqual(replay.url,created.url)
        self.assertEqual(BackupDispatch.objects.count(),1)
        self.assertEqual(BackupEvent.objects.filter(kind="dispatch_requested").count(),1)
        self.assertFalse(self.submissions)

    def test_posted_steps_send_all_parts_and_get_refresh_never_sends(self):
        created=self.start()
        self.assertEqual(created.status_code,201)
        url=self.progress_url(created.data["id"])
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            self.assertEqual(self.client.get(url).status_code,200)
            self.assertEqual(self.client.post(url,{}).status_code,200)
            self.assertFalse(self.submissions)
            first=self.client.post(url,{"action":"advance"})
            self.assertEqual(first.status_code,302)
            progress=self.client.get(first.url,HTTP_ACCEPT_LANGUAGE="fr")
            self.assertContains(progress,"1 / 2")
            self.assertEqual(len(self.submissions),1)
            last=self.client.post(url,{"action":"advance"})
            self.assertEqual(last.status_code,302)
            complete=self.client.get(last.url,HTTP_ACCEPT_LANGUAGE="fr")
            self.assertContains(complete,"2 / 2")
            self.assertContains(complete,"Acceptée par SMTP, livraison non confirmée")
            self.assertContains(complete,"Fichiers de l’archive nettoyés")
            self.assertNotContains(complete,'name="action"')
            self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(len(self.submissions),2)
        self.assertEqual(SendAttempt.objects.count(),2)

    def test_staff_owner_cannot_start_or_advance_a_global_dispatch(self):
        created=self.start()
        self.assertEqual(created.status_code,201)
        self.owner.is_staff=True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.start_url()).status_code,403)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            self.assertEqual(self.client.post(self.progress_url(created.data["id"]),{"action":"advance"}).status_code,403)
        self.assertFalse(self.submissions)

    def test_message_preparation_failure_is_visible_and_command_can_resume(self):
        created=self.start()
        self.assertEqual(created.status_code,201)
        url=self.progress_url(created.data["id"])
        with override_settings(BACKUP_EMAIL_MAX_BYTES=1),patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            response=self.client.post(url,{"action":"advance"},HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"Impossible d’avancer cet envoi")
        self.assertFalse(self.submissions)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            self.assertEqual(self.client.post(url,{"action":"advance"}).status_code,302)
        self.assertEqual(len(self.submissions),1)

    def test_csrf_is_required_for_starting_a_dispatch(self):
        from django.test import Client
        protected=Client(enforce_csrf_checks=True)
        protected.force_login(self.operator)
        url=self.start_url()
        self.assertEqual(protected.get(url).status_code,200)
        data={"operation_id":str(uuid.uuid4()),"reason":"Envoi protégé"}
        self.assertEqual(protected.post(url,data).status_code,403)
        self.assertFalse(BackupDispatch.objects.exists())
        data["csrfmiddlewaretoken"]=protected.cookies["csrftoken"].value
        self.assertEqual(protected.post(url,data).status_code,302)
        self.assertEqual(BackupDispatch.objects.count(),1)

    def test_role_removed_after_form_validation_prevents_command_creation(self):
        from identity.models import User
        from backups.services.dispatch import start_dispatch
        def removed(*args,**kwargs):
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return start_dispatch(*args,**kwargs)
        with patch("backups.admin.start_dispatch",side_effect=removed):
            response=self.client.post(self.start_url(),{"operation_id":str(uuid.uuid4()),"reason":"contrôle"})
        self.assertEqual(response.status_code,403)
        self.assertFalse(BackupDispatch.objects.exists())

    def test_role_removed_before_step_prevents_smtp_and_does_not_hide_permission_error(self):
        from identity.models import User
        from backups.services.dispatch import advance_dispatch
        started=self.start()
        self.assertEqual(started.status_code,201)
        def removed(*args,**kwargs):
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return advance_dispatch(*args,**kwargs)
        with patch("backups.admin.advance_dispatch",side_effect=removed),patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            response=self.client.post(self.progress_url(started.data["id"]),{"action":"advance"})
        self.assertEqual(response.status_code,403)
        self.assertFalse(self.submissions)
        self.assertFalse(SendAttempt.objects.exists())

    def test_waiting_step_shows_due_date_and_repeated_post_does_not_bypass_retry_delay(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        url=self.progress_url(started.data["id"])
        with patch("backups.services.sending.submit_smtp",side_effect=self.reject):
            self.assertEqual(self.client.post(url,{"action":"advance"}).status_code,302)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            self.assertEqual(self.client.post(url,{"action":"advance"}).status_code,302)
            self.assertEqual(self.client.post(url,{"action":"advance"}).status_code,302)
            page=self.client.get(url,HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page,"Prochaine étape possible (UTC)")
        self.assertEqual(len(self.submissions),2)
        self.assertEqual(SendAttempt.objects.count(),2)

    def test_attempt_history_link_filters_to_the_current_dispatch(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        url=self.progress_url(started.data["id"])
        with patch("backups.services.sending.submit_smtp",side_effect=self.reject):
            self.client.post(url,{"action":"advance"})
        owned=SendAttempt.objects.get()
        other,_=self.send()
        page=self.client.get(url)
        history=self.client.get(page.context["attempts_url"])
        self.assertEqual(history.status_code,200)
        self.assertContains(history,str(owned.id))
        self.assertNotContains(history,str(other.id))

    def test_dispatch_from_another_backup_is_not_executed_by_mismatched_url(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        url=reverse("admin:backups_backupjob_dispatch_progress",args=[uuid.uuid4(),started.data["id"]])
        self.assertEqual(self.client.get(url).status_code,404)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            self.assertEqual(self.client.post(url,{"action":"advance"}).status_code,404)
        self.assertFalse(self.submissions)
