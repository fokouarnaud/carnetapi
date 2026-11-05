"""Disposable loopback HTTP verification; no production account or external API."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import uuid
from datetime import timedelta


def main():
    with TemporaryDirectory(prefix="catalogue-smoke-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "smoke.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from wsgiref.simple_server import make_server
        from cash.models import CashMovement
        from identity.models import DeviceSession, Installation, Membership, Shop, User
        from stock.models import StockMovement

        with open(os.devnull, "w") as sink:
            call_command("migrate", verbosity=0, stdout=sink)
        owner = User.objects.create_user(username="smoke-owner")
        seller = User.objects.create_user(username="smoke-seller")
        shop = Shop.objects.create(name="Boutique de vérification", owner=owner)
        Membership.objects.create(shop=shop, user=seller)

        def token(user):
            device = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
            access = AccessToken.for_user(user)
            access["session_id"] = str(session.id)
            return str(access)

        owner_token, seller_token = token(owner), token(seller)
        server = make_server("127.0.0.1", 0, get_wsgi_application())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/products/"

            def request(bearer, payload=None, endpoint=None, method=None):
                body = json.dumps(payload).encode() if payload is not None else None
                req = Request(endpoint or url, data=body, method=method,
                              headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
                with urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)

            category_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/categories/"
            category_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Alimentation"}
            category_status, category = request(owner_token, category_payload, category_url)
            category_retry_status, category_retry = request(owner_token, category_payload, category_url)
            assert category_status == 201 and category_retry_status == 200 and category == category_retry
            rename_payload = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Épicerie", "reason": "Classement corrigé"}
            renamed_status, renamed = request(owner_token, rename_payload, category_url + category["id"] + "/", method="PATCH")
            renamed_retry_status, renamed_retry = request(owner_token, rename_payload, category_url + category["id"] + "/", method="PATCH")
            assert renamed_status == 200 and renamed_retry_status == 200 and renamed == renamed_retry
            assert renamed["version"] == 2 and renamed["name"] == "Épicerie"
            payload = {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Farine",
                "unit_kind": "weight", "unit_label": "kg", "sale_price_fcfa": 1000,
                "purchase_price_fcfa": 600,
                "category_id": category["id"],
                "opening": {"quantity_milli": 1250, "unit_cost_fcfa": "600.123456",
                            "occurred_at": "2026-01-01T00:00:00Z", "reason": "Stock de démarrage"},
            }
            created_status, created = request(owner_token, payload)
            retry_status, retried = request(owner_token, payload)
            list_status, visible = request(seller_token)
            cost = StockMovement.objects.get(product_id=payload["id"]).unit_cost_fcfa
            assert created_status == 201 and retry_status == 200 and list_status == 200
            assert created == retried and visible["count"] == 1
            assert visible["results"][0]["stock_validated_milli"] == 1250
            assert visible["results"][0]["category_id"] == category["id"]
            assert "purchase_price_fcfa" not in visible["results"][0]
            assert str(cost) == "600.123456"
            pack_url = f"{url}{payload['id']}/packs/"
            pack_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Sachet",
                            "base_quantity_milli": 1250, "sale_price_fcfa": 1100}
            pack_status, pack = request(owner_token, pack_payload, pack_url)
            pack_retry_status, pack_retry = request(owner_token, pack_payload, pack_url)
            pack_list_status, pack_list = request(seller_token, endpoint=pack_url)
            _, stock_after = request(seller_token)
            assert pack_status == 201 and pack_retry_status == 200 and pack_list_status == 200
            assert pack == pack_retry and pack_list["count"] == 1
            assert pack_list["results"][0]["sale_price_fcfa"] == 1100
            assert stock_after["results"][0]["stock_validated_milli"] == 1250
            clients_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/clients/"
            client_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Client exemple"}
            client_status, client_created = request(seller_token, client_payload, clients_url)
            client_url = f"{clients_url}{client_payload['id']}/"
            corrected_status, corrected = request(owner_token, {
                "operation_id": str(uuid.uuid4()), "base_version": 1,
                "name": "Client corrigé", "reason": "Erreur de saisie",
            }, client_url, method="PATCH")
            history_status, history = request(owner_token, endpoint=client_url + "history/")
            assert client_status == 201 and corrected_status == 200 and history_status == 200
            assert corrected["id"] == client_created["id"] and corrected["version"] == 2
            assert history["count"] == 2
            assert history["results"][1]["details"]["before"]["name"] == "Client exemple"
            assert history["results"][1]["details"]["after"]["name"] == "Client corrigé"
            member = Membership.objects.get(shop=shop, user=seller)
            team_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/team/{member.id}/"
            team_payload = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                            "reason": "Autorisation pilote synthétique", "can_grant_credit": True}
            team_status, changed = request(owner_token, team_payload, team_url, method="PATCH")
            team_retry_status, changed_retry = request(owner_token, team_payload, team_url, method="PATCH")
            team_history_status, team_history = request(owner_token, endpoint=team_url + "history/")
            lease_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/verify-access/"
            lease_status, lease = request(seller_token, {}, lease_url)
            assert team_status == 200 and team_retry_status == 200 and changed == changed_retry
            assert changed["version"] == 2 and changed["permissions"]["can_grant_credit"] is True
            assert team_history_status == 200 and team_history["count"] == 1
            assert lease_status == 200 and lease["permissions"]["can_grant_credit"] is True
            assert lease["permissions"]["can_read_cash_balance"] is False
            product_url = url + payload["id"] + "/"
            product_update = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                              "reason": "Nouveau tarif synthétique", "sale_price_fcfa": 1200}
            product_status, updated_product = request(owner_token, product_update, product_url, method="PATCH")
            product_retry_status, updated_retry = request(owner_token, product_update, product_url, method="PATCH")
            _, original_after_update = request(owner_token, payload)
            _, catalogue_history = request(owner_token, endpoint=product_url + "history/")
            assert product_status == product_retry_status == 200 and updated_product == updated_retry
            assert updated_product["version"] == 2 and updated_product["sale_price_fcfa"] == 1200
            assert updated_product["stock_validated_milli"] == 1250
            assert original_after_update == created and original_after_update["sale_price_fcfa"] == 1000
            assert StockMovement.objects.get(product_id=payload["id"]).unit_cost_micros == 600123456
            assert catalogue_history["count"] == 2
            policy_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/discount-policy/"
            policy_status, policy = request(owner_token, {
                "operation_id": str(uuid.uuid4()), "base_version": 1,
                "seller_discount_cap_basis_points": 500, "reason": "Remise de cinq pour cent synthétique",
            }, policy_url)
            _, configured_lease = request(seller_token, {}, lease_url)
            assert policy_status == 200 and policy["version"] == 2
            assert configured_lease["seller_discount_cap_basis_points"] == 500
            assert configured_lease["discount_policy_version"] == 2
            sales_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/sales/"
            sale_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                            "global_discount_fcfa": 30,
                            "lines": [{"id": str(uuid.uuid4()), "product_id": payload["id"], "product_version": 2,
                                       "quantity_milli": 500}],
                            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 200},
                                         {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 370}]}
            sale_status, sale_created = request(seller_token, sale_payload, sales_url)
            sale_retry_status, sale_retried = request(seller_token, sale_payload, sales_url)
            _, sale_read = request(seller_token, endpoint=sales_url + sale_payload["id"] + "/")
            _, after_sale = request(seller_token)
            assert sale_status == 201 and sale_retry_status == 200 and sale_created == sale_retried == sale_read
            assert sale_created["reference_total_fcfa"] == 600
            assert sale_created["net_total_fcfa"] == 570
            assert after_sale["results"][0]["stock_validated_milli"] == 750
            assert CashMovement.objects.get(method="cash").amount_fcfa == 200
            assert CashMovement.objects.get(method="orange").amount_fcfa == 370
            credit_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit",
                              "customer_id": client_payload["id"], "due_date": "2026-10-31",
                              "lines": [{"id": str(uuid.uuid4()), "product_id": payload["id"], "product_version": 2, "quantity_milli": 500}],
                              "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}]}
            credit_status, credit = request(seller_token, credit_payload, sales_url)
            assert credit_status == 201 and credit["net_total_fcfa"] == 600
            assert credit["debt"]["amount_fcfa"] == 500
            assert credit["debt"]["due_at"] == "2026-10-30T23:00:00Z"
            _, allocation_preview = request(seller_token, endpoint=client_url + "payment-preview/?amount_fcfa=200")
            assert allocation_preview["known_debt_fcfa"] == 500
            assert allocation_preview["allocations"] == [{"debt_id": credit["debt"]["id"], "amount_fcfa": 200}]
            collection_url = client_url + "payments/"
            collection_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                                  "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 200}],
                                  "allocations": [{"id": str(uuid.uuid4()), "debt_id": credit["debt"]["id"], "amount_fcfa": 200}]}
            collection_status, collection = request(seller_token, collection_payload, collection_url)
            collection_retry_status, collection_retry = request(seller_token, collection_payload, collection_url)
            _, debts_after = request(seller_token, endpoint=client_url + "debts/")
            assert collection_status == 201 and collection_retry_status == 200 and collection == collection_retry
            assert collection["allocations"][0]["applied_amount_fcfa"] == 200
            assert debts_after["results"][0]["remaining_fcfa"] == 300
            assert CashMovement.objects.get(customer_payment_part__payment_id=collection_payload["id"]).amount_fcfa == 200
            extra_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                             "parts": [{"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 500}], "allocations": []}
            _, extra = request(seller_token, extra_payload, collection_url)
            extra_url = collection_url + extra_payload["id"] + "/"
            allocate_status, allocated = request(owner_token, {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation décidée",
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": credit["debt"]["id"], "amount_fcfa": 200}],
            }, extra_url + "allocate/")
            refund_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Argent rendu déclaré",
                              "method": "orange", "amount_fcfa": 300}
            refund_status, refunded = request(owner_token, refund_payload, extra_url + "refund/")
            refund_retry_status, refund_retry = request(owner_token, refund_payload, extra_url + "refund/")
            _, debt_after_regularization = request(seller_token, endpoint=client_url + "debts/")
            _, regularization_history = request(owner_token, endpoint=extra_url + "regularizations/")
            assert extra["unallocated_fcfa"] == 500 and allocate_status == 201
            assert allocated["after_unallocated_fcfa"] == 300
            assert refund_status == 201 and refund_retry_status == 200 and refunded == refund_retry
            assert refunded["after_unallocated_fcfa"] == 0
            assert debt_after_regularization["results"][0]["remaining_fcfa"] == 100
            assert regularization_history["count"] == 2
            assert CashMovement.objects.get(customer_refund__payment_id=extra_payload["id"]).amount_fcfa == -300
            cash_count_before_opening = CashMovement.objects.count()
            opening_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 1000,
                               "occurred_at": "2026-01-01T00:00:00Z", "reason": "Dette antérieure vérifiée"}
            opening_status, opening_debt = request(owner_token, opening_payload, client_url + "debts/")
            opening_retry_status, opening_retry = request(owner_token, opening_payload, client_url + "debts/")
            deadline_status, deadline = request(owner_token, {
                "operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Échéance convenue", "due_date": "2026-11-30",
            }, client_url + "debts/" + opening_payload["id"] + "/due-date/")
            _, opening_history = request(owner_token, endpoint=client_url + "debts/" + opening_payload["id"] + "/history/")
            assert opening_status == 201 and opening_retry_status == 200 and opening_debt == opening_retry
            assert opening_debt["sale_id"] is None and opening_debt["remaining_fcfa"] == 1000
            assert deadline_status == 200 and deadline["due_version"] == 2
            assert deadline["due_at"] == "2026-11-29T23:00:00Z"
            assert CashMovement.objects.count() == cash_count_before_opening and opening_history["count"] == 2
            statement_status, statement = request(owner_token, endpoint=client_url + "statement/?page_size=3")
            _, statement_next = request(owner_token, endpoint=statement["next"])
            assert statement_status == 200 and statement["count"] == 8
            assert statement["closing_balance_fcfa"] == 1100
            assert statement["closing_debt_fcfa"] == 1100 and statement["closing_unallocated_fcfa"] == 0
            assert statement_next["as_of"] == statement["as_of"]
            assert statement_next["closing_balance_fcfa"] == 1100
            assert statement["provisional"] is True
            change_client_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Client monnaie"}
            _, change_client = request(owner_token, change_client_payload, clients_url)
            change_client_url = clients_url + change_client["id"] + "/"
            change_product_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Produit monnaie",
                                      "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 7500}
            _, change_product = request(owner_token, change_product_payload)
            change_sale_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "customer_id": change_client["id"],
                                   "lines": [{"id": str(uuid.uuid4()), "product_id": change_product["id"], "product_version": 1, "quantity_milli": 1000}],
                                   "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10000,
                                                 "received_fcfa": 10000, "returned_fcfa": 0}]}
            change_status, change_sale = request(owner_token, change_sale_payload, sales_url)
            _, change_credits = request(owner_token, endpoint=change_client_url + "credits/")
            assert change_status == 201 and change_sale["net_total_fcfa"] == 7500
            assert change_sale["customer_credit"]["remaining_fcfa"] == 2500 and change_credits["count"] == 1
            assert CashMovement.objects.get(sale_payment__sale_id=change_sale["id"]).amount_fcfa == 10000
            change_refund_url = change_client_url + "credits/" + change_sale["customer_credit"]["id"] + "/refund/"
            change_refund_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "method": "cash",
                                     "amount_fcfa": 2500, "reason": "Monnaie réellement rendue"}
            change_refund_status, change_refund = request(owner_token, change_refund_payload, change_refund_url)
            change_retry_status, change_retry = request(owner_token, change_refund_payload, change_refund_url)
            _, change_statement = request(owner_token, endpoint=change_client_url + "statement/")
            assert change_refund_status == 201 and change_retry_status == 200 and change_refund == change_retry
            assert change_refund["after_unallocated_fcfa"] == 0
            assert change_statement["closing_balance_fcfa"] == 0 and change_statement["closing_unallocated_fcfa"] == 0
            assert CashMovement.objects.filter(customer_refund__credit_id=change_sale["customer_credit"]["id"]).count() == 1
            cash_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/cash/"
            cash_opening_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "opening",
                                    "method": "cash", "amount_fcfa": 50000, "occurred_at": "2026-01-01T00:00:00Z", "reason": "Fonds physique"}
            cash_opening_status, cash_opening = request(owner_token, cash_opening_payload, cash_url)
            cash_retry_status, cash_retry = request(owner_token, cash_opening_payload, cash_url)
            cash_expense_status, cash_expense = request(owner_token, {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "expense", "method": "cash",
                "amount_fcfa": 3000, "occurred_at": "2026-01-02T00:00:00Z", "reason": "Dépense déclarée"}, cash_url)
            _, cash_ledger = request(owner_token, endpoint=cash_url + "?page_size=1")
            _, personal_cash = request(seller_token, endpoint=cash_url)
            assert cash_opening_status == 201 and cash_retry_status == 200 and cash_opening == cash_retry
            assert cash_expense_status == 201 and cash_expense["amount_fcfa"] == -3000
            assert cash_ledger["scope"] == "global"
            assert cash_ledger["totals_by_method_fcfa"] == {
                "cash": 50000 - 3000 + 200 + 100 + 200 + 10000 - 2500, "orange": 370 + 500 - 300,
            }, cash_ledger["totals_by_method_fcfa"]
            assert personal_cash["scope"] == "personal"
            assert personal_cash["totals_by_method_fcfa"] == {"cash": 200 + 100 + 200, "orange": 370 + 500}, personal_cash["totals_by_method_fcfa"]
            counts_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/cash-counts/"
            count_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "method": "cash",
                             "physical_fcfa": 45000, "counted_at": "2026-01-02T00:00:00Z", "reason": "Comptage réel"}
            before_count_cash = CashMovement.objects.count()
            count_status, counted = request(owner_token, count_payload, counts_url)
            count_retry_status, count_retry = request(owner_token, count_payload, counts_url)
            assert count_status == 201 and count_retry_status == 200 and counted == count_retry
            assert counted["expected_fcfa"] == 47000 and counted["difference_fcfa"] == -2000
            assert CashMovement.objects.count() == before_count_cash
            request(owner_token, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "expense", "method": "cash",
                                  "amount_fcfa": 1000, "occurred_at": "2026-01-01T12:00:00Z", "reason": "Opération oubliée réelle"}, cash_url)
            _, revised_count = request(owner_token, endpoint=counts_url + count_payload["id"] + "/")
            assert revised_count["physical_fcfa"] == 45000 and revised_count["expected_at_creation_fcfa"] == 47000
            assert revised_count["expected_fcfa"] == 46000 and revised_count["difference_fcfa"] == -1000
            assert revised_count["expected_revised"] is True
            assert CashMovement.objects.count() == before_count_cash + 1
            supplier_url = f"http://127.0.0.1:{server.server_port}/api/v1/shops/{shop.id}/suppliers/"
            supplier_status, supplier = request(owner_token, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Fournisseur pilote"}, supplier_url)
            assert supplier_status == 201
            invoice_url = supplier_url + supplier["id"] + "/invoices/"
            invoice_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z",
                               "reason": "Facture reçue", "lines": [{"id": str(uuid.uuid4()), "product_id": payload["id"],
                                                                      "pack_id": pack_payload["id"], "quantity_milli": 10000, "unit_price_fcfa": 1000}]}
            invoice_status, invoice = request(owner_token, invoice_payload, invoice_url)
            invoice_retry_status, invoice_retry = request(owner_token, invoice_payload, invoice_url)
            assert invoice_status == 201 and invoice_retry_status == 200 and invoice == invoice_retry
            assert invoice["total_fcfa"] == 10000 and invoice["debt"]["remaining_fcfa"] == 10000
            receipt_url = invoice_url + invoice["id"] + "/receipts/"
            receipt_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
                               "reason": "Six sachets reçus", "lines": [{"line_id": invoice_payload["lines"][0]["id"], "quantity_milli": 6000}]}
            cash_before_receipt = CashMovement.objects.count()
            receipt_status, receipt = request(owner_token, receipt_payload, receipt_url)
            receipt_retry_status, receipt_retry = request(owner_token, receipt_payload, receipt_url)
            _, revised_invoice = request(owner_token, endpoint=invoice_url + invoice["id"] + "/")
            assert receipt_status == 201 and receipt_retry_status == 200 and receipt == receipt_retry
            assert receipt["lines"][0]["base_quantity_milli"] == 7500
            assert revised_invoice["lines"][0]["awaiting_quantity_milli"] == 4000
            assert revised_invoice["debt"]["remaining_fcfa"] == 10000
            assert CashMovement.objects.count() == cash_before_receipt
            assert StockMovement.objects.get(kind="purchase_receipt").quantity_milli == 7500
            payment_url = supplier_url + supplier["id"] + "/payments/"
            supplier_payment_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-03T00:00:00Z",
                                        "reason": "Avance versée au fournisseur", "parts": [
                                            {"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000},
                                            {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 2000}]}
            supplier_payment_status, supplier_payment = request(owner_token, supplier_payment_payload, payment_url)
            supplier_retry_status, supplier_retry = request(owner_token, supplier_payment_payload, payment_url)
            assert supplier_payment_status == 201 and supplier_retry_status == 200 and supplier_payment == supplier_retry
            allocation_url = payment_url + supplier_payment["id"] + "/allocate/"
            supplier_allocation = {"operation_id": str(uuid.uuid4()), "reason": "Avance affectée à la facture",
                                   "allocations": [{"debt_id": invoice["debt"]["id"], "amount_fcfa": 3000}]}
            allocation_status, allocation_result = request(owner_token, supplier_allocation, allocation_url)
            allocation_retry_status, allocation_retry = request(owner_token, supplier_allocation, allocation_url)
            assert allocation_status == 201 and allocation_retry_status == 200 and allocation_result == allocation_retry
            _, paid_invoice = request(owner_token, endpoint=invoice_url + invoice["id"] + "/")
            assert paid_invoice["debt"]["remaining_fcfa"] == 7000
            assert allocation_result["after_unallocated_fcfa"] == 0
            assert CashMovement.objects.count() == cash_before_receipt + 2
            assert sorted(CashMovement.objects.filter(supplier_payment_part__isnull=False).values_list("amount_fcfa", flat=True)) == [-2000, -1000]
            departure_url = invoice_url + invoice["id"] + "/returns/"
            departure_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-04T00:00:00Z",
                                 "reason": "Deux sachets retournés", "lines": [{"line_id": invoice_payload["lines"][0]["id"], "quantity_milli": 2000}]}
            departure_status, departure = request(owner_token, departure_payload, departure_url)
            departure_retry_status, departure_retry = request(owner_token, departure_payload, departure_url)
            assert departure_status == 201 and departure_retry_status == 200 and departure == departure_retry
            assert departure["lines"][0]["base_quantity_milli"] == 2500
            assert StockMovement.objects.get(kind="supplier_return").quantity_milli == -2500
            _, invoice_after_departure = request(owner_token, endpoint=invoice_url + invoice["id"] + "/")
            assert invoice_after_departure["debt"]["remaining_fcfa"] == 7000
            assert CashMovement.objects.count() == cash_before_receipt + 2
            credit_url = supplier_url + supplier["id"] + "/credits/"
            credit_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "return_id": departure["id"],
                              "occurred_at": "2026-01-05T00:00:00Z", "amount_fcfa": 9000, "reason": "Avoir accepté avec écart motivé"}
            supplier_credit_status, supplier_credit = request(owner_token, credit_payload, credit_url)
            credit_retry_status, credit_retry = request(owner_token, credit_payload, credit_url)
            assert supplier_credit_status == 201 and credit_retry_status == 200 and supplier_credit == credit_retry
            assert supplier_credit["reference_fcfa"] == 2000 and supplier_credit["applied_to_origin_fcfa"] == 7000 and supplier_credit["remaining_fcfa"] == 2000
            refund_url = credit_url + supplier_credit["id"] + "/refund/"
            refund_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-06T00:00:00Z",
                              "reason": "Remboursement fournisseur reçu", "method": "orange", "amount_fcfa": 1500}
            supplier_refund_status, supplier_refund = request(owner_token, refund_payload, refund_url)
            supplier_refund_retry_status, supplier_refund_retry = request(owner_token, refund_payload, refund_url)
            assert supplier_refund_status == 201 and supplier_refund_retry_status == 200 and supplier_refund == supplier_refund_retry
            assert supplier_refund["after_remaining_fcfa"] == 500
            assert CashMovement.objects.get(supplier_refund__isnull=False).amount_fcfa == 1500
            _, supplier_history = request(owner_token, endpoint=credit_url + supplier_credit["id"] + "/history/")
            assert supplier_history["count"] == 2
            loss_url = url + payload["id"] + "/losses/"
            loss_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-07T00:00:00Z",
                            "reason": "Perte physique de farine", "quantity_milli": 125}
            money_before_loss = CashMovement.objects.count()
            stock_before_loss = sum(StockMovement.objects.filter(product_id=payload["id"]).values_list("quantity_milli", flat=True))
            loss_status, loss = request(owner_token, loss_payload, loss_url)
            loss_retry_status, loss_retry = request(owner_token, loss_payload, loss_url)
            assert loss_status == 201 and loss_retry_status == 200 and loss == loss_retry
            assert loss["stock_after_milli"] == stock_before_loss - 125
            assert CashMovement.objects.count() == money_before_loss
            assert StockMovement.objects.get(kind="loss").quantity_milli == -125
            stock_count_url = url + payload["id"] + "/stock-counts/"
            stock_count_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "counted_at": "2026-01-10T10:00:00Z",
                                   "physical_milli": 5000, "reason": "Farine comptée physiquement"}
            stock_count_status, stock_count = request(owner_token, stock_count_payload, stock_count_url)
            # Jan 10 excludes the two later sales: 1250 +7500 -2500 -125 =6125.
            assert stock_count_status == 201 and stock_count["expected_milli"] == 6125
            stock_confirm_url = stock_count_url + stock_count["id"] + "/confirm/"
            stock_confirm_payload = {"operation_id": str(uuid.uuid4()), "expected_milli": 6125, "applied_adjustment_milli": 0,
                                     "reason": "Écart d'inventaire confirmé"}
            stock_confirm_status, stock_confirmation = request(owner_token, stock_confirm_payload, stock_confirm_url)
            stock_confirm_retry_status, stock_confirm_retry = request(owner_token, stock_confirm_payload, stock_confirm_url)
            assert stock_confirm_status == 201 and stock_confirm_retry_status == 200 and stock_confirmation == stock_confirm_retry
            assert stock_confirmation["delta_milli"] == -1125
            late_loss_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-09T10:00:00Z",
                                 "quantity_milli": 100, "reason": "Perte antérieure oubliée"}
            request(owner_token, late_loss_payload, loss_url)
            _, stock_revised = request(owner_token, endpoint=stock_count_url + stock_count["id"] + "/")
            assert stock_revised["expected_milli"] == 6025 and stock_revised["physical_milli"] == 5000
            assert stock_revised["proposed_adjustment_milli"] == 100
            complement_status, complement = request(owner_token, {
                "operation_id": str(uuid.uuid4()), "expected_milli": 6025, "applied_adjustment_milli": -1125,
                "reason": "Rapprochement de la perte tardive"}, stock_confirm_url)
            assert complement_status == 201 and complement["delta_milli"] == 100
            _, stock_count_history = request(owner_token, endpoint=stock_count_url + stock_count["id"] + "/history/")
            assert stock_count_history["count"] == 3
            assert CashMovement.objects.count() == money_before_loss
            assert sum(StockMovement.objects.filter(product_id=payload["id"]).values_list("quantity_milli", flat=True)) == 4000
            print(json.dumps({"stock_count_http": {"physical_milli": 5000, "initial_expected_milli": 6125,
                                                   "first_correction_milli": -1125, "late_expected_milli": 6025,
                                                   "complement_milli": 100, "history_count": 3}}))
            print(json.dumps({"category_rename_and_loss_http": {"rename": renamed_status, "rename_retry": renamed_retry_status,
                                                             "category_version": 2, "loss": loss_status, "loss_retry": loss_retry_status,
                                                             "loss_quantity_milli": 125, "stock_after_milli": loss["stock_after_milli"], "cash_unchanged": True}}))
            print(json.dumps({"supplier_credit_http": {"credit": supplier_credit_status, "retry": credit_retry_status,
                                                       "reference_fcfa": 2000, "accepted_fcfa": 9000, "origin_debt_settled_fcfa": 7000,
                                                       "refund": supplier_refund_status, "refund_retry": supplier_refund_retry_status,
                                                       "refund_cash_fcfa": 1500, "credit_remaining_fcfa": 500, "history_count": 2}}))
            print(json.dumps({"category_and_return_http": {"category": category_status, "category_retry": category_retry_status,
                                                         "departure": departure_status, "departure_retry": departure_retry_status,
                                                         "base_departed_milli": 2500, "debt_unchanged_fcfa": 7000, "cash_unchanged": True}}))
            print(json.dumps({"supplier_payment_http": {"payment": supplier_payment_status, "retry": supplier_retry_status,
                                                       "allocation": allocation_status, "allocation_retry": allocation_retry_status,
                                                       "amount_paid_fcfa": 3000, "remaining_debt_fcfa": 7000, "allocation_adds_no_cash": True}}))
            print(json.dumps({"purchase_http": {"invoice": invoice_status, "invoice_retry": invoice_retry_status,
                                               "receipt": receipt_status, "receipt_retry": receipt_retry_status,
                                               "base_stock_added_milli": 7500, "awaiting_quantity_milli": 4000,
                                               "supplier_debt_fcfa": 10000, "cash_unchanged": True}}))
            print(json.dumps({"transport": "HTTP loopback", "creation": created_status,
                              "retry": retry_status, "seller_list": list_status, "products": visible["count"],
                              "stock_milli": 1250, "cost_exact": str(cost), "cost_hidden_for_seller": True,
                              "pack_creation": pack_status, "pack_retry": pack_retry_status,
                              "seller_pack_list": pack_list_status, "pack_price_fcfa": 1100, "stock_unchanged": True,
                              "seller_client_creation": client_status, "manager_correction": corrected_status,
                              "client_uuid_stable": True, "history_events": history["count"],
                              "team_change": team_status, "team_retry": team_retry_status,
                              "team_history_events": team_history["count"], "current_lease_permissions": True,
                              "product_update": product_status, "product_update_retry": product_retry_status,
                              "original_product_receipt_preserved": True, "catalogue_history_events": catalogue_history["count"],
                              "sale_creation": sale_status, "sale_retry": sale_retry_status, "sale_net_fcfa": 570,
                              "sale_stock_milli": 750, "cash_fcfa": 200, "orange_fcfa": 370,
                              "discount_policy_version": 2, "sale_discount_fcfa": 30,
                              "credit_sale_creation": credit_status, "credit_debt_fcfa": 500,
                              "collection_creation": collection_status, "collection_retry": collection_retry_status,
                              "remaining_debt_fcfa": 300, "debt_due_at_utc": credit["debt"]["due_at"],
                              "manual_allocation": allocate_status, "refund": refund_status, "refund_retry": refund_retry_status,
                              "refund_cash_fcfa": -300, "regularization_history": 2, "final_sale_debt_fcfa": 100,
                              "opening_debt_creation": opening_status, "opening_debt_retry": opening_retry_status,
                              "opening_debt_fcfa": 1000, "opening_without_cash": True, "deadline_change": deadline_status,
                              "new_deadline_utc": deadline["due_at"], "opening_history_events": 2,
                              "statement_status": statement_status, "statement_entries": 8,
                              "statement_balance_fcfa": 1100, "statement_snapshot_stable": True,
                              "unreturned_change_sale": change_status, "change_due_fcfa": 2500,
                              "change_refund": change_refund_status, "change_refund_retry": change_retry_status,
                              "change_final_balance_fcfa": 0, "cash_opening": cash_opening_status,
                              "cash_opening_retry": cash_retry_status, "cash_expense": cash_expense_status,
                              "cash_global_fcfa": cash_ledger["totals_by_method_fcfa"],
                              "cash_personal_fcfa": personal_cash["totals_by_method_fcfa"],
                              "physical_count": count_status, "count_retry": count_retry_status,
                              "count_original_expected_fcfa": 47000, "count_revised_expected_fcfa": 46000,
                              "count_physical_unchanged_fcfa": 45000}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
