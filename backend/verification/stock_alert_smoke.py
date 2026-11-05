"""Exercise US-15 over loopback HTTP with a disposable SQLite database."""
import json
import hashlib
from io import BytesIO
from PIL import Image
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from wsgiref.simple_server import make_server
import uuid


def main():
    with TemporaryDirectory(prefix="stock-alert-smoke-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "smoke.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from identity.models import DeviceSession, Installation, Membership, Shop, User
        from catalogue.models import Product
        from stock.models import StockMovement
        from customers.models import Customer, CustomerDebt
        from purchasing.models import SupplierDebt, PurchaseDocument, SupplierCredit
        from cash.models import CashMovement

        call_command("migrate", verbosity=0)
        owner = User.objects.create_user(username="alert-owner")
        seller = User.objects.create_user(username="alert-seller")
        shop = Shop.objects.create(name="Alertes HTTP", owner=owner)
        Membership.objects.create(shop=shop, user=seller)

        def token(user):
            installation = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
            access = AccessToken.for_user(user)
            access["session_id"] = str(session.id)
            return str(access)

        owner_token, seller_token = token(owner), token(seller)
        server = make_server("127.0.0.1", 0, get_wsgi_application())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/"

        def request(path, bearer=owner_token, body=None, method="GET", language="fr"):
            target = f"http://127.0.0.1:{server.server_port}" + path if path.startswith("/") else root + path
            req = Request(target, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json", "Accept-Language": language})
            try:
                with urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)

        try:
            oversized_status, oversized_error = request("sales/", body={"padding": "x" * 1000001}, method="POST", language="en")
            assert oversized_status == 413 and oversized_error == {
                "code": "json_body_too_large", "detail": "JSON request body is too large."}
            language_change = {"operation_id": str(uuid.uuid4()), "base_version": 1, "language": "en"}
            status, language_receipt = request("/api/v1/account/language/", body=language_change, method="PATCH")
            assert status == 200 and language_receipt["language"] == "en" and language_receipt["version"] == 2
            assert request("/api/v1/account/language/", body=language_change, method="PATCH") == (200, language_receipt)
            assert request("/api/v1/account/language/")[1] == language_receipt
            assert request("/api/v1/account/language/", seller_token)[1]["language"] == "fr"
            stale_language = {**language_change, "operation_id": str(uuid.uuid4()), "language": "fr"}
            status, language_conflict = request("/api/v1/account/language/", body=stale_language, method="PATCH", language="en")
            assert status == 409 and language_conflict == {"code": "language_version_conflict",
                "detail": "The language preference has changed. Read its current version."}
            template_request = Request(root + "catalogue-imports/template/", headers={"Authorization": f"Bearer {owner_token}",
                "Accept": "text/csv", "Accept-Language": "en"})
            with urlopen(template_request, timeout=10) as response:
                assert response.status == 200 and b"Sample product" in response.read()
            def upload_csv(identifier, source, prefix="catalogue-imports"):
                boundary = "csv-" + uuid.uuid4().hex
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="id"\r\n\r\n{identifier}\r\n'
                        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="catalogue.csv"\r\nContent-Type: text/csv\r\n\r\n').encode()
                body += source + f'\r\n--{boundary}--\r\n'.encode()
                req = Request(root + prefix + "/", data=body, method="POST",
                    headers={"Authorization": f"Bearer {owner_token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
                try:
                    with urlopen(req, timeout=10) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            status, oversized_csv = upload_csv(str(uuid.uuid4()), b"x" * 2000000)
            assert status == 400 and "file" in oversized_csv
            customer_csv = ("name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason,due_date\n"
                            "Amina,+237699123456,3000,2026-01-01T10:00:00Z,Créance antérieure,2026-01-31\n"
                            "Amina,+237699123456,,,,\n").encode()
            status, customers_preview = upload_csv(str(uuid.uuid4()), customer_csv, "customer-imports")
            assert status == 201 and customers_preview["kind"] == "customers" and not customers_preview["has_errors"]
            customers_confirmation = {"operation_id": str(uuid.uuid4()), "source_sha256": customers_preview["source_sha256"], "reason": "Reprise clients"}
            customers_path = f"customer-imports/{customers_preview['id']}/confirm/"
            status, customers_receipt = request(customers_path, body=customers_confirmation, method="POST")
            assert status == 200 and customers_receipt["created_count"] == 2 and Customer.objects.count() == 2
            assert CustomerDebt.objects.get().amount_fcfa == 3000
            assert request(customers_path, body=customers_confirmation, method="POST") == (200, customers_receipt)
            assert request(f"supplier-imports/{customers_preview['id']}/confirm/", body=customers_confirmation, method="POST")[0] == 404
            assert request(f"customer-imports/{customers_preview['id']}/", seller_token)[0] == 403
            supplier_csv = b"name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason,opening_advance_fcfa,opening_credit_fcfa\nFournisseur,,7000,2026-01-01T10:00:00Z,Solde anterieur,30000,5000\n"
            status, suppliers_preview = upload_csv(str(uuid.uuid4()), supplier_csv, "supplier-imports")
            assert status == 201 and suppliers_preview["kind"] == "suppliers" and not suppliers_preview["has_errors"]
            status, suppliers_receipt = request(f"supplier-imports/{suppliers_preview['id']}/confirm/", body={
                "operation_id": str(uuid.uuid4()), "source_sha256": suppliers_preview["source_sha256"], "reason": "Reprise fournisseurs"}, method="POST")
            assert status == 200 and suppliers_receipt["created_count"] == 1 and SupplierDebt.objects.get().remaining_fcfa == 7000
            assert not CashMovement.objects.exists() and not PurchaseDocument.objects.exists()
            assert dict(SupplierCredit.objects.values_list("kind", "remaining_fcfa")) == {"opening_advance": 30000, "opening_credit": 5000}
            supplier_id = suppliers_receipt["supplier_ids"][0]
            supplier_prefix = f"suppliers/{supplier_id}/"
            status, opening_list = request(supplier_prefix + "opening-balances/")
            assert status == 200 and opening_list["count"] == 2
            advance_id = next(row["id"] for row in opening_list["results"] if row["kind"] == "opening_advance")
            credit_id = next(row["id"] for row in opening_list["results"] if row["kind"] == "opening_credit")
            assert all(row["return_id"] is None and row["reference_fcfa"] is None for row in opening_list["results"])
            allocation = {"operation_id": str(uuid.uuid4()), "reason": "Avance affectée explicitement", "allocations": [
                {"debt_id": str(SupplierDebt.objects.get().id), "amount_fcfa": 3000}]}
            status, allocated_opening = request(supplier_prefix + f"credits/{advance_id}/allocate/", body=allocation, method="POST")
            assert status == 201 and allocated_opening["after_remaining_fcfa"] == 27000
            assert request(supplier_prefix + f"credits/{advance_id}/allocate/", body=allocation, method="POST") == (200, allocated_opening)
            assert SupplierDebt.objects.get().remaining_fcfa == 4000 and not CashMovement.objects.exists()
            opening_refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T10:00:00Z",
                              "reason": "Remboursement réellement reçu", "method": "orange", "amount_fcfa": 2000}
            status, refunded_opening = request(supplier_prefix + f"credits/{credit_id}/refund/", body=opening_refund, method="POST")
            assert status == 201 and refunded_opening["after_remaining_fcfa"] == 3000
            assert request(supplier_prefix + f"credits/{credit_id}/refund/", body=opening_refund, method="POST") == (200, refunded_opening)
            assert CashMovement.objects.get().amount_fcfa == 2000
            status, supplier_statement = request(supplier_prefix + "statement/?page_size=2")
            assert status == 200 and supplier_statement["count"] == 5 and len(supplier_statement["results"]) == 2
            assert supplier_statement["closing_debt_fcfa"] == 4000
            assert supplier_statement["closing_advance_fcfa"] == 27000
            assert supplier_statement["closing_credit_fcfa"] == 3000
            assert supplier_statement["closing_balance_fcfa"] == -26000 and supplier_statement["provisional"]
            next_statement = supplier_statement["next"].split(f"http://127.0.0.1:{server.server_port}", 1)[1]
            status, supplier_statement_page_two = request(next_statement)
            assert status == 200 and supplier_statement_page_two["as_of"] == supplier_statement["as_of"]
            assert supplier_statement_page_two["closing_balance_fcfa"] == -26000
            assert request(supplier_prefix + "statement/", seller_token)[0] == 403


            csv_source = ('name,unit_kind,unit_label,sale_price_fcfa,stock_threshold_milli,barcode,opening_quantity_milli,opening_unit_cost_fcfa,opening_occurred_at,opening_reason\r\n'
                          '"Savon, importé",piece,pièce,250,8000,IMPORT-001,10000,,2026-01-01T10:00:00Z,Comptage confirmé\r\n'
                          'Farine importée,weight,kg,1000,,,250,600.123456,2026-01-01T10:00:00Z,Pesée réelle\r\n').encode("utf-8-sig")
            import_id = str(uuid.uuid4())
            status, import_preview = upload_csv(import_id, csv_source)
            assert status == 201 and not import_preview["has_errors"] and import_preview["valid_count"] == 2
            assert import_preview["rows"][1]["data"]["opening"]["unit_cost_fcfa"] == "600.123456"
            assert Product.objects.count() == 0
            assert upload_csv(import_id, csv_source) == (200, import_preview)
            import_confirmation = {"operation_id": str(uuid.uuid4()), "source_sha256": import_preview["source_sha256"], "reason": "Reprise CSV vérifiée"}
            status, import_receipt = request(f"catalogue-imports/{import_id}/confirm/", body=import_confirmation, method="POST")
            assert status == 200 and import_receipt["created_count"] == 2
            assert request(f"catalogue-imports/{import_id}/confirm/", body=import_confirmation, method="POST") == (200, import_receipt)
            assert Product.objects.count() == 2 and StockMovement.objects.filter(kind="opening").count() == 2
            assert StockMovement.objects.get(product__name="Farine importée").unit_cost_micros == 600123456
            invalid_id = str(uuid.uuid4())
            status, invalid_import = upload_csv(invalid_id, b"name,unit_kind,unit_label,sale_price_fcfa\nA,piece,piece,1\nB,piece,piece,1.5\n")
            assert status == 201 and invalid_import["has_errors"]
            status, invalid_receipt = request(f"catalogue-imports/{invalid_id}/confirm/", body={"operation_id": str(uuid.uuid4()),
                "source_sha256": invalid_import["source_sha256"], "reason": "Confirmation refusée"}, method="POST")
            assert status == 409 and invalid_receipt["code"] == "catalogue_import_invalid" and Product.objects.count() == 2
            data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Épices Savon", "barcode": "0012345678905",
                    "unit_kind": "piece", "unit_label": "piece", "sale_price_fcfa": 250,
                    "stock_threshold_milli": 8000,
                    "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None,
                                "occurred_at": "2026-01-01T10:00:00Z", "reason": "Reprise"}}
            status, created = request("products/", body=data, method="POST")
            assert status == 201 and created["stock_available_milli"] == 10000
            invalid_threshold = {**data, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "stock_threshold_milli": 1500}
            status, validation = request("products/", body=invalid_threshold, method="POST", language="en")
            assert status == 400 and validation["stock_threshold_milli"] == ["A piece quantity must be a whole number."]
            detail = f"products/{data['id']}/"
            photo_source = BytesIO()
            Image.new("RGB", (1024, 768), "#246A9C").save(photo_source, format="JPEG")
            source = photo_source.getvalue()
            photo_operation = str(uuid.uuid4())
            def upload_photo():
                boundary = "photo-" + uuid.uuid4().hex
                fields = {"operation_id": photo_operation, "base_version": "0", "reason": "Photo pilote"}
                pieces = []
                for key, value in fields.items():
                    pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
                pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="phone.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode())
                pieces.extend([source, f'\r\n--{boundary}--\r\n'.encode()])
                req = Request(root + detail + "photo/", data=b"".join(pieces), method="POST",
                              headers={"Authorization": f"Bearer {owner_token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
                with urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)
            status, photo = upload_photo()
            assert status == 200 and photo["size_bytes"] <= 20000
            assert upload_photo() == (200, photo)
            req = Request(root + detail + "photo/", headers={"Authorization": f"Bearer {seller_token}", "Accept": "image/jpeg"})
            with urlopen(req, timeout=10) as response:
                downloaded = response.read()
                assert response.status == 200 and response.headers["Content-Type"] == "image/jpeg"
            assert hashlib.sha256(downloaded).hexdigest() == photo["sha256"]
            assert request(detail, seller_token)[1]["photo"]["version"] == 1
            assert created["barcode"] == "0012345678905" and created["barcode_version"] == 1
            pack = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                    "base_quantity_milli": 24000, "sale_price_fcfa": 4800, "barcode": "CARTON-24"}
            status, format_created = request(detail + "packs/", body=pack, method="POST")
            assert status == 201 and format_created["barcode"] == "CARTON-24"
            status, scanned = request("barcode-lookup/?" + urlencode({"code": "CARTON-24"}), seller_token)
            assert status == 200 and scanned["pack_id"] == pack["id"]
            assert scanned["base_quantity_milli"] == 24000 and scanned["sale_price_fcfa"] == 4800
            assert "purchase_price_fcfa" not in scanned
            duplicate = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                         "code": "0012345678905", "reason": "Doublon à refuser"}
            status, conflict = request(detail + f"packs/{pack['id']}/barcode/", body=duplicate, method="PATCH")
            assert status == 409 and conflict["code"] == "barcode_conflict"
            assert conflict["conflicting_pack_id"] is None
            assert request("products/?" + urlencode({"search": "épices"}), seller_token)[1]["count"] == 1
            change = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                      "reason": "Seuil neuf", "stock_threshold_milli": 9000}
            status, updated = request(detail, body=change, method="PATCH")
            assert status == 200 and updated["stock_threshold_milli"] == 9000
            assert request(detail, body=change, method="PATCH") == (200, updated)
            assert request(detail, seller_token, change, "PATCH")[0] == 403
            assert request("stock-alerts/", seller_token)[0] == 403
            assert request("stock-alerts/")[1]["count"] == 0
            sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"], "product_version": 2, "quantity_milli": 2000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}]}
            status, sold = request("sales/", seller_token, sale, "POST")
            assert status == 201
            assert request("sales/", seller_token, sale, "POST") == (200, sold)
            status, product = request(detail, seller_token)
            assert status == 200 and product["stock_available_milli"] == 8000 and product["is_stock_low"]
            assert "purchase_price_fcfa" not in product
            assert request("stock-alerts/")[1]["count"] == 1
            status, candidates = request("stock-alert-events/")
            assert status == 200 and candidates["count"] == 1
            candidate_id = candidates["results"][0]["id"]
            assert request("stock-alert-events/")[1] == candidates
            assert request("stock-alert-events/", seller_token)[0] == 403
            preference = {"operation_id": str(uuid.uuid4()), "notifications_enabled": False}
            status, saved = request("stock-notification-preference/", body=preference, method="PATCH")
            assert status == 200 and saved["notifications_enabled"] is False
            assert request("stock-notification-preference/", body=preference, method="PATCH") == (200, saved)
            assert request("stock-alert-events/")[1]["count"] == 0
            assert request("stock-alerts/")[1]["count"] == 1
            clear = {"operation_id": str(uuid.uuid4()), "base_version": 2,
                     "reason": "Retrait seuil", "stock_threshold_milli": None}
            status, cleared = request(detail, body=clear, method="PATCH")
            assert status == 200 and cleared["stock_threshold_milli"] is None
            assert request("stock-alerts/")[1]["count"] == 0
            assert StockMovement.objects.filter(kind="sale").count() == 1
            remove_code = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                           "code": None, "reason": "Retrait code unité"}
            status, removed = request(detail + "barcode/", body=remove_code, method="PATCH")
            assert status == 200 and removed["version"] == 2 and removed["code"] is None
            assert request("barcode-lookup/?code=0012345678905", seller_token)[0] == 404
            remove_photo = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Retrait photo"}
            status, removed_photo = request(detail + "photo/", body=remove_photo, method="DELETE")
            assert status == 200 and removed_photo["version"] == 2 and not removed_photo["present"]
            assert request(detail, seller_token)[1]["photo"]["version"] == 2
            pack_detail = detail + f"packs/{pack['id']}/"
            pack_sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"], "product_version": 3,
                           "pack_id": pack["id"], "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4800}]}
            status, original_pack_sale = request("sales/", seller_token, pack_sale, "POST")
            assert status == 201 and original_pack_sale["lines"][0]["factor_milli"] == 24000
            pack_change = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Douzaine confirmée",
                           "name": "Douzaine", "base_quantity_milli": 12000, "sale_price_fcfa": 2600}
            status, edited_pack = request(pack_detail, body=pack_change, method="PATCH")
            assert status == 200 and edited_pack["version"] == 2 and edited_pack["product_version"] == 4
            assert edited_pack["barcode"] == "CARTON-24" and edited_pack["barcode_version"] == 1
            assert request(pack_detail, body=pack_change, method="PATCH") == (200, edited_pack)
            assert request("sales/", seller_token, pack_sale, "POST") == (200, original_pack_sale)
            assert request("sales/" + pack_sale["id"] + "/", seller_token)[1]["lines"][0]["unit_price_fcfa"] == 4800
            next_sale = {**pack_sale, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                         "lines": [{**pack_sale["lines"][0], "id": str(uuid.uuid4())}]}
            assert request("sales/", seller_token, next_sale, "POST")[0] == 409
            next_sale["lines"][0]["product_version"] = 4
            next_sale["payments"] = [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2600}]
            status, current_pack_sale = request("sales/", seller_token, next_sale, "POST")
            assert status == 201 and current_pack_sale["lines"][0]["factor_milli"] == 12000
            assert request("barcode-lookup/?code=CARTON-24", seller_token)[1]["base_quantity_milli"] == 12000
            assert request(pack_detail + "history/")[1]["count"] == 2
            assert request(pack_detail + "history/", seller_token)[0] == 403
            status, archived_pack = request(pack_detail, body={"operation_id": str(uuid.uuid4()), "base_version": 2,
                "reason": "Format retiré du catalogue", "is_active": False}, method="PATCH")
            assert status == 200 and archived_pack["version"] == 3
            assert request("barcode-lookup/?code=CARTON-24", seller_token)[0] == 404
            current_product = request(detail)[1]
            dated_sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": "2026-01-01T09:00:00Z", "reason": "Vente oubliée retrouvée",
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"],
                           "product_version": current_product["version"], "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 250}]}
            status, dated_receipt = request("sales/", body=dated_sale, method="POST")
            assert status == 201 and dated_receipt["occurred_at"] == "2026-01-01T09:00:00Z"
            assert dated_receipt["created_at"] > dated_receipt["occurred_at"]
            assert request("sales/", body=dated_sale, method="POST") == (200, dated_receipt)
            assert request("sales/", seller_token, dated_sale, "POST")[0] == 403
            journal_url = "sales/" + dated_sale["id"] + "/history/"
            assert request(journal_url)[1]["results"][0]["reason"] == dated_sale["reason"]
            assert request(journal_url, seller_token)[0] == 403
            cash_entries_before = CashMovement.objects.count()
            stock_before_returns = request(detail)[1]["stock_available_milli"]
            return_body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Retour physique à contrôler",
                "lines": [{"id": str(uuid.uuid4()), "sale_line_id": dated_receipt["lines"][0]["id"], "quantity_milli": 1000}]}
            returns_path = "sales/" + dated_sale["id"] + "/returns/"
            status, returned = request(returns_path, body=return_body, method="POST")
            assert status == 201 and returned["amount_fcfa"] == 250 and returned["credit_fcfa"] == 250
            assert request(returns_path, body=return_body, method="POST") == (200, returned)
            assert request(returns_path, seller_token, return_body, "POST")[0] == 403
            receipt_path = returns_path + returned["id"] + "/lines/" + returned["lines"][0]["id"] + "/receipts/"
            physical = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": 1000,
                "condition": "unsellable", "occurred_at": timezone.now().isoformat(), "reason": "Emballage endommagé"}
            status, received = request(receipt_path, body=physical, method="POST")
            assert status == 201 and received["movement_id"] is None
            assert request(receipt_path, body=physical, method="POST") == (200, received)
            assert request(detail)[1]["stock_available_milli"] == stock_before_returns
            sellable_return = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Autre article reçu vendable",
                "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sold["lines"][0]["id"], "quantity_milli": 1000}]}
            other_returns_path = "sales/" + sold["id"] + "/returns/"
            status, sellable_claim = request(other_returns_path, body=sellable_return, method="POST")
            assert status == 201 and sellable_claim["amount_fcfa"] == 250
            sellable_receipts = other_returns_path + sellable_claim["id"] + "/lines/" + sellable_claim["lines"][0]["id"] + "/receipts/"
            physical_sellable = {**physical, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "condition": "sellable", "occurred_at": timezone.now().isoformat(), "reason": "État vendable confirmé"}
            status, restocked = request(sellable_receipts, body=physical_sellable, method="POST")
            assert status == 201 and restocked["movement_id"] is not None
            assert request(sellable_receipts, body=physical_sellable, method="POST") == (200, restocked)
            assert request(detail)[1]["stock_available_milli"] == stock_before_returns + 1000
            assert CashMovement.objects.count() == cash_entries_before
            anonymous_refund_path = returns_path + returned["id"] + "/refunds/"
            refund_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Espèces effectivement rendues", "method": "cash", "amount_fcfa": 100}
            status, anonymous_refund = request(anonymous_refund_path, body=refund_data, method="POST")
            assert status == 201 and anonymous_refund["after_credit_fcfa"] == 150
            assert request(anonymous_refund_path, body=refund_data, method="POST") == (200, anonymous_refund)
            assert request(anonymous_refund_path, seller_token, refund_data, "POST")[0] == 403
            assert CashMovement.objects.count() == cash_entries_before + 1
            assert request(returns_path + returned["id"] + "/")[1]["remaining_credit_fcfa"] == 150
            customer_debt = CustomerDebt.objects.get()
            identified_sale_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "customer_id": str(customer_debt.customer_id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"], "product_version": current_product["version"], "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 250}]}
            status, identified_sale = request("sales/", body=identified_sale_data, method="POST")
            assert status == 201
            identified_returns = "sales/" + identified_sale["id"] + "/returns/"
            status, identified_return = request(identified_returns, body={"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Retour client identifié",
                "lines": [{"id": str(uuid.uuid4()), "sale_line_id": identified_sale["lines"][0]["id"], "quantity_milli": 1000}]}, method="POST")
            assert status == 201 and identified_return["credit_fcfa"] == 250
            identified_credit_base = identified_returns + identified_return["id"] + "/"
            allocation_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Affectation à la dette confirmée", "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(customer_debt.id), "amount_fcfa": 100}]}
            before_allocation_cash = CashMovement.objects.count()
            status, allocated = request(identified_credit_base + "allocations/", body=allocation_data, method="POST")
            assert status == 201 and allocated["after_credit_fcfa"] == 150
            assert request(identified_credit_base + "allocations/", body=allocation_data, method="POST") == (200, allocated)
            assert CashMovement.objects.count() == before_allocation_cash
            customer_debt.refresh_from_db()
            assert customer_debt.remaining_fcfa == 2900
            remaining_refund = {**refund_data, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "amount_fcfa": 150}
            status, final_refund = request(identified_credit_base + "refunds/", body=remaining_refund, method="POST")
            assert status == 201 and final_refund["after_credit_fcfa"] == 0
            client_prefix = "clients/" + str(customer_debt.customer_id) + "/"
            status, customer_credits = request(client_prefix + "return-credits/")
            assert status == 200 and customer_credits["results"][0]["remaining_fcfa"] == 0
            statement = request(client_prefix + "statement/")[1]
            assert statement["closing_debt_fcfa"] == 2900 and statement["closing_unallocated_fcfa"] == 0
            assert statement["closing_balance_fcfa"] == 2900
            cash_before_decisions = CashMovement.objects.count()
            available_before_decisions = request(detail)[1]["stock_available_milli"]
            decisions_path = receipt_path + received["id"] + "/decisions/"
            restock_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "kind": "restock", "quantity_milli": 1000, "is_sellable_confirmed": True, "reason": "État vendable après contrôle"}
            status, decided = request(decisions_path, body=restock_data, method="POST")
            assert status == 201 and decided["after_quantity_milli"] == 0 and decided["movement_id"] is not None
            assert request(decisions_path, body=restock_data, method="POST") == (200, decided)
            assert request(decisions_path, seller_token, restock_data, "POST")[0] == 403
            assert request(detail)[1]["stock_available_milli"] == available_before_decisions + 1000
            assert request("unsellable-stock/")[1]["count"] == 0
            identified_receipts = identified_credit_base + "lines/" + identified_return["lines"][0]["id"] + "/receipts/"
            status, supplier_goods = request(identified_receipts, body={**physical, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat()}, method="POST")
            assert status == 201
            supplier_decisions = identified_receipts + supplier_goods["id"] + "/decisions/"
            supplier_departure = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "kind": "supplier_return", "quantity_milli": 1000, "supplier_id": supplier_id, "reason": "Départ physique non vendable confirmé"}
            status, departed_unsellable = request(supplier_decisions, body=supplier_departure, method="POST")
            assert status == 201 and departed_unsellable["movement_id"] is None and departed_unsellable["after_quantity_milli"] == 0
            assert request(supplier_decisions, body=supplier_departure, method="POST") == (200, departed_unsellable)
            assert request(supplier_prefix + "statement/")[1]["closing_balance_fcfa"] == -26000
            extra_return = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Dernière pièce retournée", "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sold["lines"][0]["id"], "quantity_milli": 1000}]}
            status, scrap_return = request(other_returns_path, body=extra_return, method="POST")
            assert status == 201
            scrap_receipts = other_returns_path + scrap_return["id"] + "/lines/" + scrap_return["lines"][0]["id"] + "/receipts/"
            status, scrap_goods = request(scrap_receipts, body={**physical, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat()}, method="POST")
            assert status == 201
            scrap_path = scrap_receipts + scrap_goods["id"] + "/decisions/"
            scrap_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "kind": "scrap", "quantity_milli": 1000, "reason": "Rebut physique confirmé"}
            status, scrapped = request(scrap_path, body=scrap_data, method="POST")
            assert status == 201 and scrapped["movement_id"] is None and scrapped["after_quantity_milli"] == 0
            assert request(scrap_path, body=scrap_data, method="POST") == (200, scrapped)
            assert request("unsellable-stock/")[1]["count"] == 0
            assert request("unsellable-stock/?include_closed=true")[1]["count"] == 3
            assert request(detail)[1]["stock_available_milli"] == available_before_decisions + 1000
            assert CashMovement.objects.count() == cash_before_decisions
            cancellation_sale_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"], "product_version": current_product["version"], "quantity_milli": 2000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}]}
            status, cancellation_sale = request("sales/", body=cancellation_sale_data, method="POST")
            assert status == 201 and cancellation_sale["state"] == "active"
            cancellation_returns_path = "sales/" + cancellation_sale["id"] + "/returns/"
            status, prior_cancel_return = request(cancellation_returns_path, body={"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Une pièce retournée avant annulation",
                "lines": [{"id": str(uuid.uuid4()), "sale_line_id": cancellation_sale["lines"][0]["id"], "quantity_milli": 1000}]}, method="POST")
            assert status == 201 and prior_cancel_return["amount_fcfa"] == 250
            cancellation_path = "sales/" + cancellation_sale["id"] + "/cancellation/"
            cancellation_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(), "reason": "Vente saisie par erreur"}
            cancel_cash_before = CashMovement.objects.count()
            cancel_stock_before = request(detail)[1]["stock_available_milli"]
            status, cancellation = request(cancellation_path, body=cancellation_data, method="POST")
            assert status == 201 and cancellation["sale_return_id"] is not None
            assert request(cancellation_path, body=cancellation_data, method="POST") == (200, cancellation)
            assert request(cancellation_path, seller_token, cancellation_data, "POST")[0] == 403
            assert request(cancellation_path, seller_token)[0] == 403
            status, cancelled_sale = request("sales/" + cancellation_sale["id"] + "/", seller_token)
            assert status == 200 and cancelled_sale["state"] == "cancelled" and cancelled_sale["net_total_fcfa"] == 500
            assert cancelled_sale["lines"] == cancellation_sale["lines"] and "reason" not in cancelled_sale["cancellation"]
            assert request("sales/", body=cancellation_sale_data, method="POST") == (200, cancellation_sale)
            compensation_path = cancellation_returns_path + cancellation["sale_return_id"] + "/"
            compensation = request(compensation_path)[1]
            assert compensation["amount_fcfa"] == 250 and compensation["lines"][0]["quantity_milli"] == 1000
            assert CashMovement.objects.count() == cancel_cash_before
            assert request(detail)[1]["stock_available_milli"] == cancel_stock_before
            compensation_refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Argent effectivement rendu après annulation", "method": "cash", "amount_fcfa": 250}
            assert request(compensation_path + "refunds/", body=compensation_refund, method="POST")[0] == 201
            assert CashMovement.objects.count() == cancel_cash_before + 1
            assert request(compensation_path)[1]["remaining_credit_fcfa"] == 0
            compensation_receipts = compensation_path + "lines/" + compensation["lines"][0]["id"] + "/receipts/"
            compensation_physical = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Pièce effectivement reçue vendable", "condition": "sellable", "quantity_milli": 1000}
            assert request(compensation_receipts, body=compensation_physical, method="POST")[0] == 201
            assert request(detail)[1]["stock_available_milli"] == cancel_stock_before + 1000
            assert "sale_cancelled" in [row["action"] for row in request("sales/" + cancellation_sale["id"] + "/history/")[1]["results"]]
            from common.models import Activity
            from stock.models import StockReservation, StockAlertEvent
            from stock.services.stock_alert_services import observe_stock
            status, rejected_product = request("products/", body={"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "name": "Rejetstock", "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 100,
                "stock_threshold_milli": 8000, "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None,
                    "occurred_at": "2026-01-01T10:00:00Z", "reason": "Stock réellement présent"}}, method="POST")
            assert status == 201
            rejected_id = uuid.UUID(rejected_product["id"])
            physical_before = StockMovement.objects.count()
            cash_before = CashMovement.objects.count()
            # Pending-operation ingestion is not implemented; seed its known source explicitly.
            pending_event = Activity.objects.create(shop=shop, actor=seller, action="pending_sale_fixture",
                target_id=rejected_id, reason="Opération connue en attente")
            claim = StockReservation.objects.create(product_id=rejected_id, operation=pending_event, quantity_milli=2000)
            observe_stock([rejected_id], pending_event)
            rejected_detail = f"products/{rejected_id}/"
            row = request(rejected_detail)[1]
            assert row["stock_pending_reserved_milli"] == 2000 and row["stock_available_milli"] == 8000
            claim.state = "rejected"
            claim.save()
            rejected_event = Activity.objects.create(shop=shop, actor=owner, action="pending_sale_rejected_fixture",
                target_id=rejected_id, reason="Refus sans constat de restitution")
            observe_stock([rejected_id], rejected_event)
            row = request(rejected_detail, seller_token)[1]
            assert row["stock_rejected_reserved_milli"] == 2000 and row["stock_pending_reserved_milli"] == 0
            assert row["stock_available_milli"] == 8000 and row["is_stock_low"]
            assert "purchase_price_fcfa" not in row
            assert request("stock-alerts/?search=Rejetstock")[1]["count"] == 1
            another = Activity.objects.create(shop=shop, actor=seller, action="pending_sale_fixture",
                target_id=rejected_id, reason="Autre opération connue en attente")
            StockReservation.objects.create(product_id=rejected_id, operation=another, quantity_milli=2000)
            observe_stock([rejected_id], another)
            row = request(rejected_detail)[1]
            assert row["stock_reserved_milli"] == 4000 and row["stock_available_milli"] == 6000
            assert row["stock_rejected_reserved_milli"] == row["stock_pending_reserved_milli"] == 2000
            assert StockAlertEvent.objects.filter(product_id=rejected_id).count() == 1
            assert StockMovement.objects.count() == physical_before and CashMovement.objects.count() == cash_before
            print(json.dumps({"http_rejected_stock_reservation": "passed", "pending_ingestion_seeded": True,
                "reserved_milli": 4000, "rejected_reserved_milli": 2000, "available_milli": 6000,
                "one_alert_episode": True, "no_stock_or_cash_effect": True}))
            print(json.dumps({"http": "passed", "cancellation_remainder": 250, "cancellation_automatic_cash": 0, "cancellation_automatic_stock": 0, "cancellation_actual_refund": 250, "restocked_unsellable": 1000, "unsellable_supplier_departure": 1000, "unsellable_scrap": 1000, "unsellable_remaining_lots": 0, "anonymous_return_refund": 100, "return_credit_allocation": 100, "return_credit_final_refund": 150, "customer_statement_balance": 2900, "customer_return_value": 250, "unsellable_stock_added": 0, "sellable_stock_added": 1000, "return_automatic_cash": 0, "oversized_json": 413, "backdated_sale": 201, "backdated_retry": 200, "backdated_seller": 403, "csv_customers_created": customers_receipt["created_count"], "csv_supplier_opening_debt": 7000, "supplier_debt_after_allocation": 4000, "opening_advance_remaining": 27000, "opening_credit_remaining": 3000, "opening_refund_cash": 2000, "supplier_statement_balance": -26000, "csv_created": import_receipt["created_count"], "csv_invalid_confirmation": 409, "csv_oversized_upload": 400, "language_version": language_receipt["version"], "english_conflict": language_conflict["code"], "threshold": 9000, "stock_after_sale": 8000,
                              "alert_count_after_sale": 1, "alert_count_after_clear": 0,
                              "seller_write_and_center": 403, "sale_retry": 200,
                              "notification_candidate": candidate_id, "muted_feed_count": 0, "muted_center_count": 1,
                              "pack_scan_factor": 24000, "barcode_duplicate": 409, "barcode_clear_version": 2, "name_search": 1,
                              "pack_edit_version": edited_pack["version"], "pack_new_factor": 12000, "pack_old_billed_factor": 24000, "pack_archived_version": archived_pack["version"], "photo_bytes": photo["size_bytes"], "photo_width": photo["width"], "photo_clear_version": 2}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
