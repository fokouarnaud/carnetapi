"""US-33: expose uncertain current costs without treating missing costs as zero."""
import uuid
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Membership, Shop, User
from catalogue.models import Product
from stock.models import StockCostSnapshot, StockMovement
from stock.tests import test_return_costs as fixtures


class SaleCostIssueTests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    make_sale = fixtures.ReturnCostTests.make_sale

    def path(self):
        return f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/sale-cost-issues/"

    def shop_path(self):
        return f"/api/v1/shops/{self.shop.id}/sale-cost-issues/"

    def another_product(self, shop=None):
        return Product.objects.create(shop=shop or self.shop, name="Autre produit", unit_kind="piece",
            unit_label="pièce", sale_price_fcfa=100)

    def test_shop_centre_spans_products_and_totals_cover_all_pages_without_other_shops(self):
        first_product = self.product
        first = self.make_sale(1000)
        self.product = self.another_product()
        second = self.make_sale(1000)
        own_shop, second_product = self.shop, self.product
        other_owner = User.objects.create_user(username="private-issues")
        self.shop = Shop.objects.create(owner=other_owner, name="Privée")
        self.product = self.another_product(self.shop)
        foreign_product_id = self.product.id
        self.authenticate(other_owner)
        foreign = self.make_sale(1000)
        self.shop, self.product = own_shop, first_product
        self.authenticate(self.owner)
        response = self.client.get(self.shop_path(), {"page_size": 1})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["totals"], {"unknown_lines": 2, "provisional_lines": 0})
        self.assertEqual(response.data["data_scope"], "server_known")
        row = response.data["results"][0]
        self.assertEqual(row["sale_id"], first["id"])
        self.assertEqual(row["product_id"], str(first_product.id))
        second_page = self.client.get(response.data["next"])
        self.assertEqual(second_page.data["totals"], response.data["totals"])
        self.assertEqual(second_page.data["results"][0]["sale_id"], second["id"])
        self.assertEqual(second_page.data["results"][0]["product_id"], str(second_product.id))
        self.assertNotIn(foreign["id"], [row["sale_id"] for row in response.data["results"] + second_page.data["results"]])
        self.assertEqual(self.client.get(self.shop_path(), {"product_id": foreign_product_id}).status_code, 404)

    def test_centre_filters_by_current_cost_state_and_authorized_product(self):
        original_product = self.product
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        provisional = self.make_sale(2000)
        self.product = self.another_product()
        unknown = self.make_sale(1000)
        page = self.client.get(self.shop_path(), {"cost_state": "provisional", "page_size": 1})
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual(page.data["count"], 1)
        self.assertEqual(page.data["totals"], {"unknown_lines": 0, "provisional_lines": 1})
        self.assertEqual(page.data["results"][0]["sale_id"], provisional["id"])
        page = self.client.get(self.shop_path(), {"product_id": self.product.id, "cost_state": "unknown"})
        self.assertEqual(page.data["results"][0]["sale_id"], unknown["id"])
        self.assertEqual(self.client.get(self.shop_path(), {"product_id": original_product.id, "cost_state": "unknown"}).data["count"], 0)

    def test_centre_pages_keep_their_cutoff_when_sales_and_cost_versions_arrive(self):
        self.make_sale(1000)
        second = self.make_sale(1000)
        page = self.client.get(self.shop_path(), {"page_size": 1})
        self.assertEqual(page.status_code, 200, page.content)
        self.make_sale(1000)
        old = StockCostSnapshot.objects.get(movement__sale_line__sale_id=second["id"])
        operation = Activity.objects.create(shop=self.shop, actor=self.owner,
            action="cost_revalued", target_id=self.product.id, reason="Information tardive")
        revised = StockCostSnapshot.objects.create(movement=old.movement, trigger_operation=operation,
            version=2, supersedes=old, cost_state="unknown", total_cost_nanos_fcfa=None,
            unit_cost_micros_numerator=None, unit_cost_micros_denominator=None,
            quantity_after_milli=old.quantity_after_milli, stock_value_after_nanos_fcfa=old.stock_value_after_nanos_fcfa)
        next_page = self.client.get(page.data["next"])
        self.assertEqual(next_page.data["count"], 2)
        self.assertEqual(next_page.data["as_of"], page.data["as_of"])
        self.assertEqual(next_page.data["results"][0]["cost"]["version"], 1)
        self.assertIsNone(next_page.data["next"])
        fresh = self.client.get(self.shop_path())
        self.assertEqual(fresh.data["count"], 3)
        self.assertEqual(next(row for row in fresh.data["results"] if row["sale_id"] == second["id"])["cost"]["version"], 2)
        operation = Activity.objects.create(shop=self.shop, actor=self.owner,
            action="cost_revalued", target_id=self.product.id, reason="Source de coût connue reçue tardivement")
        StockCostSnapshot.objects.create(movement=old.movement, trigger_operation=operation,
            version=3, supersedes=revised, cost_state="known", total_cost_nanos_fcfa=1000000000000,
            unit_cost_micros_numerator=1000000000, unit_cost_micros_denominator=1,
            quantity_after_milli=0, stock_value_after_nanos_fcfa=0)
        historical = self.client.get(page.data["next"])
        self.assertEqual(historical.data["count"], 2)
        self.assertEqual(historical.data["results"][0]["cost_state"], "unknown")
        self.assertEqual(historical.data["results"][0]["cost"]["version"], 1)
        self.assertEqual(self.client.get(self.shop_path()).data["count"], 2)

    def test_centre_rejects_invalid_filters_and_future_cutoff(self):
        from datetime import timedelta
        for params in ({"cost_state": "known"}, {"product_id": "incorrect"}, {"page_size": 0},
                {"as_of": (timezone.now() + timedelta(days=1)).isoformat()}, {"unsupported": "yes"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.shop_path(), params).status_code, 400)
        self.assertEqual(self.client.get(self.shop_path(), {"product_id": uuid.uuid4()}).status_code, 404)

    def test_centre_requires_current_manager_and_session(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.shop_path()).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.get(self.shop_path().replace(str(self.shop.id), str(uuid.uuid4()))).status_code, 404)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.shop_path()).status_code, 401)

    def test_unknown_and_provisional_lines_are_paged_without_known_costs_or_writes(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        known = self.make_sale(1000)
        provisional = self.make_sale(1000)
        # A legacy line with no captured valuation must also remain visible.
        missing = self.make_sale(1000)
        StockCostSnapshot.objects.filter(movement__sale_line__sale_id=missing["id"]).delete()
        before = list(StockCostSnapshot.objects.order_by("id").values())
        counts = (Activity.objects.count(), StockMovement.objects.count(), CashMovement.objects.count())
        page = self.client.get(self.path(), {"page_size": 1})
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(len(page.data["results"]), 1)
        first = page.data["results"][0]
        self.assertEqual(first["sale_id"], provisional["id"])
        self.assertNotEqual(first["sale_id"], known["id"])
        self.assertEqual(first["cost_state"], "provisional")
        self.assertEqual(first["cost"]["total_cost_nanos_fcfa"], "1000000000000")
        self.assertEqual(first["net_fcfa"], "100")
        self.assertEqual(first["base_quantity_milli"], "1000")
        second = self.client.get(page.data["next"]).data["results"][0]
        self.assertEqual(second["sale_id"], missing["id"])
        self.assertEqual(second["cost_state"], "unknown")
        self.assertIsNone(second["cost"])
        self.assertEqual(list(StockCostSnapshot.objects.order_by("id").values()), before)
        self.assertEqual((Activity.objects.count(), StockMovement.objects.count(), CashMovement.objects.count()), counts)

    def test_unknown_frozen_cost_remains_null_and_has_its_sale_reference(self):
        sale = self.make_sale(1000)
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 200, response.content)
        row = response.data["results"][0]
        self.assertEqual(row["id"], sale["lines"][0]["id"])
        self.assertEqual(row["cost_state"], "unknown")
        self.assertIsNone(row["cost"]["total_cost_nanos_fcfa"])
        self.assertEqual(row["occurred_at"], sale["occurred_at"])

    def test_only_latest_cost_state_is_used_without_duplicate_lines(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        sale = self.make_sale(1000)
        previous = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        # Existing versioned data, as produced by a prior cost replay.
        operation = Activity.objects.create(shop=self.shop, actor=self.owner,
            action="cost_revalued", target_id=self.product.id, reason="Source devenue incertaine")
        uncertain = StockCostSnapshot.objects.create(movement=previous.movement, trigger_operation=operation,
            version=2, supersedes=previous, cost_state="unknown", total_cost_nanos_fcfa=None,
            unit_cost_micros_numerator=None, unit_cost_micros_denominator=None,
            quantity_after_milli=0, stock_value_after_nanos_fcfa=0)
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["cost"]["version"], 2)
        self.assertEqual(response.data["results"][0]["cost_state"], "unknown")
        operation = Activity.objects.create(shop=self.shop, actor=self.owner,
            action="cost_revalued", target_id=self.product.id, reason="Source connue après rapprochement")
        StockCostSnapshot.objects.create(movement=previous.movement, trigger_operation=operation,
            version=3, supersedes=uncertain, cost_state="known", total_cost_nanos_fcfa=1000000000000,
            unit_cost_micros_numerator=1000000000, unit_cost_micros_denominator=1,
            quantity_after_milli=0, stock_value_after_nanos_fcfa=0)
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(StockCostSnapshot.objects.get(id=uncertain.id).cost_state, "unknown")

    def test_current_role_shop_and_session_are_required_even_with_seller_cost_permission(self):
        self.make_sale(1000)
        Membership.objects.filter(user=self.seller, shop=self.shop).update(can_read_cost=True)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.path()).status_code, 403)
        self.authenticate(self.owner)
        foreign = self.path().replace(str(self.shop.id), str(uuid.uuid4()))
        self.assertEqual(self.client.get(foreign).status_code, 404)
        other_owner = User.objects.create_user(username="other-cost-owner")
        other_shop = Shop.objects.create(owner=other_owner, name="Autre boutique")
        other_product = Product.objects.create(shop=other_shop, name="Privé", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        self.assertEqual(self.client.get(self.path().replace(str(self.product.id), str(other_product.id))).status_code, 404)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.path()).status_code, 401)

    def test_product_with_no_sales_has_no_cost_issue(self):
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])
