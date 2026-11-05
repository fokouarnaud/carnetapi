"""Verify duplicate warnings over real HTTP and upgrade populated customer indexes."""
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def main():
    with TemporaryDirectory(prefix="client-duplicate-http-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "clients.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connection, connections
        from django.db.migrations.executor import MigrationExecutor
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from cash.models import CashMovement
        from customers.models import Customer, CustomerDebt, CustomerLedgerEntry
        from customers.services.duplicate_queries import with_duplicate_hint
        from identity.models import DeviceSession, Installation, Membership, Shop, User
        from sales.models import Sale
        from stock.models import StockMovement

        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [node for node in latest if node[0] != "customers"] + [("customers", "0001_domain_state")]
        executor.migrate(previous)
        owner = User.objects.create_user(username="duplicate-http-owner")
        seller = User.objects.create_user(username="duplicate-http-seller")
        shop = Shop.objects.create(owner=owner, name="Boutique A")
        other = Shop.objects.create(owner=owner, name="Boutique B")
        Membership.objects.create(shop=shop, user=seller)
        anchor = Customer.objects.create(shop=shop, name="Client commun", phone="+237600000001")
        name_match = Customer.objects.create(shop=shop, name="Client commun", phone=None)
        phone_match = Customer.objects.create(shop=shop, name="Autre client", phone=anchor.phone)
        Customer.objects.create(shop=other, name=anchor.name, phone=anchor.phone)
        Customer.objects.bulk_create([Customer(shop=shop, name=f"Client distinct {index}") for index in range(200)])
        before = list(Customer.objects.order_by("id").values())
        call_command("migrate", verbosity=0)
        assert list(Customer.objects.order_by("id").values()) == before
        plan = with_duplicate_hint(Customer.objects.filter(shop=shop)).order_by("created_at", "id")[:25].explain()
        assert "customer_shop_name_match" in plan and "customer_shop_phone_match" in plan

        def token(user):
            device = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
            access = AccessToken.for_user(user)
            access["session_id"] = str(session.id)
            return str(access), session

        owner_token, _owner_session = token(owner)
        seller_token, seller_session = token(seller)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/clients/"

        def request(bearer, endpoint, body=None):
            req = Request(endpoint, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
            try:
                response = urlopen(req, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        try:
            debt = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                "occurred_at": "2026-01-01T00:00:00Z", "reason": "Dette antérieure confirmée"}
            status, _opened = request(owner_token, base + str(anchor.id) + "/debts/", debt)
            assert status == 201
            debt_before = list(CustomerDebt.objects.values())
            ledger_before = list(CustomerLedgerEntry.objects.values())
            endpoint = base + str(anchor.id) + "/possible-duplicates/?page_size=1"
            status, first = request(seller_token, endpoint)
            assert status == 200 and first["count"] == 2 and len(first["results"]) == 1
            status, second = request(seller_token, first["next"])
            assert status == 200 and second["next"] is None
            ids = [first["results"][0]["id"], second["results"][0]["id"]]
            assert ids == [str(name_match.id), str(phone_match.id)]
            assert all("amount_fcfa" not in row and "debts" not in row for row in first["results"] + second["results"])
            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Nouvelle fiche", "phone": anchor.phone}
            status, created = request(seller_token, base, body)
            retry_status, retry = request(seller_token, base, body)
            assert status == 201 and created["has_possible_duplicate"] is True
            assert retry_status == 200 and retry == created
            status, current = request(seller_token, base + str(anchor.id) + "/")
            assert status == 200 and current["has_possible_duplicate"] is True
            assert Customer.objects.filter(shop=shop).count() == 204
            assert list(CustomerDebt.objects.values()) == debt_before
            assert list(CustomerLedgerEntry.objects.values()) == ledger_before
            assert not Sale.objects.exists() and not StockMovement.objects.exists() and not CashMovement.objects.exists()
            DeviceSession.objects.filter(id=seller_session.id).update(revoked_at=timezone.now())
            revoked, _response = request(seller_token, endpoint)
            assert revoked == 401
            print(json.dumps({"http_client_duplicates": "passed", "populated_index_upgrade_preserves_rows": True,
                "indexed_name_and_phone": True, "shop_local_matches": 2, "profile_creation": 201, "retry": 200,
                "separate_uuids": True, "debt_and_ledger_unchanged": True, "no_cash_sale_or_stock_created": True,
                "revoked_session": revoked}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
