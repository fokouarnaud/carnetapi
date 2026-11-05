"""Exercise real HTTP transfer departure and partial receipts on private SQLite.

Provides a fixture session; does not qualify OTP, Android, hosting or transit loss.
"""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone as utc
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import uuid
from wsgiref.simple_server import make_server,WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):pass


def main():
    with TemporaryDirectory(prefix="transfer-http-") as directory,ExitStack() as resources:
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(Path(directory)/"private.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from catalogue.models import Product
        from cash.models import CashMovement
        from common.models import Activity
        from identity.models import User,Shop,Installation,DeviceSession
        from stock.models import StockMovement
        from stock.services.stock_queries import with_stock
        from stock.api.stock_valuations import current_valuation
        resources.callback(connections.close_all)
        call_command("migrate",verbosity=0)
        owner=User.objects.create_user(username="private-transfer-owner")
        source=Shop.objects.create(owner=owner,name="Source")
        destination=Shop.objects.create(owner=owner,name="Destination")
        product=Product.objects.create(shop=source,name="Source",unit_kind="piece",unit_label="pièce",sale_price_fcfa=900)
        targets=[Product.objects.create(shop=destination,name=name,unit_kind="piece",unit_label="pièce",sale_price_fcfa=1200)
            for name in ("Première fiche","Seconde fiche")]
        event=Activity.objects.create(shop=source,actor=owner,action="opening_stock",target_id=product.id,reason="Fixture privée")
        StockMovement.objects.create(product=product,actor=owner,operation=event,kind="opening",quantity_milli=10000,
            unit_cost_micros=123456789,occurred_at=datetime(2026,10,20,tzinfo=utc.utc),reason="Fixture privée")
        installation=Installation.objects.create(user=owner)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(owner);token["session_id"]=str(session.id)
        server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f"http://127.0.0.1:{server.server_port}/api/v1/shops/"
        try:
            def http(path,data=None):
                body=None if data is None else json.dumps(data).encode()
                request=Request(root+path,data=body,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
                try:
                    with urlopen(request,timeout=60) as response:return response.status,json.load(response)
                except HTTPError as error:
                    with error:return error.code,json.load(error)
            prefix=f"{source.id}/transfers/"
            command={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"destination_shop_id":str(destination.id),
                "occurred_at":"2026-10-22T09:00:00Z","reason":"Départ physique","is_departure_confirmed":True,
                "lines":[{"id":str(uuid.uuid4()),"product_id":str(product.id),"product_version":1,"quantity_milli":5000}]}
            status,transfer=http(prefix,command)
            assert status==201,(status,transfer)
            assert transfer["lines"][0]["in_transit_milli"]=="5000"
            assert transfer["lines"][0]["cost"]["total_cost_nanos_fcfa"]=="617283945000"
            assert http(prefix,command)==(200,transfer)
            receipts=f"{destination.id}/transfers/{transfer['id']}/receipts/"
            costs=[]
            for target,quantity,remaining in ((targets[0],3000,"2000"),(targets[1],2000,"0")):
                arrival={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-22T10:00:00Z",
                    "reason":"Réception physique vendable","is_sellable_confirmed":True,
                    "lines":[{"id":str(uuid.uuid4()),"transfer_line_id":transfer["lines"][0]["id"],
                        "product_id":str(target.id),"quantity_milli":quantity}]}
                status,receipt=http(receipts,arrival)
                assert status==201,(status,receipt)
                assert http(receipts,arrival)==(200,receipt)
                costs.append(receipt["lines"][0]["total_cost_nanos_fcfa"])
                status,current=http(prefix+transfer["id"]+"/")
                assert status==200 and current["lines"][0]["in_transit_milli"]==remaining
            assert costs==["370370367000","246913578000"],costs
            assert [with_stock(Product.objects.filter(id=item.id)).get().stock_validated_milli for item in [product,*targets]]==["5000","3000","2000"]
            assert current_valuation(targets[0]).state.stock_value_nanos_fcfa==370370367000
            assert http(prefix,command)==(200,transfer)
            assert http(receipts)[1]["count"]==2
            assert StockMovement.objects.count()==4 and CashMovement.objects.count()==0
            session.revoked_at=timezone.now();session.save(update_fields=["revoked_at"])
            assert http(prefix,command)[0]==401
            print(json.dumps({"http":"passed","source_milli":5000,"destination_milli":[3000,2000],"in_transit_milli":0,
                "allocated_cost_nanos":costs,"movements":4,"cash_movements":0,"revoked_session":401}))
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()


if __name__=="__main__":main()
