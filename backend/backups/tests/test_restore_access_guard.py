"""A current restoration review is scoped to each account and shop."""
import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.utils.translation import override
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from identity.models import DeviceSession, Installation, Membership, Shop, User
from identity.services.access import accessible_shops, effective_permissions


class RestoreAccessGuardTests(TestCase):
    def test_pending_access_message_is_translated_in_french(self):
        with override("fr"), self.assertRaisesMessage(PermissionDenied,
                "L’accès à cette boutique exige une confirmation après restauration."):
            effective_permissions(self.b, self.owner)

    def test_pending_owner_cannot_revoke_an_installation_through_a_direct_service_call(self):
        from identity.services.device_services import revoke_device
        session = DeviceSession.objects.create(installation=self.installation,
            expires_at=timezone.now() + timedelta(days=30))
        with self.assertRaises(PermissionDenied):
            revoke_device(self.b, self.owner, self.installation.id, "Contrôle", uuid.uuid4(),
                session_id=session.id, actor_installation=self.installation)

    def setUp(self):
        self.owner = User.objects.create_user(username="held-owner")
        self.a = Shop.objects.create(owner=self.owner, name="A")
        self.b = Shop.objects.create(owner=self.owner, name="B")
        self.installation = Installation.objects.create(user=self.owner)
        self.generation = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),
            backup_id=uuid.uuid4(), initiated_by=uuid.uuid4())
        flags = dict.fromkeys(("can_grant_credit", "can_collect_debt", "can_read_debt",
            "can_prepare_reminder", "can_read_cost", "can_read_cash_balance"), True)
        self.reviews = [RestoreAccessReview.objects.create(generation=self.generation,
            shop=shop, user=self.owner, archived_role="owner", was_active=True, archived_permissions=flags)
            for shop in (self.a, self.b)]

    def decide(self, review, state="confirmed", version=1):
        return RestoreAccessDecision.objects.create(review=review, operation_id=uuid.uuid4(),
            requested_by=self.owner, reason="Contrôle actuel", previous_version=version-1, version=version, state=state)

    def test_confirmation_a_does_not_expose_b_through_lists_detail_or_direct_permissions(self):
        self.decide(self.reviews[0])
        self.assertEqual(list(accessible_shops(self.owner, self.installation)), [self.a])
        self.assertEqual(effective_permissions(self.a, self.owner)[0], "manager")
        with self.assertRaises(PermissionDenied):
            effective_permissions(self.b, self.owner)
        session = DeviceSession.objects.create(installation=self.installation, expires_at=timezone.now()+timedelta(days=30))
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(session.id)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get("/api/v1/shops/?page_size=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(client.get(f"/api/v1/shops/{self.b.id}/").status_code, 404)

    def test_latest_refusal_and_new_generation_override_archived_confirmation(self):
        self.decide(self.reviews[0])
        self.decide(self.reviews[0], state="refused", version=2)
        self.assertFalse(accessible_shops(self.owner, self.installation).exists())
        with self.assertRaises(PermissionDenied):
            effective_permissions(self.a, self.owner)
        self.decide(self.reviews[0], version=3)
        self.assertEqual(list(accessible_shops(self.owner, self.installation)), [self.a])
        RestoreAccessGeneration.objects.filter(pk=self.generation.pk).update(current_slot=None)
        following = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), initiated_by=uuid.uuid4())
        RestoreAccessReview.objects.create(generation=following, shop=self.a, user=self.owner,
            archived_role="owner", was_active=True, archived_permissions=self.reviews[0].archived_permissions)
        self.assertFalse(accessible_shops(self.owner, self.installation).filter(pk=self.a.pk).exists())
        with self.assertRaises(PermissionDenied):
            effective_permissions(self.a, self.owner)

    def test_new_shop_is_available_but_confirmation_does_not_override_membership_withdrawal(self):
        new = Shop.objects.create(owner=self.owner, name="New after restoration")
        self.assertEqual(list(accessible_shops(self.owner, self.installation)), [new])
        manager = User.objects.create_user(username="held-manager")
        installation = Installation.objects.create(user=manager)
        membership = Membership.objects.create(shop=self.a, user=manager, role="manager", is_active=False)
        review = RestoreAccessReview.objects.create(generation=self.generation, shop=self.a, user=manager,
            membership=membership, archived_role="manager", was_active=True, archived_permissions=self.reviews[0].archived_permissions)
        self.decide(review)
        self.assertFalse(accessible_shops(manager, installation).exists())
        membership.is_active = True
        membership.save(update_fields=["is_active"])
        self.assertEqual(list(accessible_shops(manager, installation)), [self.a])
