"""US-43: preparation audit cannot stand in for activation or current rights."""
import uuid
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from django.db import IntegrityError, transaction
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from backups.models import RestoreImport
from identity.models import User


class ImportPreparationModelTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_superuser(username="preparation-model", password=None)
        self.batch = RestoreImport.objects.create(requested_by=self.operator, reason="Copie vérifiée",
            backup_id=uuid.uuid4(), part_count=1, snapshot_bytes=8192, header_sha256="a"*64, header={})

    def preparation(self, **values):
        from backups.models import ImportPreparation
        return ImportPreparation.objects.create(batch=self.batch, operation_id=uuid.uuid4(),
            requested_by=self.operator, reason="Contrôle avant reprise", **values)

    def test_preparation_operation_is_unique_and_only_one_worker_is_active(self):
        from backups.models import ImportPreparation
        first = self.preparation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.preparation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.preparation(worker_slot=2)
        ImportPreparation.objects.filter(id=first.id).update(state="failed", failure_code="backup_authentication_failed", finished_at=timezone.now())
        second = self.preparation()
        self.assertNotEqual(first.id, second.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportPreparation.objects.create(batch=self.batch, operation_id=first.operation_id,
                requested_by=self.operator, reason="Même commande")

    def test_ready_and_failure_require_their_evidence_and_completion_date(self):
        from backups.models import ImportPreparation
        record = self.preparation()
        for values in ({"state":"prepared"}, {"state":"failed"}, {"state":"activated"},
                {"state":"preparing","finished_at":timezone.now()}, {"reason":""}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                ImportPreparation.objects.filter(id=record.id).update(**values)

    def test_prepared_manifest_cannot_claim_activation_or_revalidated_permissions(self):
        from backups.models import ImportPreparation
        record = self.preparation()
        manifest = {"migration_execution_checked":True,"schema_audit_checked":True,
            "activation_allowed":False,"permissions_revalidated":False,"live_database_replaced":False,
            "candidate_bytes":8192,"candidate_sha256":"b"*64}
        ImportPreparation.objects.filter(id=record.id).update(state="prepared", finished_at=timezone.now(),
            candidate_bytes=8192, candidate_sha256="b"*64, manifest=manifest)
        for key in ("activation_allowed","permissions_revalidated","live_database_replaced"):
            with self.subTest(key=key), self.assertRaises(IntegrityError), transaction.atomic():
                ImportPreparation.objects.filter(id=record.id).update(manifest={**manifest,key:True})
        for key in ("activation_allowed", "schema_audit_checked"):
            with self.subTest(missing=key), self.assertRaises(IntegrityError), transaction.atomic():
                ImportPreparation.objects.filter(id=record.id).update(manifest={name:value for name,value in manifest.items() if name != key})

    def test_prepared_evidence_uses_json_booleans_and_integer_bytes(self):
        from backups.models import ImportPreparation
        record = self.preparation()
        base = {"migration_execution_checked":True,"schema_audit_checked":True,
            "activation_allowed":False,"permissions_revalidated":False,"live_database_replaced":False,
            "candidate_bytes":8192,"candidate_sha256":"b"*64}
        for key, value in (("migration_execution_checked","true"),("activation_allowed","false"),
                ("candidate_bytes",8192.0)):
            with self.subTest(key=key), self.assertRaises(IntegrityError), transaction.atomic():
                ImportPreparation.objects.filter(id=record.id).update(state="prepared", finished_at=timezone.now(),
                    candidate_bytes=8192, candidate_sha256="b"*64, manifest={**base,key:value})


class PreparationSchemaDeterminismTests(SimpleTestCase):
    def test_preparation_check_ddl_is_identical_across_python_hash_seeds(self):
        repository = Path(__file__).resolve().parents[3]
        code = ("import django\ndjango.setup()\n"
            "from django.db import connection\nfrom backups.models import ImportPreparation,ImportQuarantine\n"
            "for model,name in ((ImportPreparation,'restore_prepare_state_evidence'),(ImportQuarantine,'restore_quarantine_evidence')):\n"
            " constraint=next(item for item in model._meta.constraints if item.name==name)\n"
            " print(constraint.constraint_sql(model, connection.schema_editor(collect_sql=True, atomic=False)))\n")
        statements = []
        with TemporaryDirectory(prefix="prepare-schema-seeds-") as directory:
            for seed in ("1", "2", "3"):
                env = {name:os.environ[name] for name in ("SYSTEMROOT","WINDIR","PATH") if name in os.environ}
                env.update(PYTHONHASHSEED=seed, DJANGO_SETTINGS_MODULE="config.test_settings",
                    USERPROFILE=directory, APP_DB_PATH=str(Path(directory) / (seed+".sqlite3")))
                completed = subprocess.run([sys.executable,"-X","utf8","-c",code],
                    cwd=repository / "backend", env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                statements.append(completed.stdout)
        self.assertTrue(all(statement==statements[0] for statement in statements),
            "Preparation check DDL depends on Python hash seed")
