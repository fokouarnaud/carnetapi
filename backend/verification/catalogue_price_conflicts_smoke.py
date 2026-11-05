"""Exercise product price conflict decisions over real, private loopback HTTP.

Users and authenticated installations are explicit qualification fixtures. This
scenario neither qualifies OTP nor adds an activation path to the product.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import uuid
from wsgiref.simple_server import WSGIRequestHandler,make_server


class QuietHandler(WSGIRequestHandler):
    def log_message(self,*args):
        pass


@contextmanager
def private_installation():
    """Close all configured SQLite connections before deleting the fixture."""
    with TemporaryDirectory(prefix='catalogue-price-conflicts-') as directory:
        try:
            yield Path(directory)
        finally:
            from django.conf import settings
            if settings.configured:
                from django.db import connections
                connections.close_all()


def main():
    with private_installation() as root:
        database=root/'qualification.sqlite3'
        os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
        os.environ['APP_DB_PATH']=str(database)
        import django
        django.setup()
        from django.conf import settings
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.db.models import Sum
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from catalogue.models import Product,ProductPriceConflict,ProductPriceProposal,ProductPriceResolution
        from cash.models import CashMovement
        from common.models import Activity
        from identity.models import DeviceSession,Installation,Membership,Shop,User
        from sales.models import Sale,SaleLine
        from stock.models import StockMovement

        assert Path(str(settings.DATABASES['default']['NAME'])).resolve()==database.resolve(), 'A private database is required.'
        with open(os.devnull,'w') as sink:
            call_command('migrate',verbosity=0,stdout=sink)
        owner=User.objects.create_user(username='qualification-price-owner')
        manager=User.objects.create_user(username='qualification-price-manager')
        seller=User.objects.create_user(username='qualification-price-seller')
        shop=Shop.objects.create(owner=owner,name='Qualification prix A')
        foreign_shop=Shop.objects.create(owner=owner,name='Qualification prix B')
        Membership.objects.create(shop=shop,user=manager,role=Membership.Role.MANAGER)
        Membership.objects.create(shop=shop,user=seller)

        def fixture_session(user,label):
            installation=Installation.objects.create(user=user,label=label)
            session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
            token=AccessToken.for_user(user);token['session_id']=str(session.id)
            return {'token':str(token),'session':session,'installation':installation}

        device_a=fixture_session(owner,'Qualification installation A')
        device_b=fixture_session(manager,'Qualification installation B')
        seller_device=fixture_session(seller,'Qualification installation vendeur')
        connections.close_all()
        server=make_server('127.0.0.1',0,get_wsgi_application(),handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'

        def request(device,path,payload=None,method=None):
            body=None if payload is None else json.dumps(payload).encode('utf-8')
            incoming=Request(base+path,data=body,method=method,headers={
                'Authorization':'Bearer '+device['token'],'Content-Type':'application/json','Accept-Language':'fr'})
            try:
                response=urlopen(incoming,timeout=15)
            except HTTPError as error:
                response=error
            with response:
                return response.status,json.load(response)

        try:
            products_path=f'/api/v1/shops/{shop.id}/products/'
            product_id=str(uuid.uuid4())
            product_path=products_path+product_id+'/'
            opening={'id':product_id,'operation_id':str(uuid.uuid4()),'name':'Produit de qualification prix',
                'unit_kind':'piece','unit_label':'pièce','sale_price_fcfa':1000,'purchase_price_fcfa':600,
                'opening':{'quantity_milli':10000,'unit_cost_fcfa':'600.000000',
                    'occurred_at':'2026-01-01T00:00:00Z','reason':'Dix pièces physiques de qualification'}}
            status,created=request(device_a,products_path,opening)
            assert status==201 and created['version']==1 and created['sale_price_fcfa']==1000,(status,created)
            assert request(device_a,products_path,opening)==(200,created)
            assert int(created['stock_validated_milli'])==10000

            change_a={'operation_id':str(uuid.uuid4()),'base_version':1,'sale_price_fcfa':1100,
                'reason':'Tarif confirmé par installation A'}
            status,changed=request(device_a,product_path,change_a,method='PATCH')
            assert status==200 and changed['version']==2 and changed['sale_price_fcfa']==1100,(status,changed)
            assert request(device_a,product_path,change_a,method='PATCH')==(200,changed)
            sales_path=f'/api/v1/shops/{shop.id}/sales/'

            def sell_one(expected_price,expected_version):
                status,visible=request(seller_device,product_path)
                assert status==200 and visible['sale_price_fcfa']==expected_price and visible['version']==expected_version,(status,visible)
                assert 'purchase_price_fcfa' not in visible
                command={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
                    'lines':[{'id':str(uuid.uuid4()),'product_id':product_id,'product_version':visible['version'],'quantity_milli':1000}],
                    'payments':[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':expected_price}]}
                status,receipt=request(seller_device,sales_path,command)
                assert status==201 and receipt['net_total_fcfa']==expected_price,(status,receipt)
                assert receipt['lines'][0]['unit_price_fcfa']==expected_price
                assert request(seller_device,sales_path,command)==(200,receipt)
                return command,receipt

            first_sale,first_receipt=sell_one(1100,2)
            financial_before=(StockMovement.objects.count(),CashMovement.objects.count())
            conflicts_path=product_path+'price-conflicts/'
            proposal_b={'operation_id':str(uuid.uuid4()),'source_change_id':change_a['operation_id'],
                'base_version':1,'proposed_price_fcfa':1200,'reason':'Proposition concurrente installation B depuis version un'}
            status,recorded=request(device_b,conflicts_path,proposal_b)
            assert status==201 and recorded['state']=='open' and recorded['common_price_fcfa']==1000,(status,recorded)
            assert recorded['product_id']==product_id and recorded['source_change_id']==change_a['operation_id']
            assert recorded['base_version']==1 and recorded['product_version']==3
            assert len(recorded['proposals'])==2 and {row['proposed_price_fcfa'] for row in recorded['proposals']}=={1100,1200}
            assert request(device_b,conflicts_path,proposal_b)==(200,recorded)
            assert financial_before==(StockMovement.objects.count(),CashMovement.objects.count())
            selected=next(row for row in recorded['proposals'] if row['operation_id']==proposal_b['operation_id'])
            assert selected['proposed_price_fcfa']==1200 and selected['actor_id']==str(manager.id)
            assert selected['installation_id']==str(device_b['installation'].id)
            conflict_path=conflicts_path+recorded['id']+'/'
            status,listing=request(device_a,conflicts_path)
            assert status==200 and listing['count']==1 and listing['results'][0]==recorded,(status,listing)
            assert request(device_a,conflict_path)==(200,recorded)

            status,fallback=request(seller_device,product_path)
            assert status==200 and fallback['sale_price_fcfa']==1000 and fallback['version']==3,(status,fallback)
            assert 'purchase_price_fcfa' not in fallback
            assert fallback['price_conflict']=={'id':recorded['id'],'state':'open','common_price_fcfa':1000}
            status,public_list=request(seller_device,products_path)
            assert status==200 and public_list['count']==1 and public_list['results'][0]['sale_price_fcfa']==1000
            assert 'purchase_price_fcfa' not in public_list['results'][0]
            assert request(seller_device,sales_path+first_sale['id']+'/')==(200,first_receipt)
            second_sale,second_receipt=sell_one(1000,3)

            blocked_patch={'operation_id':str(uuid.uuid4()),'base_version':3,'sale_price_fcfa':1300,
                'reason':'Un prix en conflit exige la résolution dédiée'}
            patch_status,patch_error=request(device_a,product_path,blocked_patch,method='PATCH')
            assert patch_status==409,(patch_status,patch_error)
            assert Product.objects.get(id=product_id).sale_price_fcfa==1000
            status,preview=request(device_a,conflict_path+'preview/')
            assert status==200 and preview['can_resolve'] is True and preview['state']=='open',(status,preview)
            assert preview['proposals']==recorded['proposals'] and 64<len(preview['preview_token'])<=512
            resolution={'operation_id':str(uuid.uuid4()),'preview_token':preview['preview_token'],
                'proposal_id':selected['id'],'reason':'Responsable retient explicitement la proposition à 1200'}

            for suffix in ('','preview/'):
                denied_status,denied=request(seller_device,conflict_path+suffix)
                assert denied_status==403,(denied_status,denied)
            assert request(seller_device,conflicts_path)[0]==403
            assert request(seller_device,conflicts_path,proposal_b)[0]==403
            assert request(seller_device,conflict_path+'resolve/',resolution)[0]==403
            foreign_prefix=f'/api/v1/shops/{foreign_shop.id}/products/{product_id}/price-conflicts/'
            assert request(device_a,foreign_prefix)[0]==404
            assert request(device_a,foreign_prefix,proposal_b)[0]==404
            assert request(device_a,foreign_prefix+recorded['id']+'/')[0]==404
            assert request(device_a,foreign_prefix+recorded['id']+'/preview/')[0]==404
            assert request(device_a,foreign_prefix+recorded['id']+'/resolve/',resolution)[0]==404

            financial_before_resolution=(StockMovement.objects.count(),CashMovement.objects.count())
            status,resolved=request(device_a,conflict_path+'resolve/',resolution)
            assert status==200 and resolved['state']=='resolved' and resolved['product_version']==4,(status,resolved)
            assert resolved['resolution']['operation_id']==resolution['operation_id']
            assert resolved['resolution']['proposal_id']==selected['id'] and resolved['resolution']['price_fcfa']==1200
            assert resolved['resolution']['actor_id']==str(owner.id)
            assert request(device_a,conflict_path+'resolve/',resolution)==(200,resolved)
            assert request(device_a,conflict_path)==(200,resolved)
            assert request(device_b,conflicts_path,proposal_b)==(200,recorded)
            assert financial_before_resolution==(StockMovement.objects.count(),CashMovement.objects.count())
            status,final=request(seller_device,product_path)
            assert status==200 and final['sale_price_fcfa']==1200 and final['version']==4,(status,final)
            assert final['price_conflict'] is None and 'purchase_price_fcfa' not in final
            for command,receipt in ((first_sale,first_receipt),(second_sale,second_receipt)):
                assert request(seller_device,sales_path,command)==(200,receipt)
                assert request(seller_device,sales_path+command['id']+'/')==(200,receipt)
            assert Activity.objects.get(id=change_a['operation_id']).details['response']['sale_price_fcfa']==1100

            DeviceSession.objects.filter(id=device_b['session'].id).update(revoked_at=timezone.now())
            revoked_status,revoked=request(device_b,conflicts_path,proposal_b)
            assert revoked_status in (401,403),(revoked_status,revoked)
            assert request(device_b,conflict_path)[0] in (401,403)
            assert (ProductPriceConflict.objects.count(),ProductPriceProposal.objects.count(),ProductPriceResolution.objects.count())==(1,2,1)
            assert (Sale.objects.count(),SaleLine.objects.count(),CashMovement.objects.count(),StockMovement.objects.count())==(2,2,2,3)
            assert sorted(SaleLine.objects.values_list('unit_price_fcfa',flat=True))==[1000,1100]
            assert StockMovement.objects.aggregate(quantity=Sum('quantity_milli'))['quantity']==8000
            assert CashMovement.objects.aggregate(amount=Sum('amount_fcfa'))['amount']==2100
            assert int(final['stock_validated_milli'])==8000
            report={'catalogue_price_conflicts_wsgi':'passed','transport':'real_loopback_http','database':'disposable_sqlite',
                'fixture_authentication_only':True,'otp_exercised':False,'external_messages_sent':0,'shops':2,'installations':3,
                'initial_price_fcfa':1000,'native_change_price_fcfa':1100,'common_price_during_conflict_fcfa':1000,
                'historical_sale_price_fcfa':1100,'sale_during_conflict_price_fcfa':1000,'resolved_price_fcfa':1200,
                'product_versions':[1,2,3,4],'conflicts':1,'proposals':2,'resolutions':1,
                'initial_stock_milli':10000,'final_stock_milli':8000,'cash_movement_count':2,'cash_total_fcfa':2100,
                'record_retry_exact':True,'resolution_retry_exact':True,'sale_retries_preserve_historical_receipts':True,
                'seller_common_price_without_cost':True,'seller_conflict_access_status':403,'foreign_shop_status':404,
                'native_price_patch_during_conflict_status':patch_status,'revoked_session_status':revoked_status,
                'price_decisions_create_no_stock_or_cash':True,'source_change_receipt_unchanged':True,
                'verification_limits':['No Android client or ordinary offline synchronization qualified.','No OTP activation or external provider exercised.']}
            result_path=Path(__file__).resolve().parents[2]/'analysis'/'catalogue-price-conflicts-smoke-2026-10-29.json'
            result_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
            return report
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()


if __name__=='__main__':
    main()
