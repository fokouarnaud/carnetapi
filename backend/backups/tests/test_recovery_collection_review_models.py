"""SQL protects frozen source money and linked, versioned review decisions."""
import uuid

from django.db import connection,IntegrityError,transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from backups import models as backups
from backups.tests import test_recovery_collections as fixtures
from common.models import Activity


class RecoveryCollectionReviewModelTests(TestCase):
    setUp=fixtures.RecoveryCollectionTests.setUp
    authenticate=fixtures.RecoveryCollectionTests.authenticate
    generation=fixtures.RecoveryCollectionTests.generation
    record=fixtures.RecoveryCollectionTests.record
    device=fixtures.RecoveryCollectionTests.device
    review=fixtures.RecoveryCollectionTests.review
    payload=fixtures.RecoveryCollectionTests.payload
    url=fixtures.RecoveryCollectionTests.url
    source=fixtures.RecoveryCollectionTests.source
    capture=fixtures.RecoveryCollectionTests.capture

    def pending(self):
        capture=self.capture(self.source())
        return backups.RecoveryCollectionReview.objects.create(capture=capture,source_operation_id=capture.operation_id,
            payment_id=capture.payload["collection"]["id"],customer_id=self.customer.id,amount_fcfa=5000,
            original_applied_fcfa=4000,original_unallocated_fcfa=1000)

    def test_review_rejects_invalid_money_state_and_noninteger_values_in_sql(self):
        review=self.pending()
        updates=(('amount_fcfa',0),('original_applied_fcfa',5001),('original_unallocated_fcfa',1001),
            ('state','lost'),('version',-1),('version',1.5),('amount_fcfa',5000.5))
        for field,value in updates:
            with self.subTest(field=field,value=value),self.assertRaises(IntegrityError),transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE backups_recoverycollectionreview SET {field}=%s WHERE id=%s',[value,review.id.hex])

    def test_source_and_payment_uuids_cannot_be_reserved_twice_and_capture_is_protected(self):
        review=self.pending();other=self.capture(self.source())
        for changes in ({'source_operation_id':review.source_operation_id},{'payment_id':review.payment_id}):
            with self.subTest(changes=changes),self.assertRaises(IntegrityError),transaction.atomic():
                backups.RecoveryCollectionReview.objects.create(**{'capture':other,'source_operation_id':other.operation_id,
                    'payment_id':uuid.uuid4(),'customer_id':self.customer.id,'amount_fcfa':5000,
                    'original_applied_fcfa':5000,'original_unallocated_fcfa':0,**changes})
        with self.assertRaises(ProtectedError):review.capture.delete()

    def test_decision_requires_object_receipts_motive_sequential_version_and_validation_link(self):
        review=self.pending()
        event=Activity.objects.create(shop=self.a,actor=self.owner,action='recovery_collection_pending',target_id=review.payment_id,reason='Contrôle')
        base={'review':review,'capture':review.capture,'operation':event,'reviewed_by':self.owner,
            'reviewed_installation':self.live.installation,'previous_version':0,'version':1,'state':'pending',
            'reason':'Contrôle actuel','request':{},'receipt':{}}
        for changes in ({'version':2},{'reason':'   '},{'request':[]},{'receipt':[]},{'state':'validated'},{'state':'erased'}):
            with self.subTest(changes=changes),self.assertRaises(IntegrityError),transaction.atomic():
                backups.RecoveryCollectionReviewDecision.objects.create(**{**base,**changes})
        decision=backups.RecoveryCollectionReviewDecision.objects.create(**base)
        with self.assertRaises(ProtectedError):event.delete()
        with self.assertRaises(ProtectedError):decision.review.delete()
