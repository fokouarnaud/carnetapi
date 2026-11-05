"""Keep a native T1 physical arrival outside a restored T0 business database."""
from datetime import datetime, timezone
import json
import uuid


def prepare_transfer_baseline(*, source_shop, destination_shop, owner, installation, session, product):
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken
    from catalogue.models import Product
    from common.models import Activity
    from stock.models import StockMovement
    token = AccessToken.for_user(owner)
    token["session_id"] = str(session.id)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + str(token))
    source = Product.objects.create(shop=source_shop, name="Marchandise source T0", unit_kind="piece", unit_label="pièce", sale_price_fcfa=900)
    event = Activity.objects.create(shop=source_shop, actor=owner, action="opening_stock", target_id=source.id, reason="Ouverture physique T0")
    StockMovement.objects.create(product=source, actor=owner, operation=event, kind="opening", quantity_milli=10000,
        unit_cost_micros=123456789, occurred_at=datetime.now(timezone.utc), reason="Ouverture physique T0")
    command = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "destination_shop_id": str(destination_shop.id),
        "is_departure_confirmed": True, "reason": "Cinq unités réellement parties en T0", "occurred_at": datetime.now(timezone.utc).isoformat(),
        "lines": [{"id": str(uuid.uuid4()), "product_id": str(source.id), "product_version": 1, "quantity_milli": 5000}]}
    response = api.post(f"/api/v1/shops/{source_shop.id}/transfers/", command, format="json")
    assert response.status_code == 201, (response.status_code, response.content)
    assert response.data["lines"][0]["in_transit_milli"] == "5000"
    return {"transfer": response.data, "destination_product_id": str(product.id), "source_product_id": str(source.id)}


def record_transfer_receipt_flow(request, *, shop_id, actor_id, installation_id, baseline):
    transfer = baseline["transfer"]
    command = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": datetime.now(timezone.utc).isoformat(),
        "reason": "Trois unités physiquement reçues en T1", "is_sellable_confirmed": True,
        "lines": [{"id": str(uuid.uuid4()), "transfer_line_id": transfer["lines"][0]["id"],
            "product_id": baseline["destination_product_id"], "quantity_milli": 3000}]}
    endpoint = f"/api/v1/shops/{shop_id}/transfers/{transfer['id']}/receipts/"
    status, receipt, _ = request(endpoint, data=command)
    assert status == 201, (status, receipt)
    assert receipt["lines"][0]["total_cost_nanos_fcfa"] == "370370367000"
    assert receipt["lines"][0]["received_before_milli"] == "0"
    assert request(endpoint, data=command)[:2] == (200, receipt)
    entry = {"operation_id": command["operation_id"], "operation_type": "transfer_receipt",
        "origin_user_id": str(actor_id), "origin_installation_id": str(installation_id),
        "occurred_at": receipt["occurred_at"], "origin_recorded_at": receipt["created_at"], "dependencies": [],
        "payload": {"schema_version": 1, "original_request": {**command, "transfer_id": transfer["id"],
            "installation_id": str(installation_id)}, "receipt": receipt}}
    return {"baseline": baseline, "operations": [entry]}


def audit_transfer_receipt_t0(db, survivor):
    assert db.execute("SELECT COUNT(*) FROM stock_stocktransfer").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM stock_stocktransferreceipt").fetchone()[0] == 0
    assert db.execute("SELECT quantity_milli FROM stock_stocktransferline").fetchone()[0] == 5000
    assert db.execute("SELECT COUNT(*) FROM core_stockmovement WHERE kind='transfer_in'").fetchone()[0] == 0


