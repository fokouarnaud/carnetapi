"""Return claims are settled separately from sales, physical receipts and rights."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_allocations as fixtures
from common.models import Activity
from customers.models import CustomerDebt,CustomerLedgerEntry,ReturnCreditSettlement,ReturnCreditAllocation
from customers.services.return_credit_balance import remaining_return_credit
from cash.models import CashMovement
from sales.models import SaleReturn
from stock.models import StockMovement,ReturnReceipt


class SettlementSources:
    def source(self,kind="refund",returned=None):
        returned=returned or self.returned
        operation,actor,device,decision=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,24,10,tzinfo=timezone.utc)
        request={"id":str(decision),"operation_id":str(operation),"occurred_at":occurred.isoformat(),
            "reason":"Règlement réel de l'avoir de retour","sale_return_id":str(returned.id),"kind":kind,"installation_id":str(device)}
        rows=[{"id":str(uuid.uuid4()),"debt_id":str(debt.id),"amount_fcfa":amount}
            for debt,amount in zip(self.debts,(1000,1500),strict=True)] if kind=="allocation" else []
        amount=2500 if kind=="allocation" else 2000
        if kind=="refund":request.update(amount_fcfa=amount,method="other",other_name="Portefeuille déclaré",external_reference="R-RETOUR")
        else:request["allocations"]=deepcopy(rows)
        snapshot={"id":str(decision),"sale_return_id":str(returned.id),"sale_id":str(returned.sale_id),
            "customer_id":str(returned.sale.customer_id) if returned.sale.customer_id else None,"kind":kind,
            "amount_fcfa":amount,"before_credit_fcfa":5000,"after_credit_fcfa":5000-amount,"occurred_at":occurred.isoformat(),
            "created_at":(occurred+timedelta(seconds=5)).isoformat(),"method":request.get("method",""),
            "other_name":request.get("other_name",""),"external_reference":request.get("external_reference",""),"allocations":rows}
        return SimpleNamespace(operation_type="return_credit_settlement",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,"original_request":request,"settlement":snapshot})


class RecoveryReturnSettlementSnapshotTests(SimpleTestCase,SettlementSources):
    def setUp(self):
        self.returned=SimpleNamespace(id=uuid.uuid4(),sale_id=uuid.uuid4(),sale=SimpleNamespace(customer_id=uuid.uuid4()))
        self.debts=[SimpleNamespace(id=uuid.uuid4()) for _ in range(2)]

    def test_source_kind_dates_arithmetic_fields_and_allocations_must_match(self):
        from backups.services.recovery_return_settlements import decode_return_settlement,RecoveryReturnSettlementInvalid
        for kind in ("refund","allocation"):
            with self.subTest(kind=kind):
                source=self.source(kind);decoded=decode_return_settlement(source)
                self.assertEqual(decoded.snapshot["kind"],kind)
                for case in ("amount","after","kind","device","date","created","bool","wrong_fields"):
                    with self.subTest(case=case):
                        changed=deepcopy(source);snapshot=changed.payload["settlement"];request=changed.payload["original_request"]
                        if case=="amount":snapshot["amount_fcfa"]-=1
                        elif case=="after":snapshot["after_credit_fcfa"]-=1
                        elif case=="kind":request["kind"]="other"
                        elif case=="device":request["installation_id"]=str(uuid.uuid4())
                        elif case=="date":snapshot["occurred_at"]="2026-10-24T11:00:00Z"
                        elif case=="created":snapshot["created_at"]="2026-10-24T10:00:05"
                        elif case=="bool":snapshot["amount_fcfa"]=True
                        else:snapshot["allocations"]=[{"id":str(uuid.uuid4()),"debt_id":str(self.debts[0].id),"amount_fcfa":2000}] if kind=="refund" else []
                        with self.assertRaises(RecoveryReturnSettlementInvalid):decode_return_settlement(changed)


class RecoveryReturnSettlementTests(TestCase,SettlementSources):
    authenticate=fixtures.RecoveryCustomerAllocationTests.authenticate
    generation=fixtures.RecoveryCustomerAllocationTests.generation
    record=fixtures.RecoveryCustomerAllocationTests.record
    device=fixtures.RecoveryCustomerAllocationTests.device
    review=fixtures.RecoveryCustomerAllocationTests.review
    payload=fixtures.RecoveryCustomerAllocationTests.payload
    url=fixtures.RecoveryCustomerAllocationTests.url
    capture=fixtures.RecoveryCustomerAllocationTests.capture
    preview=fixtures.RecoveryCustomerAllocationTests.preview
    apply=fixtures.RecoveryCustomerAllocationTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerAllocationTests.setUp(self)
        opening=Activity.objects.create(shop=self.a,actor=self.owner,action="opening_stock",target_id=self.product.id,reason="Stock de la fixture")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=opening,kind="opening",quantity_milli=30000,
            unit_cost_micros=100000000,occurred_at=datetime(2026,10,21,9,tzinfo=timezone.utc),reason="Stock connu avant vente")
        self.returned=self.prepare_return()

    def prepare_return(self,anonymous=False):
        data={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-23T09:00:00Z","reason":"Vente avant retour",
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":self.product.version,"quantity_milli":10000}],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":5000}],"customer_id":None if anonymous else str(self.customer.id)}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",data,format="json");self.assertEqual(response.status_code,201,response.content)
        returned={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-23T10:00:00Z","reason":"Retour commercial avant règlement",
            "lines":[{"id":str(uuid.uuid4()),"sale_line_id":response.data["lines"][0]["id"],"quantity_milli":10000}]}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{data['id']}/returns/",returned,format="json")
        self.assertEqual(response.status_code,201,response.content);self.assertEqual(response.data["credit_fcfa"],5000)
        return SaleReturn.objects.select_related("sale").get(id=returned["id"])

    def native_url(self,kind="refund",returned=None):
        returned=returned or self.returned
        return f"/api/v1/shops/{self.a.id}/sales/{returned.sale_id}/returns/{returned.id}/{'refunds' if kind=='refund' else 'allocations'}/"

    def native_body(self,source):
        return {key:value for key,value in source.payload["original_request"].items()
            if key not in ("sale_return_id","kind","installation_id","sale_id","customer_id")}

    def test_refund_and_allocation_reduce_the_claim_once_without_physical_stock_or_new_payment(self):
        stock=StockMovement.objects.count();users=self.owner.__class__.objects.count()
        for kind,after in (("refund",3000),("allocation",500)):
            with self.subTest(kind=kind):
                source=self.source(kind);capture=self.capture(source);preview=self.preview(capture);self.assertTrue(preview["can_apply"])
                self.assertEqual(preview["cash_delta_by_method_fcfa"],{"other:Portefeuille déclaré":-2000} if kind=="refund" else {})
                response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
                receipt=response.data["business_receipt"]
                self.assertEqual((receipt["kind"],receipt["after_credit_fcfa"],receipt["actor_id"]),(kind,after,str(source.origin_user_id)))
                self.assertNotEqual(datetime.fromisoformat(receipt["created_at"].replace("Z","+00:00")),
                    datetime.fromisoformat(source.payload["settlement"]["created_at"].replace("Z","+00:00")))
                self.assertEqual(remaining_return_credit(self.returned),after)
                retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
                self.assertEqual((retry.status_code,retry.data),(200,response.data))
                known=self.preview(capture);self.assertEqual(known["outcome"],"already_known")
                ack,_=self.apply(capture,known);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,receipt))
                ledger=CustomerLedgerEntry.objects.get(operation_id=source.operation_id)
                self.assertEqual((ledger.delta_fcfa,ledger.debt_delta_fcfa,ledger.credit_delta_fcfa),
                    (2000,0,-2000) if kind=="refund" else (0,-2500,-2500))
                history=self.client.get(self.native_url(kind));self.assertEqual(history.status_code,200,history.content)
                self.assertEqual(history.data["results"][0]["actor_id"],str(source.origin_user_id))
        self.assertEqual((StockMovement.objects.count(),ReturnReceipt.objects.count(),self.owner.__class__.objects.count()),(stock,0,users))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,5000])
        self.assertEqual(list(CustomerDebt.objects.order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[2000,2500])

    def test_anonymous_refund_never_creates_a_customer_or_customer_ledger(self):
        returned=self.prepare_return(anonymous=True);source=self.source(returned=returned)
        before=self.customer.__class__.objects.count();ledger=CustomerLedgerEntry.objects.count()
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((self.customer.__class__.objects.count(),CustomerLedgerEntry.objects.count()),(before,ledger))
        self.assertEqual(remaining_return_credit(returned),3000)
        self.assertEqual(CashMovement.objects.get(operation_id=source.operation_id).amount_fcfa,-2000)
        source=self.source("allocation",returned);capture=self.capture(source);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"]);self.assertIn("allocation_customer_required",{row["code"] for row in preview["issues"]})

    def test_changed_credit_or_debt_blocks_stale_or_insufficient_decisions_without_partial_effects(self):
        source=self.source("allocation");capture=self.capture(source);old=self.preview(capture)
        CustomerDebt.objects.filter(id=self.debts[1].id).update(remaining_fcfa=1499)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(remaining_return_credit(self.returned),5000)
        self.assertEqual(ReturnCreditSettlement.objects.count(),0)

    def test_late_failure_rolls_back_cash_claim_debts_ledger_and_origin(self):
        for kind in ("refund","allocation"):
            with self.subTest(kind=kind):
                source=self.source(kind);capture=self.capture(source);preview=self.preview(capture)
                with connection.cursor() as cursor:
                    cursor.execute("CREATE TEMP TRIGGER fail_return_settlement_decision BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected return settlement failure'); END")
                try:
                    with self.assertRaises(IntegrityError):self.apply(capture,preview)
                finally:
                    with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_return_settlement_decision")
                self.assertEqual(remaining_return_credit(self.returned),5000)
                self.assertFalse(ReturnCreditSettlement.objects.exists());self.assertFalse(ReturnCreditAllocation.objects.exists())
                self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())
                self.assertEqual(list(CustomerDebt.objects.order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[3000,4000])

    def test_native_receipts_are_recognized_after_later_settlements_without_new_effects(self):
        for kind in ("refund","allocation"):
            with self.subTest(kind=kind):
                source=self.source(kind);source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
                source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
                before=remaining_return_credit(self.returned)
                native=self.client.post(self.native_url(kind),self.native_body(source),format="json")
                self.assertEqual(native.status_code,201,native.content)
                snapshot=source.payload["settlement"];snapshot.update(before_credit_fcfa=before)
                for key in snapshot:
                    if key in native.data:snapshot[key]=native.data[key]
                capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
                self.assertTrue(preview["origin_installation_verified"])
                ack,_=self.apply(capture,preview);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,native.data))
        self.assertEqual(remaining_return_credit(self.returned),500)
        self.assertEqual((ReturnCreditSettlement.objects.count(),ReturnCreditAllocation.objects.count()),(2,2))
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)

    def test_adjusted_receipt_survives_next_generation_and_keeps_original_created_timestamp(self):
        initial=self.source();body=self.native_body(initial);body.update(id=str(uuid.uuid4()),operation_id=str(uuid.uuid4()),amount_fcfa=500)
        self.assertEqual(self.client.post(self.native_url(),body,format="json").status_code,201)
        source=self.source("allocation");capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        first=response.data["business_receipt"];self.assertEqual(first["after_credit_fcfa"],2000)
        operation=Activity.objects.get(id=source.operation_id)
        self.assertEqual(operation.details["recovery"]["source_snapshot"]["created_at"],"2026-10-24T10:00:05Z")
        body.update(id=str(uuid.uuid4()),operation_id=str(uuid.uuid4()),amount_fcfa=100)
        self.assertEqual(self.client.post(self.native_url(),body,format="json").status_code,201)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        response,_=self.apply(capture,preview);self.assertEqual((response.status_code,response.data["business_receipt"]),(201,first))
        self.assertEqual(remaining_return_credit(self.returned),1900)
        self.assertEqual((ReturnCreditSettlement.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(3,1))

    def test_foreign_parent_or_customer_and_missing_dependencies_remain_private_and_blocked(self):
        source=self.source();source.payload["settlement"]["sale_id"]=str(uuid.uuid4())
        capture=self.capture(source,[uuid.uuid4()]);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"]);self.assertIsNone(preview["current_state"])
        self.assertIn("missing_return",{row["code"] for row in preview["issues"]})
        self.assertIn("missing_dependencies",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        source=self.source("allocation");source.payload["settlement"]["customer_id"]=str(uuid.uuid4())
        preview=self.preview(self.capture(source));self.assertFalse(preview["can_apply"])
        self.assertIn("return_customer_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(remaining_return_credit(self.returned),5000)

    def test_current_rights_and_native_bypass_cannot_apply_or_reapply_a_capture(self):
        from identity.models import User,Membership
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username="return-settlement-seller");member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic("POST",self.url(f"operations/{capture.id}/integrate/"),b"invalid",content_type="application/json").status_code,403)
        self.assertEqual(self.client.post(self.native_url(),self.native_body(source),format="json").status_code,409)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/",shop=self.b)).status_code,404)
        preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Qualité actuelle retirée")
        self.assertEqual(self.apply(capture,preview)[0].status_code,404)
        self.assertFalse(ReturnCreditSettlement.objects.exists())

    def test_corrupt_origin_refuses_native_list_and_detail_without_moving_money(self):
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b,origin_user_id=uuid.uuid4())
        for language in ("fr","en"):
            for suffix in ("",source.payload["settlement"]["id"]+"/"):
                with self.subTest(language=language,suffix=suffix):
                    response=self.client.get(self.native_url()+suffix,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual(response.status_code,409,response.content);self.assertEqual(response.data["code"],"origin_reference_conflict")
        self.assertEqual(remaining_return_credit(self.returned),3000)
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,5000])

    def test_live_preview_receipts_and_native_history_match_openapi_and_errors_are_bilingual(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method="get"):
            path=next(path for path in schema["paths"] if path.endswith(ending))
            rule=schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
            errors=list(Draft4Validator(nullable({**rule,"components":schema["components"]}),format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.message) for error in errors],[])
        for kind in ("refund","allocation"):
            with self.subTest(kind=kind):
                source=self.source(kind);capture=self.capture(source)
                preview=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"));self.assertEqual(preview.status_code,200,preview.content);check(preview,"/integration-preview/")
                response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,"/integrate/","post")
                history=self.client.get(self.native_url(kind));self.assertEqual(history.status_code,200,history.content)
                ending="/returns/{return_id}/"+("refunds/" if kind=="refund" else "allocations/")
                check(history,ending)
        source=self.source();source.payload["settlement"]["amount_fcfa"]=1;capture=self.capture(source)
        for language,detail in (("en","Recovery return credit settlement snapshot is inconsistent."),("fr","Les données figées du règlement d'avoir de retour récupéré sont incohérentes.")):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data["detail"])),(400,detail))
