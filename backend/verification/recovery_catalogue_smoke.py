"""Real HTTP catalogue replay after a disposable SQLite T1-to-T0 rollback.

This isolates catalogue recovery; production quarantine/bootstrap and Android
are qualified by their own flows, not by this fixture.
"""
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import shutil
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):pass


@contextmanager
def disposable_directory(root):
    """Use inherited workspace ACLs, including on Python 3.14 Windows."""
    directory = root.resolve() / ("recovery-catalogue-http-" + uuid.uuid4().hex)
    directory.mkdir()
    try:yield directory
    finally:
        if directory.resolve().parent != root.resolve():raise ValueError("invalid verification cleanup path")
        shutil.rmtree(directory)


def main():
    temporary_root = Path(__file__).resolve().parents[1] / "runtime-control" / "verification"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with disposable_directory(temporary_root) as directory, ExitStack() as resources:
        database = Path(directory) / "business.sqlite3"
        snapshot = Path(directory) / "t0.sqlite3"
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(database)
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import RestoreAccessGeneration, RestoreAccessReview, RestoreAccessDecision, RestoreSession, RecoveryJournalOperation
        from backups.access_models import PERMISSION_FIELDS
        from catalogue.models import Product, ProductPack
        from cash.models import CashMovement
        from common.models import Activity
        from identity.models import User, Shop, Installation, DeviceSession
        from stock.models import StockMovement
        resources.callback(connections.close_all)
        call_command("migrate", verbosity=0)
        owner = User.objects.create_user(username="catalogue-http-owner")
        technical = User.objects.create_superuser(username="catalogue-http-technical", password=None)
        shop, foreign = [Shop.objects.create(owner=owner, name=name) for name in ("A", "B")]
        installation = Installation.objects.create(user=owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(owner); token["session_id"] = str(session.id)
        product = Product.objects.create(shop=shop, name="Bouteille", unit_kind="piece", unit_label="bouteille",
            sale_price_fcfa=250, purchase_price_fcfa=100)
        pack = ProductPack.objects.create(product=product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=6000)
        opening = Activity.objects.create(shop=shop, actor=owner, action="stock_opened", target_id=product.id, reason="Ouverture T0")
        StockMovement.objects.create(product=product, actor=owner, operation=opening, kind="opening", quantity_milli=17000,
            unit_cost_micros=100123456, occurred_at=timezone.now(), reason="Ouverture T0")
        connections.close_all()
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as target:source.backup(target)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        def http(path, data=None, method=None):
            encoded = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
            request = Request(root + path, data=encoded, method=method,
                headers={"Authorization": "Bearer " + str(token), "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=20) as response:return response.status, json.load(response)
            except HTTPError as error:return error.code, json.load(error)
        try:
            base = f"/api/v1/shops/{shop.id}/products/{product.id}/"
            pack_request = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Format T1 contrôlé",
                "base_quantity_milli": 12000, "sale_price_fcfa": 3000}
            status, pack_t1 = http(base + f"packs/{pack.id}/", pack_request, "PATCH")
            assert status == 200, (status, pack_t1)
            product_request = {"operation_id": str(uuid.uuid4()), "base_version": 2,
                "reason": "Prix T1 contrôlé", "sale_price_fcfa": 400, "purchase_price_fcfa": None}
            status, product_t1 = http(base, product_request, "PATCH")
            assert status == 200, (status, product_t1)
            product_fields = ("id", "shop_id", "category_id", "name", "unit_kind", "unit_label", "sale_price_fcfa",
                "purchase_price_fcfa", "stock_threshold_milli", "is_active", "version", "created_at", "updated_at")
            pack_fields = ("id", "product_id", "product_version", "product_updated_at", "name", "base_quantity_milli",
                "sale_price_fcfa", "is_active", "version", "created_at", "updated_at")
            entries = []
            recorded_at = timezone.now().isoformat()
            for raw, kind, fields in ((pack_request, "pack_update", pack_fields), (product_request, "product_update", product_fields)):
                event = Activity.objects.get(id=raw["operation_id"])
                before, after = event.details["before"], event.details["response"]
                entries.append({"operation_id": str(event.id), "operation_type": kind, "origin_user_id": str(owner.id),
                    "origin_installation_id": str(installation.id), "occurred_at": after["updated_at"], "origin_recorded_at": recorded_at,
                    "payload": {"schema_version": 1, "original_request": event.details["request"], "catalogue_revision": {
                        "before": {key: before[key] for key in fields}, "after": {key: after[key] for key in fields}}},
                    "dependencies": [pack_request["operation_id"]] if kind == "product_update" else []})
            assert entries[0]["payload"]["catalogue_revision"]["after"]["product_updated_at"] == entries[1]["payload"]["catalogue_revision"]["before"]["updated_at"]
            connections.close_all()
            with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(database)) as target:source.backup(target)
            assert Activity.objects.filter(id__in=[row["operation_id"] for row in entries]).count() == 0
            generation = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), initiated_by=technical.id)
            review = RestoreAccessReview.objects.create(generation=generation, shop=shop, user=owner, archived_role="owner",
                was_active=True, archived_permissions=dict.fromkeys(PERMISSION_FIELDS, True))
            RestoreAccessDecision.objects.create(review=review, operation_id=uuid.uuid4(), requested_by=technical,
                previous_version=0, version=1, state="confirmed", reason="Droits actuels de la fixture contrôlés")
            registry = RestoreSession.objects.create(generation=generation, inventory_recorded_at=timezone.now())
            recovery = f"/api/v1/shops/{shop.id}/recovery/"
            journal = {"operation_id": str(uuid.uuid4()), "session_id": str(registry.id), "shop_id": str(shop.id),
                "format_version": 1, "reason": "Copies T1 contrôlées après retour T0", "operations": entries[::-1]}
            status, deposited = http(recovery + "journals/", journal)
            assert status == 201 and deposited["business_effects_created"] == 0, (status, deposited)
            captures = {row["operation_id"]: row["capture_id"] for row in deposited["operations"]}
            paths = [recovery + "operations/" + captures[row["operation_id"]] + "/" for row in entries]
            status, old_product = http(paths[1] + "integration-preview/")
            assert status == 200 and not old_product["can_apply"], (status, old_product)
            assert http(f"/api/v1/shops/{foreign.id}/recovery/operations/{captures[entries[0]['operation_id']]}/integration-preview/")[0] == 404
            def apply(path, preview):
                command = {"operation_id": str(uuid.uuid4()), "previous_version": preview["version"],
                    "preview_token": preview["preview_token"], "reason": "État et source vérifiés"}
                return http(path + "integrate/", command), command
            for path in paths:
                status, preview = http(path + "integration-preview/")
                assert status == 200 and preview["can_apply"], (status, preview)
                (status, receipt), command = apply(path, preview)
                assert status == 201, (status, receipt)
                assert http(path + "integrate/", command) == (200, receipt)
                if path == paths[0]:assert apply(paths[1], old_product)[0][0] == 409
            status, current = http(base)
            assert status == 200 and (current["sale_price_fcfa"], current["version"], current["stock_validated_milli"]) == (400, 3, 17000)
            assert current["purchase_price_fcfa"] is None
            status, current_pack = http(base + f"packs/{pack.id}/")
            assert status == 200 and (current_pack["base_quantity_milli"], current_pack["product_version"]) == (12000, 3)
            assert (StockMovement.objects.count(), CashMovement.objects.count()) == (1, 0)
            assert StockMovement.objects.get().unit_cost_micros == 100123456
            assert RecoveryJournalOperation.objects.count() == 2
            print(json.dumps({"http": "passed", "fixture_sqlite_rollback": True, "source_operations": 2,
                "product_version": 3, "product_price_fcfa": 400, "pack_factor_milli": 12000,
                "validated_stock_milli": 17000, "historical_cost_micros": 100123456, "cash_movements": 0,
                "exact_parent_timestamp_recovered": True, "foreign_shop": 404, "stale_preview": 409,
                "retries": "initial receipts preserved"}))
        finally:
            server.shutdown(); thread.join(timeout=5); server.server_close(); connections.close_all()


if __name__ == "__main__":main()
