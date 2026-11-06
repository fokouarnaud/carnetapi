"""Qualify pack-price fallback and exact retries over private loopback HTTP."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import WSGIServer, make_server

from verification.catalogue_price_conflicts_smoke import QuietHandler, private_installation


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    with private_installation() as root:
        database = root / "pack-price-qualification.sqlite3"
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(database)
        import django
        django.setup()
        from django.conf import settings
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.db.models import Sum
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from catalogue.models import ProductPack, PackPriceConflict, PackPriceProposal, PackPriceResolution
        from cash.models import CashMovement
        from common.models import Activity
        from identity.models import DeviceSession, Installation, Membership, Shop, User
        from sales.models import SaleLine
        from stock.models import StockMovement

        assert Path(settings.DATABASES["default"]["NAME"]).resolve() == database.resolve()
        with open(os.devnull, "w") as sink:
            call_command("migrate", verbosity=0, stdout=sink)
        owner = User.objects.create_user(username="qualification-pack-price-owner")
        manager = User.objects.create_user(username="qualification-pack-price-manager")
        seller = User.objects.create_user(username="qualification-pack-price-seller")
        shop = Shop.objects.create(owner=owner, name="Qualification format A")
        other = Shop.objects.create(owner=owner, name="Qualification format B")
        Membership.objects.create(shop=shop, user=manager, role="manager")
        Membership.objects.create(shop=shop, user=seller)

        def device(user):
            installation = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
            token = AccessToken.for_user(user)
            token["session_id"] = str(session.id)
            return {"token": str(token), "session": session, "installation": installation}

        a, b, cashier = device(owner), device(manager), device(seller)
        connections.close_all()
        server = make_server("127.0.0.1", 0, get_wsgi_application(), server_class=ThreadedServer, handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(actor, path, data=None, method=None):
            incoming = Request(base + path, data=None if data is None else json.dumps(data).encode("utf-8"),
                method=method, headers={"Authorization": "Bearer " + actor["token"], "Content-Type": "application/json"})
            try:
                response = urlopen(incoming, timeout=25)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        def simultaneous(actor, path, data):
            barrier = Barrier(2)
            def invoke():
                barrier.wait(timeout=10)
                return request(actor, path, data)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(invoke) for _ in range(2)]
                return [task.result(timeout=35) for task in pending]

        try:
            products = f"/api/v1/shops/{shop.id}/products/"
            product_id, pack_id = str(uuid.uuid4()), str(uuid.uuid4())
            product_path = products + product_id + "/"
            status, product = request(a, products, {"id": product_id, "operation_id": str(uuid.uuid4()),
                "name": "Bouteille qualification", "unit_kind": "piece", "unit_label": "bouteille", "sale_price_fcfa": 250,
                "opening": {"quantity_milli": 100000, "unit_cost_fcfa": "200.000000",
                    "occurred_at": "2026-01-01T00:00:00Z", "reason": "Stock physique"}})
            assert status == 201, (status, product)
            packs = product_path + "packs/"
            pack_path = packs + pack_id + "/"
            status, pack = request(a, packs, {"id": pack_id, "operation_id": str(uuid.uuid4()), "name": "Carton de 24",
                "base_quantity_milli": 24000, "sale_price_fcfa": 4800})
            assert status == 201, (status, pack)
            change = {"operation_id": str(uuid.uuid4()), "base_version": 1, "sale_price_fcfa": 5200, "reason": "Tarif A confirmé"}
            status, source_receipt = request(a, pack_path, change, "PATCH")
            assert status == 200 and source_receipt["version"] == 2, (status, source_receipt)
            sales = f"/api/v1/shops/{shop.id}/sales/"

            def sell(price):
                status, current = request(cashier, pack_path)
                assert status == 200 and current["sale_price_fcfa"] == price, (status, current)
                command = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": product_id, "product_version": current["product_version"],
                        "pack_id": pack_id, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": price}]}
                status, receipt = request(cashier, sales, command)
                assert status == 201 and receipt["lines"][0]["unit_price_fcfa"] == price, (status, receipt)
                return command, receipt

            history = [sell(5200)]
            counts = StockMovement.objects.count(), CashMovement.objects.count()
            conflicts = pack_path + "price-conflicts/"
            command = {"operation_id": str(uuid.uuid4()), "source_change_id": change["operation_id"], "base_version": 1,
                "proposed_price_fcfa": 5000, "reason": "Tarif concurrent B depuis version 1"}
            concurrent = simultaneous(b, conflicts, command)
            assert sorted(status for status, _ in concurrent) == [200, 201], concurrent
            recorded = concurrent[0][1]
            assert concurrent[1][1] == recorded
            assert recorded["common_price_fcfa"] == 4800 and recorded["base_quantity_milli"] == 24000
            assert recorded["pack_version"] == recorded["product_version"] == 3
            assert (StockMovement.objects.count(), CashMovement.objects.count()) == counts
            status, public = request(cashier, pack_path)
            assert status == 200 and public["price_conflict"] == {"id": recorded["id"], "state": "open", "common_price_fcfa": 4800}
            assert "proposals" not in public and "purchase_price_fcfa" not in public
            assert request(cashier, conflicts)[0] == 403
            assert request(a, f"/api/v1/shops/{other.id}/products/{product_id}/packs/{pack_id}/price-conflicts/")[0] == 404
            assert request(a, pack_path, {"operation_id": str(uuid.uuid4()), "base_version": 3,
                "base_quantity_milli": 12000, "reason": "Facteur à revoir"}, "PATCH")[0] == 409
            assert request(a, pack_path, change, "PATCH") == (200, source_receipt)
            history.append(sell(4800))
            detail = conflicts + recorded["id"] + "/"
            status, preview = request(b, detail + "preview/")
            assert status == 200 and preview["can_resolve"] and len(preview["preview_token"]) <= 512
            selected = next(row for row in recorded["proposals"] if row["proposed_price_fcfa"] == 5000)
            resolution = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
                "proposal_id": selected["id"], "reason": "Tarif 5000 relu et validé"}
            assert request(a, detail + "resolve/", resolution)[0] == 409
            counts = StockMovement.objects.count(), CashMovement.objects.count()
            concurrent = simultaneous(b, detail + "resolve/", resolution)
            assert [status for status, _ in concurrent] == [200, 200], concurrent
            resolved = concurrent[0][1]
            assert concurrent[1][1] == resolved and resolved["resolution"]["price_fcfa"] == 5000
            assert resolved["pack_version"] == resolved["product_version"] == 4
            assert (StockMovement.objects.count(), CashMovement.objects.count()) == counts
            assert request(b, conflicts, command) == (200, recorded)
            history.append(sell(5000))
            for sale, receipt in history:
                assert request(cashier, sales, sale) == (200, receipt)
                assert request(cashier, sales + sale["id"] + "/") == (200, receipt)
            assert list(SaleLine.objects.order_by("created_at").values_list("unit_price_fcfa", "factor_milli")) == [(5200, 24000), (4800, 24000), (5000, 24000)]
            assert (PackPriceConflict.objects.count(), PackPriceProposal.objects.count(), PackPriceResolution.objects.count()) == (1, 2, 1)
            assert ProductPack.objects.get(id=pack_id).base_quantity_milli == 24000
            assert StockMovement.objects.aggregate(total=Sum("quantity_milli"))["total"] == 28000
            assert CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"] == 15000
            assert Activity.objects.get(id=change["operation_id"]).details["response"] == source_receipt
            DeviceSession.objects.filter(id=b["session"].id).update(revoked_at=timezone.now())
            assert request(b, conflicts, command)[0] == 401
            report = {"pack_price_conflicts_wsgi": "passed", "transport": "real_threaded_loopback_http", "database": "disposable_sqlite",
                "fixture_authentication_only": True, "otp_exercised": False, "historical_prices_fcfa": [5200, 4800, 5000],
                "factor_milli": 24000, "final_stock_milli": 28000, "cash_total_fcfa": 15000,
                "concurrent_record_statuses": [200, 201], "concurrent_resolution_statuses": [200, 200],
                "record_and_resolution_retries_exact": True, "source_receipt_unchanged": True,
                "seller_private_conflict_status": 403, "revoked_session_status": 401,
                "limits": ["Price-only pack conflicts; factor-changing conflicts remain to implement.", "No Android or ordinary synchronization qualification."]}
            output = Path(__file__).resolve().parents[2] / "analysis" / "pack-price-conflicts-smoke-2026-11-05.json"
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
