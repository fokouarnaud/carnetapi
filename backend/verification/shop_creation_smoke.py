"""Exercise connected shop creation over HTTP using a disposable migrated database."""
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
    with TemporaryDirectory(prefix="shop-creation-http-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "shops.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from common.models import Activity
        from cash.models import CashMovement
        from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User
        from stock.models import StockMovement
        call_command("migrate", verbosity=0)
        owner = User.objects.create_user(username="shop-http-owner")
        other = User.objects.create_user(username="shop-http-other")

        def credentials(user):
            installation = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
            token = AccessToken.for_user(user)
            token["session_id"] = str(session.id)
            return str(token), session

        bearer, session = credentials(owner)
        foreign_bearer, _foreign_session = credentials(other)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}/api/v1/"
        try:
            def request(path, body=None, token=bearer):
                req = Request(root + path, method="POST" if body is not None else "GET",
                    data=None if body is None else json.dumps(body).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                try:
                    with urlopen(req, timeout=15) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)

            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Boutique pilote"}
            status, created = request("shops/", body)
            assert status == 201 and created["id"] == body["id"] and created["timezone"] == "Africa/Douala"
            assert request("shops/", body) == (200, created)
            assert Shop.objects.get(id=body["id"]).owner_id == owner.id
            assert not Membership.objects.exists() and not ShopDeviceAccess.objects.exists()
            operation = Activity.objects.get(id=body["operation_id"])
            assert operation.details["installation_id"] == str(session.installation_id)
            assert request("shops/", {**body, "name": "Autre"})[0] == 409
            assert request("shops/", body, foreign_bearer)[0] == 409
            assert request(f"shops/{created['id']}/", token=foreign_bearer)[0] == 404
            assert request("shops/", token=foreign_bearer)[1]["count"] == 0
            status, product = request(f"shops/{created['id']}/products/", {"id": str(uuid.uuid4()),
                "operation_id": str(uuid.uuid4()), "name": "Premier produit", "unit_kind": "piece",
                "unit_label": "pièce", "sale_price_fcfa": 100, "stock_threshold_milli": 0})
            assert status == 201 and product["stock_available_milli"] == 0 and product["is_stock_low"]
            status, lease = request(f"shops/{created['id']}/verify-access/", {})
            assert status == 200 and lease["role"] == "manager" and lease["permissions"]["can_read_cost"]
            second = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Boutique pilote"}
            assert request("shops/", second, foreign_bearer)[0] == 201
            assert request("shops/")[1]["count"] == request("shops/", token=foreign_bearer)[1]["count"] == 1
            session.revoked_at = timezone.now()
            session.save()
            assert request("shops/", body)[0] == 401
            assert request("shops/", {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())})[0] == 401
            assert Shop.objects.count() == 2 and Activity.objects.filter(action="shop_created").count() == 2
            assert not StockMovement.objects.exists() and not CashMovement.objects.exists()
            print(json.dumps({"http_shop_creation": "passed", "owner_from_current_account": True,
                "same_operation_one_shop": True, "shop_isolation": True, "creator_can_prepare_catalogue": True,
                "lease_separate_from_creation": True, "revoked_session_blocks_retry": True,
                "no_stock_or_cash_created": True}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
