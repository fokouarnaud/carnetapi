"""Exercise real invoice/receipt/sale HTTP flows and inspect their frozen costs."""
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass  # The final JSON reports verification; do not log request traffic.


def main():
    with TemporaryDirectory(prefix="sale-cost-http-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "costs.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from identity.models import User, Shop, Installation, DeviceSession
        from catalogue.models import Product
        from stock.models import StockCostSnapshot
        from cash.models import CashMovement
        from purchasing.models import SupplierDebt

        call_command("migrate", verbosity=0)
        owner = User.objects.create_user(username="cost-http-owner")
        shop = Shop.objects.create(owner=owner, name="Valorisation HTTP")
        product = Product.objects.create(shop=shop, name="Article", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1500)
        installation = Installation.objects.create(user=owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(owner)
        token["session_id"] = str(session.id)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/"
        try:
            def post(path, body, expected=201):
                request = Request(root + path, data=json.dumps(body).encode(), method="POST",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(request, timeout=30) as response:
                    assert response.status == expected
                    return json.loads(response.read())

            def get(path):
                request = Request(root + path, headers={"Authorization": f"Bearer {token}"})
                with urlopen(request, timeout=30) as response:
                    assert response.status == 200
                    return json.loads(response.read())

            def event(at):
                return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "occurred_at": at.isoformat(), "reason": "Vérification physique"}

            now = timezone.now()
            supplier = post("suppliers/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Fournisseur"})
            base = f"suppliers/{supplier['id']}/invoices/"

            def receive(quantity, price, at):
                invoice = post(base, {**event(at), "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id),
                    "quantity_milli": quantity, "unit_price_fcfa": price}]})
                post(base + invoice["id"] + "/receipts/", {**event(at),
                    "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": quantity}]})

            def sale(quantity, at):
                body = {**event(at), "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id),
                    "product_version": 1, "quantity_milli": quantity}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": quantity * 1500 // 1000}]}
                receipt = post("sales/", body)
                assert post("sales/", body, expected=200) == receipt
                assert "unit_cost_micros" not in receipt["lines"][0]
                return StockCostSnapshot.objects.get(movement__sale_line__sale_id=receipt["id"], version=1)

            receive(10000, 1000, now - timedelta(days=3))
            same_instant = now - timedelta(days=2)
            receive(10000, 1200, same_instant)
            original = sale(5000, same_instant)
            assert original.total_cost_nanos_fcfa == 5500000000000
            assert original.stock_value_after_nanos_fcfa == 16500000000000
            assert original.cost_state == "known"
            negative = sale(17000, now - timedelta(days=1))
            assert negative.cost_state == "provisional" and negative.quantity_after_milli == -2000
            assert negative.total_cost_nanos_fcfa == 18700000000000
            receive(5000, 1200, now - timedelta(hours=12))
            recovered = sale(1000, now - timedelta(hours=1))
            assert recovered.cost_state == "known" and recovered.quantity_after_milli == 2000
            assert recovered.total_cost_nanos_fcfa == 1200000000000
            negative.refresh_from_db()
            assert negative.cost_state == "provisional" and negative.total_cost_nanos_fcfa == 18700000000000
            assert StockCostSnapshot.objects.count() == 3
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 34500
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 28000
            empty_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Ouverture vide", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500,
                "opening": {"quantity_milli": 0, "unit_cost_fcfa": "1000", "occurred_at": same_instant.isoformat(),
                    "reason": "Aucune unité présente"}})
            product = Product.objects.get(id=empty_product["id"])
            empty_cost = sale(1000, now - timedelta(minutes=1))
            assert empty_cost.cost_state == "unknown" and empty_cost.total_cost_nanos_fcfa is None
            assert StockCostSnapshot.objects.count() == 4
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 36000
            product = Product.objects.get(id=original.movement.product_id)
            returned_root = f"sales/{original.movement.sale_line.sale_id}/returns/"
            returned = post(returned_root, {**event(now), "lines": [{"id": str(uuid.uuid4()),
                "sale_line_id": str(original.movement.sale_line_id), "quantity_milli": 1000}]})
            receipt_path = returned_root + returned["id"] + "/lines/" + returned["lines"][0]["id"] + "/receipts/"
            physical = post(receipt_path, {**event(now), "quantity_milli": 1000, "condition": "unsellable"})
            assert physical["cost"]["sale_cost_id"] == str(original.id)
            assert physical["cost"]["total_cost_nanos_fcfa"] == "1100000000000"
            assert physical["unit_cost_micros"] == 1100000000 and not physical["cost_is_provisional"]
            restocked = post(receipt_path + physical["id"] + "/decisions/", {**event(now),
                "quantity_milli": 1000, "kind": "restock", "is_sellable_confirmed": True})
            assert restocked["cost"]["total_cost_nanos_fcfa"] == "1100000000000"
            mixed = sale(1000, now)
            assert mixed.total_cost_nanos_fcfa == 1166666666667 and mixed.cost_state == "known"
            assert StockCostSnapshot.objects.count() == 5
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 37500
            print(json.dumps({"http_sale_costs": "passed", "same_instant_receipt_first": True,
                "average_fcfa": 1100, "initial_sale_cost_fcfa": 5500,
                "original_oversale_still_provisional": True, "new_sale_cost_fcfa": 1200,
                "empty_opening_sale": "passed", "return_original_cost_fcfa": 1100,
                "sale_after_return_cost_nanos_fcfa": mixed.total_cost_nanos_fcfa,
                "frozen_sale_costs": 5, "cash_fcfa": 37500, "supplier_debt_fcfa": 28000}))
            from stock.models import CostRevaluation, ReturnCostRevision, StockMovement
            from purchasing.models import PurchaseLine
            first_line = PurchaseLine.objects.filter(product=product, unit_price_fcfa=1000).get()
            fee = post(base + str(first_line.document_id) + "/charges/", {**event(now), "label": "Transport tardif",
                "amount_fcfa": 2000, "allocations": [{"line_id": str(first_line.id), "amount_fcfa": 2000}]})
            before_movements = StockMovement.objects.count()
            revaluation_path = f"products/{product.id}/cost-revaluations/"
            preview = get(revaluation_path + "preview/")
            assert preview["sales_cost_before"]["total_nanos_fcfa"] == "26566666666667"
            assert preview["sales_cost_after"]["total_nanos_fcfa"] == "28800000000000"
            assert not CostRevaluation.objects.exists()
            confirmation = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "reason": "Vérification du transport tardif", "preview_token": preview["preview_token"]}
            applied = post(revaluation_path, confirmation)
            assert post(revaluation_path, confirmation, expected=200) == applied
            assert get(revaluation_path + applied["id"] + "/") == applied
            batch = CostRevaluation.objects.get(id=applied["id"])
            revised = StockCostSnapshot.objects.get(supersedes=original)
            provisional = StockCostSnapshot.objects.get(supersedes=negative)
            assert revised.total_cost_nanos_fcfa == 6000000000000
            assert provisional.total_cost_nanos_fcfa == 20400000000000 and provisional.cost_state == "provisional"
            assert ReturnCostRevision.objects.get(origin__receipt_id=physical["id"]).total_cost_nanos_fcfa == 1200000000000
            latest_return = get(receipt_path + physical["id"] + "/")
            assert latest_return["cost"]["version"] == 2 and latest_return["unit_cost_micros"] == 1200000000
            original.refresh_from_db()
            assert original.total_cost_nanos_fcfa == 5500000000000
            assert StockMovement.objects.count() == before_movements
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 37500
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 28000
            batch.refresh_from_db()
            assert fee["current_revision"]["id"] in batch.source_revision_ids
            count = StockCostSnapshot.objects.count()
            assert post(revaluation_path, confirmation, expected=200) == applied
            assert StockCostSnapshot.objects.count() == count
            history = get(revaluation_path + applied["id"] + "/items/?page_size=1")
            assert history["count"] >= 2 and len(history["results"]) == 1 and history["next"]
            charge = get(base + str(first_line.document_id) + "/charges/" + fee["id"] + "/")
            assert not charge["current_revision"]["historical_costs_pending_review"]
            print(json.dumps({"http_cost_replay": "passed", "old_sale_cost_fcfa": 5500,
                "revised_sale_cost_fcfa": 6000, "revised_return_cost_fcfa": 1200,
                "oversale_still_provisional": True, "batch_retry_no_effect": True,
                "fee_review_closed": True, "history_sql_paginated": True, "stock_cash_debt_unchanged": True}))
            from stock.models import ReturnCostReconstruction, ReturnCostSnapshot
            legacy_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Ancien retour", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=legacy_product["id"])
            receive(2000, 1000, now - timedelta(hours=2))
            legacy_sale = sale(2000, now - timedelta(hours=1))
            returns_path = f"sales/{legacy_sale.movement.sale_line.sale_id}/returns/"
            returned = post(returns_path, {**event(now), "lines": [{"id": str(uuid.uuid4()),
                "sale_line_id": str(legacy_sale.movement.sale_line_id), "quantity_milli": 1000}]})
            receipt_path = returns_path + returned["id"] + "/lines/" + returned["lines"][0]["id"] + "/receipts/"
            physical = post(receipt_path, {**event(now), "quantity_milli": 1000, "condition": "unsellable"})
            # Seed a receipt predating snapshots; reconstruction itself uses only HTTP.
            ReturnCostSnapshot.objects.filter(receipt_id=physical["id"]).delete()
            post(receipt_path + physical["id"] + "/decisions/", {**event(now), "quantity_milli": 1000,
                "kind": "restock", "is_sellable_confirmed": True})
            sale(1000, now)
            line = PurchaseLine.objects.get(product=product)
            post(base + str(line.document_id) + "/charges/", {**event(now), "label": "Transport tardif",
                "amount_fcfa": 200, "allocations": [{"line_id": str(line.id), "amount_fcfa": 200}]})
            revaluation_path = f"products/{product.id}/cost-revaluations/"
            preview = get(revaluation_path + "preview/?reconstruct_legacy_returns=true")
            assert preview["legacy_return_receipts"] == preview["reconstructed_return_receipts"] == 1
            assert preview["sales_cost_after"]["total_nanos_fcfa"] == "3300000000000"
            before_movements = StockMovement.objects.count()
            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Ancien coût contrôlé",
                "preview_token": preview["preview_token"], "reconstruct_legacy_returns": True}
            applied = post(revaluation_path, body)
            assert post(revaluation_path, body, expected=200) == applied
            baseline = ReturnCostSnapshot.objects.get(receipt_id=physical["id"])
            assert baseline.total_cost_nanos_fcfa == 1000000000000 and baseline.sale_cost_id is None
            assert ReturnCostReconstruction.objects.filter(original=baseline, batch_id=applied["id"]).count() == 1
            current = get(receipt_path + physical["id"] + "/")
            assert current["cost"]["origin_kind"] == "reconstructed_legacy" and current["unit_cost_micros"] == 1100000000
            assert StockMovement.objects.count() == before_movements
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 42000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 30000
            print(json.dumps({"http_legacy_return_reconstruction": "passed", "legacy_baseline_fcfa": 1000,
                "current_return_cost_fcfa": 1100, "replayed_sales_cost_fcfa": 3300,
                "provenance_visible": True, "retry_no_effect": True, "cash_fcfa": 42000, "supplier_debt_fcfa": 30000}))
            loss_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Perte valorisée", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=loss_product["id"])
            receive(2000, 1000, now - timedelta(hours=2))
            loss_path = f"products/{product.id}/losses/"
            loss_body = {**event(now), "quantity_milli": 1000}
            loss = post(loss_path, loss_body)
            assert loss["cost"]["total_cost_nanos_fcfa"] == "1000000000000"
            line = PurchaseLine.objects.get(product=product)
            post(base + str(line.document_id) + "/charges/", {**event(now), "label": "Transport affecté",
                "amount_fcfa": 200, "allocations": [{"line_id": str(line.id), "amount_fcfa": 200}]})
            revaluation_path = f"products/{product.id}/cost-revaluations/"
            preview = get(revaluation_path + "preview/")
            assert preview["loss_cost_before"]["total_nanos_fcfa"] == "1000000000000"
            assert preview["loss_cost_after"]["total_nanos_fcfa"] == "1100000000000"
            assert preview["sales_cost_after"]["total_nanos_fcfa"] == "0"
            before_movements = StockMovement.objects.count()
            post(revaluation_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "reason": "Perte revalorisée", "preview_token": preview["preview_token"]})
            current = get(loss_path + loss["id"] + "/")
            assert current["cost"]["version"] == 2 and current["cost"]["total_cost_nanos_fcfa"] == "1100000000000"
            assert post(loss_path, loss_body, expected=200) == loss
            assert StockCostSnapshot.objects.get(movement__product=product, movement__kind="loss", version=1).total_cost_nanos_fcfa == 1000000000000
            assert sum(StockMovement.objects.filter(product=product).values_list("quantity_milli", flat=True)) == 1000
            assert StockMovement.objects.count() == before_movements
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 42000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 32000
            print(json.dumps({"http_loss_costs": "passed", "initial_cost_fcfa": 1000, "revised_cost_fcfa": 1100,
                "initial_receipt_preserved": True, "stock_milli": 1000, "cash_fcfa": 42000, "supplier_debt_fcfa": 32000}))
            from purchasing.models import SupplierCredit
            returned_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Départ fournisseur", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=returned_product["id"])
            receive(10000, 1000, now - timedelta(hours=2))
            receive(10000, 1200, now - timedelta(hours=1))
            origin = PurchaseLine.objects.get(product=product, unit_price_fcfa=1000)
            departures_path = base + str(origin.document_id) + "/returns/"
            departure_body = {**event(now), "lines": [{"line_id": str(origin.id), "quantity_milli": 5000}]}
            departed = post(departures_path, departure_body)
            assert departed["lines"][0]["cost"]["total_cost_nanos_fcfa"] == "5000000000000"
            assert departed["lines"][0]["cost"]["stock_value_after_nanos_fcfa"] == "17000000000000"
            assert not SupplierCredit.objects.exists()
            credit = post(f"suppliers/{supplier['id']}/credits/", {**event(now), "return_id": departed["id"], "amount_fcfa": 5000})
            assert credit["reference_fcfa"] == credit["amount_fcfa"] == 5000
            post(base + str(origin.document_id) + "/charges/", {**event(now), "label": "Transport d'origine",
                "amount_fcfa": 1000, "allocations": [{"line_id": str(origin.id), "amount_fcfa": 1000}]})
            revaluation_path = f"products/{product.id}/cost-revaluations/"
            preview = get(revaluation_path + "preview/")
            assert preview["stock_value_after_nanos_fcfa"] == "17500000000000"
            post(revaluation_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "reason": "Coût d'origine révisé", "preview_token": preview["preview_token"]})
            current = get(departures_path)["results"][0]
            assert current["lines"][0]["cost"]["total_cost_nanos_fcfa"] == "5500000000000"
            assert current["lines"][0]["cost"]["valuation_kind"] == "revised"
            assert post(departures_path, departure_body, expected=200) == departed
            accepted = SupplierCredit.objects.get(id=credit["id"])
            assert accepted.reference_fcfa == accepted.amount_fcfa == 5000
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 42000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 49000
            print(json.dumps({"http_supplier_departure_cost": "passed", "original_cost_fcfa": 5000,
                "revised_cost_fcfa": 5500, "remaining_stock_value_fcfa": 17500,
                "credit_reference_unchanged_fcfa": 5000, "cash_fcfa": 42000, "supplier_debt_fcfa": 49000}))
            gap_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Écart à régulariser", "unit_kind": "weight", "unit_label": "kg", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=gap_product["id"])
            receive(1000, 10000, now - timedelta(hours=3))
            receive(100000, 1, now - timedelta(hours=2))
            sale(100000, now - timedelta(hours=1))
            origin = PurchaseLine.objects.get(product=product, unit_price_fcfa=10000)
            gap = post(base + str(origin.document_id) + "/returns/", {**event(now),
                "lines": [{"line_id": str(origin.id), "quantity_milli": 500}]})
            cost = gap["lines"][0]["cost"]
            assert cost["total_cost_nanos_fcfa"] == "5000000000000"
            assert cost["valuation_gap_nanos_fcfa"] == "-4900000000000" and cost["valuation_requires_regularization"]
            assert cost["stock_value_after_nanos_fcfa"] is None
            assert not SupplierCredit.objects.filter(supplier_return_id=gap["id"]).exists()
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 192000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 59100
            print(json.dumps({"http_supplier_valuation_gap": "passed", "departure_cost_fcfa": 5000,
                "gap_fcfa": -4900, "remaining_value": None, "regularization_required": True,
                "no_automatic_credit": True, "cash_fcfa": 192000, "supplier_debt_fcfa": 59100}))
            valuation_path = f"products/{product.id}/stock-valuations/"
            value_preview = post(valuation_path + "preview/", {"value_fcfa": 600}, expected=200)
            assert value_preview["quantity_milli"] == "500" and value_preview["previous_value_nanos_fcfa"] is None
            confirmation = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": 600,
                "preview_token": value_preview["preview_token"], "reason": "Valeur restante contrôlée"}
            valued = post(valuation_path, confirmation)
            assert post(valuation_path, confirmation, expected=200) == valued
            current = get(valuation_path + "current/")
            assert current["stock_value_nanos_fcfa"] == "600000000000" and not current["valuation_requires_regularization"]
            assert get(valuation_path + valued["id"] + "/") == valued
            assert get(valuation_path + "?page_size=1")["results"] == [valued]
            post(base + str(origin.document_id) + "/charges/", {**event(now), "label": "Frais arrivés après contrôle",
                "amount_fcfa": 100, "allocations": [{"line_id": str(origin.id), "amount_fcfa": 100}]})
            current = get(valuation_path + "current/")
            assert current["stock_value_nanos_fcfa"] is None and current["valuation_requires_regularization"]
            assert current["invalid_adjustments_count"] == 1
            assert post(valuation_path, confirmation, expected=200) == valued
            new_preview = post(valuation_path + "preview/", {"value_fcfa": 600}, expected=200)
            post(valuation_path, {**confirmation, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "preview_token": new_preview["preview_token"], "reason": "Valeur confirmée après nouveaux frais"})
            current = get(valuation_path + "current/")
            assert current["stock_value_nanos_fcfa"] == "600000000000" and not current["valuation_requires_regularization"]
            assert current["invalid_adjustments_count"] == 1  # The superseded confirmation remains in history.
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 192000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 59100
            print(json.dumps({"http_stock_valuation": "passed", "confirmed_remaining_value_fcfa": 600,
                "late_fee_invalidates_basis": True, "explicit_reconfirmation": True,
                "old_decision_preserved": True, "cash_fcfa": 192000, "supplier_debt_fcfa": 59100}))
            counted_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Inventaire valorisé", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=counted_product["id"])
            receive(2000, 1000, now - timedelta(hours=2))
            count_path = f"products/{product.id}/stock-counts/"
            decisions = []
            for physical, expected, at in ((3000, 2000, now - timedelta(hours=1)),
                    (2000, 3000, now - timedelta(minutes=30))):
                count = post(count_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "physical_milli": physical, "counted_at": at.isoformat(), "reason": "Comptage réel"})
                detail = count_path + count["id"] + "/"
                body = {"operation_id": str(uuid.uuid4()), "expected_milli": expected,
                    "applied_adjustment_milli": 0, "reason": "Correction confirmée"}
                correction = post(detail + "confirm/", body)
                assert correction["cost"]["total_cost_nanos_fcfa"] == "1000000000000"
                assert post(detail + "confirm/", body, expected=200) == correction
                decisions.append((detail, body, correction, physical))
            source = PurchaseLine.objects.get(product=product)
            post(base + str(source.document_id) + "/charges/", {**event(now), "label": "Frais d'inventaire",
                "amount_fcfa": 200, "allocations": [{"line_id": str(source.id), "amount_fcfa": 200}]})
            revaluation_path = f"products/{product.id}/cost-revaluations/"
            preview = get(revaluation_path + "preview/")
            post(revaluation_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "reason": "Inventaire revalorisé", "preview_token": preview["preview_token"]})
            for detail, body, correction, physical in decisions:
                latest = get(detail + "adjustments/?page_size=1")["results"][0]["cost"]
                assert latest["total_cost_nanos_fcfa"] == "1100000000000" and latest["version"] == 2
                assert latest["supersedes_id"] == correction["cost"]["id"]
                assert post(detail + "confirm/", body, expected=200) == correction
                assert get(detail)["physical_milli"] == physical
            assert sum(StockMovement.objects.filter(product=product).values_list("quantity_milli", flat=True)) == 2000
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 192000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 61100
            print(json.dumps({"http_inventory_costs": "passed", "positive_and_negative_cost_fcfa": 1000,
                "revised_cost_fcfa": 1100, "initial_receipts_preserved": True, "stock_milli": 2000,
                "cash_fcfa": 192000, "supplier_debt_fcfa": 61100}))
            empty_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Écart sur stock vide", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=empty_product["id"])
            receive(10000, 1000, now - timedelta(hours=2))
            receive(10000, 1200, now - timedelta(hours=1))
            sale(19000, now)
            source = PurchaseLine.objects.get(product=product, unit_price_fcfa=1000)
            departed = post(base + str(source.document_id) + "/returns/", {**event(now),
                "lines": [{"line_id": str(source.id), "quantity_milli": 1000}]})
            assert departed["lines"][0]["cost"]["valuation_gap_nanos_fcfa"] == "100000000000"
            valuation_path = f"products/{product.id}/stock-valuations/"
            post(base + str(source.document_id) + "/charges/", {**event(now), "label": "Frais égalisant les coûts",
                "amount_fcfa": 2000, "allocations": [{"line_id": str(source.id), "amount_fcfa": 2000}]})
            # Fresh arithmetic now balances, but the captured historical gap still needs approval.
            assert get(valuation_path + "current/")["has_unresolved_valuation"]
            preview = post(valuation_path + "preview/", {"value_fcfa": 0}, expected=200)
            assert preview["valuation_action"] == "close_empty_gap" and preview["quantity_milli"] == "0"
            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": 0,
                "preview_token": preview["preview_token"], "reason": "Écart de stock vide examiné"}
            closed = post(valuation_path, body)
            assert post(valuation_path, body, expected=200) == closed
            current = get(valuation_path + "current/")
            assert current["stock_value_nanos_fcfa"] == "0" and not current["has_unresolved_valuation"]
            assert not current["has_unresolved_shortage"]
            post(base + str(source.document_id) + "/charges/", {**event(now), "label": "Frais arrivés après clôture",
                "amount_fcfa": 1000, "allocations": [{"line_id": str(source.id), "amount_fcfa": 1000}]})
            assert get(valuation_path + "current/")["has_unresolved_valuation"]
            assert post(valuation_path, body, expected=200) == closed
            fresh = post(valuation_path + "preview/", {"value_fcfa": 0}, expected=200)
            post(valuation_path, {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "preview_token": fresh["preview_token"], "reason": "Écart vide revu après frais"})
            current = get(valuation_path + "current/")
            assert not current["has_unresolved_valuation"] and current["quantity_milli"] == "0"
            original = StockCostSnapshot.objects.get(movement__product=product, movement__kind="supplier_return", version=1)
            assert original.valuation_gap_nanos_fcfa == 100000000000
            assert original.total_cost_nanos_fcfa == 1000000000000
            assert StockMovement.objects.filter(product=product).count() == 4
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 220500
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 83100
            print(json.dumps({"http_empty_stock_closure": "passed", "original_gap_fcfa": 100,
                "confirmed_empty_value_fcfa": 0, "late_fee_reopens_review": True, "explicit_reclosure": True,
                "cost_replay_cannot_close_gap": True, "historical_gap_unchanged": True,
                "cash_fcfa": 220500, "supplier_debt_fcfa": 83100}))
            shortage_product = post("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Survente à examiner", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500})
            product = Product.objects.get(id=shortage_product["id"])
            receive(1000, 1000, now - timedelta(hours=3))
            provisional = sale(2000, now - timedelta(hours=2))
            assert provisional.cost_state == "provisional" and provisional.total_cost_nanos_fcfa == 2000000000000
            receive(2000, 1200, now - timedelta(hours=2, minutes=30))
            valuation_path = f"products/{product.id}/stock-valuations/"
            current = get(valuation_path + "current/")
            assert current["quantity_milli"] == "1000" and current["has_unresolved_shortage"]
            preview = post(valuation_path + "preview/", {"value_fcfa": 1100}, expected=200)
            assert preview["valuation_requires_regularization_after"]
            post(valuation_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": 1100,
                "preview_token": preview["preview_token"], "reason": "Valeur actuelle contrôlée sans clôturer l'ancienne survente"})
            current = get(valuation_path + "current/")
            assert current["stock_value_nanos_fcfa"] == "1100000000000" and current["has_unresolved_shortage"]
            provisional.refresh_from_db()
            assert provisional.cost_state == "provisional" and provisional.total_cost_nanos_fcfa == 2000000000000
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 223500
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 86500
            print(json.dumps({"http_historical_shortage": "passed", "late_prior_receipt_keeps_review": True,
                "current_stock_value_fcfa": 1100, "old_sale_cost_fcfa": 2000, "old_sale_still_provisional": True,
                "cash_fcfa": 223500, "supplier_debt_fcfa": 86500}))
            review_path = f"products/{product.id}/stock-shortage-reviews/"
            preview = post(review_path + "preview/", {}, expected=200)
            assert preview["movement_count"] == 3
            movement_page = get(review_path + "movements/?page_size=1")
            assert movement_page["count"] == 3 and len(movement_page["results"]) == 1 and movement_page["next"]
            second_page = get(review_path + "movements/?page_size=1&page=2")
            final_page = get(review_path + "movements/?page_size=1&page=3")
            physical_history = movement_page["results"] + second_page["results"] + final_page["results"]
            old_sale_source = next(row for row in physical_history if row["kind"] == "sale")
            assert old_sale_source["source_id"] == str(provisional.movement.sale_line.sale_id)
            assert old_sale_source["cost"]["cost_state"] == "provisional"
            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Contrôle du manque historique",
                "preview_token": preview["preview_token"]}
            reviewed = post(review_path, body)
            assert post(review_path, body, expected=200) == reviewed
            assert get(review_path + reviewed["id"] + "/") == reviewed
            assert not get(valuation_path + "current/")["has_unresolved_shortage"]
            latest_source = PurchaseLine.objects.get(product=product, unit_price_fcfa=1200)
            post(base + str(latest_source.document_id) + "/charges/", {**event(now), "label": "Frais indépendants du contrôle physique",
                "amount_fcfa": 100, "allocations": [{"line_id": str(latest_source.id), "amount_fcfa": 100}]})
            current = get(valuation_path + "current/")
            assert not current["has_unresolved_shortage"] and current["has_unresolved_valuation"]
            sale(1000, now - timedelta(hours=1))
            current = get(valuation_path + "current/")
            assert current["has_unresolved_shortage"] and current["invalid_shortage_reviews_count"] == 1
            assert post(review_path, body, expected=200) == reviewed
            preview = post(review_path + "preview/", {}, expected=200)
            assert preview["quantity_milli"] == "0"
            post(review_path, {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "reason": "Nouvelle opération physique examinée", "preview_token": preview["preview_token"]})
            current = get(valuation_path + "current/")
            assert not current["has_unresolved_shortage"] and current["has_unresolved_valuation"]
            provisional.refresh_from_db()
            assert provisional.cost_state == "provisional" and provisional.total_cost_nanos_fcfa == 2000000000000
            page = get(review_path + "?page_size=1")
            assert page["count"] == 2 and len(page["results"]) == 1 and page["next"]
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 225000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 86500
            print(json.dumps({"http_stock_shortage_review": "passed", "fee_does_not_reopen_physical_review": True,
                "late_physical_sale_reopens_review": True, "explicit_reconfirmation": True,
                "old_cost_still_provisional": True, "value_still_needs_review": True,
                "cash_fcfa": 225000, "supplier_debt_fcfa": 86500}))
            before_movements = StockMovement.objects.count()
            before_costs = StockCostSnapshot.objects.count()
            issues = get(f"products/{product.id}/sale-cost-issues/?page_size=1")
            assert issues["count"] == 1 and len(issues["results"]) == 1
            issue = issues["results"][0]
            assert issue["id"] == str(provisional.movement.sale_line_id)
            assert issue["cost_state"] == "provisional" and issue["net_fcfa"] == "3000"
            assert issue["base_quantity_milli"] == "2000"
            assert issue["cost"]["total_cost_nanos_fcfa"] == "2000000000000"
            assert StockMovement.objects.count() == before_movements and StockCostSnapshot.objects.count() == before_costs
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 225000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 86500
            print(json.dumps({"http_sale_cost_issues": "passed", "physical_review_keeps_uncertain_cost_visible": True,
                "latest_cost_only": True, "cash_fcfa": 225000, "supplier_debt_fcfa": 86500}))
            centre = get("sale-cost-issues/?page_size=1")
            assert centre["count"] == 3 and centre["totals"] == {"unknown_lines": 1, "provisional_lines": 2}
            assert centre["data_scope"] == "server_known"
            second_page = get(centre["next"].replace(root, ""))
            last_page = get(second_page["next"].replace(root, ""))
            assert centre["as_of"] == second_page["as_of"] == last_page["as_of"]
            assert centre["totals"] == second_page["totals"] == last_page["totals"]
            assert last_page["next"] is None
            assert centre["results"][0]["id"] == str(negative.movement.sale_line_id)
            assert second_page["results"][0]["id"] == str(provisional.movement.sale_line_id)
            assert last_page["results"][0]["id"] == str(empty_cost.movement.sale_line_id)
            assert last_page["results"][0]["cost"]["total_cost_nanos_fcfa"] is None
            filtered = get(f"sale-cost-issues/?product_id={product.id}&cost_state=provisional")
            assert filtered["count"] == 1 and filtered["totals"] == {"unknown_lines": 0, "provisional_lines": 1}
            assert filtered["results"][0]["product_id"] == str(product.id)
            assert StockMovement.objects.count() == before_movements and StockCostSnapshot.objects.count() == before_costs
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == 225000
            assert sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)) == 86500
            print(json.dumps({"http_shop_sale_cost_issues": "passed", "unknown_lines": 1, "provisional_lines": 2,
                "all_filter_totals": True, "stable_cutoff_across_pages": True, "product_filter": True,
                "cash_fcfa": 225000, "supplier_debt_fcfa": 86500}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
