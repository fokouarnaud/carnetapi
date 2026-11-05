from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User


class ClientDirectoryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="client-owner")
        self.seller = User.objects.create_user(username="client-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.membership = Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.seller)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return device

    def url(self, client_id=None, shop=None):
        base = f"/api/v1/shops/{(shop or self.shop).id}/clients/"
        return base if client_id is None else f"{base}{client_id}/"

    def payload(self, **changes):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Client de test"}
        data.update(changes)
        return data

    def create(self, data=None, shop=None):
        return self.client.post(self.url(shop=shop), data or self.payload(), format="json")

    def test_seller_creates_without_phone_or_credit_permission_and_retry_is_idempotent(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["id"], data["id"])
        self.assertIsNone(response.data["phone"])
        self.assertEqual(response.data["version"], 1)
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(self.client.get(self.url()).data["count"], 1)
        self.assertEqual(Activity.objects.filter(target_id=data["id"]).count(), 1)

    def test_same_name_and_phone_keep_distinct_uuids(self):
        first = self.payload(phone="+237600000001")
        second = self.payload(phone="+237600000001")
        self.assertEqual(self.create(first).status_code, 201)
        self.assertEqual(self.create(second).status_code, 201)
        listed = self.client.get(self.url())
        self.assertEqual(listed.data["count"], 2)
        self.assertEqual({c["id"] for c in listed.data["results"]}, {first["id"], second["id"]})

    def test_invalid_name_phone_or_unknown_field_do_not_create(self):
        for changes in [{"name": " "}, {"phone": "abc"}, {"phone": "600000001"},
                        {"shop_id": str(self.other.id)}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(self.payload(**changes)).status_code, 400)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)

    def test_reused_operation_or_client_uuid_with_changed_payload_is_refused(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.create({**data, "name": "Autre"}).status_code, 409)
        self.assertEqual(self.create({**data, "operation_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(self.client.get(self.url(data["id"])).data["name"], "Client de test")

    def test_manager_corrects_stable_uuid_with_before_after_audit_and_retry(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.authenticate(self.owner)
        correction = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                      "name": "Nom corrigé", "phone": "+237600000001", "reason": "Erreur de saisie"}
        response = self.client.patch(self.url(data["id"]), correction, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], data["id"])
        self.assertEqual(response.data["version"], 2)
        self.assertEqual(response.data["name"], "Nom corrigé")
        audit = Activity.objects.get(id=correction["operation_id"])
        self.assertEqual(audit.details["before"]["name"], "Client de test")
        self.assertEqual(audit.details["after"]["phone"], "+237600000001")
        retry = self.client.patch(self.url(data["id"]), correction, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(Activity.objects.filter(target_id=data["id"]).count(), 2)

    def test_seller_cannot_correct_but_can_report_without_modifying_client(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Changement", "reason": "Erreur"}
        self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 403)
        report = {"operation_id": str(uuid.uuid4()), "reason": "Merci de corriger le nom"}
        report_url = self.url(data["id"]) + "report-correction/"
        response = self.client.post(report_url, report, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.post(report_url, report, format="json").data, response.data)
        self.assertEqual(self.client.get(self.url(data["id"])).data["name"], "Client de test")
        self.assertEqual(Activity.objects.filter(action="client_correction_reported").count(), 1)

    def test_stale_correction_cannot_overwrite_manager_change(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.authenticate(self.owner)
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Premier", "reason": "Correction"}
        self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 200)
        stale = {**body, "operation_id": str(uuid.uuid4()), "name": "Deuxième"}
        self.assertEqual(self.client.patch(self.url(data["id"]), stale, format="json").status_code, 409)
        self.assertEqual(self.client.get(self.url(data["id"])).data["name"], "Premier")

    def test_cross_shop_client_uuid_and_revoked_device_are_hidden(self):
        data = self.payload()
        self.authenticate(self.owner)
        self.assertEqual(self.create(data, self.other).status_code, 201)
        device = self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(data["id"])).status_code, 404)
        self.assertEqual(self.client.get(self.url(shop=self.other)).status_code, 404)
        self.assertEqual(self.client.patch(self.url(data["id"]), {}, format="json").status_code, 404)
        ShopDeviceAccess.objects.create(shop=self.shop, installation=device, revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.create().status_code, 404)

    def test_phone_can_be_removed_but_reason_and_changed_field_are_required(self):
        data = self.payload(phone="+237600000001")
        self.assertEqual(self.create(data).status_code, 201)
        self.authenticate(self.owner)
        for extra in [{"reason": " " , "name": "Correction"}, {"reason": "Correction"}]:
            body = {"operation_id": str(uuid.uuid4()), "base_version": 1, **extra}
            self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 400)
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1, "phone": None, "reason": "Numéro erroné"}
        response = self.client.patch(self.url(data["id"]), body, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["phone"])

    def test_listing_is_shop_filtered_and_paginated(self):
        for index in range(3):
            self.assertEqual(self.create(self.payload(name=f"Client {index}")).status_code, 201)
        first = self.client.get(self.url(), {"page_size": 2})
        self.assertEqual(first.data["count"], 3)
        self.assertEqual(len(first.data["results"]), 2)
        second = self.client.get(first.data["next"])
        self.assertEqual(len(second.data["results"]), 1)
        self.assertFalse({c["id"] for c in first.data["results"]} & {c["id"] for c in second.data["results"]})

    def test_client_history_is_manager_only_and_excludes_other_clients(self):
        first, second = self.payload(), self.payload()
        self.assertEqual(self.create(first).status_code, 201)
        self.assertEqual(self.create(second).status_code, 201)
        history_url = self.url(first["id"]) + "history/"
        self.assertEqual(self.client.get(history_url).status_code, 403)
        report = {"operation_id": str(uuid.uuid4()), "reason": "Le nom doit être corrigé"}
        self.assertEqual(self.client.post(self.url(first["id"]) + "report-correction/", report, format="json").status_code, 201)
        self.authenticate(self.owner)
        history = self.client.get(history_url)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 2)
        self.assertEqual({event["action"] for event in history.data["results"]}, {"client_created", "client_correction_reported"})
        self.assertEqual({event["target_id"] for event in history.data["results"]}, {first["id"]})

    def test_existing_jwt_loses_correction_permission_and_client_history_immediately(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.membership.role = Membership.Role.MANAGER
        self.membership.save()
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Corrigé", "reason": "Erreur"}
        self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 200)
        self.membership.role = Membership.Role.SELLER
        self.membership.save()
        self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url(data["id"]) + "history/").status_code, 403)
