"""Physical intershop transfers conserve quantities, exact costs and current rights."""
from datetime import datetime, timedelta, timezone as utc
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.db import connection, IntegrityError, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from catalogue.models import Product
from cash.models import CashMovement
from common.models import Activity
from identity.models import User, Shop, Membership, Installation, DeviceSession
from stock.models import StockMovement
from stock.services.stock_queries import with_stock


class TransferApiTests(TestCase):
    def setUp(self):
        self.owner=User.objects.create_user(username="transfer-owner")
        self.other_owner=User.objects.create_user(username="unrelated-owner")
        self.source=Shop.objects.create(owner=self.owner,name="Source")
        self.destination=Shop.objects.create(owner=self.owner,name="Destination")
        self.foreign=Shop.objects.create(owner=self.other_owner,name="Autre propriétaire")
        self.source_manager=User.objects.create_user(username="source-responsible")
        self.destination_manager=User.objects.create_user(username="destination-responsible")
        Membership.objects.create(shop=self.source,user=self.source_manager,role="manager")
        Membership.objects.create(shop=self.destination,user=self.destination_manager,role="manager")
        self.product=self.product_at(self.source,"Origine")
        self.target=self.product_at(self.destination,"Destination une")
        self.target2=self.product_at(self.destination,"Destination deux")
        self.opening=Activity.objects.create(shop=self.source,actor=self.owner,action="opening_stock",
            target_id=self.product.id,reason="Ouverture")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=self.opening,kind="opening",
            quantity_milli=10000,unit_cost_micros=123456789,occurred_at=datetime(2026,10,20,tzinfo=utc.utc),reason="Ouverture")
        self.client=self.authenticate(self.source_manager)

    def product_at(self,shop,name,kind="piece",label="pièce"):
        return Product.objects.create(shop=shop,name=name,unit_kind=kind,unit_label=label,sale_price_fcfa=900)

    def authenticate(self,user):
        installation=Installation.objects.create(user=user)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(user);token["session_id"]=str(session.id)
        client=APIClient();client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def url(self,shop=None,suffix=""):
        return f"/api/v1/shops/{(shop or self.source).id}/transfers/"+suffix

    def shipment(self,**changes):
        return {"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"destination_shop_id":str(self.destination.id),
            "occurred_at":"2026-10-22T09:00:00Z","reason":"Départ physique confirmé","is_departure_confirmed":True,
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":1,"quantity_milli":5000}],**changes}

    def depart(self,data=None):
        data=data or self.shipment()
        response=self.client.post(self.url(),data,format="json")
        self.assertEqual(response.status_code,201,response.content)
        return response.data,data

    def arrival(self,transfer,product=None,quantity=3000,**changes):
        return {"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-22T10:00:00Z",
            "reason":"Réception physique vendable confirmée","is_sellable_confirmed":True,
            "lines":[{"id":str(uuid.uuid4()),"transfer_line_id":transfer["lines"][0]["id"],
                "product_id":str((product or self.target).id),"quantity_milli":quantity}],**changes}

    def stock(self,product):
        return with_stock(Product.objects.filter(id=product.id)).get().stock_validated_milli

    def test_departure_then_partial_receipts_can_choose_different_existing_products(self):
        transfer,command=self.depart()
        self.assertEqual(self.stock(self.product),"5000")
        self.assertEqual(self.stock(self.target),"0")
        self.assertEqual(transfer["lines"][0]["in_transit_milli"],"5000")
        self.assertEqual(transfer["lines"][0]["cost"]["total_cost_nanos_fcfa"],"617283945000")
        self.assertEqual(self.client.post(self.url(),command,format="json").data,transfer)
        self.client=self.authenticate(self.destination_manager)
        receipts=self.url(self.destination,transfer["id"]+"/receipts/")
        first=self.arrival(transfer)
        received=self.client.post(receipts,first,format="json")
        self.assertEqual(received.status_code,201,received.content)
        self.assertEqual(self.client.post(receipts,first,format="json").data,received.data)
        self.assertEqual((self.stock(self.target),self.stock(self.target2)),("3000","0"))
        current=self.client.get(self.url(self.destination,transfer["id"]+"/"))
        self.assertEqual(current.data["lines"][0]["in_transit_milli"],"2000")
        second=self.client.post(receipts,self.arrival(transfer,self.target2,2000),format="json")
        self.assertEqual(second.status_code,201,second.content)
        self.assertEqual((self.stock(self.product),self.stock(self.target),self.stock(self.target2)),("5000","3000","2000"))
        current=self.client.get(self.url(self.destination,transfer["id"]+"/"))
        self.assertEqual(current.data["lines"][0]["in_transit_milli"],"0")
        self.assertEqual(StockMovement.objects.count(),4)
        self.assertFalse(CashMovement.objects.exists())
        from sales.models import Sale
        from customers.models import CustomerDebt
        from purchasing.models import SupplierDebt
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CustomerDebt.objects.exists())
        self.assertFalse(SupplierDebt.objects.exists())
        self.assertEqual(self.client.get(receipts).data["count"],2)

    def test_same_owner_and_current_responsibles_are_required_without_granting_other_shop_access(self):
        denied=self.client.post(self.url(),self.shipment(destination_shop_id=str(self.foreign.id)),format="json")
        self.assertEqual(denied.status_code,404,denied.content)
        transfer,_=self.depart()
        path=self.url(self.destination,transfer["id"]+"/receipts/")
        self.assertEqual(self.client.post(path,self.arrival(transfer),format="json").status_code,404)
        self.client=self.authenticate(self.destination_manager)
        self.assertEqual(self.client.get(self.url(suffix=transfer["id"]+"/")).status_code,404)
        seller=User.objects.create_user(username="transfer-seller")
        Membership.objects.create(shop=self.destination,user=seller)
        self.client=self.authenticate(seller)
        self.assertEqual(self.client.get(self.url(self.destination)).status_code,403)
        self.assertEqual(self.client.post(path,self.arrival(transfer),format="json").status_code,403)

    def test_overreceipt_wrong_unit_or_product_and_unconfirmed_goods_have_no_stock_effect(self):
        transfer,_=self.depart()
        self.client=self.authenticate(self.destination_manager)
        path=self.url(self.destination,transfer["id"]+"/receipts/")
        wrong=self.product_at(self.destination,"Poids",kind="weight",label="kg")
        for data in (self.arrival(transfer,quantity=6000),self.arrival(transfer,wrong),
                     self.arrival(transfer,is_sellable_confirmed=False),self.arrival(transfer,self.product),
                     self.arrival(transfer,quantity=500)):
            with self.subTest(data=data):
                response=self.client.post(path,data,format="json")
                self.assertIn(response.status_code,(400,404,409),response.content)
                self.assertEqual((self.stock(self.product),self.stock(self.target)),("5000","0"))
                self.assertEqual(StockMovement.objects.count(),2)

    def test_receipts_keep_the_departure_cost_after_a_new_source_receipt_and_value_future_destination_sales(self):
        transfer,_=self.depart()
        event=Activity.objects.create(shop=self.source,actor=self.owner,action="opening_stock",target_id=self.product.id,reason="Autre coût")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=event,kind="opening",quantity_milli=10000,
            unit_cost_micros=900000000,occurred_at=datetime(2026,10,22,9,30,tzinfo=utc.utc),reason="Autre coût")
        self.client=self.authenticate(self.destination_manager)
        response=self.client.post(self.url(self.destination,transfer["id"]+"/receipts/"),self.arrival(transfer,quantity=5000),format="json")
        self.assertEqual(response.status_code,201,response.content)
        from stock.api.stock_valuations import current_valuation
        reading=current_valuation(self.target)
        self.assertEqual(reading.state.stock_value_nanos_fcfa,617283945000)
        self.assertEqual(reading.state.average_unit_cost_micros,123456789)

    def test_current_role_is_rechecked_before_a_historical_departure_receipt(self):
        transfer,command=self.depart()
        Membership.objects.filter(shop=self.source,user=self.source_manager).update(is_active=False)
        self.assertEqual(self.client.post(self.url(),command,format="json").status_code,404)
        self.assertEqual(StockMovement.objects.count(),2)

    def test_physical_transfer_movements_are_valid_domain_models(self):
        transfer,_=self.depart()
        self.client=self.authenticate(self.destination_manager)
        response=self.client.post(self.url(self.destination,transfer["id"]+"/receipts/"),self.arrival(transfer),format="json")
        self.assertEqual(response.status_code,201,response.content)
        for movement in StockMovement.objects.filter(kind__in=["transfer_out","transfer_in"]):
            movement.full_clean()

    def test_unknown_source_cost_remains_unknown_at_destination(self):
        StockMovement.objects.filter(operation=self.opening).update(unit_cost_micros=None)
        transfer,_=self.depart()
        self.assertEqual(transfer["lines"][0]["cost"]["cost_state"],"unknown")
        self.client=self.authenticate(self.destination_manager)
        response=self.client.post(self.url(self.destination,transfer["id"]+"/receipts/"),self.arrival(transfer),format="json")
        self.assertEqual(response.status_code,201,response.content)
        self.assertIsNone(response.data["lines"][0]["total_cost_nanos_fcfa"])
        from stock.api.stock_valuations import current_valuation
        self.assertIsNone(current_valuation(self.target).state.stock_value_nanos_fcfa)

    def test_fractional_source_cost_is_shared_by_cumulative_rounding_without_lost_nanos(self):
        StockMovement.objects.filter(operation=self.opening).update(quantity_milli=2000,unit_cost_micros=0)
        other=Activity.objects.create(shop=self.source,actor=self.owner,action="opening_stock",target_id=self.product.id,reason="Coût fractionnaire")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=other,kind="opening",quantity_milli=1000,
            unit_cost_micros=1,occurred_at=datetime(2026,10,21,tzinfo=utc.utc),reason="Coût fractionnaire")
        data=self.shipment();data["lines"][0]["quantity_milli"]=3000
        transfer,_=self.depart(data)
        self.assertEqual(transfer["lines"][0]["cost"]["total_cost_nanos_fcfa"],"1000")
        self.client=self.authenticate(self.destination_manager)
        allocated=[]
        for product in (self.target,self.target2,self.target):
            response=self.client.post(self.url(self.destination,transfer["id"]+"/receipts/"),self.arrival(transfer,product,1000),format="json")
            self.assertEqual(response.status_code,201,response.content)
            allocated.append(response.data["lines"][0]["total_cost_nanos_fcfa"])
        self.assertEqual(allocated,["333","334","333"])
        from stock.api.stock_valuations import current_valuation
        self.assertEqual(current_valuation(self.target).state.stock_value_nanos_fcfa,666)
        self.assertEqual(current_valuation(self.target2).state.stock_value_nanos_fcfa,334)

    def test_late_transaction_failure_rolls_back_receipt_movement_and_remaining_quantity(self):
        transfer,_=self.depart()
        self.client=self.authenticate(self.destination_manager)
        path=self.url(self.destination,transfer["id"]+"/receipts/")
        before=Activity.objects.count()
        data=self.arrival(transfer)
        with patch("stock.services.transfers.observe_stock",side_effect=RuntimeError("receipt interrupted")):
            with self.assertRaisesMessage(RuntimeError,"receipt interrupted"):
                self.client.post(path,data,format="json")
        self.assertEqual(Activity.objects.count(),before)
        self.assertEqual(StockMovement.objects.count(),2)
        self.assertEqual(self.stock(self.target),"0")
        current=self.client.get(self.url(self.destination,transfer["id"]+"/"))
        self.assertEqual(current.data["lines"][0]["in_transit_milli"],"5000")
        self.assertEqual(self.client.post(path,data,format="json").status_code,201)

    def test_listing_uses_sql_pagination_without_queries_per_line_or_cost(self):
        for _ in range(3):
            data=self.shipment();data["lines"][0]["quantity_milli"]=1000
            self.depart(data)
        with CaptureQueriesContext(connection) as small:
            first=self.client.get(self.url(),{"page_size":1})
        with CaptureQueriesContext(connection) as bigger:
            second=self.client.get(self.url(),{"page_size":3})
        self.assertEqual((first.status_code,second.status_code),(200,200))
        self.assertEqual((first.data["count"],len(first.data["results"])),(3,1))
        self.assertEqual(len(second.data["results"]),3)
        self.assertEqual(len(small),len(bigger))
        self.assertTrue(any('FROM "stock_stocktransfer"' in row["sql"] and "LIMIT 1" in row["sql"] for row in small))

    def test_receipt_storage_refuses_fractional_money_quantities_and_false_unknown_cost(self):
        transfer,_=self.depart()
        self.client=self.authenticate(self.destination_manager)
        response=self.client.post(self.url(self.destination,transfer["id"]+"/receipts/"),self.arrival(transfer),format="json")
        self.assertEqual(response.status_code,201,response.content)
        identity=response.data["lines"][0]["id"].replace("-","")
        for field,value in (("quantity_milli",1000.5),("quantity_milli",500),("received_before_milli",0.5),
                            ("total_cost_nanos_fcfa","1.5"),("total_cost_nanos_fcfa","-1"),("total_cost_nanos_fcfa",None),
                            ("cost_state","unknown")):
            with self.subTest(field=field,value=value),self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
                cursor.execute(f"UPDATE stock_stocktransferreceiptline SET {connection.ops.quote_name(field)}=%s WHERE id=%s",[value,identity])

    def test_actual_transfer_and_receipt_responses_match_the_published_schema(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        def check(response,suffix,method="get"):
            path=next(path for path in schema["paths"] if path.endswith(suffix))
            definition=schema["paths"][path][method]["responses"][str(response.status_code)]
            rule=nullable({**definition["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        command=self.shipment()
        response=self.client.post(self.url(),command,format="json");check(response,"/transfers/","post")
        transfer=response.data
        check(self.client.post(self.url(),command,format="json"),"/transfers/","post")
        check(self.client.get(self.url()),"/transfers/")
        check(self.client.get(self.url(suffix=transfer["id"]+"/")),"/transfers/{id}/")
        self.client=self.authenticate(self.destination_manager)
        path=self.url(self.destination,transfer["id"]+"/receipts/")
        command=self.arrival(transfer)
        check(self.client.post(path,command,format="json"),"/transfers/{id}/receipts/","post")
        check(self.client.get(path),"/transfers/{id}/receipts/")
        check(self.client.post(path,{},format="json"),"/transfers/{id}/receipts/","post")

    def test_a_new_transfer_cannot_take_the_uuid_of_an_archived_captured_operation(self):
        from backups.models import RestoreAccessGeneration, RestoreSession, RecoveryJournalDeposit, RecoveryJournalOperation
        generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),
            initiated_by=self.owner.id,current_slot=None)
        session=RestoreSession.objects.create(generation=generation)
        device=DeviceSession.objects.get(installation__user=self.source_manager)
        deposit=RecoveryJournalDeposit.objects.create(session=session,shop=self.source,operation_id=uuid.uuid4(),
            submitted_by=self.source_manager,submitted_installation=device.installation,reason="Copie historique",format_version=1,
            content_sha256="a"*64,receipt={})
        capture=RecoveryJournalOperation.objects.create(session=session,shop=self.source,first_deposit=deposit,
            operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),operation_type="sale",
            occurred_at=timezone.now(),origin_recorded_at=timezone.now(),payload={},content_sha256="b"*64)
        response=self.client.post(self.url(),self.shipment(operation_id=str(capture.operation_id)),format="json")
        self.assertEqual(response.status_code,409,response.content)
        self.assertEqual(self.stock(self.product),"10000")
        self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())

    def test_transfer_commands_refuse_form_uploads_before_their_body_is_parsed(self):
        response=self.client.post(self.url(),{"id":str(uuid.uuid4())},format="multipart")
        self.assertEqual(response.status_code,415,response.content)
        self.assertEqual(StockMovement.objects.count(),1)
