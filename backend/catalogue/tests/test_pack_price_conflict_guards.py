"""An unresolved pack price protects its tariff and billed conversion factor."""
from copy import deepcopy
import uuid

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from catalogue.models import PackPriceConflict, ProductPack
from catalogue.services.pack_price_conflicts import record_pack_price_conflict
from catalogue.tests import test_pack_price_conflicts as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import Installation, ShopDeviceAccess
from stock.models import StockMovement


class PackPriceConflictGuardTests(TestCase):
    setUp = fixtures.PackPriceConflictTests.setUp
    authenticate = fixtures.PackPriceConflictTests.authenticate
    url = fixtures.PackPriceConflictTests.url
    payload = fixtures.PackPriceConflictTests.payload
    create = fixtures.PackPriceConflictTests.create
    pack_url = fixtures.PackPriceConflictTests.pack_url
    command = fixtures.PackPriceConflictTests.command

    def open_conflict(self):
        receipt, created = record_pack_price_conflict(
            shop_id=self.shop.id, product_id=self.product.id, pack_id=self.pack_id,
            actor=self.manager, installation=self.b_install,
            validated_data=self.command(), session_id=self.b_session.id,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["common_price_fcfa"], 4800)
        return receipt

    def test_new_price_or_factor_changes_cannot_bypass_an_open_conflict(self):
        self.open_conflict()
        self.product.refresh_from_db()
        before = deepcopy(Activity.objects.get(id=self.source.id).details)
        for changes in ({"sale_price_fcfa": 5300}, {"base_quantity_milli": 12000},
                        {"sale_price_fcfa": 4800}, {"base_quantity_milli": 24000}):
            with self.subTest(changes=changes), transaction.atomic():
                operation_id = str(uuid.uuid4())
                response = self.client.patch(self.pack_url(), {
                    "operation_id": operation_id, "base_version": 3,
                    "reason": "Ne pas contourner les propositions", **changes,
                }, format="json")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertFalse(Activity.objects.filter(id=operation_id).exists())
                pack = ProductPack.objects.get(id=self.pack_id)
                self.product.refresh_from_db()
                self.assertEqual((pack.sale_price_fcfa, pack.base_quantity_milli,
                                  pack.version, self.product.version), (4800, 24000, 3, 3))
                transaction.set_rollback(True)
        self.source.refresh_from_db()
        self.assertEqual(self.source.details, before)
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_name_change_remains_allowed_and_stale_catalogue_version_stays_rejected(self):
        self.open_conflict()
        command = {"operation_id": str(uuid.uuid4()), "base_version": 3,
                   "name": "Carton identifié", "reason": "Libellé sans arbitrage tarifaire"}
        response = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["name"], response.data["sale_price_fcfa"],
                          response.data["base_quantity_milli"]), ("Carton identifié", 4800, 24000))
        stale = self.client.patch(self.pack_url(), {**command,
            "operation_id": str(uuid.uuid4()), "name": "Nom périmé"}, format="json")
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertEqual(PackPriceConflict.objects.filter(pack_id=self.pack_id, state="open").count(), 1)
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_previous_native_receipt_is_immutable_and_current_access_is_still_required(self):
        self.open_conflict()
        original = deepcopy(self.source.details["response"])
        command = {key: value for key, value in self.source.details["request"].items()
                   if key not in ("product_id", "pack_id")}
        retry = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, original))
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 4800)
        device = Installation.objects.get(user=self.owner)
        ShopDeviceAccess.objects.create(shop=self.shop, installation=device, revoked_at=timezone.now())
        denied = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual(denied.status_code, 404, denied.content)

    def test_another_pack_can_change_its_own_price_without_resolving_this_conflict(self):
        self.open_conflict()
        created = self.create(self.payload(name="Paquet", base_quantity_milli=6000,
                                           sale_price_fcfa=1400))
        self.assertEqual(created.status_code, 201, created.content)
        response = self.client.patch(self.url() + created.data["id"] + "/", {
            "operation_id": str(uuid.uuid4()), "base_version": 1,
            "sale_price_fcfa": 1500, "reason": "Autre conditionnement sans conflit",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["sale_price_fcfa"], 1500)
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 4800)
