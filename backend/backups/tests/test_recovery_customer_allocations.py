"""Recovered allocations consume existing credit and chosen debts, never cash."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_refunds as fixtures
from common.models import Activity
from customers.models import CustomerDebt,PaymentRegularization,RegularizationAllocation,CustomerLedgerEntry,CustomerPayment
from cash.models import CashMovement


class AllocationSources:
    def source(self,kind="payment"):
        operation,actor,device,decision=[uuid.uuid4() for _ in range(4)]
        source_id=self.payment.id if kind=="payment" else self.credit.id
        occurred=datetime(2026,10,24,11,tzinfo=timezone.utc)
        rows=[{"id":str(uuid.uuid4()),"debt_id":str(debt.id),"amount_fcfa":amount}
            for debt,amount in zip(self.debts,(1000,1500),strict=True)]
        request={"id":str(decision),"operation_id":str(operation),"reason":"Affectation explicite contrôlée",
            "allocations":deepcopy(rows),kind+"_id":str(source_id),"kind":"allocation","installation_id":str(device)}
        snapshot={"id":str(decision),"customer_id":str(self.customer.id),
            "payment_id":str(source_id) if kind=="payment" else None,"credit_id":str(source_id) if kind=="credit" else None,
            "amount_fcfa":2500,"before_unallocated_fcfa":5000,"after_unallocated_fcfa":2500,
            "occurred_at":occurred.isoformat(),"allocations":rows}
        return SimpleNamespace(operation_type="customer_allocation",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,"original_request":request,"allocation":snapshot})


class RecoveryAllocationSnapshotTests(SimpleTestCase,AllocationSources):
    def setUp(self):
        self.customer=SimpleNamespace(id=uuid.uuid4());self.payment=SimpleNamespace(id=uuid.uuid4());self.credit=SimpleNamespace(id=uuid.uuid4())
        self.debts=[SimpleNamespace(id=uuid.uuid4()) for _ in range(2)]

    def test_exact_allocation_source_order_totals_and_identity_are_validated(self):
        from backups.services.recovery_customer_allocations import decode_customer_allocation,RecoveryAllocationSnapshotInvalid
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);decoded=decode_customer_allocation(source)
                self.assertEqual(decoded.source_kind,kind);self.assertEqual(decoded.snapshot["amount_fcfa"],2500)
                for case in ("amount","after","rows","order","id","duplicate","bool","method","origin","both","time"):
                    with self.subTest(case=case):
                        changed=deepcopy(source);snapshot=changed.payload["allocation"];request=changed.payload["original_request"]
                        if case=="amount":snapshot["amount_fcfa"]=2499
                        elif case=="after":snapshot["after_unallocated_fcfa"]=2499
                        elif case=="rows":snapshot["allocations"][0]["amount_fcfa"]=999
                        elif case=="order":snapshot["allocations"].reverse()
                        elif case=="id":request["id"]=str(uuid.uuid4())
                        elif case=="duplicate":request["allocations"][1]["id"]=request["allocations"][0]["id"];snapshot["allocations"]=deepcopy(request["allocations"])
                        elif case=="bool":snapshot["amount_fcfa"]=True
                        elif case=="method":snapshot["method"]="cash"
                        elif case=="origin":request["installation_id"]=str(uuid.uuid4())
                        elif case=="both":snapshot.update(payment_id=str(self.payment.id),credit_id=str(self.credit.id))
                        else:snapshot["occurred_at"]="2026-10-24T12:00:00Z"
                        with self.assertRaises(RecoveryAllocationSnapshotInvalid):decode_customer_allocation(changed)


class RecoveryCustomerAllocationTests(TestCase,AllocationSources):
    authenticate=fixtures.RecoveryCustomerRefundTests.authenticate
    generation=fixtures.RecoveryCustomerRefundTests.generation
    record=fixtures.RecoveryCustomerRefundTests.record
    device=fixtures.RecoveryCustomerRefundTests.device
    review=fixtures.RecoveryCustomerRefundTests.review
    payload=fixtures.RecoveryCustomerRefundTests.payload
    url=fixtures.RecoveryCustomerRefundTests.url
    capture=fixtures.RecoveryCustomerRefundTests.capture
    preview=fixtures.RecoveryCustomerRefundTests.preview
    apply=fixtures.RecoveryCustomerRefundTests.apply
    native_body=fixtures.RecoveryCustomerRefundTests.native_body
    native_url=fixtures.RecoveryCustomerRefundTests.native_url

    def setUp(self):
        fixtures.RecoveryCustomerRefundTests.setUp(self)
        self.debts=[]
        for amount in (3000,4000):
            event=Activity.objects.create(shop=self.a,actor=self.owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Dette connue avant affectation")
            self.debts.append(CustomerDebt.objects.create(id=event.target_id,shop=self.a,customer=self.customer,
                actor=self.owner,installation=self.live.installation,operation=event,amount_fcfa=amount,remaining_fcfa=amount,
                occurred_at=datetime(2026,10,22,9,tzinfo=timezone.utc)))

    def test_both_credit_sources_allocate_only_the_chosen_debts_once_without_cash(self):
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);capture=self.capture(source);preview=self.preview(capture)
                self.assertTrue(preview["can_apply"]);self.assertEqual(preview["cash_delta_by_method_fcfa"],{})
                response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
                receipt=response.data["business_receipt"]
                self.assertEqual((receipt["kind"],receipt["amount_fcfa"],receipt["after_unallocated_fcfa"]),("allocation",2500,2500))
                self.assertEqual(receipt["actor_id"],str(source.origin_user_id))
                self.assertEqual(receipt["allocations"],source.payload["allocation"]["allocations"])
                retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
                self.assertEqual((retry.status_code,retry.data),(200,response.data))
                known=self.preview(capture);self.assertEqual(known["outcome"],"already_known")
                ack,_=self.apply(capture,known);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,receipt))
                entry=CustomerLedgerEntry.objects.get(operation_id=source.operation_id)
                self.assertEqual((entry.delta_fcfa,entry.debt_delta_fcfa,entry.credit_delta_fcfa,entry.occurred_at),(0,-2500,-2500,source.occurred_at))
        self.payment.refresh_from_db();self.credit.refresh_from_db()
        self.assertEqual((self.payment.unallocated_fcfa,self.credit.remaining_fcfa),(2500,2500))
        self.assertEqual(list(CustomerDebt.objects.filter(id__in=[row.id for row in self.debts]).order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[1000,1000])
        self.assertEqual((CashMovement.objects.count(),PaymentRegularization.objects.count(),RegularizationAllocation.objects.count()),(0,2,4))

    def test_changed_credit_or_debt_requires_new_review_and_does_not_partially_allocate(self):
        source=self.source();capture=self.capture(source);old=self.preview(capture)
        CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=3000)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        old=self.preview(capture)
        CustomerDebt.objects.filter(id=self.debts[1].id).update(remaining_fcfa=1499)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        fresh=self.preview(capture);self.assertFalse(fresh["can_apply"])
        self.assertIn("allocation_exceeds_debt",{row["code"] for row in fresh["issues"]})
        self.assertEqual(self.apply(capture,fresh)[0].status_code,409)
        self.debts[0].refresh_from_db();self.assertEqual(self.debts[0].remaining_fcfa,3000)
        self.assertEqual((PaymentRegularization.objects.count(),CashMovement.objects.count()),(0,0))

    def test_repeated_debt_rows_are_aggregated_without_overdraft_or_invented_automatic_selection(self):
        source=self.source()
        for payload in (source.payload["original_request"],source.payload["allocation"]):
            payload["allocations"][1]["debt_id"]=str(self.debts[0].id)
        capture=self.capture(source);CustomerDebt.objects.filter(id=self.debts[0].id).update(remaining_fcfa=2499)
        self.assertFalse(self.preview(capture)["can_apply"])
        CustomerDebt.objects.filter(id=self.debts[0].id).update(remaining_fcfa=2500)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.debts[0].refresh_from_db();self.debts[1].refresh_from_db()
        self.assertEqual((self.debts[0].remaining_fcfa,self.debts[1].remaining_fcfa),(0,4000))
        self.assertEqual((RegularizationAllocation.objects.count(),CashMovement.objects.count()),(2,0))

    def test_late_decision_error_rolls_back_every_debt_source_ledger_and_origin(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_allocation_decision BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected allocation failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_allocation_decision")
        self.payment.refresh_from_db();self.assertEqual(self.payment.unallocated_fcfa,5000)
        self.assertEqual(list(CustomerDebt.objects.order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[3000,4000])
        self.assertEqual((PaymentRegularization.objects.count(),RegularizationAllocation.objects.count(),CustomerLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(0,0,0,0))

    def test_native_allocations_are_known_after_refunds_and_later_debt_settlement(self):
        for kind in ("payment","credit"):
            with self.subTest(kind=kind):
                source=self.source(kind);source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
                source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
                native=self.client.post(self.native_url(kind)+"allocate/",self.native_body(source),format="json")
                self.assertEqual(native.status_code,201,native.content)
                source.occurred_at=datetime.fromisoformat(native.data["occurred_at"].replace("Z","+00:00"))
                source.payload["allocation"]["occurred_at"]=source.occurred_at.isoformat()
                refund={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"reason":"Remboursement ultérieur réel","amount_fcfa":500,"method":"cash"}
                later=self.client.post(self.native_url(kind)+"refund/",refund,format="json");self.assertEqual(later.status_code,201,later.content)
                capture=self.capture(source);preview=self.preview(capture)
                self.assertEqual(preview["outcome"],"already_known");self.assertTrue(preview["origin_installation_verified"])
                ack,_=self.apply(capture,preview);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,native.data))
        CustomerDebt.objects.filter(id=self.debts[0].id).update(remaining_fcfa=0)
        preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        ack,_=self.apply(capture,preview);self.assertEqual(ack.status_code,201,ack.content)
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[-500,-500])
        self.assertEqual((RegularizationAllocation.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(4,0))

    def test_adjusted_acknowledgement_survives_next_generation_and_an_actual_later_collection(self):
        source=self.source();CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=2500)
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        first=response.data["business_receipt"];self.assertEqual(first["after_unallocated_fcfa"],0)
        collection={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "parts":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":1500}],
            "allocations":[{"id":str(uuid.uuid4()),"debt_id":str(self.debts[0].id),"amount_fcfa":1500}]}
        collected=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",collection,format="json")
        self.assertEqual(collected.status_code,201,collected.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        ack,_=self.apply(capture,preview);self.assertEqual((ack.status_code,ack.data["business_receipt"]),(201,first))
        self.debts[0].refresh_from_db();self.payment.refresh_from_db()
        self.assertEqual((self.debts[0].remaining_fcfa,self.payment.unallocated_fcfa),(500,0))
        self.assertEqual((CashMovement.objects.get().amount_fcfa,CustomerPayment.objects.count(),RegularizationAllocation.objects.count()),(1500,2,2))

    def test_insufficient_credit_missing_dependency_and_foreign_debt_stay_blocked_and_private(self):
        from customers.models import Customer
        other=Customer.objects.create(shop=self.b,name="Identité B confidentielle")
        event=Activity.objects.create(shop=self.b,actor=self.owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Dette privée B")
        foreign=CustomerDebt.objects.create(id=event.target_id,shop=self.b,customer=other,actor=self.owner,
            installation=self.live.installation,operation=event,amount_fcfa=999999,remaining_fcfa=999999,
            occurred_at=datetime(2026,10,22,9,tzinfo=timezone.utc))
        source=self.source()
        for payload in (source.payload["original_request"],source.payload["allocation"]):payload["allocations"][1]["debt_id"]=str(foreign.id)
        capture=self.capture(source,[uuid.uuid4()]);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"]);self.assertIn("missing_debt",{row["code"] for row in preview["issues"]})
        self.assertIn("missing_dependencies",{row["code"] for row in preview["issues"]})
        foreign_impact=next(row for row in preview["debt_allocation_impacts"] if row["debt_id"]==str(foreign.id))
        self.assertIsNone(foreign_impact["remaining_before_fcfa"]);self.assertIsNone(foreign_impact["remaining_after_fcfa"])
        self.assertNotIn(other.name,str(preview));self.assertNotIn("999999",str(preview));self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        capture=self.capture(self.source());CustomerPayment.objects.filter(id=self.payment.id).update(unallocated_fcfa=2499)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("insufficient_allocation_credit",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual((PaymentRegularization.objects.count(),CashMovement.objects.count()),(0,0))

    def test_native_bypass_and_seller_or_withdrawn_current_rights_cannot_allocate(self):
        from identity.models import User,Membership
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username="allocation-recovery-seller");member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic("POST",self.url(f"operations/{capture.id}/integrate/"),b"invalid",content_type="application/json").status_code,403)
        native=self.client.post(self.native_url()+"allocate/",self.native_body(source),format="json");self.assertEqual(native.status_code,409,native.content)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/",shop=self.b)).status_code,404)
        preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Droit actuel retiré après contrôle")
        self.assertEqual(self.apply(capture,preview)[0].status_code,404)
        self.assertEqual((PaymentRegularization.objects.count(),CashMovement.objects.count()),(0,0))

    def test_source_proof_and_duplicate_allocation_uuid_conflicts_cannot_consume_twice(self):
        source=self.source();source.origin_installation_id=self.live.installation_id
        source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
        capture=self.capture(source);preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("origin_identity_conflict",{row["code"] for row in preview["issues"]})
        source=self.source();response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        collision=self.source()
        for payload in (collision.payload["original_request"],collision.payload["allocation"]):
            payload["allocations"][0]["id"]=source.payload["allocation"]["allocations"][0]["id"]
        preview=self.preview(self.capture(collision));self.assertFalse(preview["can_apply"])
        self.assertIn("entity_uuid_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual((PaymentRegularization.objects.count(),RegularizationAllocation.objects.count(),CashMovement.objects.count()),(1,2,0))

    def test_preview_and_receipt_match_openapi_and_invalid_snapshot_is_bilingual(self):
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
            self.assertEqual([(list(error.absolute_path),error.validator,error.message,
                [(list(child.absolute_path),child.message) for child in error.context]) for error in errors],[])
        capture=self.capture(self.source());preview=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(preview.status_code,200,preview.content);check(preview,"/integration-preview/")
        response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,"/integrate/","post")
        source=self.source();source.payload["allocation"]["amount_fcfa"]=2501;capture=self.capture(source)
        for language,detail in (("en","Recovery customer allocation snapshot is inconsistent."),("fr","Les données figées de l'affectation client récupérée sont incohérentes.")):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data["detail"])),(400,detail))

    def test_missing_sale_credit_is_recreated_before_allocation_without_another_collection(self):
        from backups.tests.test_recovery_sale_snapshot import RecoverySaleSnapshotTests
        from customers.models import CustomerCredit
        from sales.models import Sale
        from stock.models import StockMovement
        sale=RecoverySaleSnapshotTests().source();sale.operation_type="sale"
        for payload in (sale.payload["original_request"],sale.payload["sale"]):
            payload["customer_id"]=str(self.customer.id)
            payload["lines"][0]["product_id"]=str(self.product.id)
            payload["payments"][0].update(amount_fcfa=5500,received_fcfa=5500)
        credit_id=uuid.uuid5(uuid.UUID(sale.payload["sale"]["id"]),"customer-credit")
        allocation=self.source("credit")
        allocation.payload["original_request"]["credit_id"]=str(credit_id)
        allocation.payload["allocation"]["credit_id"]=str(credit_id)
        capture=self.capture(allocation,[sale.operation_id]);old=self.preview(capture)
        self.assertFalse(old["can_apply"])
        response,_=self.apply(self.capture(sale));self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(CustomerCredit.objects.get(id=credit_id).remaining_fcfa,5000)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(CustomerCredit.objects.get(id=credit_id).remaining_fcfa,2500)
        self.assertEqual(list(CustomerDebt.objects.order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[2000,2500])
        self.assertEqual((CashMovement.objects.count(),CashMovement.objects.get().amount_fcfa),(1,5500))
        self.assertEqual((Sale.objects.count(),StockMovement.objects.count(),RegularizationAllocation.objects.count()),(1,1,2))
        self.credit.refresh_from_db();self.payment.refresh_from_db()
        self.assertEqual((self.credit.remaining_fcfa,self.payment.unallocated_fcfa),(5000,5000))