def recover_transfer_receipt_flow(request, *, shop_id, foreign_shop_id, session_id, credential, survivor):
    prefix = f"/api/v1/shops/{shop_id}/"
    entry = survivor["operations"][0]
    journal = {"operation_id": str(uuid.uuid4()), "shop_id": str(shop_id), "session_id": session_id,
        "format_version": 1, "reason": "Copie physique survivante contrôlée après restauration", "operations": [entry]}
    status, captured, _ = request(prefix + "recovery/journals/", credential, journal)
    assert status == 201 and captured["business_effects_created"] == 0, (status, captured)
    assert request(prefix + "recovery/journals/", credential, journal)[:2] == (200, captured)
    capture = captured["operations"][0]["capture_id"]
    endpoint = prefix + f"recovery/operations/{capture}/"
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{capture}/integration-preview/", credential)[0] == 404
    status, preview, _ = request(endpoint + "integration-preview/", credential)
    assert status == 200 and preview["can_apply"] and preview["cash_delta_by_method_fcfa"] == {}, (status, preview)
    assert preview["stocks"][0]["validated_before_milli"] == "0" and preview["stocks"][0]["validated_after_milli"] == "3000"
    command = {"operation_id": str(uuid.uuid4()), "previous_version": preview["version"], "preview_token": preview["preview_token"],
        "reason": "Destination choisie, quantité et coût de départ vérifiés"}
    status, result, _ = request(endpoint + "integrate/", credential, command)
    assert status == 201, (status, result)
    expected = entry["payload"]["receipt"]
    actual = result["business_receipt"]
    assert {key: value for key, value in actual.items() if key != "created_at"} == {key: value for key, value in expected.items() if key != "created_at"}
    assert request(endpoint + "integrate/", credential, command)[:2] == (200, result)
    status, known, _ = request(endpoint + "integration-preview/", credential)
    assert status == 200 and known["can_apply"] and known["outcome"] == "already_known", (status, known)
    again = {**command, "operation_id": str(uuid.uuid4()), "previous_version": known["version"], "preview_token": known["preview_token"]}
    status, acknowledged, _ = request(endpoint + "integrate/", credential, again)
    assert status == 201 and acknowledged["business_receipt"] == actual, (status, acknowledged)
    transfer_path = prefix + f"transfers/{expected['transfer_id']}/"
    status, transfer, _ = request(transfer_path, credential)
    assert status == 200 and transfer["lines"][0]["in_transit_milli"] == "2000", (status, transfer)
    status, receipts, _ = request(transfer_path + "receipts/", credential)
    assert status == 200 and receipts["count"] == 1 and receipts["results"][0] == actual, (status, receipts)
    raw = {key: value for key, value in entry["payload"]["original_request"].items() if key not in ("installation_id", "transfer_id")}
    assert request(transfer_path + "receipts/", credential, raw)[0] == 409
    assert request(prefix + f"products/{survivor['baseline']['destination_product_id']}/", credential)[1]["stock_validated_milli"] == 3000
    return {"transfer_departure_already_in_t0": True, "lost_t1_transfer_receipts_recovered": 1,
        "transfer_destination_stock_milli": 3000, "transfer_remaining_in_transit_milli": 2000,
        "transfer_receipt_ids_before_marks_and_cost_preserved": True, "transfer_origin_without_access_grant": True,
        "transfer_receipt_creates_no_cash_or_debt": True}


def audit_transfer_receipt_flow(db, survivor):
    entry = survivor["operations"][0]
    frozen = entry["payload"]["receipt"]["lines"][0]
    for table, count in (("stock_stocktransfer", 1), ("stock_stocktransferreceipt", 1), ("stock_stocktransferreceiptline", 1),
            ("backups_recoveredoperationorigin", 1), ("backups_recoveryintegrationdecision", 2), ("core_cashmovement", 0),
            ("core_customerdebt", 0), ("core_supplierdebt", 0)):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
    assert db.execute("SELECT quantity_milli,received_before_milli,total_cost_nanos_fcfa,movement_id FROM stock_stocktransferreceiptline").fetchone() == (
        3000, 0, "370370367000", uuid.UUID(frozen["movement_id"]).hex)
    details = json.loads(db.execute("SELECT details FROM core_activity WHERE id=?", [uuid.UUID(entry["operation_id"]).hex]).fetchone()[0])
    assert details["recovery"]["source_snapshot"] == entry["payload"]["receipt"]
    assert details["response"]["lines"] == entry["payload"]["receipt"]["lines"]
    assert db.execute("SELECT SUM(quantity_milli) FROM core_stockmovement WHERE product_id=?", [uuid.UUID(survivor["baseline"]["destination_product_id"]).hex]).fetchone()[0] == 3000
    assert db.execute("SELECT SUM(quantity_milli) FROM core_stockmovement WHERE product_id=?", [uuid.UUID(survivor["baseline"]["source_product_id"]).hex]).fetchone()[0] == 5000
