"""Exercise fee preview, real expense, allocation history and sale costing over HTTP."""
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server
from verification.sale_cost_smoke import QuietHandler


def main():
    with TemporaryDirectory(prefix="purchase-charge-http-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "charges.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from identity.models import User, Shop, Installation, DeviceSession
        from purchasing.models import PurchaseCharge, SupplierDebt
        from stock.models import StockCostSnapshot, StockMovement
        from cash.models import CashMovement
        call_command("migrate", verbosity=0)
        owner = User.objects.create_user(username="charge-http-owner")
        shop = Shop.objects.create(owner=owner, name="Frais HTTP")
        device = Installation.objects.create(user=owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(owner)
        token["session_id"] = str(session.id)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/"
        try:
            def request(path, body=None, status=201):
                query = Request(root + path, data=None if body is None else json.dumps(body).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(query, timeout=30) as response:
                    assert response.status == status
                    return json.loads(response.read())
            def event(**values):
                return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                    "reason": "Contrôle des frais", **values}
            products = [request("products/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": name, "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1500}) for name in ("A", "B")]
            supplier = request("suppliers/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Fournisseur"})
            invoices = f"suppliers/{supplier['id']}/invoices/"
            invoice = request(invoices, event(lines=[{"id": str(uuid.uuid4()), "product_id": product["id"],
                "quantity_milli": 10000, "unit_price_fcfa": price} for product, price in zip(products, (1000, 3000), strict=True)]))
            charges = invoices + invoice["id"] + "/charges/"
            preview = request(charges + "preview/", {"amount_fcfa": 4000}, status=200)
            assert [row["amount_fcfa"] for row in preview["allocations"]] == [1000, 3000]
            assert not PurchaseCharge.objects.exists()
            request("cash/", event(kind="expense", method="cash", amount_fcfa=4000))
            body = event(label="Transport", amount_fcfa=4000, allocations=preview["allocations"])
            charge = request(charges, body)
            assert CashMovement.objects.count() == 1
            request(invoices + invoice["id"] + "/receipts/", event(lines=[{"line_id": invoice["lines"][0]["id"], "quantity_milli": 10000}]))
            def sell():
                sale = request("sales/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": products[0]["id"], "product_version": 1, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1500}]})
                return StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
            original = sell()
            assert original.total_cost_nanos_fcfa == 1100000000000
            revisions = charges + charge["id"] + "/revisions/"
            correction = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Répartition corrigée",
                "allocations": [{"line_id": row["line_id"], "amount_fcfa": 2000} for row in preview["allocations"]]}
            revised = request(revisions, correction)
            assert revised["version"] == 2 and revised["historical_costs_pending_review"]
            assert request(revisions, correction, status=200) == revised
            assert request(charges, body, status=200) == charge
            current = request(charges + charge["id"] + "/", status=200)
            assert current["current_revision"]["version"] == 2
            following = sell()
            assert following.total_cost_nanos_fcfa == 1200000000000
            original.refresh_from_db()
            assert original.total_cost_nanos_fcfa == 1100000000000
            assert CashMovement.objects.count() == 3
            assert sum(CashMovement.objects.values_list("amount_fcfa", flat=True)) == -1000
            assert SupplierDebt.objects.get().remaining_fcfa == 40000
            assert sum(StockMovement.objects.values_list("quantity_milli", flat=True)) == 8000
            print(json.dumps({"http_purchase_charges": "passed", "allocation_fcfa": [1000, 3000],
                "revised_allocation_fcfa": [2000, 2000], "first_sale_cost_fcfa": 1100, "next_sale_cost_fcfa": 1200,
                "original_cost_preserved": True, "historical_costs_pending_review": True,
                "cash_fcfa": -1000, "supplier_debt_fcfa": 40000, "stock_milli": 8000}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
