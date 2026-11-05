"""Manager reconciliation is bounded by both already counted and refused quantities."""
import uuid
from unittest.mock import patch
from django.test import TestCase
from django.db import connection, IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from backups.tests import test_recovery_sale_hold_api as fixtures
from catalogue.models import Product
from common.models import Activity
from identity.models import Membership,User,DeviceSession
from cash.models import CashMovement
from sales.models import Sale
from stock.models import StockReservation,StockMovement,StockLoss,StockAlertEvent,StockReservationReconciliation
from stock.services.stock_queries import with_stock


class RecoveryReservationReconciliationTests(TestCase):
    setUp=fixtures.RecoverySaleHoldApiTests.setUp
    authenticate=fixtures.RecoverySaleHoldApiTests.authenticate
    generation=fixtures.RecoverySaleHoldApiTests.generation
    record=fixtures.RecoverySaleHoldApiTests.record
    device=fixtures.RecoverySaleHoldApiTests.device
    review=fixtures.RecoverySaleHoldApiTests.review
    payload=fixtures.RecoverySaleHoldApiTests.payload
    url=fixtures.RecoverySaleHoldApiTests.url
    source=fixtures.RecoverySaleHoldApiTests.source
    capture=fixtures.RecoverySaleHoldApiTests.capture
    preview=fixtures.RecoverySaleHoldApiTests.preview
    decide=fixtures.RecoverySaleHoldApiTests.decide
    stage=fixtures.RecoverySaleHoldApiTests.stage

    def refused(self,quantity=1000):
        source=self.source()
        source.payload["original_request"]["lines"][0]["quantity_milli"]=quantity
        line=source.payload["sale"]["lines"][0]
        line.update(quantity_milli=quantity,base_quantity_milli=quantity,reference_fcfa=quantity//2,net_fcfa=quantity//2)
        source.payload["sale"].update(reference_total_fcfa=quantity//2,net_total_fcfa=quantity//2)
        for payment in (source.payload["original_request"]["payments"][0],source.payload["sale"]["payments"][0]):
            payment.update(amount_fcfa=quantity//2,received_fcfa=quantity//2)
        capture=self.capture(source)
        self.stage(capture)
        response,_=self.decide(capture,state="refused")
        self.assertEqual(response.status_code,201,response.content)
        reservation=StockReservation.objects.get(operation__recovery_sale_hold__source_operation_id=capture.operation_id)
        return capture,reservation

    def loss(self,quantity=1000,at="2026-10-22T09:00:00Z"):
        response=self.client.post(f"/api/v1/shops/{self.a.id}/products/{self.product.id}/losses/",{
            "id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"quantity_milli":quantity,
            "occurred_at":at,"reason":"Perte physique déjà validée"},format="json")
        self.assertEqual(response.status_code,201,response.content)
        return StockLoss.objects.get(id=response.data["id"]).movement

    def reconciliation_url(self,capture,reservation,suffix="reconciliations/"):
        return self.url(f"operations/{capture.id}/reservations/{reservation.id}/"+suffix)

    def reconciliation_preview(self,capture,reservation,movement,quantity=1000):
        response=self.client.get(self.reconciliation_url(capture,reservation,"reconciliation-preview/"),
            {"movement_id":str(movement.id),"quantity_milli":quantity})
        self.assertEqual(response.status_code,200,response.content)
        return response.data

    def reconcile(self,capture,reservation,movement,quantity=1000,preview=None,**changes):
        preview=preview or self.reconciliation_preview(capture,reservation,movement,quantity)
        data={"operation_id":str(uuid.uuid4()),"movement_id":str(movement.id),"quantity_milli":quantity,
            "preview_token":preview["preview_token"],"reason":"Même départ physique, sources rapprochées",**changes}
        return self.client.post(self.reconciliation_url(capture,reservation),data,format="json"),data

    def test_partial_then_complete_movement_matching_has_no_second_stock_cash_or_sale_effect(self):
        capture,reservation=self.refused(3000)
        movement=self.loss(2000)
        before=StockMovement.objects.count(); alerts=StockAlertEvent.objects.count()
        preview=self.reconciliation_preview(capture,reservation,movement)
        self.assertTrue(preview["can_apply"])
        self.assertEqual((preview["reserved_before_milli"],preview["reserved_after_milli"],
            preview["available_before_milli"],preview["available_after_milli"]),("3000","2000","-2000","-1000"))
        result,command=self.reconcile(capture,reservation,movement,preview=preview)
        self.assertEqual(result.status_code,201,result.content)
        self.assertEqual(self.client.post(self.reconciliation_url(capture,reservation),command,format="json").data,result.data)
        result,_=self.reconcile(capture,reservation,movement)
        self.assertEqual(result.status_code,201,result.content)
        blocked=self.reconciliation_preview(capture,reservation,movement)
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(blocked["movement_remaining_milli"],"0")
        self.assertEqual(self.reconcile(capture,reservation,movement,preview=blocked)[0].status_code,409)
        stock=with_stock(Product.objects.filter(id=self.product.id)).get()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),("1000","1000","0"))
        reservation.refresh_from_db();movement.refresh_from_db()
        self.assertEqual((reservation.quantity_milli,reservation.state,movement.quantity_milli),(3000,"rejected",-2000))
        self.assertEqual((StockMovement.objects.count(),StockAlertEvent.objects.count()),(before,alerts))
        self.assertFalse(CashMovement.objects.exists());self.assertFalse(Sale.objects.exists())
        page=self.client.get(self.reconciliation_url(capture,reservation))
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual(page.data["count"],2)
        self.assertEqual(sum(row["quantity_milli"] for row in page.data["results"]),2000)
        # A reconciliation does not reopen or approve the refused sale.
        self.assertFalse(self.preview(capture)["can_apply"])
        self.assertEqual(self.preview(capture)["stocks"][0]["reservation_id"],str(reservation.id))

    def test_one_loss_cannot_be_claimed_by_two_refused_sources_and_stale_preview_is_rejected(self):
        first,r1=self.refused();second,r2=self.refused()
        movement=self.loss()
        stale=self.reconciliation_preview(second,r2,movement)
        result,_=self.reconcile(first,r1,movement)
        self.assertEqual(result.status_code,201,result.content)
        self.assertEqual(self.reconcile(second,r2,movement,preview=stale)[0].status_code,409)
        blocked=self.reconciliation_preview(second,r2,movement)
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(blocked["movement_remaining_milli"],"0")
        self.assertEqual(with_stock(Product.objects.filter(id=self.product.id)).get().stock_reserved_milli,"1000")

    def test_reconciliation_cannot_occupy_another_captured_operation_uuid(self):
        capture,reservation=self.refused()
        other=self.capture(self.source())
        movement=self.loss()
        before=Activity.objects.count()
        response,_=self.reconcile(capture,reservation,movement,operation_id=str(other.operation_id))
        self.assertEqual(response.status_code,409,response.content)
        self.assertEqual(Activity.objects.count(),before)
        self.assertFalse(Activity.objects.filter(id=other.operation_id).exists())
        self.assertEqual(with_stock(Product.objects.filter(id=self.product.id)).get().stock_reserved_milli,"1000")

    def test_reconciliation_preserves_exact_stock_totals_beyond_int64(self):
        capture,reservation=self.refused(6000000000000000000)
        self.refused(6000000000000000000)
        movement=self.loss()
        preview=self.reconciliation_preview(capture,reservation,movement)
        self.assertEqual(preview["reservation_remaining_milli"],"6000000000000000000")
        self.assertEqual((preview["reserved_before_milli"],preview["reserved_after_milli"]),
            ("12000000000000000000","11999999999999999000"))
        self.assertEqual((preview["available_before_milli"],preview["available_after_milli"]),
            ("-11999999999999998000","-11999999999999997000"))
        response,_=self.reconcile(capture,reservation,movement,preview=preview)
        self.assertEqual(response.status_code,201,response.content)
        stock=with_stock(Product.objects.filter(id=self.product.id)).get()
        self.assertEqual((stock.stock_reserved_milli,stock.stock_rejected_reserved_milli,stock.stock_pending_reserved_milli),
            ("11999999999999999000","11999999999999999000","0"))
        self.assertEqual(stock.stock_available_milli,"-11999999999999997000")

    def test_history_is_paginated_in_sql_and_old_receipts_remain_unchanged(self):
        capture,reservation=self.refused(3000)
        movement=self.loss(3000)
        receipts=[]
        for _ in range(3):
            response,_=self.reconcile(capture,reservation,movement)
            self.assertEqual(response.status_code,201,response.content)
            receipts.append(response.data)
        url=self.reconciliation_url(capture,reservation)
        with CaptureQueriesContext(connection) as small:
            first=self.client.get(url,{"page_size":1})
        with CaptureQueriesContext(connection) as larger:
            all_rows=self.client.get(url,{"page_size":1000})
        self.assertEqual((first.status_code,all_rows.status_code),(200,200))
        self.assertEqual((first.data["count"],len(first.data["results"])),(3,1))
        self.assertEqual(first.data["results"],[receipts[0]])
        self.assertEqual(all_rows.data["results"],receipts)
        self.assertEqual(len(small),len(larger))
        self.assertTrue(any('FROM "stock_stockreservationreconciliation"' in row["sql"] and "LIMIT 1" in row["sql"] for row in small))
        next_page=self.client.get(first.data["next"])
        self.assertEqual(next_page.data["results"],[receipts[1]])
        self.assertEqual(next_page.data["count"],3)
        self.assertEqual(receipts[0]["before_after"]["reservation_remaining_milli"],"3000")
        self.assertEqual(self.reconciliation_preview(capture,reservation,movement)["reservation_remaining_milli"],"0")

    def test_reconciliation_rearms_a_future_threshold_crossing_without_a_duplicate_alert(self):
        self.product.stock_threshold_milli=0
        self.product.save(update_fields=["stock_threshold_milli"])
        capture,reservation=self.refused(2000)
        self.assertEqual(StockAlertEvent.objects.count(),0)
        movement=self.loss()
        self.assertEqual(StockAlertEvent.objects.count(),1)
        response,_=self.reconcile(capture,reservation,movement)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(StockAlertEvent.objects.count(),1)
        self.assertFalse(self.product.stock_alert_state.is_low)
        self.loss()
        self.assertEqual(list(StockAlertEvent.objects.order_by("episode").values_list("episode","available_milli")),
            [(1,0),(2,0)])

    def test_a_new_recovery_generation_keeps_previously_matched_quantities_and_refusals(self):
        capture,reservation=self.refused(2000)
        movement=self.loss(2000)
        first,_=self.reconcile(capture,reservation,movement)
        self.assertEqual(first.status_code,201,first.content)
        self.current.current_slot=None
        self.current.save(update_fields=["current_slot"])
        self.current=self.generation()
        self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        new_capture=self.capture(capture)
        self.assertNotEqual(new_capture.id,capture.id)
        self.assertEqual(self.client.get(self.reconciliation_url(capture,reservation)).status_code,404)
        preview=self.reconciliation_preview(new_capture,reservation,movement)
        self.assertEqual((preview["reservation_reconciled_milli"],preview["reservation_remaining_milli"],
            preview["movement_remaining_milli"]),("1000","1000","1000"))
        self.assertEqual(self.client.get(self.reconciliation_url(new_capture,reservation)).data["results"],[first.data])
        second,_=self.reconcile(new_capture,reservation,movement,preview=preview)
        self.assertEqual(second.status_code,201,second.content)
        self.assertEqual(StockReservation.objects.count(),1)
        reservation.refresh_from_db()
        self.assertEqual((reservation.state,reservation.quantity_milli),("rejected",2000))
        stock=with_stock(Product.objects.filter(id=self.product.id)).get()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),
            ("1000","0","1000"))
        self.assertFalse(self.preview(new_capture)["can_apply"])
        self.assertFalse(CashMovement.objects.exists())

    def test_storage_rejects_fractional_quantities_duplicate_commands_and_deletion_of_provenance(self):
        capture,reservation=self.refused();movement=self.loss()
        response,_=self.reconcile(capture,reservation,movement)
        self.assertEqual(response.status_code,201,response.content)
        row=StockReservationReconciliation.objects.get()
        for invalid in (0,-1,0.5):
            with self.subTest(quantity=invalid), self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("UPDATE stock_stockreservationreconciliation SET quantity_milli=%s WHERE id=%s",[invalid,row.id.hex])
        with self.assertRaises(IntegrityError),transaction.atomic():
            StockReservationReconciliation.objects.create(reservation=reservation,movement=movement,
                operation=row.operation,installation=row.installation,quantity_milli=1000)
        for original in (reservation,movement,row.operation,row.installation):
            with self.subTest(model=original._meta.label),self.assertRaises(ProtectedError),transaction.atomic():
                original.delete()
        row.refresh_from_db()
        self.assertEqual(row.quantity_milli,1000)

    def test_pending_wrong_product_and_unvalidated_departures_cannot_release_stock(self):
        capture=self.capture(self.source());self.stage(capture)
        reservation=StockReservation.objects.get()
        movement=self.loss()
        pending=self.reconciliation_preview(capture,reservation,movement)
        self.assertFalse(pending["can_apply"])
        self.assertEqual(self.reconcile(capture,reservation,movement,preview=pending)[0].status_code,409)
        self.assertEqual(self.decide(capture,state="refused")[0].status_code,201)
        forged=StockMovement.objects.create(product=self.product,actor=self.owner,operation=movement.operation,kind="loss",
            quantity_milli=-1000,occurred_at=movement.occurred_at,reason="Sans perte validée")
        self.assertFalse(self.reconciliation_preview(capture,reservation,forged)["can_apply"])
        other=Product.objects.create(shop=self.a,name="Autre",unit_kind="piece",unit_label="pièce",sale_price_fcfa=1)
        forged.product=other;forged.save(update_fields=["product"])
        response=self.client.get(self.reconciliation_url(capture,reservation,"reconciliation-preview/"),
            {"movement_id":str(forged.id),"quantity_milli":1000})
        self.assertEqual(response.status_code,404)
        too_much=self.reconciliation_preview(capture,reservation,movement,2000)
        self.assertFalse(too_much["can_apply"])
        self.assertFalse(self.reconciliation_preview(capture,reservation,movement,500)["can_apply"])
        for value in (0,-1,True,1.5):
            with self.subTest(quantity=value):self.assertEqual(self.reconcile(capture,reservation,movement,preview=too_much,quantity_milli=value)[0].status_code,400)

    def test_live_authorization_precedes_idempotent_receipt_and_failure_rolls_back(self):
        capture,reservation=self.refused();movement=self.loss()
        preview=self.reconciliation_preview(capture,reservation,movement)
        activities=Activity.objects.count()
        with patch("backups.services.recovery_reservation_reconciliation.observe_stock",side_effect=RuntimeError("transaction interruption")):
            with self.assertRaisesMessage(RuntimeError,"transaction interruption"):
                self.reconcile(capture,reservation,movement,preview=preview)
        self.assertEqual(Activity.objects.count(),activities)
        self.assertEqual(with_stock(Product.objects.filter(id=self.product.id)).get().stock_reserved_milli,"1000")
        result,command=self.reconcile(capture,reservation,movement,preview=preview)
        self.assertEqual(result.status_code,201,result.content)
        session=DeviceSession.objects.filter(installation__user=self.owner).first()
        session.revoked_at=movement.occurred_at;session.save(update_fields=["revoked_at"])
        self.assertIn(self.client.post(self.reconciliation_url(capture,reservation),command,format="json").status_code,(401,403))

    def test_negative_inventory_adjustment_can_be_matched_but_its_later_revision_limits_capacity(self):
        capture,reservation=self.refused(2000)
        base=f"/api/v1/shops/{self.a.id}/products/{self.product.id}/stock-counts/"
        count=self.client.post(base,{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"physical_milli":1000,
            "counted_at":"2026-10-22T10:00:00Z","reason":"Comptage physique"},format="json")
        self.assertEqual(count.status_code,201,count.content)
        confirm=base+count.data["id"]+"/confirm/"
        adjusted=self.client.post(confirm,{"operation_id":str(uuid.uuid4()),"expected_milli":3000,
            "applied_adjustment_milli":0,"reason":"Écart confirmé"},format="json")
        self.assertEqual(adjusted.status_code,201,adjusted.content)
        movement=StockMovement.objects.get(kind="inventory_adjustment")
        self.assertEqual(self.reconcile(capture,reservation,movement)[0].status_code,201)
        self.loss(at="2026-10-22T09:30:00Z")
        revised=self.client.post(confirm,{"operation_id":str(uuid.uuid4()),"expected_milli":2000,
            "applied_adjustment_milli":-2000,"reason":"Arrivée tardive rapprochée"},format="json")
        self.assertEqual(revised.status_code,201,revised.content)
        preview=self.reconciliation_preview(capture,reservation,movement)
        self.assertEqual(preview["movement_remaining_milli"],"0")
        self.assertFalse(preview["can_apply"])

    def test_current_scope_and_role_protect_preview_receipts_and_history(self):
        capture,reservation=self.refused();movement=self.loss()
        response,command=self.reconcile(capture,reservation,movement)
        self.assertEqual(response.status_code,201,response.content)
        foreign=self.reconciliation_url(capture,reservation).replace(str(self.a.id),str(self.b.id))
        self.assertEqual(self.client.post(foreign,command,format="json").status_code,404)
        seller=User.objects.create_user(username="reservation-current-seller")
        membership=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=membership,confirmed=True)
        self.client=self.authenticate(seller)
        self.assertEqual(self.client.post(self.reconciliation_url(capture,reservation),command,format="json").status_code,403)
        self.assertEqual(self.client.get(self.reconciliation_url(capture,reservation)).status_code,403)
        self.assertEqual(self.client.get(self.reconciliation_url(capture,reservation,"reconciliation-preview/"),
            {"movement_id":str(movement.id),"quantity_milli":1000}).status_code,403)

    def test_preview_receipt_retry_and_history_match_the_published_schema(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        from rest_framework.test import APIClient
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method="get"):
            path=next(path for path in schema["paths"] if path.endswith(ending))
            definition=schema["paths"][path][method]["responses"][str(response.status_code)]
            rule=nullable({**definition["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        capture,reservation=self.refused();movement=self.loss()
        preview=self.client.get(self.reconciliation_url(capture,reservation,"reconciliation-preview/"),
            {"movement_id":str(movement.id),"quantity_milli":1000})
        check(preview,"/reconciliation-preview/")
        response,command=self.reconcile(capture,reservation,movement,preview=preview.data)
        check(response,"/reconciliations/","post")
        check(self.client.post(self.reconciliation_url(capture,reservation),command,format="json"),"/reconciliations/","post")
        check(self.client.post(self.reconciliation_url(capture,reservation),{**command,"reason":"Changé"},format="json"),"/reconciliations/","post")
        check(self.client.post(self.reconciliation_url(capture,reservation),{},format="json"),"/reconciliations/","post")
        check(self.client.get(self.reconciliation_url(capture,reservation)),"/reconciliations/")
        check(APIClient().get(self.reconciliation_url(capture,reservation)),"/reconciliations/")
