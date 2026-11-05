"""A surviving partial-transfer receipt restores the chosen product only once."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone as django_timezone

from backups import models as backups
from backups.tests import test_recovery_customer_dependencies as fixtures
from cash.models import CashMovement
from catalogue.models import Product
from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User
from stock.models import StockCostSnapshot, StockMovement, StockTransfer, StockTransferLine, StockTransferReceipt, StockTransferReceiptLine
from stock.services.stock_queries import with_stock


class RecoveryTransferReceiptTests(TestCase):
    authenticate = fixtures.RecoveryCustomerDependencyTests.authenticate
    generation = fixtures.RecoveryCustomerDependencyTests.generation
    record = fixtures.RecoveryCustomerDependencyTests.record
    device = fixtures.RecoveryCustomerDependencyTests.device
    review = fixtures.RecoveryCustomerDependencyTests.review
    payload = fixtures.RecoveryCustomerDependencyTests.payload
    url = fixtures.RecoveryCustomerDependencyTests.url
    capture = fixtures.RecoveryCustomerDependencyTests.capture
    preview = fixtures.RecoveryCustomerDependencyTests.preview
    apply = fixtures.RecoveryCustomerDependencyTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerDependencyTests.setUp(self)
        review = backups.RestoreAccessReview.objects.get(generation=self.current, shop=self.b, user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review, operation_id=uuid.uuid4(), requested_by=self.technical,
            previous_version=0, version=1, state="confirmed", reason="Responsable source confirmé")
        self.source_product = Product.objects.create(shop=self.b, name="Marchandise source", unit_kind="piece", unit_label="pièce", sale_price_fcfa=900)
        self.second_product = Product.objects.create(shop=self.a, name="Autre fiche destinataire", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1200)
        opening = Activity.objects.create(shop=self.b, actor=self.owner, action="opening_stock", target_id=self.source_product.id, reason="Stock physique")
        StockMovement.objects.create(product=self.source_product, actor=self.owner, operation=opening, kind="opening",
            quantity_milli=10000, unit_cost_micros=123456789, occurred_at=datetime(2026, 10, 20, tzinfo=timezone.utc), reason="Stock physique")
        self.departure = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "destination_shop_id": str(self.a.id),
            "occurred_at": "2026-10-22T09:00:00Z", "reason": "Départ physique confirmé", "is_departure_confirmed": True,
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.source_product.id), "product_version": 1, "quantity_milli": 5000}]}
        response = self.client.post(self.transfer_url(self.b), self.departure, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.transfer = response.data

    def transfer_url(self, shop=None, suffix=""):
        return f"/api/v1/shops/{(shop or self.a).id}/transfers/" + suffix

    def stock(self, product):
        return with_stock(Product.objects.filter(id=product.id)).get().stock_validated_milli

    def native_receipt_source(self, *, keep=False, quantity=3000, product=None, rows=None):
        raw = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-10-22T10:00:00Z",
            "reason": "Marchandises physiquement reçues", "is_sellable_confirmed": True,
            "lines": rows or [{"id": str(uuid.uuid4()), "transfer_line_id": self.transfer["lines"][0]["id"],
                "product_id": str((product or self.product).id), "quantity_milli": quantity}]}
        with transaction.atomic():
            response = self.client.post(self.transfer_url(suffix=self.transfer["id"] + "/receipts/"), raw, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            event = Activity.objects.get(id=raw["operation_id"])
            payload = {"schema_version": 1, "original_request": deepcopy(event.details["request"]), "receipt": deepcopy(response.data)}
            if not keep:
                transaction.set_rollback(True)
        return SimpleNamespace(operation_type="transfer_receipt", operation_id=uuid.UUID(raw["operation_id"]),
            origin_user_id=self.owner.id, origin_installation_id=uuid.UUID(payload["original_request"]["installation_id"]),
            occurred_at=datetime.fromisoformat(raw["occurred_at"].replace("Z", "+00:00")), payload=payload)

    def test_lost_native_partial_receipt_recovers_three_units_and_two_in_transit_once(self):
        source = self.native_receipt_source()
        self.assertEqual((self.stock(self.product), self.stock(self.source_product)), ("0", "5000"))
        capture = self.capture(source)
        preview = self.preview(capture)
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(preview["stocks"][0]["validated_after_milli"], "3000")
        self.assertEqual(preview["cash_delta_by_method_fcfa"], {})
        counts = (User.objects.count(), Installation.objects.count())
        response, command = self.apply(capture, preview)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["business_receipt"]["lines"], source.payload["receipt"]["lines"])
        self.assertEqual((self.stock(self.product), self.stock(self.source_product)), ("3000", "5000"))
        current = self.client.get(self.transfer_url(suffix=self.transfer["id"] + "/"))
        self.assertEqual(current.data["lines"][0]["in_transit_milli"], "2000")
        row = StockTransferReceiptLine.objects.get()
        self.assertEqual((str(row.id), str(row.movement_id)), (source.payload["receipt"]["lines"][0]["id"], source.payload["receipt"]["lines"][0]["movement_id"]))
        self.assertEqual((row.received_before_milli, row.total_cost_nanos_fcfa), (0, 370370367000))
        self.assertEqual(StockCostSnapshot.objects.get(movement=row.movement, version=1).total_cost_nanos_fcfa, 370370367000)
        event = Activity.objects.get(id=source.operation_id)
        self.assertEqual(event.details["request"], source.payload["original_request"])
        self.assertEqual(event.details["recovery"]["source_snapshot"], source.payload["receipt"])
        self.assertEqual(backups.RecoveredOperationOrigin.objects.get(operation=event).capture_id, capture.id)
        self.assertEqual((User.objects.count(), Installation.objects.count()), counts)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json").data, response.data)
        known = self.preview(capture)
        self.assertEqual(known["outcome"], "already_known")
        another, _ = self.apply(capture, known)
        self.assertEqual(another.status_code, 201, another.content)
        self.assertEqual(another.data["outcome"], "already_known")
        self.assertEqual((StockTransferReceipt.objects.count(), StockTransferReceiptLine.objects.count(), StockMovement.objects.filter(kind="transfer_in").count()), (1, 1, 1))

    def test_an_existing_native_receipt_is_recognized_without_a_second_stock_or_origin(self):
        source = self.native_receipt_source(keep=True)
        capture = self.capture(source)
        preview = self.preview(capture)
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(preview["outcome"], "already_known")
        response, _ = self.apply(capture, preview)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["business_receipt"], source.payload["receipt"])
        self.assertEqual((self.stock(self.product), StockTransferReceipt.objects.count()), ("3000", 1))
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())

    def test_a_second_partial_keeps_its_new_explicit_product_and_absent_source_identity(self):
        first = self.native_receipt_source()
        first.origin_user_id, first.origin_installation_id = uuid.uuid4(), uuid.uuid4()
        first.payload["original_request"]["installation_id"] = str(first.origin_installation_id)
        first.payload["receipt"].update(actor_id=str(first.origin_user_id), installation_id=str(first.origin_installation_id))
        counts = (User.objects.count(), Installation.objects.count())
        one = self.capture(first)
        applied, _ = self.apply(one)
        self.assertEqual(applied.status_code, 201, applied.content)
        second = self.native_receipt_source(quantity=2000, product=self.second_product)
        two = self.capture(second, [first.operation_id])
        applied, _ = self.apply(two)
        self.assertEqual(applied.status_code, 201, applied.content)
        self.assertEqual((self.stock(self.product), self.stock(self.second_product)), ("3000", "2000"))
        self.assertEqual(list(StockTransferReceiptLine.objects.order_by("received_before_milli").values_list(
            "product_id", "received_before_milli", "total_cost_nanos_fcfa")),
            [(self.product.id, 0, 370370367000), (self.second_product.id, 3000, 246913578000)])
        self.assertEqual((User.objects.count(), Installation.objects.count()), counts)
        self.assertFalse(User.objects.filter(id=first.origin_user_id).exists())
        self.assertFalse(Installation.objects.filter(id=first.origin_installation_id).exists())
        self.assertEqual(self.preview(one)["outcome"], "already_known")
        self.assertEqual(self.client.get(self.transfer_url(suffix=self.transfer["id"] + "/")).data["lines"][0]["in_transit_milli"], "0")

    def test_missing_explicit_destination_and_inconsistent_source_numbers_are_invalid_without_effect(self):
        source = self.native_receipt_source()
        for case in ("destination", "quantity", "before", "cost", "actor", "installation", "extra", "bool", "same_movement"):
            with self.subTest(case=case):
                broken = deepcopy(source)
                row = broken.payload["receipt"]["lines"][0]
                if case == "destination": row.pop("product_id")
                elif case == "quantity": row["quantity_milli"] = 2999
                elif case == "before": row["received_before_milli"] = "-1"
                elif case == "cost": row["total_cost_nanos_fcfa"] = "3.5"
                elif case == "actor": broken.payload["receipt"]["actor_id"] = str(uuid.uuid4())
                elif case == "installation": broken.payload["original_request"]["installation_id"] = str(uuid.uuid4())
                elif case == "extra": row["cash_fcfa"] = 0
                elif case == "bool": row["quantity_milli"] = True
                else:
                    duplicate = deepcopy(row); duplicate["id"] = str(uuid.uuid4())
                    broken.payload["receipt"]["lines"].append(duplicate)
                    asked = deepcopy(broken.payload["original_request"]["lines"][0]); asked["id"] = duplicate["id"]
                    broken.payload["original_request"]["lines"].append(asked)
                broken.operation_id = uuid.uuid4()
                broken.payload["original_request"]["operation_id"] = str(broken.operation_id)
                broken.payload["receipt"]["operation_id"] = str(broken.operation_id)
                capture = self.capture(broken)
                response = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.data["code"], "recovery_transfer_receipt_invalid")
                capture.refresh_from_db(); self.assertEqual(capture.payload, broken.payload)
        self.assertFalse(StockTransferReceipt.objects.exists())
        self.assertFalse(StockMovement.objects.filter(kind="transfer_in").exists())

    def test_unknown_destination_incompatible_unit_and_changed_or_exhausted_context_stay_blocked(self):
        source = self.native_receipt_source()
        for case in ("missing_product", "unit", "cost", "foreign_transfer", "before", "exhausted"):
            with self.subTest(case=case), transaction.atomic():
                broken = deepcopy(source)
                if case == "missing_product":
                    identity = str(uuid.uuid4())
                    broken.payload["receipt"]["lines"][0]["product_id"] = identity
                    broken.payload["original_request"]["lines"][0]["product_id"] = identity
                elif case == "unit": Product.objects.filter(id=self.product.id).update(unit_label="boîte")
                elif case == "cost": broken.payload["receipt"]["lines"][0]["total_cost_nanos_fcfa"] = "1"
                elif case == "foreign_transfer":
                    foreign = Shop.objects.create(owner=self.owner, name="Autre destination privée")
                    StockTransfer.objects.filter(id=self.transfer["id"]).update(destination_shop=foreign)
                elif case == "before": self.native_receipt_source(keep=True, quantity=1000, product=self.second_product)
                else: self.native_receipt_source(keep=True, quantity=5000, product=self.second_product)
                capture = self.capture(broken)
                preview = self.preview(capture)
                self.assertFalse(preview["can_apply"], preview)
                self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
                self.assertFalse(StockTransferReceipt.objects.filter(id=source.payload["receipt"]["id"]).exists())
                if case == "foreign_transfer":
                    self.assertIsNone(preview["current_state"])
                    self.assertNotIn(self.source_product.name, str(preview))
                transaction.set_rollback(True)

    def test_new_arrival_invalidates_preview_and_known_receipt_survives_later_receipts(self):
        source = self.native_receipt_source()
        capture = self.capture(source)
        preview = self.preview(capture)
        self.native_receipt_source(keep=True, quantity=1000, product=self.second_product)
        self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
        self.assertFalse(self.preview(capture)["can_apply"])
        self.assertEqual((self.stock(self.product), self.stock(self.second_product)), ("0", "1000"))

    def test_review_failure_rolls_back_receipt_movements_costs_origin_and_activity(self):
        source = self.native_receipt_source(); capture = self.capture(source)
        before = (StockMovement.objects.count(), StockCostSnapshot.objects.count())
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_transfer_receipt_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected transfer review failure'); END")
        try:
            with self.assertRaises(IntegrityError): self.apply(capture)
        finally:
            with connection.cursor() as cursor: cursor.execute("DROP TRIGGER fail_transfer_receipt_review")
        self.assertFalse(StockTransferReceipt.objects.exists())
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())
        self.assertEqual((StockMovement.objects.count(), StockCostSnapshot.objects.count()), before)
        self.assertEqual(self.apply(capture)[0].status_code, 201)

    def test_session_and_current_shop_rights_are_rechecked_before_new_and_original_receipts(self):
        source = self.native_receipt_source(); capture = self.capture(source)
        manager = User.objects.create_user(username="transfer-recovery-responsible")
        membership = Membership.objects.create(shop=self.a, user=manager, role="manager")
        self.review(self.a, manager, "manager", membership=membership, confirmed=True)
        self.client = self.authenticate(manager)
        preview = self.preview(capture)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/", shop=self.b)).status_code, 404)
        response, command = self.apply(capture, preview)
        self.assertEqual(response.status_code, 201, response.content)
        membership.is_active = False; membership.save(update_fields=["is_active", "updated_at"])
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json").status_code, 404)
        membership.is_active = True; membership.save(update_fields=["is_active", "updated_at"])
        Installation.objects.filter(user=manager).update(revoked_at=datetime.now(timezone.utc))
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json").status_code, 401)
        self.assertEqual(StockTransferReceipt.objects.count(), 1)

    def test_same_departure_line_can_have_two_chosen_products_with_native_rows_in_reverse_order(self):
        identifiers = sorted([uuid.uuid4(), uuid.uuid4()], reverse=True)
        rows = [{"id": str(identity), "transfer_line_id": self.transfer["lines"][0]["id"],
            "product_id": str(product.id), "quantity_milli": quantity}
            for identity, product, quantity in zip(identifiers, (self.product, self.second_product), (1000, 2000), strict=True)]
        with patch("django.utils.timezone.now", return_value=django_timezone.now()):
            source = self.native_receipt_source(rows=rows)
        self.assertEqual([row["id"] for row in source.payload["receipt"]["lines"]], list(reversed([row["id"] for row in rows])))
        capture = self.capture(source)
        response, command = self.apply(capture)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["business_receipt"]["lines"], source.payload["receipt"]["lines"])
        expected = {row["id"]: row for row in source.payload["receipt"]["lines"]}
        for row in StockTransferReceiptLine.objects.all():
            frozen = expected[str(row.id)]
            self.assertEqual((str(row.product_id), str(row.movement_id), row.received_before_milli, row.total_cost_nanos_fcfa),
                (frozen["product_id"], frozen["movement_id"], int(frozen["received_before_milli"]), int(frozen["total_cost_nanos_fcfa"])))
        self.assertEqual((self.stock(self.product), self.stock(self.second_product)), ("1000", "2000"))
        self.assertEqual(self.preview(capture)["outcome"], "already_known")
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json").data, response.data)

    def test_unknown_departure_cost_is_recovered_as_unknown_not_zero(self):
        unknown = Product.objects.create(shop=self.b, name="Coût non connu", unit_kind="piece", unit_label="pièce", sale_price_fcfa=900)
        event = Activity.objects.create(shop=self.b, actor=self.owner, action="opening_stock", target_id=unknown.id, reason="Reprise sans coût")
        StockMovement.objects.create(product=unknown, actor=self.owner, operation=event, kind="opening", quantity_milli=5000,
            unit_cost_micros=None, occurred_at=datetime(2026, 10, 20, tzinfo=timezone.utc), reason="Reprise sans coût")
        command = deepcopy(self.departure)
        command.update(id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()))
        command["lines"] = [{"id": str(uuid.uuid4()), "product_id": str(unknown.id), "product_version": 1, "quantity_milli": 5000}]
        response = self.client.post(self.transfer_url(self.b), command, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.transfer = response.data
        source = self.native_receipt_source()
        capture = self.capture(source)
        applied, _ = self.apply(capture)
        self.assertEqual(applied.status_code, 201, applied.content)
        row = StockTransferReceiptLine.objects.get()
        self.assertEqual((row.cost_state, row.total_cost_nanos_fcfa), ("unknown", None))
        cost = StockCostSnapshot.objects.get(movement=row.movement, version=1)
        self.assertEqual((cost.cost_state, cost.total_cost_nanos_fcfa), ("unknown", None))
        self.assertFalse(CashMovement.objects.exists())
