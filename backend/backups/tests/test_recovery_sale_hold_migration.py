"""0021 never reclassifies existing sales or legacy pending departures."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoverySaleHoldMigrationTests(TransactionTestCase):
    def test_existing_origin_decision_sale_and_unresolved_stock_reservation_are_preserved(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0020_recovery_integration_decisions")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="hold-migration-owner",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
            install=old.get_model("identity","Installation").objects.create(user=owner,revoked_at=timezone.now())
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(operation_id=uuid.uuid4(),
                backup_id=uuid.uuid4(),initiated_by=owner.id)
            session=old.get_model("backups","RestoreSession").objects.create(generation=generation)
            deposit=old.get_model("backups","RecoveryJournalDeposit").objects.create(session=session,shop=shop,
                operation_id=uuid.uuid4(),submitted_by=owner,submitted_installation=install,reason="Copie conservée",
                format_version=1,content_sha256="a"*64,receipt={"capture_state":"captured"})
            capture=old.get_model("backups","RecoveryJournalOperation").objects.create(session=session,shop=shop,
                first_deposit=deposit,operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),
                operation_type="sale",occurred_at=timezone.now(),origin_recorded_at=timezone.now(),payload={"net":500},content_sha256="b"*64)
            sale_id=uuid.uuid4()
            business=old.get_model("core","Activity").objects.create(id=capture.operation_id,shop=shop,actor=owner,
                action="sale_created",target_id=sale_id,reason="Déjà intégrée")
            old.get_model("sales","Sale").objects.create(id=sale_id,shop=shop,actor=owner,installation=install,
                operation=business,occurred_at=timezone.now(),reference_total_fcfa=500,discount_total_fcfa=0,net_total_fcfa=500)
            origin=old.get_model("backups","RecoveredOperationOrigin").objects.create(capture=capture,operation=business)
            decision=old.get_model("backups","RecoveryIntegrationDecision").objects.create(capture=capture,business_operation=business,
                operation_id=uuid.uuid4(),reviewed_by=owner,reviewed_installation=install,previous_version=0,version=1,
                outcome="applied",reason="Contrôlée",source_sha256="b"*64,preview_sha256="c"*64,request={},receipt={"net":500})
            product=old.get_model("catalogue","Product").objects.create(shop=shop,name="Pièce",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
            pending=old.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="pending_departure",
                target_id=product.id,reason="Sortie à contrôler")
            reservation=old.get_model("stock","StockReservation").objects.create(product=product,operation=pending,quantity_milli=1000)
            executor=MigrationExecutor(connection)
            executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            self.assertEqual(current.get_model("backups","RecoveredOperationOrigin").objects.get().id,origin.id)
            self.assertEqual(current.get_model("backups","RecoveryIntegrationDecision").objects.get(id=decision.id).receipt,{"net":500})
            self.assertEqual(current.get_model("sales","Sale").objects.get(id=sale_id).net_total_fcfa,500)
            stored=current.get_model("stock","StockReservation").objects.get(id=reservation.id)
            self.assertEqual((stored.state,stored.quantity_milli),("pending",1000))
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertIsNotNone(current.get_model("identity","Installation").objects.get(id=install.id).revoked_at)
            self.assertEqual(current.get_model("backups","RecoverySaleHold").objects.count(),0)
            self.assertEqual(current.get_model("backups","RecoverySaleHoldDecision").objects.count(),0)
        finally:MigrationExecutor(connection).migrate(latest)
