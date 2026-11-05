"""US-46: operator-reported expiration and bounded reminder actions are separate."""
from datetime import datetime, timezone as utc
import uuid
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from identity.models import User


class HostingApiTests(TestCase):
    def setUp(self):
        self.operator=User.objects.create_superuser(username="hosting-operator",password=None)
        self.owner=User.objects.create_user(username="hosting-owner")
        self.client=APIClient();self.client.force_authenticate(self.operator)
        self.settings_override=override_settings(HOSTING_REMINDER_TOKEN="h"*48,HOSTING_REMINDER_ACTOR_ID=str(self.operator.id),
            DEFAULT_FROM_EMAIL="sender@example.test")
        self.settings_override.enable();self.addCleanup(self.settings_override.disable)

    def configure(self,**changes):
        data={"operation_id":str(uuid.uuid4()),"expected_version":0,"enabled":True,
            "expires_at":"2026-11-10T15:00:00Z","recipient":"operator@example.test","language":"fr",
            "action":"verify_expiration","evidence_reference":"Console hébergeur, constat du 1 novembre",
            "reason":"Date et destinataire vérifiés",**changes}
        response=self.client.patch("/api/v1/hosting/configuration/",data,format="json")
        return response,data

    def trigger(self,at):
        api=APIClient();api.credentials(HTTP_AUTHORIZATION="Bearer "+"h"*48)
        with patch("django.utils.timezone.now",return_value=at):
            return api.post("/api/v1/hosting/trigger/",{},format="json")

    def test_unconfigured_status_is_unknown_and_get_never_sends_or_creates_settings(self):
        response=self.client.get("/api/v1/hosting/configuration/")
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.data["version"],0)
        self.assertIsNone(response.data["expires_at"])
        status=self.client.get("/api/v1/hosting/status/")
        self.assertEqual(status.status_code,200,status.content)
        self.assertEqual(status.data["expiration_state"],"unknown")
        self.assertIsNone(status.data["reported_space"])
        self.assertEqual(len(getattr(mail,"outbox",[])),0)

    def test_configuration_is_versioned_audited_and_replaying_it_does_not_claim_a_renewal(self):
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response,command=self.configure()
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(response.data["version"],1)
            self.assertIsNone(response.data["last_reported_renewal_at"])
            same=self.client.patch("/api/v1/hosting/configuration/",command,format="json")
            self.assertEqual(same.data,response.data)
            self.assertEqual(self.configure(expected_version=0)[0].status_code,409)
            renewal,_=self.configure(expected_version=1,action="confirm_renewal",expires_at="2026-12-10T15:00:00Z")
            self.assertEqual(renewal.status_code,200,renewal.content)
            self.assertIsNotNone(renewal.data["last_reported_renewal_at"])
        self.assertEqual(len(getattr(mail,"outbox",[])),0)

    def test_only_j7_and_j2_calendar_days_in_douala_send_once_without_extending_expiry(self):
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            configured,_=self.configure()
        self.assertEqual(configured.status_code,200,configured.content)
        before=datetime(2026,11,2,22,59,tzinfo=utc.utc)
        self.assertEqual(self.trigger(before).data["status"],"idle")
        j7=datetime(2026,11,2,23,0,tzinfo=utc.utc)
        accepted=self.trigger(j7)
        self.assertEqual(accepted.status_code,200,accepted.content)
        self.assertEqual(accepted.data["status"],"accepted")
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(mail.outbox[0].to,["operator@example.test"])
        self.assertIn("7",mail.outbox[0].subject)
        self.assertEqual(self.trigger(j7).data["status"],"idle")
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(self.trigger(datetime(2026,11,8,tzinfo=utc.utc)).data["status"],"accepted")
        self.assertEqual(len(mail.outbox),2)
        current=self.client.get("/api/v1/hosting/configuration/")
        self.assertEqual(current.data["expires_at"],"2026-11-10T15:00:00Z")
        self.assertIsNone(current.data["last_reported_renewal_at"])

    def test_owner_staff_and_trigger_secret_do_not_grant_configuration_or_health_access(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.configure()[0].status_code,403)
        self.assertEqual(self.client.get("/api/v1/hosting/status/").status_code,403)
        self.owner.is_staff=True;self.owner.save()
        self.assertEqual(self.configure()[0].status_code,403)
        opaque=APIClient();opaque.credentials(HTTP_AUTHORIZATION="Bearer "+"h"*48)
        self.assertEqual(opaque.get("/api/v1/hosting/status/").status_code,401)
        self.assertEqual(APIClient().post("/api/v1/hosting/trigger/",{},format="json").status_code,401)

    def test_failed_or_uncertain_delivery_is_visible_and_not_automatically_resent(self):
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response,_=self.configure()
        self.assertEqual(response.status_code,200,response.content)
        j7=datetime(2026,11,3,tzinfo=utc.utc)
        with patch("django.core.mail.message.EmailMessage.send",side_effect=TimeoutError("private transport failure")):
            result=self.trigger(j7)
        self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(result.data["status"],"uncertain")
        self.assertNotIn("private transport failure",str(result.data))
        self.assertEqual(self.trigger(j7).data["status"],"idle")
        with patch("django.utils.timezone.now",return_value=j7):
            status=self.client.get("/api/v1/hosting/status/")
        self.assertTrue(status.data["requires_attention"])
        self.assertEqual(status.data["reminders"][0]["state"],"uncertain")
        self.assertEqual(len(getattr(mail,"outbox",[])),0)

    def test_expiry_and_space_are_dated_reports_without_an_automatic_renewal_or_budget_claim(self):
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response,_=self.configure()
        self.assertEqual(response.status_code,200,response.content)
        observation=self.client.post("/api/v1/hosting/observations/",{
            "id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"observed_at":"2026-10-20T10:00:00Z",
            "installed_bytes":500000000,"temporary_peak_bytes":1000,"quota_used_bytes":600,
            "quota_limit_bytes":500,"service_state":"up","evidence_reference":"Rapport opérateur",
            "reason":"Mesure du déploiement"},format="json")
        self.assertEqual(observation.status_code,201,observation.content)
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,11,tzinfo=utc.utc)):
            status=self.client.get("/api/v1/hosting/status/")
        self.assertEqual(status.status_code,200,status.content)
        self.assertEqual(status.data["expiration_state"],"expired")
        self.assertEqual(status.data["reported_space"]["installed_budget_state"],"reported_limit_exceeded")
        self.assertEqual(status.data["reported_space"]["quota_remaining_bytes"],"-100")
        self.assertTrue(status.data["requires_attention"])

    def test_reported_byte_counts_are_integers_in_sql_not_only_in_the_json_serializer(self):
        from django.db import connection, IntegrityError, transaction
        from backups.models import HostingObservation
        observation=HostingObservation.objects.create(operation_id=uuid.uuid4(),actor=self.operator,
            observed_at=datetime(2026,10,20,tzinfo=utc.utc),installed_bytes=499999999,temporary_peak_bytes=None,
            service_state="unknown",evidence_reference="Mesure privée",reason="Contrôle SQL",request={},receipt={})
        for name in ("installed_bytes","temporary_peak_bytes","quota_used_bytes","quota_limit_bytes"):
            with self.subTest(field=name),self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
                cursor.execute(f"UPDATE backups_hostingobservation SET {connection.ops.quote_name(name)}=%s WHERE id=%s",[1.5,observation.id.hex])
