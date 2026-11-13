"""US-01 / US-07 / Q154: single-use recovery codes, then OTP on the new number; same account UUID."""
import tempfile
import uuid
from pathlib import Path

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from identity.models import Installation, Membership, RecoveryCode, Shop, User
from identity.services import otp
from identity.services.sms_budget import SmsBudgetStore

OLD_PHONE = "+237670000111"
NEW_PHONE = "+237690000222"


class RecoveryCodeTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        budget = Path(tmp.name) / "budget.sqlite3"
        SmsBudgetStore(budget).initialize(operator_id=uuid.uuid4(), reason="test", fresh_deployment=True)
        overrides = override_settings(OTP_SMS_TRANSPORT="fake", SMS_BUDGET_PATH=str(budget))
        overrides.enable()
        self.addCleanup(overrides.disable)
        otp.fake_outbox.clear()
        self.client, self.identity = self.otp_login({"phone": OLD_PHONE, "purpose": "signup"})

    def code_from_outbox(self):
        return otp.fake_outbox[-1]["message"].split(":")[1].strip()[:6]

    def otp_login(self, request, label="Phone"):
        client = APIClient()
        challenge = client.post("/api/v1/auth/otp/request/", {"operation_id": str(uuid.uuid4()), **request},
                                format="json")
        self.assertEqual(challenge.status_code, 202, challenge.data)
        session = client.post("/api/v1/auth/otp/verify/", {
            "challenge_id": challenge.data["challenge_id"], "code": self.code_from_outbox(),
            "installation_label": label}, format="json")
        self.assertEqual(session.status_code, 200, session.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.data['access']}")
        return client, session.data

    def generate(self, client=None):
        return (client or self.client).post("/api/v1/account/recovery-codes/", {}, format="json")

    def recovery_request(self, code, phone=NEW_PHONE, old_phone=OLD_PHONE):
        return APIClient().post("/api/v1/auth/otp/request/", {
            "operation_id": str(uuid.uuid4()), "phone": phone, "purpose": "recovery",
            "old_phone": old_phone, "recovery_code": code}, format="json")

    def test_generation_returns_ten_high_entropy_codes_once_and_stores_only_hashes(self):
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        codes = response.data["codes"]
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
        stored = list(RecoveryCode.objects.values_list("code_hash", flat=True))
        self.assertEqual(len(stored), 10)
        self.assertFalse(any(code.replace("-", "") in value for code in codes for value in stored))

    def test_anonymous_cannot_generate_codes(self):
        self.assertEqual(self.generate(APIClient()).status_code, 401)

    def test_regeneration_invalidates_previous_unused_codes(self):
        old = self.generate().data["codes"]
        new = self.generate().data["codes"]
        self.assertEqual(self.recovery_request(old[0]).status_code, 404)
        self.assertEqual(self.recovery_request(new[0]).status_code, 202)

    def test_recovery_moves_account_to_new_number_keeping_uuid_and_memberships(self):
        owner = User.objects.get(id=self.identity["user_id"])
        shop = Shop.objects.create(name="A", owner=owner)
        code = self.generate().data["codes"][3]
        challenge = self.recovery_request(code)
        self.assertEqual(challenge.status_code, 202, challenge.data)
        self.assertEqual(otp.fake_outbox[-1]["phone"], NEW_PHONE)
        response = APIClient().post("/api/v1/auth/otp/verify/", {
            "challenge_id": challenge.data["challenge_id"], "code": self.code_from_outbox(),
            "installation_label": "Replacement"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["user_id"], self.identity["user_id"])
        owner.refresh_from_db()
        self.assertEqual(owner.phone, NEW_PHONE)
        self.assertEqual(Shop.objects.get(id=shop.id).owner_id, owner.id)
        self.assertEqual(Installation.objects.filter(user=owner).count(), 2)
        self.assertIsNotNone(RecoveryCode.objects.get(account=owner, consumed_at__isnull=False).consumed_at)
        # The same code is single-use.
        self.assertEqual(self.recovery_request(code, phone="+237690000333", old_phone=NEW_PHONE).status_code, 404)

    def test_recovery_does_not_reactivate_a_withdrawn_membership(self):
        owner = User.objects.create_user(username="other-owner")
        shop = Shop.objects.create(name="B", owner=owner)
        Membership.objects.create(shop=shop, user_id=self.identity["user_id"], is_active=False)
        code = self.generate().data["codes"][0]
        challenge = self.recovery_request(code).data
        APIClient().post("/api/v1/auth/otp/verify/", {"challenge_id": challenge["challenge_id"],
                                                      "code": self.code_from_outbox()}, format="json")
        self.assertFalse(Membership.objects.get(shop=shop).is_active)

    def test_wrong_code_or_wrong_old_number_spends_no_sms(self):
        code = self.generate().data["codes"][0]
        sent = len(otp.fake_outbox)
        self.assertEqual(self.recovery_request("ABCD-EFGH-JKMN").status_code, 404)
        self.assertEqual(self.recovery_request(code, old_phone="+237670009999").status_code, 404)
        self.assertEqual(self.recovery_request(code.lower()).status_code, 202)
        self.assertEqual(len(otp.fake_outbox), sent + 1)

    def test_new_number_already_used_by_another_account_is_refused(self):
        self.otp_login({"phone": NEW_PHONE, "purpose": "signup"})
        code = self.generate().data["codes"][0]
        self.assertEqual(self.recovery_request(code).status_code, 409)

    def test_code_consumed_by_a_concurrent_recovery_cannot_complete_a_second_one(self):
        code = self.generate().data["codes"][0]
        first = self.recovery_request(code).data
        first_code = self.code_from_outbox()
        second = self.recovery_request(code, phone="+237690000444").data
        second_code = self.code_from_outbox()
        ok = APIClient().post("/api/v1/auth/otp/verify/", {"challenge_id": first["challenge_id"], "code": first_code},
                              format="json")
        self.assertEqual(ok.status_code, 200)
        late = APIClient().post("/api/v1/auth/otp/verify/", {"challenge_id": second["challenge_id"],
                                                             "code": second_code}, format="json")
        self.assertEqual(late.status_code, 410)
        self.assertEqual(User.objects.get(id=self.identity["user_id"]).phone, NEW_PHONE)

    def test_recovery_fields_are_only_accepted_with_recovery_purpose(self):
        response = APIClient().post("/api/v1/auth/otp/request/", {
            "operation_id": str(uuid.uuid4()), "phone": NEW_PHONE, "purpose": "signup",
            "recovery_code": "ABCD-EFGH-JKMN"}, format="json")
        self.assertEqual(response.status_code, 400)
