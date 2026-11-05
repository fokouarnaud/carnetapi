"""Reopening requires the installed generation and current proven technical identity."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from django.test import TransactionTestCase, override_settings
from django.db import connection
from django.utils import timezone as django_timezone
from identity.models import Installation,DeviceSession,ShopDeviceAccess,Shop
from backups.tests import test_technical_recovery as fixtures
from backups.services.installation_journal import installation_control
from backups.services.restore_gate import (read_gate,enter_maintenance,acquire_request,acquire_recovery_request)
from backups.services.restore_quarantine import _suspend


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


INTERRUPTED_ACTIVATION=r'''
import json,os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backups.services.restore_activation import activate_restore
from backups.services import restore_gate
real_connect=sqlite3.connect
class Interrupted(sqlite3.Connection):
 def commit(self):
  if sys.argv[2]=='before':os._exit(73)
  super().commit();os._exit(74)
def connect(name,*args,**kwargs):
 if str(name).startswith(Path(sys.argv[3]).resolve().as_uri()):kwargs['factory']=Interrupted
 return real_connect(name,*args,**kwargs)
restore_gate.sqlite3.connect=connect
activate_restore(**json.load(sys.stdin))
raise AssertionError('Private interruption point was not reached')
'''


class RestoreActivationTests(TransactionTestCase):
    provision=fixtures.TechnicalRecoveryTests.provision
    bootstrap=fixtures.TechnicalRecoveryTests.bootstrap

    def setUp(self):
        fixtures.TechnicalRecoveryTests.setUp(self)
        installation=Installation.objects.create(user=self.owner,label="Ancien téléphone")
        DeviceSession.objects.create(installation=installation,expires_at=django_timezone.now()+timedelta(days=1))
        ShopDeviceAccess.objects.create(installation=installation,shop_id=self.product.shop_id,
            verified_at=django_timezone.now(),offline_expires_at=django_timezone.now()+timedelta(days=7))
        Shop.objects.create(owner=self.owner,name="Seconde boutique")
        with closing(sqlite3.connect(self.source)) as db:connection.connection.backup(db)
        issued=self.provision()
        original=self.root/"before-incident.sqlite3";shutil.copyfile(self.source,original)
        candidate=self.root/"installed-candidate.sqlite3";shutil.copyfile(self.source,candidate)
        self.generation,self.backup=uuid.uuid4(),uuid.uuid4()
        _suspend(candidate,datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
            generation_id=self.generation,operation_id=uuid.uuid4(),backup_id=self.backup,requested_by=self.operator.id)
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.operator.id,reason="Incident privé",expected_version=1)
        self.installation=uuid.uuid4()
        rollback=self.root/"rollback";rollback.mkdir();shutil.copyfile(original,rollback/"original.sqlite3")
        self.plan={"candidate_dir":str(self.root),"candidate_sha256":digest(candidate),"candidate_bytes":candidate.stat().st_size,
            "restore_generation_id":str(self.generation),"backup_id":str(self.backup),
            "source_fingerprint":read_gate(self.gate)["fingerprint"],"rollback_directory":str(rollback),
            "original_files":{"":{"sha256":digest(original),"bytes":original.stat().st_size},"-wal":None,"-shm":None,"-journal":None}}
        with installation_control(self.gate,self.source,operation_id=self.installation,requested_by=self.operator.id,
                reason="Installation de la fixture",expected_version=2,plan=self.plan) as install:
            shutil.copyfile(candidate,self.source)
            install.complete({**{key:self.plan[key] for key in ("candidate_sha256","candidate_bytes","restore_generation_id","backup_id","rollback_directory","original_files")},
                "activation_allowed":False,"live_database_replaced":True,"access_quarantined":True,"previous_database_missing":False})
        self.bootstrap_operation=uuid.uuid4()
        with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
            self.identity=self.bootstrap(issued.secret,operation_id=self.bootstrap_operation,gate_version=3)
        self.operation=uuid.uuid4()

    def arguments(self,**values):
        return {"gate_path":self.gate,"database_path":self.source,"vault_path":self.vault,"backup_root":self.root/"stored",
            "installation_id":self.installation,"bootstrap_operation":self.bootstrap_operation,
            "operation_id":self.operation,"expected_version":3,"username":"technical-new","password":self.password,
            "reason":"Copie installée et accès actuels contrôlés",**values}

    def activate(self,**values):
        from backups.services.restore_activation import activate_restore
        return activate_restore(**self.arguments(**values))

    def test_opening_is_audited_and_idempotent_without_reactivating_archive_or_closing_recovery(self):
        business,vault=digest(self.source),digest(self.vault)
        receipt=self.activate()
        self.assertEqual((receipt["mode"],receipt["version"],receipt["expected_version"]),("open",4,3))
        self.assertEqual(receipt["requested_by"],self.identity["user_id"])
        self.assertEqual(receipt["restore_generation_id"],str(self.generation))
        self.assertFalse(receipt["recovery_closed"])
        self.assertFalse(receipt["archived_permissions_restored"])
        self.assertEqual(self.activate(),receipt)
        self.assertEqual((digest(self.source),digest(self.vault)),(business,vault))
        self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("open",4))
        with acquire_request(self.gate,self.source):pass
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(db.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",[self.operator.id.hex]).fetchone(),(0,0,0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_devicesession WHERE revoked_at IS NULL").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoresession WHERE generation_id=?",[self.generation.hex]).fetchone()[0],1)
        with closing(sqlite3.connect(self.gate)) as db:
            self.assertEqual(json.loads(db.execute("SELECT receipt FROM gate_events WHERE operation_id=?",[str(self.operation)]).fetchone()[0]),receipt)

    def test_bad_credentials_binding_version_or_bootstrap_keeps_maintenance_and_original_bytes(self):
        from backups.services.restore_activation import RestoreActivationError
        before=(digest(self.source),digest(self.vault))
        for values in ({"password":"incorrect"},{"username":self.operator.username},{"installation_id":uuid.uuid4()},
                {"bootstrap_operation":uuid.uuid4()},{"expected_version":2},{"database_path":self.root/"missing.sqlite3"}):
            with self.subTest(values=tuple(values)),self.assertRaises(RestoreActivationError):self.activate(**values)
            self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("maintenance",3))
            self.assertEqual((digest(self.source),digest(self.vault)),before)
        self.assertFalse((self.root/"missing.sqlite3").exists())

    def test_inflight_recovery_request_excludes_activation_before_business_read(self):
        from backups.services.restore_activation import RestoreActivationError
        before=digest(self.source)
        with acquire_recovery_request(self.gate,self.source,expected_version=3):
            with self.assertRaisesRegex(RestoreActivationError,"restore_activation_gate_unavailable"):self.activate()
        self.assertEqual(digest(self.source),before)
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")

    def test_current_technical_role_and_unchanged_request_are_required_on_replay(self):
        from backups.services.restore_activation import RestoreActivationError
        self.activate()
        with self.assertRaises(RestoreActivationError):self.activate(reason="Autre décision")
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute("UPDATE core_user SET is_superuser=0 WHERE id=?",[uuid.UUID(self.identity["user_id"]).hex])
        with self.assertRaises(RestoreActivationError):self.activate()
        self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("open",4))

    def test_untrusted_schema_or_unrevoked_archived_access_cannot_be_opened(self):
        from backups.services.restore_activation import RestoreActivationError
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute("UPDATE core_installation SET revoked_at=NULL")
        with self.assertRaises(RestoreActivationError):self.activate()
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute("UPDATE core_installation SET revoked_at=CURRENT_TIMESTAMP WHERE revoked_at IS NULL")
            db.execute("CREATE TABLE untrusted_activation(value TEXT)")
        with self.assertRaises(RestoreActivationError):self.activate()
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")

    def test_process_death_before_or_after_commit_preserves_one_durable_open_epoch(self):
        before=(digest(self.source),digest(self.vault))
        for mode,code,expected in (("before",73,"maintenance"),("after",74,"open")):
            with self.subTest(mode=mode):
                gate=self.root/("interrupted-"+mode+".sqlite3");shutil.copyfile(self.gate,gate)
                arguments=self.arguments(gate_path=gate)
                payload={key:str(value) if isinstance(value,(Path,uuid.UUID)) else value for key,value in arguments.items()}
                result=subprocess.run([sys.executable,"-I","-X","utf8","-c",INTERRUPTED_ACTIVATION,
                    str(Path(__file__).resolve().parents[2]),mode,str(gate)],input=json.dumps(payload),text=True,capture_output=True,timeout=60)
                self.assertEqual(result.returncode,code,"Private activation interruption failed")
                self.assertNotIn(self.password,result.stdout+result.stderr)
                self.assertEqual(read_gate(gate)["mode"],expected)
                receipt=self.activate(gate_path=gate)
                self.assertEqual((receipt["mode"],receipt["version"]),("open",4))
                self.assertEqual(self.activate(gate_path=gate),receipt)
                with closing(sqlite3.connect(gate)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM gate_events WHERE operation_id=?",[str(self.operation)]).fetchone()[0],1)
                self.assertEqual((digest(self.source),digest(self.vault)),before)

    def test_a_confirmed_owner_scope_does_not_require_other_scopes_to_be_confirmed(self):
        with closing(sqlite3.connect(self.source)) as db:
            review=db.execute("SELECT id FROM backups_restoreaccessreview WHERE shop_id=? AND user_id=?",[self.product.shop_id.hex,self.owner.id.hex]).fetchone()[0]
        script=Path(__file__).resolve().parents[3]/"tools/review_restore_access.py"
        result=subprocess.run([sys.executable,"-I","-X","utf8",str(script),"decide","--gate",str(self.gate),"--database",str(self.source),
            "--gate-version","3","--username","technical-new","--stdin-json","--review-id",str(uuid.UUID(review)),
            "--generation-id",str(self.generation),"--operation-id",str(uuid.uuid4()),"--previous-version","0","--state","confirmed",
            "--reason","Qualité actuelle vérifiée pour A"],input=json.dumps({"password":self.password}),text=True,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,"Private current-owner review failed")
        before=digest(self.source)
        self.assertEqual(self.activate()["mode"],"open")
        self.assertEqual(digest(self.source),before)
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_membership WHERE is_active=1").fetchone()[0],0)

    def test_incomplete_or_invalid_installation_metadata_cannot_be_treated_as_a_verified_copy(self):
        from backups.services.restore_activation import RestoreActivationError
        for key,value in (("candidate_sha256",None),("candidate_bytes",None),("candidate_sha256","z"*64),("candidate_bytes",1.5)):
            with self.subTest(key=key,value=value):
                gate=self.root/("invalid-metadata-"+uuid.uuid4().hex+".sqlite3");shutil.copyfile(self.gate,gate)
                with closing(sqlite3.connect(gate)) as db,db:
                    request,receipt=db.execute("SELECT request,receipt FROM gate_installations WHERE operation_id=?",[str(self.installation)]).fetchone()
                    request,receipt=json.loads(request),json.loads(receipt)
                    if value is None:request["plan"].pop(key);receipt.pop(key)
                    else:request["plan"][key]=value;receipt[key]=value
                    db.execute("UPDATE gate_installations SET request=?,receipt=? WHERE operation_id=?",[json.dumps(request),json.dumps(receipt),str(self.installation)])
                with self.assertRaises(RestoreActivationError):self.activate(gate_path=gate)
                self.assertEqual((read_gate(gate)["mode"],read_gate(gate)["version"]),("maintenance",3))

    def test_restored_group_or_direct_permission_assignments_are_refused_before_first_open(self):
        from backups.services.restore_activation import RestoreActivationError
        for kind in ("group","direct"):
            with self.subTest(kind=kind):
                with closing(sqlite3.connect(self.source)) as db,db:
                    if kind=="group":
                        group=db.execute("INSERT INTO auth_group(name) VALUES ('Attribution ancienne de la fixture')").lastrowid
                        db.execute("INSERT INTO core_user_groups(user_id,group_id) VALUES (?,?)",[self.owner.id.hex,group])
                    else:
                        permission=db.execute("SELECT id FROM auth_permission ORDER BY id LIMIT 1").fetchone()[0]
                        db.execute("INSERT INTO core_user_user_permissions(user_id,permission_id) VALUES (?,?)",[self.owner.id.hex,permission])
                with self.assertRaises(RestoreActivationError):self.activate()
                self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("maintenance",3))
                with closing(sqlite3.connect(self.source)) as db,db:
                    if kind=="group":
                        db.execute("DELETE FROM core_user_groups WHERE group_id=?",[group]);db.execute("DELETE FROM auth_group WHERE id=?",[group])
                    else:db.execute("DELETE FROM core_user_user_permissions WHERE user_id=? AND permission_id=?",[self.owner.id.hex,permission])

    def test_noncanonical_or_nonzero_archived_privilege_flags_cannot_hide_from_first_open_audit(self):
        from backups.services.restore_activation import RestoreActivationError
        for field,value in (("is_staff",2),("is_staff","yes"),("is_superuser",2),("is_superuser",1.5),("is_active",2)):
            with self.subTest(field=field,value=value):
                gate=self.root/("invalid-privilege-"+uuid.uuid4().hex+".sqlite3");shutil.copyfile(self.gate,gate)
                with closing(sqlite3.connect(self.source)) as db,db:
                    db.execute(f"UPDATE core_user SET {field}=? WHERE id=?",[value,self.operator.id.hex])
                with self.assertRaises(RestoreActivationError):self.activate(gate_path=gate)
                self.assertEqual((read_gate(gate)["mode"],read_gate(gate)["version"]),("maintenance",3))
                with closing(sqlite3.connect(self.source)) as db,db:
                    db.execute(f"UPDATE core_user SET {field}=0 WHERE id=?",[self.operator.id.hex])
