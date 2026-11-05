"""0022 extracts existing stable identities or stops without rewriting facts."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoverySaleIdentityMigrationTests(TransactionTestCase):
    def legacy_hold(self,apps,*,sale_id=None):
        owner=apps.get_model("core","User").objects.create(username="legacy-hold-"+uuid.uuid4().hex,is_active=False)
        shop=apps.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
        installation=apps.get_model("identity","Installation").objects.create(user=owner,revoked_at=timezone.now())
        generation=apps.get_model("backups","RestoreAccessGeneration").objects.create(operation_id=uuid.uuid4(),
            backup_id=uuid.uuid4(),initiated_by=owner.id,current_slot=None)
        session=apps.get_model("backups","RestoreSession").objects.create(generation=generation)
        deposit=apps.get_model("backups","RecoveryJournalDeposit").objects.create(session=session,shop=shop,operation_id=uuid.uuid4(),
            submitted_by=owner,submitted_installation=installation,reason="Copie",format_version=1,content_sha256="a"*64,receipt={})
        source_id=sale_id or str(uuid.uuid4()).upper()
        capture=apps.get_model("backups","RecoveryJournalOperation").objects.create(session=session,shop=shop,first_deposit=deposit,
            operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),operation_type="sale",
            occurred_at=timezone.now(),origin_recorded_at=timezone.now(),payload={"sale":{"id":source_id}},content_sha256="b"*64)
        event=apps.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="recovery_sale_pending",
            target_id=uuid.uuid4(),reason="Attente")
        hold=apps.get_model("backups","RecoverySaleHold").objects.create(capture=capture,source_operation_id=capture.operation_id,
            reservation_operation=event,net_total_fcfa=500,payments_fcfa=200,state="pending",version=1)
        decision=apps.get_model("backups","RecoverySaleHoldDecision").objects.create(hold=hold,capture=capture,
            operation_id=event.id,reviewed_by=owner,reviewed_installation=installation,previous_version=0,version=1,
            state="pending",reason="Attente",request={},receipt={"state":"pending","version":1})
        return hold,capture,decision,owner

    def test_valid_legacy_identity_preserves_raw_source_receipt_state_and_suspended_account(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0021_recovery_sale_holds")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            hold,capture,decision,owner=self.legacy_hold(old)
            executor=MigrationExecutor(connection);executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            stored=current.get_model("backups","RecoverySaleHold").objects.get(id=hold.id)
            self.assertEqual(stored.sale_id,uuid.UUID(capture.payload["sale"]["id"]))
            self.assertEqual((stored.state,stored.version,stored.net_total_fcfa,stored.payments_fcfa),("pending",1,500,200))
            self.assertEqual(current.get_model("backups","RecoveryJournalOperation").objects.get(id=capture.id).payload,capture.payload)
            self.assertEqual(current.get_model("backups","RecoverySaleHoldDecision").objects.get(id=decision.id).receipt,decision.receipt)
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertEqual(current.get_model("sales","Sale").objects.count(),0)
        finally:MigrationExecutor(connection).migrate(latest)

    def test_invalid_or_duplicate_identity_stops_atomically_instead_of_inventing_or_merging_ids(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0021_recovery_sale_holds")]
        for problem in ("invalid","duplicate"):
            repair=None
            try:
                executor=MigrationExecutor(connection);executor.migrate(previous)
                old=executor.loader.project_state(previous).apps
                first,capture,_decision,_owner=self.legacy_hold(old)
                if problem=="invalid":
                    capture.payload={"sale":{"id":"invalid-source-id"}}
                    capture.save(update_fields=["payload"])
                    repair=capture
                else:
                    _second,repair,_decision,_owner=self.legacy_hold(old,sale_id=capture.payload["sale"]["id"])
                before=old.get_model("backups","RecoverySaleHold").objects.count()
                with self.subTest(problem=problem),self.assertRaises(ValueError):MigrationExecutor(connection).migrate(latest)
                self.assertEqual(old.get_model("backups","RecoverySaleHold").objects.count(),before)
                self.assertEqual(old.get_model("backups","RecoverySaleHold").objects.get(id=first.id).payments_fcfa,200)
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA table_info(backups_recoverysalehold)")
                    self.assertNotIn("sale_id",[row[1] for row in cursor.fetchall()])
            finally:
                if repair is not None:
                    repair.payload={"sale":{"id":str(uuid.uuid4())}}
                    repair.save(update_fields=["payload"])
                MigrationExecutor(connection).migrate(latest)
