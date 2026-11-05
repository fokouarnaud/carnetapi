"""Unfold reports expiration with current technical rights, CSRF and both languages."""
from datetime import datetime, timezone as utc
import uuid
from unittest.mock import patch
from html import unescape
from django.test import TestCase, Client
from django.urls import reverse
from django.core import mail
from backups.tests import test_hosting as fixtures
from backups.models import HostingConfiguration, HostingConfigurationChange, HostingReminderAttempt


class HostingAdministrationTests(TestCase):
    setUp=fixtures.HostingApiTests.setUp

    def url(self):return reverse("admin:backups_hostingconfiguration_configure")

    def form(self,**changes):
        return {"operation_id":str(uuid.uuid4()),"expected_version":0,"enabled":"on","expires_at":"2026-11-10 15:00:00",
            "recipient":"operator@example.test","language":"fr","action":"verify_expiration",
            "evidence_reference":"Console du fournisseur","reason":"Vérification manuelle",**changes}

    def test_configuration_page_is_localized_and_save_does_not_send_or_claim_renewal(self):
        self.client.force_login(self.operator)
        for language,label in (("fr","Hébergement et expiration"),("en","Hosting and expiration")):
            response=self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual(response.status_code,200,response.content)
            self.assertContains(response,label)
            self.assertContains(response,"Africa/Douala")
        self.assertFalse(HostingConfiguration.objects.exists())
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response=self.client.post(self.url(),self.form())
        self.assertEqual(response.status_code,302,response.content)
        self.assertIsNone(HostingConfiguration.objects.get().last_reported_renewal_at)
        self.assertEqual(HostingConfigurationChange.objects.count(),1)
        self.assertEqual(HostingReminderAttempt.objects.count(),0)
        self.assertEqual(len(getattr(mail,"outbox",[])),0)

    def test_owner_and_csrf_missing_post_are_refused_and_generic_add_is_disabled(self):
        self.owner.is_staff=True;self.owner.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code,403)
        protected=Client(enforce_csrf_checks=True);protected.force_login(self.operator)
        self.assertEqual(protected.get(self.url()).status_code,200)
        data=self.form()
        self.assertEqual(protected.post(self.url(),data).status_code,403)
        data["csrfmiddlewaretoken"]=protected.cookies["csrftoken"].value
        self.assertEqual(protected.post(self.url(),data).status_code,302)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("admin:backups_hostingconfiguration_add")).status_code,403)
        self.assertEqual(self.client.get(reverse("admin:backups_hostingreminderattempt_add")).status_code,403)

    def test_stale_form_does_not_overwrite_and_histories_link_to_the_operator_view(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(self.url(),self.form()).status_code,302)
        stale=self.client.post(self.url(),self.form(recipient="other@example.test"),HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(stale.status_code,200)
        self.assertContains(stale,"La configuration a changé")
        self.assertEqual(HostingConfiguration.objects.get().recipient,"operator@example.test")
        for name in ("admin:backups_hostingconfiguration_changelist","admin:backups_hostingreminder_changelist"):
            response=self.client.get(reverse(name))
            self.assertContains(response,f'href="{self.url()}"')

    def test_role_removed_between_admin_entry_and_service_read_returns_forbidden(self):
        from identity.models import User
        from backups.services.hosting import hosting_status
        self.client.force_login(self.operator)
        def withdrawn(*args,**kwargs):
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return hosting_status(*args,**kwargs)
        with patch("backups.hosting_admin.hosting_status",side_effect=withdrawn):
            response=self.client.get(self.url())
        self.assertEqual(response.status_code,403,response.content)
        self.assertFalse(HostingConfiguration.objects.exists())

    def test_operator_status_shows_dated_outage_and_its_evidence_in_both_languages(self):
        from backups.services.hosting import record_hosting_observation
        record_hosting_observation(self.operator,{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "observed_at":"2026-10-20T10:00:00Z","service_state":"down","installed_bytes":None,
            "evidence_reference":"Incident PA-privé-42","reason":"Panne constatée"})
        self.client.force_login(self.operator)
        for language,label in (("fr","Indisponible selon observation"),("en","Reported unavailable")):
            with self.subTest(language=language):
                response=self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE=language)
                self.assertContains(response,label)
                self.assertContains(response,"Incident PA-privé-42")

    def test_operator_can_record_a_dated_space_or_outage_observation_without_sending(self):
        from backups.models import HostingObservation
        self.client.force_login(self.operator)
        url=reverse("admin:backups_hostingobservation_record")
        response=self.client.get(url,HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code,200,response.content)
        self.assertIn("Observation de l'hébergement",unescape(response.content.decode()))
        data={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"observed_at":"2026-10-20 10:00:00",
            "installed_bytes":490000000,"temporary_peak_bytes":"","quota_used_bytes":500,"quota_limit_bytes":400,
            "service_state":"down","evidence_reference":"Incident constaté","reason":"Après reprise du service"}
        saved=self.client.post(url,data)
        self.assertEqual(saved.status_code,302,saved.content)
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(HostingObservation.objects.count(),1)
        self.assertEqual(HostingObservation.objects.get().service_state,"down")
        self.assertEqual(len(getattr(mail,"outbox",[])),0)

    def test_session_authenticated_json_api_rejects_upload_before_csrf_parses_files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        protected=Client(enforce_csrf_checks=True);protected.force_login(self.operator)
        self.assertEqual(protected.get(self.url()).status_code,200)
        # Parsing an upload here would consume temporary disk before rejecting
        # this JSON-only command. Keep that boundary observable with a tripwire.
        with patch("django.core.files.uploadhandler.MemoryFileUploadHandler.new_file",side_effect=RuntimeError("upload parsed")):
            response=protected.post("/api/v1/hosting/observations/",{"unrelated_upload":SimpleUploadedFile("probe.bin",b"local fixture")})
        self.assertEqual(response.status_code,415,response.content)
