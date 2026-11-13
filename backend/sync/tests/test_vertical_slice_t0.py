"""DOD-14 (backend half): OTP → activation → sale on phone A → changes feed on phone B.

Exercises only real API routes, no fixture shortcuts for identity: both devices
obtain their sessions through the dev/test OTP transport (G4).
"""
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from identity.services import otp
from identity.services.sms_budget import SmsBudgetStore
from sales.models import Sale

PHONE = "+237670000777"


class VerticalSliceT0Tests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        budget = Path(tmp.name) / "budget.sqlite3"
        SmsBudgetStore(budget).initialize(operator_id=uuid.uuid4(), reason="t0", fresh_deployment=True)
        overrides = override_settings(OTP_SMS_TRANSPORT="fake", SMS_BUDGET_PATH=str(budget))
        overrides.enable()
        self.addCleanup(overrides.disable)
        otp.fake_outbox.clear()

    def activate(self, purpose, label):
        client = APIClient()
        challenge = client.post("/api/v1/auth/otp/request/", {
            "operation_id": str(uuid.uuid4()), "phone": PHONE, "purpose": purpose}, format="json")
        self.assertEqual(challenge.status_code, 202, challenge.data)
        code = otp.fake_outbox[-1]["message"].split(":")[1].strip()[:6]
        session = client.post("/api/v1/auth/otp/verify/", {
            "challenge_id": challenge.data["challenge_id"], "code": code, "installation_label": label}, format="json")
        self.assertEqual(session.status_code, 200, session.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.data['access']}")
        return client, session.data

    def test_sale_on_phone_a_reaches_phone_b_through_the_feed_once(self):
        phone_a, identity_a = self.activate("signup", "Phone A")
        shop_id = str(uuid.uuid4())
        created = phone_a.post("/api/v1/shops/", {"id": shop_id, "operation_id": str(uuid.uuid4()),
                                                  "name": "Boutique T0"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        lease_a = phone_a.post(f"/api/v1/shops/{shop_id}/verify-access/", {}, format="json")
        self.assertEqual(lease_a.status_code, 200, lease_a.data)
        product_id = str(uuid.uuid4())
        product = phone_a.post(f"/api/v1/shops/{shop_id}/products/", {
            "id": product_id, "operation_id": str(uuid.uuid4()), "name": "Savon", "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 500,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": "300", "reason": "Reprise",
                        "occurred_at": (timezone.now() - timedelta(minutes=1)).isoformat()},
        }, format="json")
        self.assertEqual(product.status_code, 201, product.data)

        sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": product_id, "product_version": 1,
                           "quantity_milli": 2000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        url = f"/api/v1/shops/{shop_id}/sales/"
        self.assertEqual(phone_a.post(url, sale, format="json").status_code, 201)
        # Lost response then retry (US-36): same operation, one effect.
        self.assertEqual(phone_a.post(url, sale, format="json").status_code, 200)
        self.assertEqual(Sale.objects.filter(id=sale["id"]).count(), 1)

        phone_b, identity_b = self.activate("new_device", "Phone B")
        self.assertEqual(identity_a["user_id"], identity_b["user_id"])
        self.assertNotEqual(identity_a["installation_id"], identity_b["installation_id"])
        lease_b = phone_b.post(f"/api/v1/shops/{shop_id}/verify-access/", {}, format="json")
        self.assertEqual(lease_b.status_code, 200, lease_b.data)
        self.assertNotEqual(lease_a.data["installation_code"], lease_b.data["installation_code"])

        seen, after = [], 0
        while True:
            page = phone_b.get(f"/api/v1/shops/{shop_id}/changes/?after={after}&limit=2")
            self.assertEqual(page.status_code, 200, page.data)
            seen += page.data["results"]
            after = page.data["next_after"]
            if not page.data["has_more"]:
                break
        sale_entries = [row for row in seen if row["object_id"] == sale["id"]]
        self.assertEqual(len(sale_entries), 1)
        self.assertIn(product_id, [row["object_id"] for row in seen])
        self.assertEqual([row["seq"] for row in seen], sorted({row["seq"] for row in seen}))
        # Resuming from the final cursor yields nothing new.
        self.assertEqual(phone_b.get(f"/api/v1/shops/{shop_id}/changes/?after={after}").data["results"], [])
