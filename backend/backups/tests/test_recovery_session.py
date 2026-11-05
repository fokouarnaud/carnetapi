"""US-44: restoration access approval never means the data is recovered."""
from contextlib import closing
from datetime import datetime,timezone as utc_timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import uuid

from django.db import connection,transaction,IntegrityError
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from backups import models as backups
from backups.access_models import PERMISSION_FIELDS
from catalogue.models import Product
from identity.models import User,Shop,Installation,DeviceSession,Membership


class RecoverySessionTests(TransactionTestCase):
    def setUp(self):
        self.owner=User.objects.create_user(username="recovery-register-owner")
        self.technical=User.objects.create_superuser(username="recovery-register-technical",password=None)
        self.a,self.b=[Shop.objects.create(owner=self.owner,name=name) for name in ("A","B")]
        self.product=Product.objects.create(shop=self.a,name="Stock provisoire",unit_kind="piece",unit_label="pièce",
            sale_price_fcfa=500,stock_threshold_milli=1000)
        self.snapshot_at=datetime(2026,10,20,8,tzinfo=utc_timezone.utc)

    def generation(self):
        return backups.RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=self.technical.id)

    def status(self,shop):
        from backups.services.recovery_status import recovery_status_for_shop
        return recovery_status_for_shop(shop)

    def record(self,generation,**values):
        return backups.RestoreSession.objects.create(generation=generation,**values)

    def device(self,session,shop,**values):
        return backups.RecoveryDevice.objects.create(session=session,shop=shop,
            **{"origin_installation_id":uuid.uuid4(),"origin_user_id":uuid.uuid4(),"label":"Private phone",**values})

    def authenticate(self,user):
        install=Installation.objects.create(user=user)
        session=DeviceSession.objects.create(installation=install,expires_at=timezone.now()+timezone.timedelta(days=1))
        token=AccessToken.for_user(user)
        token["session_id"]=str(session.id)
        client=APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer "+str(token))
        return client

    def confirm(self,generation,shop,user,*,membership=None):
        review=backups.RestoreAccessReview.objects.create(generation=generation,shop=shop,user=user,
            archived_role="seller" if membership else "owner",membership=membership,
            was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=0,version=1,state="confirmed",reason="Contrôle actuel")

    def test_no_restore_and_missing_inventory_are_never_conflated_with_zero_missing_data(self):
        self.assertIsNone(self.status(self.a))
        generation=self.generation()
        unknown=self.status(self.a)
        self.assertEqual(unknown["generation_id"],str(generation.id))
        self.assertEqual(unknown["state"],"recovering")
        self.assertIsNone(unknown["session_id"])
        self.assertIsNone(unknown["known_devices"])
        session=self.record(generation)
        empty=self.status(self.a)
        self.assertEqual(empty["session_id"],str(session.id))
        self.assertIsNone(empty["known_devices"])
        self.assertIsNone(empty["snapshot_at"])
        session.inventory_recorded_at=timezone.now()
        session.save()
        observed=self.status(self.a)
        self.assertEqual(observed["known_devices"],0)
        self.assertEqual(observed["state"],"recovering")

    def test_device_observations_are_scoped_unique_and_do_not_create_or_authorize_accounts(self):
        generation=self.generation()
        session=self.record(generation,snapshot_at=self.snapshot_at,inventory_recorded_at=timezone.now())
        before=User.objects.count()
        device=self.device(session,self.a)
        self.device(session,self.b,origin_installation_id=device.origin_installation_id)
        self.device(session,self.b)
        self.assertEqual(self.status(self.a)["known_devices"],1)
        self.assertEqual(self.status(self.b)["known_devices"],2)
        self.assertEqual(self.status(self.a)["snapshot_at"],self.snapshot_at)
        self.assertEqual(User.objects.count(),before)
        self.assertFalse(User.objects.filter(id=device.origin_user_id).exists())
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.device(session,self.a,origin_installation_id=device.origin_installation_id)
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.record(generation)
        with self.assertRaises(ProtectedError):
            generation.delete()

    def test_new_restoration_does_not_reuse_an_old_device_inventory(self):
        first=self.generation()
        old=self.record(first,inventory_recorded_at=timezone.now())
        self.device(old,self.a)
        self.assertEqual(self.status(self.a)["known_devices"],1)
        first.current_slot=None
        first.save()
        following=self.generation()
        self.assertIsNone(self.status(self.a)["known_devices"])
        self.assertEqual(self.status(self.a)["generation_id"],str(following.id))
        self.assertEqual(backups.RecoveryDevice.objects.filter(session=old).count(),1)

    def test_catalogue_and_alerts_explain_recovery_without_exposing_devices_from_other_shops(self):
        generation=self.generation()
        self.confirm(generation,self.a,self.owner)
        backups.RestoreAccessReview.objects.create(generation=generation,shop=self.b,user=self.owner,
            archived_role="owner",was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        client=self.authenticate(self.owner)
        # A legacy generation has no new registry: inventory remains unknown.
        legacy=client.get(f"/api/v1/shops/{self.a.id}/products/")
        self.assertEqual(legacy.status_code,200)
        self.assertIn("restoration",legacy.data)
        self.assertIsNone(legacy.data["restoration"]["known_devices"])
        session=self.record(generation,snapshot_at=self.snapshot_at,inventory_recorded_at=timezone.now())
        private=self.device(session,self.a)
        for _ in range(3):
            self.device(session,self.b)
        for path in ("products","stock-alerts"):
            response=client.get(f"/api/v1/shops/{self.a.id}/{path}/")
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(response.data["restoration"]["known_devices"],1)
            self.assertEqual(response.data["restoration"]["state"],"recovering")
            self.assertEqual(response.data["count"],1)
            self.assertTrue(response.data["results"][0]["stock_provisional"])
            body=json.loads(response.content)
            self.assertNotIn(str(private.origin_installation_id),json.dumps(body))
            self.assertNotIn("Private phone",json.dumps(body))
        self.assertEqual(client.get(f"/api/v1/shops/{self.b.id}/products/").status_code,404)
        detail=client.get(f"/api/v1/shops/{self.a.id}/products/{self.product.id}/")
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.data["restoration"]["known_devices"],1)
        seller=User.objects.create_user(username="recovery-metadata-seller")
        membership=Membership.objects.create(shop=self.a,user=seller)
        self.confirm(generation,self.a,seller,membership=membership)
        seller_api=self.authenticate(seller)
        for suffix in ("",str(self.product.id)+"/"):
            response=seller_api.get(f"/api/v1/shops/{self.a.id}/products/{suffix}")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data["restoration"]["known_devices"],1)
            product=response.data["results"][0] if not suffix else response.data
            self.assertNotIn("purchase_price_fcfa",product)

    def test_quarantine_snapshots_revoked_and_current_devices_without_restoring_any_right(self):
        from backups.services.restore_quarantine import _suspend
        seller=User.objects.create_user(username="recovery-register-seller")
        Membership.objects.create(shop=self.a,user=seller,is_active=False)
        phone=Installation.objects.create(user=self.owner,label="Owner phone",revoked_at=timezone.now())
        seller_phone=Installation.objects.create(user=seller,label="Seller phone")
        with TemporaryDirectory(prefix="recovery-register-snapshot-") as directory:
            source=Path(directory)/"candidate.sqlite3"
            with closing(sqlite3.connect(source)) as db:
                connection.connection.backup(db)
            generation=uuid.uuid4()
            instant=timezone.now().replace(tzinfo=None).isoformat(sep=" ")
            _suspend(source,instant,generation_id=generation,operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),
                requested_by=self.technical.id,snapshot_at=self.snapshot_at)
            with closing(sqlite3.connect(source)) as db:
                row=db.execute("SELECT id,snapshot_at,inventory_recorded_at FROM backups_restoresession WHERE generation_id=?",[generation.hex]).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[1],"2026-10-20 08:00:00")
                self.assertIsNotNone(row[2])
                observations=db.execute("SELECT shop_id,origin_installation_id,origin_user_id,label FROM backups_recoverydevice WHERE session_id=? ORDER BY shop_id,origin_installation_id",[row[0]]).fetchall()
                self.assertEqual(set(observations),{(self.a.id.hex,phone.id.hex,self.owner.id.hex,"Owner phone"),
                    (self.b.id.hex,phone.id.hex,self.owner.id.hex,"Owner phone"),
                    (self.a.id.hex,seller_phone.id.hex,seller.id.hex,"Seller phone")})
                self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active OR is_superuser OR is_staff").fetchone()[0],0)

    def test_openapi_accepts_actual_catalogue_metadata_including_nullable_unknowns(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        # Translate OpenAPI 3.0 nullable to JSON Schema's explicit null type.
        def nullable(value):
            if isinstance(value,list):
                return [nullable(item) for item in value]
            if not isinstance(value,dict):
                return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        client=self.authenticate(self.owner)
        generation=None
        for phase in ("none","legacy","recorded"):
            if phase=="legacy":
                generation=self.generation()
                self.confirm(generation,self.a,self.owner)
            if phase=="recorded":
                session=self.record(generation,snapshot_at=self.snapshot_at,inventory_recorded_at=timezone.now())
                self.device(session,self.a)
            for endpoint in ("products","stock-alerts",f"products/{self.product.id}"):
                response=client.get(f"/api/v1/shops/{self.a.id}/{endpoint}/")
                self.assertEqual(response.status_code,200)
                suffix="products/{id}" if "/" in endpoint else endpoint
                document=schema["paths"][f"/api/v1/shops/{{shop_id}}/{suffix}/"]["get"]["responses"]["200"]
                response_schema=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
                errors=list(Draft4Validator(response_schema,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
                with self.subTest(phase=phase,endpoint=endpoint):
                    self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
