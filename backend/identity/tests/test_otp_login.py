"""US-01 / G4: OTP login codes — 6 digits, 10 minutes, 5 attempts, global 10/day budget."""
from datetime import timedelta
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from identity.models import DeviceSession, Installation, OtpChallenge, User
from identity.services import otp
from identity.services.sms_budget import SmsBudgetStore
from identity.services.sms_gateway import SmsSendResult, SmsSendStatus

PHONE = "+237670000001"


class OtpLoginTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        budget_path = Path(self.tmp.name) / "sms-budget.sqlite3"
        SmsBudgetStore(budget_path).initialize(operator_id=uuid.uuid4(), reason="test", fresh_deployment=True)
        settings = override_settings(OTP_SMS_TRANSPORT="fake", SMS_BUDGET_PATH=str(budget_path))
        settings.enable()
        self.addCleanup(settings.disable)
        otp.fake_outbox.clear()
        self.client = APIClient()

    def request_code(self, phone=PHONE, purpose="signup", operation_id=None):
        return self.client.post("/api/v1/auth/otp/request/", {
            "operation_id": str(operation_id or uuid.uuid4()), "phone": phone, "purpose": purpose,
        }, format="json")

    def verify(self, challenge_id, code, label="Tecno A", installation_id=None):
        body = {"challenge_id": str(challenge_id), "code": code, "installation_label": label}
        if installation_id is not None:
            body["installation_id"] = str(installation_id)
        return self.client.post("/api/v1/auth/otp/verify/", body, format="json")

    def last_code(self):
        return otp.fake_outbox[-1]["message"].split(":")[1].strip()[:6]

    def test_request_sends_six_digit_code_valid_ten_minutes_and_stores_no_plaintext(self):
        response = self.request_code()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(len(otp.fake_outbox), 1)
        self.assertEqual(otp.fake_outbox[0]["phone"], PHONE)
        code = self.last_code()
        self.assertRegex(code, r"^[0-9]{6}$")
        challenge = OtpChallenge.objects.get(id=response.data["challenge_id"])
        self.assertNotIn(code, challenge.code_hash)
        self.assertEqual(challenge.purpose, "signup")
        remaining = challenge.expires_at - challenge.created_at
        self.assertEqual(remaining, timedelta(minutes=10))

    def test_signup_verify_creates_account_installation_and_device_session(self):
        challenge_id = self.request_code().data["challenge_id"]
        response = self.verify(challenge_id, self.last_code())
        self.assertEqual(response.status_code, 200, response.data)
        user = User.objects.get(phone=PHONE)
        installation = Installation.objects.get(id=response.data["installation_id"])
        self.assertEqual(installation.user, user)
        self.assertNotEqual(installation.id, user.id)
        self.assertEqual(response.data["user_id"], str(user.id))
        access = AccessToken(response.data["access"])
        refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(access["exp"] - access["iat"], 300)
        session = DeviceSession.objects.get(id=access["session_id"])
        self.assertEqual(session.installation, installation)
        self.assertEqual(session.refresh_jti, refresh["jti"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 200)

    def test_new_device_reuses_account_with_a_new_installation(self):
        first = self.verify(self.request_code().data["challenge_id"], self.last_code()).data
        second_request = self.request_code(purpose="new_device")
        self.assertEqual(second_request.status_code, 202, second_request.data)
        second = self.verify(second_request.data["challenge_id"], self.last_code()).data
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertNotEqual(first["installation_id"], second["installation_id"])
        self.assertEqual(User.objects.filter(phone=PHONE).count(), 1)

    def test_reconnect_reopens_the_same_installation_and_ends_its_old_sessions(self):
        # Decision of 14 November 2026: forgotten PIN or expired session on the same phone.
        first = self.verify(self.request_code().data["challenge_id"], self.last_code()).data
        old_session = DeviceSession.objects.get(id=AccessToken(first["access"])["session_id"])
        again = self.request_code(purpose="new_device")
        response = self.verify(again.data["challenge_id"], self.last_code(),
                               installation_id=first["installation_id"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["installation_id"], first["installation_id"])
        self.assertEqual(Installation.objects.filter(user__phone=PHONE).count(), 1)
        new_session = DeviceSession.objects.get(id=AccessToken(response.data["access"])["session_id"])
        self.assertEqual(str(new_session.installation_id), first["installation_id"])
        old_session.refresh_from_db()
        self.assertIsNotNone(old_session.revoked_at)
        self.assertIsNone(new_session.revoked_at)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_reconnect_refuses_another_account_or_a_withdrawn_installation(self):
        mine = self.verify(self.request_code().data["challenge_id"], self.last_code()).data
        other = self.verify(self.request_code(phone="+237670000002").data["challenge_id"],
                            self.last_code()).data
        challenge = self.request_code(purpose="new_device").data["challenge_id"]
        code = self.last_code()
        refused = self.verify(challenge, code, installation_id=other["installation_id"])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["code"], "otp_installation_refused")
        # A refusal does not consume the code: the same code still works.
        Installation.objects.filter(id=mine["installation_id"]).update(revoked_at=timezone.now())
        self.assertEqual(self.verify(challenge, code, installation_id=mine["installation_id"]).status_code, 403)
        self.assertEqual(self.verify(challenge, code, installation_id=uuid.uuid4()).status_code, 403)
        self.assertEqual(self.verify(challenge, code).status_code, 200)

    def test_signup_never_reuses_an_installation(self):
        mine = self.verify(self.request_code().data["challenge_id"], self.last_code()).data
        challenge = self.request_code(phone="+237670000002").data["challenge_id"]
        refused = self.verify(challenge, self.last_code(), installation_id=mine["installation_id"])
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_purpose_mismatch_is_refused_before_spending_an_sms(self):
        self.assertEqual(self.request_code(purpose="new_device").status_code, 404)
        self.verify(self.request_code().data["challenge_id"], self.last_code())
        sent = len(otp.fake_outbox)
        self.assertEqual(self.request_code(purpose="signup").status_code, 409)
        self.assertEqual(len(otp.fake_outbox), sent)

    def test_only_cameroon_numbers_and_known_purposes_are_accepted(self):
        self.assertEqual(self.request_code(phone="+33612345678").status_code, 400)
        self.assertEqual(self.request_code(phone="+23767000000").status_code, 400)
        self.assertEqual(self.request_code(purpose="recovery_by_declaration").status_code, 400)
        self.assertEqual(otp.fake_outbox, [])

    def test_fifth_wrong_attempt_exhausts_the_code(self):
        challenge_id = self.request_code().data["challenge_id"]
        code = self.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for remaining in (4, 3, 2, 1):
            response = self.verify(challenge_id, wrong)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["attempts_remaining"], remaining)
        self.assertEqual(self.verify(challenge_id, wrong).status_code, 410)
        self.assertEqual(self.verify(challenge_id, code).status_code, 410)
        self.assertFalse(User.objects.filter(phone=PHONE).exists())

    def test_expired_code_is_refused(self):
        challenge_id = self.request_code().data["challenge_id"]
        code = self.last_code()
        OtpChallenge.objects.filter(id=challenge_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify(challenge_id, code).status_code, 410)
        self.assertFalse(Installation.objects.exists())

    def test_code_is_consumed_once(self):
        challenge_id = self.request_code().data["challenge_id"]
        code = self.last_code()
        self.assertEqual(self.verify(challenge_id, code).status_code, 200)
        self.assertEqual(self.verify(challenge_id, code).status_code, 410)
        self.assertEqual(Installation.objects.count(), 1)

    def test_replayed_request_returns_same_challenge_without_new_sms(self):
        operation_id = uuid.uuid4()
        first = self.request_code(operation_id=operation_id)
        second = self.request_code(operation_id=operation_id)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.data["challenge_id"], second.data["challenge_id"])
        self.assertEqual(len(otp.fake_outbox), 1)
        self.assertEqual(self.request_code(phone="+237670000002", operation_id=operation_id).status_code, 409)

    def test_global_daily_budget_of_ten_sends_includes_resends(self):
        for index in range(10):
            self.assertEqual(self.request_code(phone=f"+2376700000{index:02d}").status_code, 202)
        response = self.request_code(phone="+237670000099")
        self.assertEqual(response.status_code, 429)
        self.assertIn("next_allowed_at", response.data)
        self.assertEqual(len(otp.fake_outbox), 10)

    def test_unavailable_transport_leaves_challenge_unusable(self):
        failing = mock.Mock()
        failing.send.return_value = SmsSendResult(SmsSendStatus.UNAVAILABLE, "configuration_missing")
        with mock.patch.object(otp, "sms_gateway", return_value=failing):
            response = self.request_code()
        self.assertEqual(response.status_code, 503)
        challenge = OtpChallenge.objects.get()
        self.assertEqual(challenge.send_status, "unavailable")
        self.assertEqual(self.verify(challenge.id, "123456").status_code, 410)

    def test_fake_transport_refuses_to_start_in_production(self):
        with override_settings(APP_ENV="production", OTP_SMS_TRANSPORT="fake"):
            with self.assertRaises(ImproperlyConfigured):
                otp.sms_gateway()
