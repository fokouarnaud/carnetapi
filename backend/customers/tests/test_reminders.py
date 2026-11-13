"""US-25 / Q83–Q88: a WhatsApp reminder is prepared online, frozen, and only declared sent by hand."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from customers.models import Customer, CustomerDebt, CustomerLedgerEntry, CustomerPayment, CustomerReminder
from identity.models import DeviceSession, Installation, Membership, Shop, User


class ReminderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="reminder-owner")
        self.seller = User.objects.create_user(username="reminder-seller")
        self.shop = Shop.objects.create(name="Boutique Akwa", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.customer = Customer.objects.create(shop=self.shop, name="Mama Ngono", phone="+237670000123")
        self.no_phone = Customer.objects.create(shop=self.shop, name="Papa Tchoua")
        self.membership = Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)
        today = timezone.localdate()
        self.overdue = self.open_debt(3000, due_date=str(today - timedelta(days=3)))
        self.not_due = self.open_debt(7000, due_date=str(today + timedelta(days=10)))
        self.no_due = self.open_debt(2000)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def open_debt(self, amount, customer=None, **extra):
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/debts/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": amount,
            "occurred_at": "2026-01-01T00:00:00Z", "reason": "Dette antérieure", **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def url(self, customer=None):
        return f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/reminders/"

    def prepare(self, customer=None, **changes):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "language": "fr", **changes}
        return self.client.post(self.url(customer), data, format="json"), data

    def declare(self, reminder_id, **changes):
        return self.client.post(f"/api/v1/shops/{self.shop.id}/reminders/{reminder_id}/declare-sent/",
                                {"operation_id": str(uuid.uuid4()), **changes}, format="json")

    def test_default_selection_is_overdue_debts_only_with_frozen_text(self):
        response, data = self.prepare()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "prepared")
        self.assertEqual(response.data["total_fcfa"], 3000)
        self.assertEqual([row["debt_id"] for row in response.data["debts"]], [self.overdue])
        self.assertTrue(response.data["debts"][0]["overdue"])
        self.assertEqual(response.data["phone"], "+237670000123")
        self.assertIn("3 000 FCFA", response.data["message"])
        self.assertIn("Mama Ngono", response.data["message"])
        self.assertIn("Boutique Akwa", response.data["message"])
        self.assertTrue(response.data["other_devices_warning"])
        reminder = CustomerReminder.objects.get(id=data["id"])
        self.assertEqual(reminder.actor, self.owner)
        self.assertEqual(reminder.total_fcfa, 3000)

    def test_extra_debts_are_added_explicitly_and_never_called_overdue(self):
        response, _ = self.prepare(extra_debt_ids=[self.not_due, self.no_due])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["total_fcfa"], 12000)
        overdue = {row["debt_id"]: row["overdue"] for row in response.data["debts"]}
        self.assertEqual(overdue, {self.overdue: True, self.not_due: False, self.no_due: False})

    def test_preview_shows_the_selection_without_recording_anything(self):
        response = self.client.get(self.url() + f"preview/?extra_debt_ids={self.no_due}&language=en")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_fcfa"], 5000)
        self.assertIn("5,000 FCFA", response.data["message"])
        self.assertEqual(CustomerReminder.objects.count(), 0)

    def test_nothing_overdue_and_no_extra_is_refused(self):
        for debt in CustomerDebt.objects.filter(id=self.overdue):
            debt.due_at = None
            debt.save(update_fields=["due_at"])
        response, _ = self.prepare()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "reminder_empty")

    def test_known_pending_or_unregularized_payment_blocks_the_figure_bearing_reminder(self):
        debt = CustomerDebt.objects.get(id=self.overdue)
        CustomerLedgerEntry.objects.create(shop=self.shop, customer=self.customer, operation=debt.operation,
                                           source_id=uuid.uuid4(), kind="payment", sequence=9,
                                           occurred_at=timezone.now(), recorded_at=timezone.now(),
                                           delta_fcfa=-1000, state="pending")
        response, _ = self.prepare()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "reminder_blocked_pending_payment")
        CustomerLedgerEntry.objects.filter(state="pending").update(state="validated")
        CustomerPayment.objects.create(shop=self.shop, customer=self.customer, actor=self.owner,
                                       installation=debt.installation, operation=debt.operation.__class__.objects.create(
                                           shop=self.shop, actor=self.owner, action="customer_payment_created",
                                           target_id=uuid.uuid4()),
                                       occurred_at=timezone.now(), amount_fcfa=500, unallocated_fcfa=500,
                                       requires_regularization=True)
        self.assertEqual(self.prepare()[0].status_code, 409)
        self.assertEqual(CustomerReminder.objects.count(), 0)

    def test_seller_needs_the_reminder_permission(self):
        self.authenticate(self.seller)
        self.assertEqual(self.prepare()[0].status_code, 403)
        self.membership.can_prepare_reminder = True
        self.membership.save(update_fields=["can_prepare_reminder"])
        self.assertEqual(self.prepare()[0].status_code, 201)

    def test_customer_without_phone_still_gets_a_copyable_text(self):
        self.open_debt(1500, customer=self.no_phone, due_date=str(timezone.localdate() - timedelta(days=1)))
        response, _ = self.prepare(customer=self.no_phone)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["phone"])
        self.assertIn("1 500 FCFA", response.data["message"])

    def test_foreign_or_settled_extra_debt_is_refused(self):
        foreign_customer = Customer.objects.create(shop=self.shop, name="Autre")
        foreign_debt = self.open_debt(900, customer=foreign_customer)
        self.assertEqual(self.prepare(extra_debt_ids=[foreign_debt])[0].status_code, 400)
        CustomerDebt.objects.filter(id=self.no_due).update(remaining_fcfa=0)
        self.assertEqual(self.prepare(extra_debt_ids=[self.no_due])[0].status_code, 400)

    def test_preparation_is_idempotent_and_conflicting_reuse_is_refused(self):
        response, data = self.prepare()
        retry = self.client.post(self.url(), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        changed = {**data, "language": "en"}
        self.assertEqual(self.client.post(self.url(), changed, format="json").status_code, 409)
        self.assertEqual(CustomerReminder.objects.count(), 1)

    def test_only_a_manual_declaration_marks_it_sent_and_it_is_idempotent(self):
        response, data = self.prepare()
        reminder = CustomerReminder.objects.get(id=data["id"])
        self.assertEqual(reminder.status, "prepared")
        declared = self.declare(data["id"], operation_id=str(uuid.uuid4()))
        self.assertEqual(declared.status_code, 200, declared.data)
        self.assertEqual(declared.data["status"], "sent_declared")
        self.assertEqual(declared.data["declared_by"], str(self.owner.id))
        again = self.declare(data["id"])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["declared_at"], declared.data["declared_at"])
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "sent_declared")
        self.assertNotIn("delivered", {choice for choice, _ in CustomerReminder.Status.choices})

    def test_history_lists_the_customer_reminders_with_author_and_amounts(self):
        self.prepare()
        self.prepare(extra_debt_ids=[self.no_due])
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual({row["total_fcfa"] for row in response.data["results"]}, {3000, 5000})
        self.assertTrue(all(row["actor_id"] == str(self.owner.id) for row in response.data["results"]))

    def test_other_shop_customer_is_not_reachable(self):
        stranger = Customer.objects.create(shop=self.other, name="Ailleurs")
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{stranger.id}/reminders/",
                                    {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}, format="json")
        self.assertEqual(response.status_code, 404)
