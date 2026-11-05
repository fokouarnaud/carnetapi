"""0027 preserves prior captures, real money and access withdrawal without decisions."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoveryCollectionReviewMigrationTests(TransactionTestCase):
    def test_existing_money_origin_receipt_and_revocations_are_preserved_without_reclassification(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0026_recovery_source_operation_index")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            now=timezone.now()
            owner=old.get_model("core","User").objects.create(username="collection-review-migration",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
            installation=old.get_model("identity","Installation").objects.create(user=owner,revoked_at=now)
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=owner.id)
            registry=old.get_model("backups","RestoreSession").objects.create(generation=generation)
            deposit=old.get_model("backups","RecoveryJournalDeposit").objects.create(session=registry,shop=shop,
                operation_id=uuid.uuid4(),submitted_by=owner,submitted_installation=installation,reason="Copie déjà reçue",
                format_version=1,content_sha256="a"*64,receipt={"capture_state":"captured"})
            capture=old.get_model("backups","RecoveryJournalOperation").objects.create(session=registry,shop=shop,
                first_deposit=deposit,operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),
                operation_type="customer_collection",occurred_at=now,origin_recorded_at=now,payload={"original":5000},
                dependencies=[],content_sha256="b"*64)
            customer=old.get_model("customers","Customer").objects.create(shop=shop,name="Client historique")
            payment_id=uuid.uuid4()
            event=old.get_model("core","Activity").objects.create(id=capture.operation_id,shop=shop,actor=owner,
                action="customer_payment_created",target_id=payment_id,reason="Encaissement déjà intégré",details={"response":{"amount_fcfa":5000}})
            payment=old.get_model("customers","CustomerPayment").objects.create(id=payment_id,shop=shop,customer=customer,
                actor=owner,installation=installation,operation=event,occurred_at=now,amount_fcfa=5000,unallocated_fcfa=5000,requires_regularization=True)
            part=old.get_model("customers","CustomerPaymentPart").objects.create(payment=payment,position=0,method="cash",amount_fcfa=5000)
            cash=old.get_model("cash","CashMovement").objects.create(shop=shop,actor=owner,installation=installation,operation=event,
                customer_payment_part=part,method="cash",amount_fcfa=5000,occurred_at=now,reason="Entrée réelle")
            ledger=old.get_model("customers","CustomerLedgerEntry").objects.create(id=uuid.uuid4(),shop=shop,customer=customer,
                operation=event,source_id=payment.id,sequence=0,kind="collection",occurred_at=now,recorded_at=now,
                delta_fcfa=-5000,debt_delta_fcfa=0,credit_delta_fcfa=5000)
            origin=old.get_model("backups","RecoveredOperationOrigin").objects.create(operation=event,capture=capture)
            integration=old.get_model("backups","RecoveryIntegrationDecision").objects.create(capture=capture,business_operation=event,
                operation_id=uuid.uuid4(),reviewed_by=owner,reviewed_installation=installation,previous_version=0,version=1,
                outcome="applied",reason="Décision historique",source_sha256="b"*64,preview_sha256="c"*64,
                request={},receipt={"amount_fcfa":5000})
            executor=MigrationExecutor(connection);executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            self.assertEqual(current.get_model("customers","CustomerPayment").objects.get(id=payment.id).unallocated_fcfa,5000)
            self.assertEqual(current.get_model("cash","CashMovement").objects.get(id=cash.id).amount_fcfa,5000)
            self.assertEqual(current.get_model("customers","CustomerLedgerEntry").objects.get(id=ledger.id).delta_fcfa,-5000)
            self.assertEqual(current.get_model("backups","RecoveryJournalOperation").objects.get(id=capture.id).payload,{"original":5000})
            self.assertEqual(current.get_model("backups","RecoveredOperationOrigin").objects.get().id,origin.id)
            self.assertEqual(current.get_model("backups","RecoveryIntegrationDecision").objects.get(id=integration.id).receipt,{"amount_fcfa":5000})
            self.assertEqual(current.get_model("backups","RecoveryCollectionReview").objects.count(),0)
            self.assertEqual(current.get_model("backups","RecoveryCollectionReviewDecision").objects.count(),0)
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertEqual(current.get_model("identity","Installation").objects.get(id=installation.id).revoked_at,now)
        finally:MigrationExecutor(connection).migrate(latest)
