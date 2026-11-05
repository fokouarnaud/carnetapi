"""Contradictory origins cannot leak identities or silently reassign real money."""
import json
import uuid
from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator

from backups import models as backups
from backups.tests import test_recovery_collections as collection_fixtures
from backups.tests import test_recovery_sale_integration as sale_fixtures
from cash.models import CashMovement
from customers.models import CustomerDebt,CustomerLedgerEntry
from identity.models import DeviceSession


class RecoveryFinancialOriginScopeTests(TestCase):
    setUp=collection_fixtures.RecoveryCollectionTests.setUp
    authenticate=collection_fixtures.RecoveryCollectionTests.authenticate
    generation=collection_fixtures.RecoveryCollectionTests.generation
    record=collection_fixtures.RecoveryCollectionTests.record
    device=collection_fixtures.RecoveryCollectionTests.device
    review=collection_fixtures.RecoveryCollectionTests.review
    payload=collection_fixtures.RecoveryCollectionTests.payload
    url=collection_fixtures.RecoveryCollectionTests.url
    source=collection_fixtures.RecoveryCollectionTests.source
    capture=collection_fixtures.RecoveryCollectionTests.capture
    preview=collection_fixtures.RecoveryCollectionTests.preview
    apply=collection_fixtures.RecoveryCollectionTests.apply
    family="customer_collection"

    def paths(self):
        root=f"/api/v1/shops/{self.a.id}/"
        return [root+f"clients/{self.customer.id}/payments/",root+"cash/",root+"cash/?scope=personal",root+f"clients/{self.customer.id}/statement/"]

    def test_invalid_foreign_origin_refuses_projections_without_money_changes_or_fabricated_actor(self):
        source=self.source();capture=self.capture(source);applied,_=self.apply(capture)
        self.assertEqual(applied.status_code,201,applied.content)
        before=(list(CashMovement.objects.order_by("id").values_list("id","amount_fcfa")),
            list(CustomerDebt.objects.order_by("id").values_list("id","remaining_fcfa")),CustomerLedgerEntry.objects.count())
        live=DeviceSession.objects.get(installation__user=self.owner)
        deposit=backups.RecoveryJournalDeposit.objects.create(session=self.registry,shop=self.b,operation_id=uuid.uuid4(),
            submitted_by=self.owner,submitted_installation=live.installation,reason="Origine étrangère fabriquée pour le test",
            format_version=1,content_sha256="f"*64,receipt={})
        foreign=backups.RecoveryJournalOperation.objects.create(session=self.registry,shop=self.b,first_deposit=deposit,
            operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),operation_type=self.family,
            occurred_at=source.occurred_at,origin_recorded_at=source.occurred_at,payload={"private":"Données B"},dependencies=[],content_sha256="e"*64)
        backups.RecoveredOperationOrigin.objects.filter(operation_id=source.operation_id).update(capture=foreign)
        schema=SchemaGenerator().get_schema(request=None,public=True)
        for language,message in (("fr","L'origine enregistrée de l'opération contredit sa boutique. Vérifier le lien avec la source."),
                ("en","Stored operation origin conflicts with its shop. Review the source linkage.")):
            for path in self.paths():
                with self.subTest(language=language,path=path):
                    response=self.client.get(path,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual(response.status_code,409,response.content)
                    self.assertEqual((response.data["code"],str(response.data["detail"])),("origin_reference_conflict",message))
                    body=json.loads(response.content)
                    self.assertNotIn(str(foreign.origin_user_id),json.dumps(body))
                    self.assertNotIn(str(foreign.origin_installation_id),json.dumps(body))
                    actual=path.split('?')[0].strip('/').split('/')
                    document=next(value for key,value in schema["paths"].items() if len(key.strip('/').split('/'))==len(actual)
                        and all(expected.startswith('{') or expected==received for expected,received in zip(key.strip('/').split('/'),actual,strict=True)))
                    rule=document["get"]["responses"]["409"]["content"]["application/json"]["schema"]
                    self.assertEqual(list(Draft4Validator({**rule,"components":schema["components"]}).iter_errors(body)),[])
        after=(list(CashMovement.objects.order_by("id").values_list("id","amount_fcfa")),
            list(CustomerDebt.objects.order_by("id").values_list("id","remaining_fcfa")),CustomerLedgerEntry.objects.count())
        self.assertEqual(after,before)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.get(operation_id=source.operation_id).capture_id,foreign.id)


class RecoverySaleOriginScopeTests(RecoveryFinancialOriginScopeTests):
    setUp=sale_fixtures.RecoverySaleIntegrationTests.setUp
    source=sale_fixtures.RecoverySaleIntegrationTests.source
    capture=sale_fixtures.RecoverySaleIntegrationTests.capture
    family="sale"

    def paths(self):
        root=f"/api/v1/shops/{self.a.id}/"
        sale_id=backups.RecoveredOperationOrigin.objects.get().operation.target_id
        return [root+"sales/",root+f"sales/{sale_id}/",root+"cash/",root+"cash/?scope=personal"]
