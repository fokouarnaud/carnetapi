"""An unresolved product price cannot be replaced through catalogue recovery."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backups import models as backups
from backups.tests import test_recovery_catalogue_revisions as fixtures
from cash.models import CashMovement
from common.models import Activity
from common.services.errors import OperationConflict
from identity.models import Installation,DeviceSession
from stock.models import StockMovement


class RecoveryCataloguePriceConflictTests(TestCase,fixtures.CatalogueSources):
    setUp=fixtures.RecoveryCatalogueRevisionTests.setUp
    authenticate=fixtures.RecoveryCatalogueRevisionTests.authenticate
    generation=fixtures.RecoveryCatalogueRevisionTests.generation
    record=fixtures.RecoveryCatalogueRevisionTests.record
    device=fixtures.RecoveryCatalogueRevisionTests.device
    review=fixtures.RecoveryCatalogueRevisionTests.review
    payload=fixtures.RecoveryCatalogueRevisionTests.payload
    url=fixtures.RecoveryCatalogueRevisionTests.url
    capture=fixtures.RecoveryCatalogueRevisionTests.capture
    preview=fixtures.RecoveryCatalogueRevisionTests.preview
    apply=fixtures.RecoveryCatalogueRevisionTests.apply
    before=fixtures.RecoveryCatalogueRevisionTests.before

    def product_url(self):
        return f"/api/v1/shops/{self.a.id}/products/{self.product.id}/"

    def open_conflict(self):
        from catalogue.services.price_conflicts import record_price_conflict
        self.product.refresh_from_db()
        base_version=self.product.version
        common=self.product.sale_price_fcfa
        native={"operation_id":str(uuid.uuid4()),"base_version":base_version,
            "sale_price_fcfa":common+50,"reason":"Première proposition native"}
        response=self.client.patch(self.product_url(),native,format="json")
        self.assertEqual(response.status_code,200,response.content)
        second_installation=Installation.objects.create(user=self.owner)
        second_session=DeviceSession.objects.create(installation=second_installation,
            expires_at=timezone.now()+timezone.timedelta(days=1))
        record_price_conflict(shop_id=self.a.id,product_id=self.product.id,actor=self.owner,
            installation=second_installation,session_id=second_session.id,validated_data={
                "operation_id":uuid.uuid4(),"source_change_id":uuid.UUID(native["operation_id"]),"base_version":base_version,
                "proposed_price_fcfa":common+100,"reason":"Seconde proposition concurrente conservée"})
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa,common)
        return native,response.data

    def compatible_source(self,*,price=True):
        self.product.refresh_from_db()
        source=self.product_source(self.before())
        source.occurred_at=timezone.now()
        source.payload["catalogue_revision"]["after"]["updated_at"]=source.occurred_at.isoformat()
        if not price:
            source.payload["original_request"].pop("sale_price_fcfa")
            source.payload["catalogue_revision"]["after"]["sale_price_fcfa"]=self.product.sale_price_fcfa
            source.payload["original_request"]["name"]="Nom corrigé sans arbitrer le prix"
            source.payload["catalogue_revision"]["after"]["name"]="Nom corrigé sans arbitrer le prix"
        return source

    def test_new_compatible_price_revision_stays_captured_and_blocked_until_price_resolution(self):
        self.open_conflict()
        source=self.compatible_source();capture=self.capture(source)
        preview=self.preview(capture)
        self.assertFalse(preview["can_apply"],preview)
        self.assertIn("catalogue_price_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        capture.refresh_from_db();self.assertEqual(capture.payload,source.payload)
        self.product.refresh_from_db();self.assertEqual(self.product.sale_price_fcfa,250)
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertEqual((StockMovement.objects.count(),CashMovement.objects.count()),(1,0))

    def test_direct_persistence_rechecks_price_guard_before_any_catalogue_or_origin_mutation(self):
        from backups.services.recovery_catalogue_revisions import persist_catalogue_revision
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision
        self.open_conflict()
        capture=self.capture(self.compatible_source())
        decoded=decode_catalogue_revision(capture)
        before=self.before()
        with self.assertRaises(OperationConflict),transaction.atomic():
            persist_catalogue_revision(shop=self.a,actor=self.owner,installation=self.live.installation,
                decoded=decoded,origin_capture=capture,recovery_review={"reason":"Tentative de contournement direct"})
        self.product.refresh_from_db();self.assertEqual(self.before(),before)
        self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())
        self.assertFalse(backups.RecoveredOperationOrigin.objects.filter(capture=capture).exists())

    def test_non_price_revision_remains_allowed_without_resolving_the_price_conflict(self):
        from catalogue.models import ProductPriceConflict
        self.open_conflict()
        capture=self.capture(self.compatible_source(price=False))
        preview=self.preview(capture)
        self.assertTrue(preview["can_apply"],preview)
        response,_command=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.product.refresh_from_db()
        self.assertEqual((self.product.name,self.product.sale_price_fcfa),("Nom corrigé sans arbitrer le prix",250))
        self.assertEqual(ProductPriceConflict.objects.filter(product=self.product,state="open").count(),1)
        self.assertEqual((StockMovement.objects.count(),CashMovement.objects.count()),(1,0))

    def test_known_native_revision_and_original_retry_do_not_replace_the_common_price(self):
        from backups.services.recovery_catalogue_snapshots import immutable_catalogue_state
        native,original=self.open_conflict()
        event=Activity.objects.get(id=native["operation_id"])
        source=SimpleNamespace(operation_type="product_update",operation_id=event.id,origin_user_id=self.owner.id,
            origin_installation_id=self.live.installation_id,occurred_at=parse_datetime(original["updated_at"]),
            payload={"schema_version":1,"original_request":event.details["request"],"catalogue_revision":{
                "before":immutable_catalogue_state(event.details["before"],pack=False),
                "after":immutable_catalogue_state(original,pack=False)}})
        capture=self.capture(source)
        preview=self.preview(capture)
        self.assertTrue(preview["can_apply"],preview)
        self.assertEqual(preview["outcome"],"already_known")
        response,command=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],original)
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json").data,response.data)
        retry=self.client.patch(self.product_url(),native,format="json")
        self.assertEqual((retry.status_code,retry.data),(200,original))
        self.product.refresh_from_db();self.assertEqual(self.product.sale_price_fcfa,250)

    def test_existing_recovery_decision_keeps_its_receipt_after_later_price_conflict(self):
        source=self.product_source(self.before());capture=self.capture(source)
        response,command=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        original=deepcopy(response.data)
        self.product.refresh_from_db();self.assertEqual(self.product.sale_price_fcfa,350)
        self.open_conflict()
        retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual((retry.status_code,retry.data),(200,original))
        known=self.preview(capture)
        self.assertTrue(known["can_apply"],known)
        self.assertEqual(known["outcome"],"already_known")
        self.product.refresh_from_db();self.assertEqual(self.product.sale_price_fcfa,350)
