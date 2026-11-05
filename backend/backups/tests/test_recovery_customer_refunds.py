"""Real refunds retain their amount, origin and one cash effect after restore."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_dependencies as fixtures
from common.models import Activity
from customers.models import Customer,CustomerPayment,CustomerCredit,PaymentRegularization,CustomerLedgerEntry
from cash.models import CashMovement
from identity.models import DeviceSession,User,Membership


class RefundSources:
    def source(self,kind="payment"):
        operation,actor,device,refund=[uuid.uuid4() for _ in range(4)]
        source_id=self.payment.id if kind=="payment" else self.credit.id
        occurred=datetime(2026,10,24,10,tzinfo=timezone.utc)
        request={"id":str(refund),"operation_id":str(operation),"reason":"Argent réellement rendu au client",
            "amount_fcfa":2000,"method":"other","other_name":"Caisse mobile déclarée","external_reference":"R-EXPLICITE",
            kind+"_id":str(source_id),"kind":"refund","installation_id":str(device)}
        snapshot={"id":str(refund),"customer_id":str(self.customer.id),"payment_id":str(source_id) if kind=="payment" else None,
            "credit_id":str(source_id) if kind=="credit" else None,"amount_fcfa":2000,
            "before_unallocated_fcfa":5000,"after_unallocated_fcfa":3000,"occurred_at":occurred.isoformat(),
            "method":"other","other_name":"Caisse mobile déclarée","external_reference":"R-EXPLICITE"}
        return SimpleNamespace(operation_type="customer_refund",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,"original_request":request,"refund":snapshot})


class RecoveryRefundSnapshotTests(SimpleTestCase,RefundSources):
    def setUp(self):
        self.customer=SimpleNamespace(id=uuid.uuid4());self.payment=SimpleNamespace(id=uuid.uuid4());self.credit=SimpleNamespace(id=uuid.uuid4())

    def test_refund_source_and_exact_historical_credit_arithmetic_are_validated(self):
        from backups.services.recovery_customer_refunds import decode_customer_refund,RecoveryRefundSnapshotInvalid
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);decoded=decode_customer_refund(source)
                self.assertEqual(decoded.source_kind,kind);self.assertEqual(decoded.snapshot["amount_fcfa"],2000)
                for case in ("both","none","amount","after","kind","device","time","extra","bool","method"):
                    with self.subTest(case=case):
                        changed=deepcopy(source);snapshot=changed.payload["refund"];raw=changed.payload["original_request"]
                        if case=="both":snapshot["credit_id"]=str(self.credit.id);snapshot["payment_id"]=str(self.payment.id)
                        elif case=="none":snapshot["credit_id"]=None;snapshot["payment_id"]=None
                        elif case=="amount":raw["amount_fcfa"]=1999
                        elif case=="after":snapshot["after_unallocated_fcfa"]=4000
                        elif case=="kind":raw["kind"]="allocation"
                        elif case=="device":raw["installation_id"]=str(uuid.uuid4())
                        elif case=="time":snapshot["occurred_at"]="2026-10-24T11:00:00Z"
                        elif case=="extra":snapshot["allocations"]=[]
                        elif case=="bool":snapshot["amount_fcfa"]=True
                        else:snapshot["method"]="cash"
                        with self.assertRaises(RecoveryRefundSnapshotInvalid):decode_customer_refund(changed)


class RecoveryCustomerRefundTests(TestCase,RefundSources):
    authenticate=fixtures.RecoveryCustomerDependencyTests.authenticate
    generation=fixtures.RecoveryCustomerDependencyTests.generation
    record=fixtures.RecoveryCustomerDependencyTests.record
    device=fixtures.RecoveryCustomerDependencyTests.device
    review=fixtures.RecoveryCustomerDependencyTests.review
    payload=fixtures.RecoveryCustomerDependencyTests.payload
    url=fixtures.RecoveryCustomerDependencyTests.url
    capture=fixtures.RecoveryCustomerDependencyTests.capture
    preview=fixtures.RecoveryCustomerDependencyTests.preview
    apply=fixtures.RecoveryCustomerDependencyTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerDependencyTests.setUp(self)
        self.live=DeviceSession.objects.get(installation__user=self.owner)
        self.customer=Customer.objects.create(shop=self.a,name="Client remboursé")
        past=datetime(2026,10,23,9,tzinfo=timezone.utc)
        event=Activity.objects.create(shop=self.a,actor=self.owner,action="customer_payment_created",target_id=uuid.uuid4(),reason="Paiement connu")
        self.payment=CustomerPayment.objects.create(id=event.target_id,shop=self.a,customer=self.customer,actor=self.owner,
            installation=self.live.installation,operation=event,occurred_at=past,amount_fcfa=5000,unallocated_fcfa=5000,requires_regularization=True)
        event=Activity.objects.create(shop=self.a,actor=self.owner,action="sale_created",target_id=uuid.uuid4(),reason="Monnaie due connue")
        self.credit=CustomerCredit.objects.create(shop=self.a,customer=self.customer,operation=event,amount_fcfa=5000,remaining_fcfa=5000,occurred_at=past)

    def native_url(self,kind="payment",source_id=None):
        source_id=source_id or (self.payment.id if kind=="payment" else self.credit.id)
        return f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/{'payments' if kind=='payment' else 'credits'}/{source_id}/"

    def test_refund_for_payment_and_credit_has_exact_cash_once_and_source_origin_everywhere(self):
        users=User.objects.count()
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);capture=self.capture(source);preview=self.preview(capture)
                self.assertTrue(preview["can_apply"]);self.assertEqual(preview["cash_delta_by_method_fcfa"],{"other:Caisse mobile déclarée":-2000})
                response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
                self.assertEqual(response.data["business_receipt"]["actor_id"],str(source.origin_user_id))
                self.assertEqual(response.data["business_receipt"]["after_unallocated_fcfa"],3000)
                decision=PaymentRegularization.objects.get(operation_id=source.operation_id)
                self.assertEqual((decision.amount_fcfa,decision.occurred_at,decision.installation_id),(2000,source.occurred_at,self.live.installation_id))
                entry=CustomerLedgerEntry.objects.get(operation_id=source.operation_id)
                self.assertEqual((entry.delta_fcfa,entry.debt_delta_fcfa,entry.credit_delta_fcfa,entry.occurred_at),(2000,0,-2000,source.occurred_at))
                retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
                self.assertEqual((retry.status_code,retry.data),(200,response.data))
                known=self.preview(capture);self.assertEqual(known["outcome"],"already_known");self.assertEqual(known["cash_delta_by_method_fcfa"],{})
                ack,_=self.apply(capture,known);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,response.data["business_receipt"]))
                history=self.client.get(self.native_url(kind)+"regularizations/")
                self.assertEqual(history.status_code,200,history.content)
                self.assertEqual(history.data["results"][0]["actor_id"],str(source.origin_user_id))
        self.payment.refresh_from_db();self.credit.refresh_from_db()
        self.assertEqual((self.payment.unallocated_fcfa,self.credit.remaining_fcfa),(3000,3000))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,-2000])
        self.assertEqual((User.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(users,2))

    def test_current_credit_changes_require_new_review_but_do_not_cap_the_real_refund(self):
        source=self.source();capture=self.capture(source);old=self.preview(capture)
        CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=2500)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"]["after_unallocated_fcfa"],500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa,-2000)
        self.payment.refresh_from_db();self.assertEqual(self.payment.unallocated_fcfa,500)

    def test_missing_or_insufficient_credit_remains_raw_and_blocked_without_fake_money(self):
        source=self.source();source.payload["refund"]["payment_id"]=str(uuid.uuid4())
        source.payload["original_request"]["payment_id"]=source.payload["refund"]["payment_id"]
        capture=self.capture(source);preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        source=self.source();capture=self.capture(source)
        CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=1999)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("insufficient_refund_credit",{row["code"] for row in preview["issues"]})
        self.assertEqual(preview["cash_delta_by_method_fcfa"],{"other:Caisse mobile déclarée":-2000})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual((CashMovement.objects.count(),PaymentRegularization.objects.count(),backups.RecoveryJournalOperation.objects.count()),(0,0,2))

    def test_late_decision_failure_rolls_back_credit_cash_ledger_and_origin(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_refund_integration BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected refund failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_refund_integration")
        self.payment.refresh_from_db();self.assertEqual(self.payment.unallocated_fcfa,5000)
        self.assertEqual((CashMovement.objects.count(),PaymentRegularization.objects.count(),CustomerLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(0,0,0,0))

    def native_body(self,source):
        return {key:value for key,value in source.payload["original_request"].items()
            if key not in ("payment_id","credit_id","customer_id","kind","installation_id")}

    def test_native_refund_known_after_later_refunds_returns_original_receipt_without_debit(self):
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
                source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
                native=self.client.post(self.native_url(kind)+"refund/",self.native_body(source),format="json")
                self.assertEqual(native.status_code,201,native.content)
                source.occurred_at=datetime.fromisoformat(native.data["occurred_at"].replace("Z","+00:00"))
                source.payload["refund"]["occurred_at"]=source.occurred_at.isoformat()
                later={**self.native_body(source),"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"amount_fcfa":500}
                response=self.client.post(self.native_url(kind)+"refund/",later,format="json")
                self.assertEqual(response.status_code,201,response.content)
                capture=self.capture(source);preview=self.preview(capture)
                self.assertEqual(preview["outcome"],"already_known");self.assertTrue(preview["origin_installation_verified"])
                ack,_=self.apply(capture,preview);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,native.data))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,-2000,-500,-500])
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)

    def test_adjusted_recovery_receipt_survives_new_generation_and_later_native_refund(self):
        source=self.source();CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=2500)
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        original=response.data["business_receipt"];self.assertEqual(original["after_unallocated_fcfa"],500)
        later={**self.native_body(source),"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"amount_fcfa":100}
        response=self.client.post(self.native_url()+"refund/",later,format="json");self.assertEqual(response.status_code,201,response.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        response,_=self.apply(capture,preview);self.assertEqual((response.status_code,response.data["business_receipt"]),(201,original))
        self.payment.refresh_from_db();self.assertEqual(self.payment.unallocated_fcfa,400)
        self.assertEqual((PaymentRegularization.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(2,1))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,-100])

    def test_current_role_foreign_source_and_native_bypass_do_not_move_money(self):
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username="refund-recovery-seller");member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic("POST",self.url(f"operations/{capture.id}/integrate/"),b"invalid",content_type="application/json").status_code,403)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/",shop=self.b)).status_code,404)
        bypass=self.client.post(self.native_url()+"refund/",self.native_body(source),format="json")
        self.assertEqual(bypass.status_code,409,bypass.content)
        other=Customer.objects.create(shop=self.b,name="Client B confidentiel")
        event=Activity.objects.create(shop=self.b,actor=self.owner,action="customer_payment_created",target_id=uuid.uuid4(),reason="Argent de B")
        foreign=CustomerPayment.objects.create(id=event.target_id,shop=self.b,customer=other,actor=self.owner,
            installation=self.live.installation,operation=event,occurred_at=source.occurred_at-timedelta(days=1),amount_fcfa=5000,unallocated_fcfa=5000)
        changed=self.source();changed.payload["refund"]["payment_id"]=str(foreign.id);changed.payload["original_request"]["payment_id"]=str(foreign.id)
        preview=self.preview(self.capture(changed));self.assertFalse(preview["can_apply"]);self.assertIsNone(preview["current_state"])
        self.assertNotIn(other.name,str(preview))
        self.assertEqual((CashMovement.objects.count(),PaymentRegularization.objects.count()),(0,0))

    def test_historical_credit_cannot_exceed_original_money_and_source_time_is_checked(self):
        source=self.source();source.payload["refund"].update(before_unallocated_fcfa=5001,after_unallocated_fcfa=3001)
        preview=self.preview(self.capture(source));self.assertFalse(preview["can_apply"])
        self.assertIn("refund_source_amount_conflict",{row["code"] for row in preview["issues"]})
        source=self.source();source.occurred_at-=timedelta(days=2);source.payload["refund"]["occurred_at"]=source.occurred_at.isoformat()
        preview=self.preview(self.capture(source));self.assertFalse(preview["can_apply"])
        self.assertIn("refund_precedes_source",{row["code"] for row in preview["issues"]})

    def test_corrupt_origin_blocks_refund_history_without_foreign_identity_or_money_change(self):
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b,origin_user_id=uuid.uuid4())
        for language in ("fr","en"):
            with self.subTest(language=language):
                response=self.client.get(self.native_url()+"regularizations/",HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,409,response.content);self.assertEqual(response.data["code"],"origin_reference_conflict")
        self.payment.refresh_from_db();self.assertEqual(self.payment.unallocated_fcfa,3000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa,-2000)

    def test_preview_acknowledgement_and_history_match_openapi_and_error_is_bilingual(self):
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
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        source=self.source();capture=self.capture(source)
        preview=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"));self.assertEqual(preview.status_code,200,preview.content);check(preview,"/integration-preview/")
        response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,"/integrate/","post")
        history=self.client.get(self.native_url()+"regularizations/");self.assertEqual(history.status_code,200,history.content);check(history,"/payments/{payment_id}/regularizations/")
        source=self.source();source.payload["refund"]["amount_fcfa"]=1;capture=self.capture(source)
        for language,detail in (("en","Recovery customer refund snapshot is inconsistent."),("fr","Les données figées du remboursement client récupéré sont incohérentes.")):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data["detail"])),(400,detail))

    def test_missing_sale_credit_is_created_by_reviewed_sale_before_its_refund_without_duplicate_cash(self):
        from backups.tests.test_recovery_sale_snapshot import RecoverySaleSnapshotTests
        from sales.models import Sale
        from stock.models import StockMovement
        sale=RecoverySaleSnapshotTests().source();sale.operation_type="sale"
        sale.payload["original_request"]["customer_id"]=str(self.customer.id)
        sale.payload["sale"]["customer_id"]=str(self.customer.id)
        for payload in (sale.payload["original_request"],sale.payload["sale"]):
            payload["lines"][0]["product_id"]=str(self.product.id)
            payload["payments"][0].update(amount_fcfa=5500,received_fcfa=5500)
        credit_id=uuid.uuid5(uuid.UUID(sale.payload["sale"]["id"]),"customer-credit")
        refund=self.source("credit")
        refund.payload["original_request"]["credit_id"]=str(credit_id)
        refund.payload["refund"]["credit_id"]=str(credit_id)
        refund_capture=self.capture(refund,[sale.operation_id]);old=self.preview(refund_capture)
        self.assertFalse(old["can_apply"])
        sale_capture=self.capture(sale);response,_=self.apply(sale_capture)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(CustomerCredit.objects.get(id=credit_id).remaining_fcfa,5000)
        self.assertEqual(self.apply(refund_capture,old)[0].status_code,409)
        response,_=self.apply(refund_capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((response.data["business_receipt"]["credit_id"],response.data["business_receipt"]["after_unallocated_fcfa"]),(str(credit_id),3000))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-2000,5500])
        self.assertEqual((Sale.objects.count(),StockMovement.objects.count(),PaymentRegularization.objects.count()),(1,1,1))
        self.credit.refresh_from_db();self.assertEqual(self.credit.remaining_fcfa,5000)
