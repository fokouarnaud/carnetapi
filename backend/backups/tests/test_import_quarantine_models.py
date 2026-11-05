"""US-43: quarantine audit cannot stand in for activation or verified rights."""
import uuid
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from backups.models import ImportPreparation, RestoreImport
from identity.models import User


class ImportQuarantineModelTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_superuser(username="quarantine-model", password=None)
        batch = RestoreImport.objects.create(requested_by=self.operator, reason="Archive", backup_id=uuid.uuid4(),
            part_count=1, snapshot_bytes=8192, header_sha256="a"*64, header={})
        self.preparation = ImportPreparation.objects.create(batch=batch, requested_by=self.operator,
            operation_id=uuid.uuid4(), reason="Copie", state="prepared", finished_at=timezone.now(),
            candidate_bytes=8192, candidate_sha256="b"*64, manifest={"migration_execution_checked":True,
                "schema_audit_checked":True,"activation_allowed":False,"permissions_revalidated":False,
                "live_database_replaced":False,"candidate_bytes":8192,"candidate_sha256":"b"*64})

    def create(self, **values):
        from backups.models import ImportQuarantine
        return ImportQuarantine.objects.create(**{"preparation":self.preparation,"requested_by":self.operator,
            "operation_id":uuid.uuid4(),"reason":"Suspendre les accès archivés", **values})

    def test_one_active_worker_and_operation_identity_survive_failures(self):
        from backups.models import ImportQuarantine
        first = self.create()
        for values in ({}, {"worker_slot":2}, {"operation_id":first.operation_id}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.create(**values)
        ImportQuarantine.objects.filter(id=first.id).update(state="failed", finished_at=timezone.now(), failure_code="restore_quarantine_source_invalid")
        # No active worker remains: this rejection must come from operation identity.
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create(operation_id=first.operation_id)
        self.assertNotEqual(self.create().id, first.id)

    def test_finished_states_require_evidence_and_do_not_accept_activation(self):
        from backups.models import ImportQuarantine
        record = self.create()
        for values in ({"state":"quarantined"}, {"state":"failed"}, {"state":"activated"},
                {"finished_at":timezone.now()}, {"reason":""}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                ImportQuarantine.objects.filter(id=record.id).update(**values)

    def test_quarantined_receipt_requires_typed_flags_and_zero_remaining_access(self):
        from backups.models import ImportQuarantine
        record = self.create()
        after = {"active_users":0,"active_memberships":0,"privileged_users":0,"group_assignments":0,
            "direct_permissions":0,"unrevoked_installations":0,"unrevoked_sessions":0,"verified_shop_leases":0,"web_sessions":0}
        manifest = {"candidate_bytes":8192,"candidate_sha256":"c"*64,"access_quarantined":True,
            "activation_allowed":False,"permissions_revalidated":False,"live_database_replaced":False,"after":after}
        ImportQuarantine.objects.filter(id=record.id).update(state="quarantined", finished_at=timezone.now(),
            candidate_bytes=8192, candidate_sha256="c"*64, manifest=manifest)
        invalid = [{**manifest,"access_quarantined":"true"}, {**manifest,"activation_allowed":True},
            {**manifest,"permissions_revalidated":True}, {**manifest,"live_database_replaced":True},
            {**manifest,"candidate_bytes":8192.0}, {**manifest,"after":{**after,"active_users":1}},
            {**manifest,"after":{**after,"web_sessions":"0"}}, {**manifest,"after":{**after,"privileged_users":False}},
            {name:value for name,value in manifest.items() if name != "after"}]
        for receipt in invalid:
            with self.subTest(receipt=receipt), self.assertRaises(IntegrityError), transaction.atomic():
                ImportQuarantine.objects.filter(id=record.id).update(manifest=receipt)
