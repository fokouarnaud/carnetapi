"""US-42: daily configuration is exact, versioned and does not execute a schedule."""
from datetime import datetime, timezone
from unittest.mock import patch
import uuid
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from identity.models import User


class BackupConfigurationFixtures:
    def setUp(self):
        self.operator=User.objects.create_superuser(username="schedule-operator",password=None)
        self.owner=User.objects.create_user(username="schedule-owner")
        self.api=APIClient()
        self.api.force_authenticate(self.operator)
        self.url="/api/v1/backups/configuration/"

    def change(self,**values):
        return self.api.patch(self.url,{"operation_id":str(uuid.uuid4()),"expected_version":0,
            "enabled":True,"local_hour":2,"local_minute":0,"reason":"Horaire confirmé",**values},format="json")



class BackupConfigurationTests(BackupConfigurationFixtures,TransactionTestCase):
    def test_default_is_enabled_at_two_douala_without_writing_or_sending_on_get(self):
        with patch("django.utils.timezone.now",return_value=datetime(2026,10,19,0,59,tzinfo=timezone.utc)),override_settings(EMAIL_HOST_USER="sender@example.test"):
            response=self.api.get(self.url)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.data["enabled"])
        self.assertEqual((response.data["local_hour"],response.data["local_minute"]),(2,0))
        self.assertEqual(response.data["timezone_name"],"Africa/Douala")
        self.assertEqual(response.data["next_run_at"],"2026-10-19T01:00:00+00:00")
        self.assertEqual(response.data["automatic_recipient"],"sender@example.test")
        self.assertEqual(response.data["version"],0)
        from django.apps import apps
        self.assertEqual(apps.get_model("backups","BackupConfiguration").objects.count(),0)

    def test_edit_is_audited_idempotent_and_rejects_stale_versions(self):
        operation=str(uuid.uuid4())
        first=self.change(operation_id=operation,local_hour=23,local_minute=45)
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.data["version"],1)
        self.assertEqual((first.data["local_hour"],first.data["local_minute"]),(23,45))
        second=self.change(expected_version=1,enabled=False)
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.data["version"],2)
        self.assertIsNone(second.data["next_run_at"])
        replay=self.change(operation_id=operation,local_hour=23,local_minute=45)
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.data,first.data)
        self.assertEqual(self.change(expected_version=1).status_code,409)
        self.assertEqual(self.change(operation_id=operation).status_code,409)
        from django.apps import apps
        changes=apps.get_model("backups","BackupConfigurationChange").objects.order_by("version")
        self.assertEqual(changes.count(),2)
        self.assertEqual(changes[0].before["version"],0)
        self.assertEqual(changes[0].after,first.data)
        self.assertEqual(changes[1].actor_id,self.operator.id)

    def test_exact_boundary_and_next_day_preview_are_utc_and_disabled_has_no_due_date(self):
        for moment,expected in (
            (datetime(2026,10,19,1,0,tzinfo=timezone.utc),"2026-10-19T01:00:00+00:00"),
            (datetime(2026,10,19,1,0,0,1,tzinfo=timezone.utc),"2026-10-20T01:00:00+00:00"),
        ):
            with self.subTest(moment=moment),patch("django.utils.timezone.now",return_value=moment):
                response=self.api.get(self.url)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.data["next_run_at"],expected)
        self.assertEqual(self.change(enabled=False).status_code,200)
        self.assertIsNone(self.api.get(self.url).data["next_run_at"])

    def test_owner_and_invalid_hours_or_types_are_refused_without_configuration_change(self):
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(self.url).status_code,403)
        self.assertEqual(self.change().status_code,403)
        self.api.force_authenticate(self.operator)
        for values in ({"local_hour":24},{"local_minute":60},{"local_hour":1.5},{"enabled":1},
                {"local_minute":"30"},{"timezone_name":"UTC"},{"reason":"  "}):
            with self.subTest(values=values):
                self.assertEqual(self.change(**values).status_code,400)
        from django.apps import apps
        self.assertFalse(apps.get_model("backups","BackupConfigurationChange").objects.exists())

    def test_model_rejects_fractional_civil_time_second_singleton_and_non_boolean_enabled(self):
        from django.apps import apps
        from django.db import connection,IntegrityError,transaction
        model=apps.get_model("backups","BackupConfiguration")
        saved=model.objects.create()
        for column,value in (("local_hour",24),("local_hour",2.5),("local_minute",60),
                ("local_minute",0.5),("singleton",2),("enabled",2),("version",1.5)):
            with self.subTest(column=column,value=value),self.assertRaises(IntegrityError),transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE backups_backupconfiguration SET "{column}"=%s WHERE id=%s',[value,saved.id.hex])
        with self.assertRaises(IntegrityError),transaction.atomic():
            model.objects.create(id=uuid.uuid4())

    def test_configuration_and_history_rollback_together_when_audit_storage_fails(self):
        from django.db import OperationalError
        from backups.models import BackupConfiguration,BackupConfigurationChange
        first=self.change()
        self.assertEqual(first.status_code,200)
        with patch.object(BackupConfigurationChange.objects,"create",side_effect=OperationalError("audit unavailable")):
            with self.assertRaises(OperationalError):
                self.change(expected_version=1,local_hour=12)
        current=BackupConfiguration.objects.get()
        self.assertEqual((current.version,current.local_hour,current.local_minute),(1,2,0))
        self.assertEqual(BackupConfigurationChange.objects.count(),1)

    def test_session_revoked_during_input_validation_does_not_change_schedule(self):
        from datetime import timedelta
        from django.utils import timezone as django_timezone
        from identity.models import Installation,DeviceSession
        from identity.services.authentication import DeviceJWTAuthentication
        from rest_framework_simplejwt.tokens import AccessToken
        installation=Installation.objects.create(user=self.operator)
        session=DeviceSession.objects.create(installation=installation,expires_at=django_timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(self.operator)
        token["session_id"]=str(session.id)
        self.api=APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        original=DeviceJWTAuthentication.authenticate
        def removed(authenticator,request):
            result=original(authenticator,request)
            DeviceSession.objects.filter(id=session.id).update(revoked_at=django_timezone.now())
            return result
        with patch.object(DeviceJWTAuthentication,"authenticate",autospec=True,side_effect=removed):
            response=self.change()
        self.assertEqual(response.status_code,401)
        from backups.models import BackupConfiguration,BackupConfigurationChange
        self.assertFalse(BackupConfiguration.objects.exists())
        self.assertFalse(BackupConfigurationChange.objects.exists())
