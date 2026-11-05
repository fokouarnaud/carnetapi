"""A quarantined copy replaces only a drained private DB, with durable recovery."""
from contextlib import closing
from datetime import datetime,timezone as utc_timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase

from backups.services.archive import build_archive
from backups.services.restore_preparation import prepare_restore
from backups.services.restore_quarantine import quarantine_restore
from backups.services.restore_gate import initialize_gate, enter_maintenance, read_gate, acquire_request, RestoreGateError
from catalogue.models import Product
from identity.models import Shop, User,Installation


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RestoreInstallationTests(TransactionTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix="restore-installation-")
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.owner=User.objects.create_user(username="installation-owner")
        self.technical=User.objects.create_superuser(username="installation-technical",password=None)
        self.shop=Shop.objects.create(owner=self.owner,name="A")
        Installation.objects.create(user=self.owner,label="Appareil connu avant incident")
        self.product=Product.objects.create(shop=self.shop,name="Conservé",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
        self.database,self.gate=self.root/"business.sqlite3",self.root/"gate.sqlite3"
        with closing(sqlite3.connect(self.database)) as target:
            connection.connection.backup(target)
        key=Fernet.generate_key()
        self.snapshot_at=datetime(2026,10,20,8,tzinfo=utc_timezone.utc)
        archive=build_archive(self.database,self.root/"parts",key,backup_id=uuid.uuid4(),key_id="test",created_at=self.snapshot_at)
        expected=sorted([list(row) for row in MigrationLoader(None).disk_migrations])
        prepared=prepare_restore([self.root/"parts"/part["filename"] for part in archive.parts],key,
            self.root/"prepared",expected_migrations=expected)
        self.quarantined=quarantine_restore(prepared.path.parent,self.root/"quarantined",operation_id=uuid.uuid4(),
            requested_by=self.technical.id,reason="Droits archivés suspendus")
        initialize_gate(self.gate,self.database,operation_id=uuid.uuid4(),requested_by=self.technical.id,reason="Fixture")
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,reason="Incident",expected_version=1)
        self.operation=uuid.uuid4()
        self.expected_version=2

    def arguments(self):
        return dict(gate_path=self.gate,database_path=self.database,candidate_dir=self.quarantined.path.parent,
            operation_id=self.operation,requested_by=self.technical.id,reason="Installer après contrôle",expected_version=self.expected_version)

    def install(self):
        from backups.services.restore_installation import install_restore
        return install_restore(**self.arguments())

    def test_install_preserves_original_and_candidate_and_keeps_http_closed(self):
        original=digest(self.database)
        candidate=digest(self.quarantined.path)
        result=self.install()
        self.assertEqual(digest(self.database),candidate)
        self.assertEqual(digest(self.quarantined.path),candidate)
        rollback=Path(result["rollback_directory"])/"original.sqlite3"
        self.assertEqual(digest(rollback),original)
        self.assertFalse(result["activation_allowed"])
        self.assertTrue(result["live_database_replaced"])
        self.assertEqual(result["restore_generation_id"],self.quarantined.manifest["restore_generation_id"])
        self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("maintenance",3))
        with self.assertRaises(RestoreGateError):
            acquire_request(self.gate,self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?",[self.product.id.hex]).fetchone()[0],500)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active OR is_staff OR is_superuser").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT snapshot_at FROM backups_restoresession WHERE generation_id=?",
                [uuid.UUID(result["restore_generation_id"]).hex]).fetchone()[0],"2026-10-20 08:00:00")
        self.assertEqual(self.install(),result)
        self.assertEqual(digest(rollback),original)

    def test_file_replacement_error_keeps_intent_and_resumes_without_losing_original(self):
        from backups.services.restore_installation import RestoreInstallationError
        original=digest(self.database)
        real_replace=os.replace
        def failed_replace(source,target):
            if Path(target)==self.database:
                raise OSError("fixture replacement unavailable")
            return real_replace(source,target)
        with patch("os.replace",side_effect=failed_replace),self.assertRaises(RestoreInstallationError):
            self.install()
        self.assertEqual(digest(self.database),original)
        self.assertEqual(read_gate(self.gate)["installation"]["state"],"reserved")
        result=self.install()
        self.assertEqual(digest(Path(result["rollback_directory"])/"original.sqlite3"),original)
        self.assertEqual(digest(self.database),self.quarantined.manifest["candidate_sha256"])

    def test_altered_candidate_is_rejected_before_intent_or_replacement(self):
        from backups.services.restore_installation import RestoreInstallationError
        original=digest(self.database)
        data=bytearray(self.quarantined.path.read_bytes())
        data[-1]^=1
        self.quarantined.path.write_bytes(data)
        with self.assertRaises(RestoreInstallationError):
            self.install()
        self.assertEqual(digest(self.database),original)
        state=read_gate(self.gate)
        self.assertEqual((state["mode"],state["version"]),("maintenance",2))
        self.assertNotIn("installation",state)

    def test_process_killed_after_atomic_replace_resumes_from_persisted_intent(self):
        backend=Path(__file__).resolve().parents[2]
        code='''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backups.services.restore_installation import install_restore
value=json.load(sys.stdin)
original=os.replace
def killed(source,target):
    original(source,target)
    if Path(target)==Path(value["database_path"]): os._exit(73)
os.replace=killed
install_restore(**value)
'''
        original=digest(self.database)
        completed=subprocess.run([sys.executable,"-I","-X","utf8","-c",code,str(backend)],
            input=json.dumps({k:str(v) if isinstance(v,(Path,uuid.UUID)) else v for k,v in self.arguments().items()}),
            text=True,capture_output=True,timeout=60,
            env={**os.environ,"DJANGO_SETTINGS_MODULE":"intentionally.invalid","APP_DB_PATH":str(self.root/"trap.sqlite3")})
        self.assertEqual(completed.returncode,73,completed.stderr)
        self.assertFalse((self.root/"trap.sqlite3").exists())
        self.assertEqual(read_gate(self.gate)["installation"]["state"],"reserved")
        self.assertEqual(digest(self.database),self.quarantined.manifest["candidate_sha256"])
        result=self.install()
        self.assertEqual(digest(Path(result["rollback_directory"])/"original.sqlite3"),original)
        self.assertEqual(read_gate(self.gate)["installation"]["state"],"installed")

    def test_wal_and_shared_memory_are_preserved_and_never_mixed_into_restored_database(self):
        from backups.services.restore_gate import abort_maintenance
        abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,
            reason="Préparer l’incident WAL de la fixture",expected_version=2)
        # A dead process leaves a genuine uncheckpointed SQLite WAL.
        code='''import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=WAL")
db.execute("UPDATE core_product SET sale_price_fcfa=700 WHERE id=?",[sys.argv[2]])
db.commit()
os._exit(0)
'''
        completed=subprocess.run([sys.executable,"-I","-c",code,str(self.database),self.product.id.hex],timeout=20)
        self.assertEqual(completed.returncode,0)
        self.assertTrue(Path(str(self.database)+"-wal").is_file())
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,reason="Stop after crash",expected_version=3)
        self.expected_version=4
        old_wal=digest(Path(str(self.database)+"-wal"))
        result=self.install()
        rollback=Path(result["rollback_directory"])
        self.assertEqual(digest(rollback/"original.sqlite3-wal"),old_wal)
        self.assertFalse(any(Path(str(self.database)+suffix).exists() for suffix in ("-wal","-journal","-shm")))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?",[self.product.id.hex]).fetchone()[0],500)
        # Read a separate copy so inspection cannot checkpoint the saved bundle.
        inspection=self.root/"rollback-inspection"
        inspection.mkdir()
        import shutil
        for suffix,value in result["original_files"].items():
            if value is not None:
                shutil.copyfile(rollback/("original.sqlite3"+suffix),inspection/("original.sqlite3"+suffix))
        with closing(sqlite3.connect(inspection/"original.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?",[self.product.id.hex]).fetchone()[0],700)

    def test_resealed_manifest_cannot_hide_active_rights_missing_reviews_or_extra_schema(self):
        from backups.services.restore_installation import RestoreInstallationError
        original=digest(self.database)
        candidate_bytes=self.quarantined.path.read_bytes()
        manifest_path=self.quarantined.path.parent/"quarantine_manifest.json"
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        for sql in ("UPDATE core_user SET is_active=1", "DELETE FROM backups_restoreaccessreview",
                    "CREATE TRIGGER unexpected_restore_trigger AFTER UPDATE ON core_product BEGIN SELECT 1; END"):
            with self.subTest(sql=sql):
                self.quarantined.path.write_bytes(candidate_bytes)
                with closing(sqlite3.connect(self.quarantined.path)) as db,db:
                    db.execute(sql)
                changed={**manifest,"candidate_sha256":digest(self.quarantined.path),
                    "candidate_bytes":self.quarantined.path.stat().st_size}
                manifest_path.write_text(json.dumps(changed),encoding="utf-8")
                with self.assertRaises(RestoreInstallationError):
                    self.install()
                self.assertEqual(digest(self.database),original)
                self.assertEqual(read_gate(self.gate)["version"],2)
                self.assertNotIn("installation",read_gate(self.gate))

    def test_console_installs_without_loading_broken_deployment_settings(self):
        script=Path(__file__).resolve().parents[3]/"tools/install_server_restore.py"
        command=[sys.executable,"-I","-X","utf8",str(script),str(self.quarantined.path.parent),
            "--gate",str(self.gate),"--database",str(self.database),"--operation-id",str(self.operation),
            "--requested-by",str(self.technical.id),"--reason","Installer après contrôle","--expected-version","2"]
        environment={**os.environ,"DJANGO_SETTINGS_MODULE":"intentionally.invalid","APP_DB_PATH":str(self.root/"trap.sqlite3"),
            "APP_EMAIL_CONFIG_FILE":str(self.root/"missing-mail.json")}
        completed=subprocess.run(command,capture_output=True,text=True,env=environment,timeout=60)
        self.assertEqual(completed.returncode,0,completed.stderr)
        result=json.loads(completed.stdout)
        self.assertTrue(result["live_database_replaced"])
        self.assertFalse(result["activation_allowed"])
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")
        self.assertFalse((self.root/"trap.sqlite3").exists())
        replay=subprocess.run(command,capture_output=True,text=True,env=environment,timeout=60)
        self.assertEqual(replay.returncode,0,replay.stderr)
        self.assertEqual(json.loads(replay.stdout),result)

    def test_missing_target_is_restored_without_inventing_an_original_database(self):
        from backups.services.restore_gate import abort_maintenance
        abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,
            reason="Fixture avant perte du fichier",expected_version=2)
        self.database.unlink()
        with self.assertRaises(RestoreGateError):
            acquire_request(self.gate,self.database)
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,
            reason="Fichier disparu",expected_version=3)
        self.expected_version=4
        result=self.install()
        self.assertTrue(result["previous_database_missing"])
        self.assertIsNone(result["original_files"][""])
        self.assertFalse((Path(result["rollback_directory"])/"original.sqlite3").exists())
        self.assertEqual(digest(self.database),self.quarantined.manifest["candidate_sha256"])
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")

    def test_unreadable_old_sqlite_is_preserved_as_bytes_before_installing_valid_copy(self):
        from backups.services.restore_gate import abort_maintenance
        abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,
            reason="Fixture avant corruption",expected_version=2)
        damaged=b"damaged sqlite fixture; preserve these original bytes"
        self.database.write_bytes(damaged)
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.technical.id,
            reason="Base devenue illisible",expected_version=3)
        self.expected_version=4
        result=self.install()
        self.assertEqual((Path(result["rollback_directory"])/"original.sqlite3").read_bytes(),damaged)
        self.assertEqual(digest(self.database),self.quarantined.manifest["candidate_sha256"])
        self.assertFalse(result["activation_allowed"])

    def test_erased_known_device_observations_cannot_be_hidden_by_a_resealed_manifest(self):
        self.resealed_registry_is_refused("DELETE FROM backups_recoverydevice")

    def test_missing_inventory_recording_cannot_be_hidden_by_a_resealed_manifest(self):
        self.resealed_registry_is_refused("UPDATE backups_restoresession SET inventory_recorded_at=NULL")

    def resealed_registry_is_refused(self,sql):
        from backups.services.restore_installation import RestoreInstallationError
        original=digest(self.database)
        with closing(sqlite3.connect(self.quarantined.path)) as db,db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoverydevice").fetchone()[0],1)
            db.execute(sql)
        manifest={**self.quarantined.manifest,"candidate_sha256":digest(self.quarantined.path),
            "candidate_bytes":self.quarantined.path.stat().st_size}
        (self.quarantined.path.parent/"quarantine_manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
        with self.assertRaises(RestoreInstallationError):
            self.install()
        self.assertEqual(digest(self.database),original)
        self.assertEqual(read_gate(self.gate)["version"],2)
        self.assertNotIn("installation",read_gate(self.gate))
