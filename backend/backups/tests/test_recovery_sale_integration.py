"""Approved recovered sales keep frozen facts and never replay an effect twice."""
from copy import deepcopy
from datetime import datetime,timezone as utc
import uuid
import json

from django.test import TestCase
from django.db import connection,IntegrityError

from backups import models as backups
from backups.tests import test_recovery_journal_api as fixtures
from backups.tests import test_recovery_sale_snapshot as snapshot_fixtures
from cash.models import CashMovement
from catalogue.models import Product,ProductPack
from common.models import Activity
from customers.models import Customer,CustomerDebt
from identity.models import User,Membership,DeviceSession
from sales.models import Sale,SaleLine
from stock.models import StockMovement,StockAlertEvent
from stock.services.stock_queries import with_stock


class RecoverySaleIntegrationTests(TestCase):
    authenticate=fixtures.RecoveryJournalApiTests.authenticate
    generation=fixtures.RecoveryJournalApiTests.generation
    record=fixtures.RecoveryJournalApiTests.record
    device=fixtures.RecoveryJournalApiTests.device
    review=fixtures.RecoveryJournalApiTests.review
    payload=fixtures.RecoveryJournalApiTests.payload
    url=fixtures.RecoveryJournalApiTests.url

    def setUp(self):
        fixtures.RecoveryJournalApiTests.setUp(self)
        self.product.sale_price_fcfa=900
        self.product.version=8
        self.product.stock_threshold_milli=2000
        self.product.save()
        opening=Activity.objects.create(shop=self.a,actor=self.owner,action="opening_stock",target_id=self.product.id,reason="Stock initial")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=opening,kind="opening",
            quantity_milli=3000,unit_cost_micros=300000000,occurred_at=datetime(2026,10,20,9,tzinfo=utc.utc),reason="Stock initial")

    def source(self):
        source=snapshot_fixtures.RecoverySaleSnapshotTests().source()
        source.payload["sale"]["lines"][0]["product_id"]=str(self.product.id)
        source.payload["original_request"]["lines"][0]["product_id"]=str(self.product.id)
        return source

    def capture(self,source,**changes):
        data=self.payload()
        data["operations"]=[{**data["operations"][0],"operation_id":str(source.operation_id),
            "origin_user_id":str(source.origin_user_id),"origin_installation_id":str(source.origin_installation_id),
            "payload":source.payload,"occurred_at":source.occurred_at.isoformat(),**changes}]
        response=self.client.post(self.url(),data,format="json")
        self.assertEqual(response.status_code,201,response.content)
        return backups.RecoveryJournalOperation.objects.get(id=response.data["operations"][0]["capture_id"])

    def preview(self,capture):
        response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code,200,response.content)
        return response.data

    def apply(self,capture,preview=None,**changes):
        preview=preview or self.preview(capture)
        data={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],"preview_token":preview["preview_token"],
            "reason":"Faits et copie d’origine contrôlés",**changes}
        return self.client.post(self.url(f"operations/{capture.id}/integrate/"),data,format="json"),data

    def test_current_responsible_applies_frozen_sale_without_repricing_or_creating_origin_accounts(self):
        source=self.source()
        capture=self.capture(source)
        users=User.objects.count()
        preview=self.preview(capture)
        self.assertTrue(preview["can_apply"])
        self.assertEqual(preview["outcome"],"new")
        self.assertEqual(preview["snapshot"]["net_total_fcfa"],500)
        self.assertEqual(Sale.objects.count(),0)
        response,command=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["outcome"],"applied")
        sale=Sale.objects.get()
        self.assertEqual(sale.net_total_fcfa,500)
        self.assertEqual(sale.occurred_at,capture.occurred_at)
        self.assertEqual(sale.lines.get().product_version,7)
        self.assertEqual(sale.lines.get().unit_price_fcfa,500)
        self.assertEqual(with_stock(type(self.product).objects.filter(id=self.product.id)).get().stock_available_milli,"2000")
        self.assertEqual(list(CashMovement.objects.values_list("amount_fcfa",flat=True)),[500])
        self.assertEqual(User.objects.count(),users)
        self.assertFalse(User.objects.filter(id=source.origin_user_id).exists())
        business=response.data["business_receipt"]
        self.assertEqual(business["actor_id"],str(source.origin_user_id))
        self.assertEqual(business["installation_id"],str(source.origin_installation_id))
        current=self.client.get(f"/api/v1/shops/{self.a.id}/sales/{sale.id}/")
        self.assertEqual(current.status_code,200,current.content)
        self.assertEqual(current.data["actor_id"],str(source.origin_user_id))
        cash=self.client.get(f"/api/v1/shops/{self.a.id}/cash/")
        self.assertEqual(cash.data["results"][0]["actor_id"],str(source.origin_user_id))
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/cash/?scope=personal").data["count"],0)
        self.assertEqual(StockAlertEvent.objects.count(),1)
        repeated=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual(repeated.status_code,200,repeated.content)
        self.assertEqual(repeated.data,response.data)
        known=self.preview(capture)
        self.assertEqual(known["outcome"],"already_known")
        again,_=self.apply(capture,known)
        self.assertEqual(again.status_code,201,again.content)
        self.assertEqual(again.data["outcome"],"already_known")
        self.assertEqual((Sale.objects.count(),SaleLine.objects.count(),CashMovement.objects.count()),(1,1,1))
        self.assertEqual(StockAlertEvent.objects.count(),1)

    def test_frozen_carton_consumes_24_units_even_after_its_live_factor_and_price_change(self):
        source=self.source()
        pack=ProductPack.objects.create(product=self.product,name="Carton nouveau",base_quantity_milli=30000,sale_price_fcfa=9900)
        frozen=source.payload["sale"]
        frozen["lines"][0].update(pack_id=str(pack.id),unit_label="Carton historique",factor_milli=24000,
            base_quantity_milli=24000,unit_price_fcfa=4800,reference_fcfa=4800,net_fcfa=4800)
        source.payload["original_request"]["lines"][0]["pack_id"]=str(pack.id)
        frozen.update(reference_total_fcfa=4800,net_total_fcfa=4800)
        for payment in (frozen["payments"][0],source.payload["original_request"]["payments"][0]):payment.update(amount_fcfa=4800,received_fcfa=4800)
        capture=self.capture(source)
        response,_=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        line=SaleLine.objects.get()
        self.assertEqual((line.factor_milli,line.base_quantity_milli,line.unit_price_fcfa),(24000,24000,4800))
        self.assertEqual(Sale.objects.get().net_total_fcfa,4800)
        self.assertEqual(StockMovement.objects.get(kind="sale").quantity_milli,-24000)
        self.assertTrue(Sale.objects.get().stock_requires_regularization)

    def test_credit_payments_and_customer_statement_keep_origin_and_create_only_the_unpaid_debt(self):
        customer=Customer.objects.create(shop=self.a,name="Client retrouvé")
        source=self.source()
        request=source.payload["original_request"]
        snapshot=source.payload["sale"]
        request.update(mode="credit",customer_id=str(customer.id),due_date="2026-10-31")
        snapshot.update(mode="credit",customer_id=str(customer.id),due_at="2026-10-30T23:00:00Z")
        for payment in (request["payments"][0],snapshot["payments"][0]):payment.update(amount_fcfa=200,received_fcfa=200)
        capture=self.capture(source)
        response,_=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        debt=CustomerDebt.objects.get()
        self.assertEqual((debt.amount_fcfa,debt.remaining_fcfa),(300,300))
        self.assertEqual(debt.id,uuid.uuid5(Sale.objects.get().id,"customer-debt"))
        self.assertEqual(CashMovement.objects.get().amount_fcfa,200)
        statement=self.client.get(f"/api/v1/shops/{self.a.id}/clients/{customer.id}/statement/")
        self.assertEqual(statement.status_code,200,statement.content)
        self.assertEqual(statement.data["closing_balance_fcfa"],300)
        self.assertEqual({row["actor_id"] for row in statement.data["results"]},{str(source.origin_user_id)})

    def test_changed_preview_tampered_capture_and_foreign_scope_cannot_write(self):
        capture=self.capture(self.source())
        stale=self.preview(capture)
        self.product.sale_price_fcfa=1000
        self.product.version+=1
        self.product.save()
        response,_=self.apply(capture,stale)
        self.assertEqual(response.status_code,409,response.content)
        self.assertEqual(Sale.objects.count(),0)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/",shop=self.b)).status_code,404)
        fresh=self.preview(capture)
        altered=deepcopy(capture.payload)
        altered["sale"]["product_note"]="changed without its stored digest"
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(payload=altered)
        response,_=self.apply(capture,fresh)
        self.assertEqual(response.status_code,409,response.content)
        self.assertEqual(Sale.objects.count(),0)

    def test_seller_cannot_approve_and_revoked_current_responsible_cannot_replay_a_decision(self):
        capture=self.capture(self.source())
        seller=User.objects.create_user(username="recovery-no-approve")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        denied=seller_api.generic("POST",self.url(f"operations/{capture.id}/integrate/"),b"not-json",content_type="application/json")
        self.assertEqual(denied.status_code,403,denied.content)
        response,command=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Contrôle retiré")
        denied=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual(denied.status_code,404,denied.content)
        self.assertEqual(Sale.objects.count(),1)

    def test_matching_native_sale_is_recognized_even_after_cancellation_without_replaying_its_effects(self):
        source=self.source()
        data=source.payload["original_request"]
        data.update(occurred_at=source.occurred_at.isoformat(),reason="Vente antérieure retrouvée")
        data["lines"][0]["product_version"]=8
        data["payments"][0].update(amount_fcfa=900,received_fcfa=900)
        first=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",data,format="json")
        self.assertEqual(first.status_code,201,first.content)
        operation=Activity.objects.get(id=source.operation_id)
        source.origin_user_id=self.owner.id
        source.origin_installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id
        source.payload["original_request"]=operation.details["request"]
        original=first.data
        frozen=source.payload["sale"]
        for key in ("id","mode","customer_id","reference_total_fcfa","discount_total_fcfa","net_total_fcfa"):
            frozen[key]=original[key]
        frozen["lines"]=[{key:row[key] for key in frozen["lines"][0]} for row in original["lines"]]
        frozen["payments"]=[{key:row[key] for key in frozen["payments"][0]} for row in original["payments"]]
        capture=self.capture(source)
        cancelled=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{original['id']}/cancellation/",
            {"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-23T09:00:00Z","reason":"Annulation confirmée"},format="json")
        self.assertEqual(cancelled.status_code,201,cancelled.content)
        before=[model.objects.count() for model in (Activity,Sale,SaleLine,StockMovement,CashMovement,StockAlertEvent)]
        preview=self.preview(capture)
        self.assertEqual(preview["outcome"],"already_known")
        self.assertEqual(preview["cash_delta_by_method_fcfa"],{})
        response,_=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],operation.details["response"])
        self.assertEqual([model.objects.count() for model in (Activity,Sale,SaleLine,StockMovement,CashMovement,StockAlertEvent)],before)
        current=self.client.get(f"/api/v1/shops/{self.a.id}/sales/{original['id']}/")
        self.assertEqual(current.data["state"],"cancelled")

    def test_missing_references_dependencies_and_foreign_operation_conflict_preserve_the_copy_without_writing(self):
        for problem in ("product","dependency","foreign_operation"):
            with self.subTest(problem=problem):
                source=self.source()
                changes={}
                if problem=="product":
                    product_id=str(uuid.uuid4())
                    source.payload["original_request"]["lines"][0]["product_id"]=product_id
                    source.payload["sale"]["lines"][0]["product_id"]=product_id
                if problem=="dependency":changes["dependencies"]=[str(uuid.uuid4())]
                if problem=="foreign_operation":
                    foreign=Activity.objects.create(id=source.operation_id,shop=self.b,actor=self.owner,
                        action="sale_created",target_id=uuid.uuid4(),details={"response":{"private":"B secret"}})
                capture=self.capture(source,**changes)
                preview=self.preview(capture)
                self.assertFalse(preview["can_apply"])
                expected={"product":"missing_product","dependency":"missing_dependencies","foreign_operation":"operation_conflict"}[problem]
                self.assertIn(expected,{row["code"] for row in preview["issues"]})
                if problem=="foreign_operation":
                    foreign.details={"response":{"private":"Another B secret"}}
                    foreign.save()
                    self.assertEqual(self.preview(capture)["preview_token"],preview["preview_token"])
                response,_=self.apply(capture,preview)
                self.assertEqual(response.status_code,409,response.content)
                self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(0,0,0))
                self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())

    def test_failure_before_decision_receipt_rolls_back_all_business_effects_and_retry_succeeds(self):
        capture=self.capture(self.source())
        preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_recovery_decision BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT, 'test decision failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_recovery_decision")
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),StockAlertEvent.objects.count()),(0,0,0))
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)
        self.assertEqual(StockMovement.objects.count(),1)
        self.assertEqual(self.preview(capture)["preview_token"],preview["preview_token"])
        response,_=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(1,1,1))

    def test_integration_denial_and_corrupt_sale_are_translated_in_french_and_english(self):
        seller=User.objects.create_user(username="integration-language-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        source=self.source()
        source.payload["sale"]["net_total_fcfa"]=499
        capture=self.capture(source)
        for language,denial,invalid in (
            ("fr","Seul un responsable actuel de la boutique peut intégrer les opérations de récupération.","Les données figées de la vente récupérée sont incohérentes."),
            ("en","Only a current shop responsible can integrate recovery operations.","Recovery sale snapshot is inconsistent.")):
            with self.subTest(language=language):
                path=self.url(f"operations/{capture.id}/integration-preview/")
                response=seller_api.get(path,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,403,response.content)
                self.assertEqual(str(response.data["detail"]),denial)
                response=self.client.get(path,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,400,response.content)
                self.assertEqual(str(response.data["detail"]),invalid)

    def test_revalidated_seller_reads_own_recovered_cash_but_cannot_approve_it(self):
        seller=User.objects.create_user(username="integration-source-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        source=self.source()
        source.origin_user_id=seller.id
        source.origin_installation_id=DeviceSession.objects.get(installation__user=seller).installation_id
        capture=self.capture(source)
        response,_=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        cash=seller_api.get(f"/api/v1/shops/{self.a.id}/cash/?scope=personal")
        self.assertEqual(cash.status_code,200,cash.content)
        self.assertEqual(cash.data["count"],1)
        self.assertEqual(cash.data["results"][0]["actor_id"],str(seller.id))
        self.assertEqual(cash.data["results"][0]["amount_fcfa"],500)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/cash/?scope=personal").data["count"],0)
        self.assertEqual(seller_api.get(self.url(f"operations/{capture.id}/integration-preview/")).status_code,403)

    def test_real_integration_previews_receipts_and_errors_match_openapi(self):
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
            document=schema["paths"][path][method]["responses"][str(response.status_code)]
            rule=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        capture=self.capture(self.source())
        path=self.url(f"operations/{capture.id}/integration-preview/")
        preview_response=self.client.get(path)
        check(preview_response,"/integration-preview/")
        response,command=self.apply(capture,preview_response.data)
        self.assertEqual(response.status_code,201,response.content)
        check(response,"/integrate/","post")
        apply_path=self.url(f"operations/{capture.id}/integrate/")
        check(self.client.post(apply_path,command,format="json"),"/integrate/","post")
        check(self.client.post(apply_path,{**command,"reason":"Changed"},format="json"),"/integrate/","post")
        check(self.client.post(apply_path,{},format="json"),"/integrate/","post")
        check(self.client.get(self.url(f"operations/{uuid.uuid4()}/integration-preview/")),"/integration-preview/")
        check(APIClient().get(path),"/integration-preview/")

    def test_a_foreign_product_reference_never_exposes_its_inventory_regularization(self):
        foreign=Product.objects.create(shop=self.b,name="Produit privé B",unit_kind="piece",unit_label="pièce",sale_price_fcfa=700)
        correction=Activity.objects.create(shop=self.b,actor=self.owner,action="inventory_count_adjusted",
            target_id=foreign.id,reason="Justification privée B")
        StockMovement.objects.create(product=foreign,actor=self.owner,operation=correction,kind="inventory_adjustment",
            quantity_milli=1000,occurred_at=datetime(2026,10,23,9,tzinfo=utc.utc),reason="Justification privée B")
        source=self.source()
        for row in (source.payload["original_request"]["lines"][0],source.payload["sale"]["lines"][0]):
            row["product_id"]=str(foreign.id)
        capture=self.capture(source)
        preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertEqual({row["code"] for row in preview["issues"]},{"missing_product"})
        self.assertNotIn(str(correction.id),json.dumps(preview))
        self.assertNotIn("Justification privée B",json.dumps(preview))

    def test_a_new_recovery_generation_recognizes_the_sale_from_the_previous_generation(self):
        source=self.source()
        initial=self.capture(source)
        first,_=self.apply(initial)
        self.assertEqual(first.status_code,201,first.content)
        before=[model.objects.count() for model in (Activity,Sale,SaleLine,StockMovement,CashMovement,StockAlertEvent,User)]
        self.current.current_slot=None
        self.current.save()
        self.current=self.generation()
        self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        old_path=self.url(f"operations/{initial.id}/integration-preview/")
        self.assertEqual(self.client.get(old_path).status_code,404)
        recovered=self.capture(source)
        self.assertNotEqual(initial.id,recovered.id)
        preview=self.preview(recovered)
        self.assertEqual(preview["outcome"],"already_known")
        acknowledged,_=self.apply(recovered,preview)
        self.assertEqual(acknowledged.status_code,201,acknowledged.content)
        self.assertEqual(acknowledged.data["business_receipt"],first.data["business_receipt"])
        self.assertEqual([model.objects.count() for model in (Activity,Sale,SaleLine,StockMovement,CashMovement,StockAlertEvent,User)],before)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),1)
        self.assertEqual(backups.RecoveryIntegrationDecision.objects.count(),2)
