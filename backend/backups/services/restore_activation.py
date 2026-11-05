"""Open an installed restoration without restoring archived access or closing recovery."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from .archive import MAX_SNAPSHOT_BYTES
from .restore_gate import _connection,_path,_state,_event,_require_no_installation,RestoreGateError


class RestoreActivationError(ValueError):
    """A stable diagnostic; failures before commit leave maintenance in force."""


def _audit(database,vault,archives,*,username,password,bootstrap_operation,bootstrap_gate_version,first_open):
    environment={key:os.environ[key] for key in ("SYSTEMROOT","WINDIR","PATH","TEMP","TMP") if key in os.environ}
    request={"username":username,"password":password,"bootstrap_operation":bootstrap_operation,
        "bootstrap_gate_version":bootstrap_gate_version,"first_open":first_open}
    result=subprocess.run([sys.executable,"-I","-X","utf8",str(Path(__file__).with_name("activation_audit.py")),
        str(database),str(vault),str(archives)],input=json.dumps(request),text=True,capture_output=True,
        env=environment,cwd=database.parent,timeout=60)
    if result.returncode or len(result.stdout)>4096:
        raise RestoreActivationError("restore_activation_identity_unavailable")
    value=json.loads(result.stdout)
    if (value.get("schema_checked") is not True or value.get("identity_confirmed") is not True
            or value.get("first_open_checks_applied") is not first_open):
        raise RestoreActivationError("restore_activation_identity_unavailable")
    for key in ("user_id","generation_id","backup_id"):value[key]=str(uuid.UUID(value[key]))
    return value


def activate_restore(gate_path,database_path,vault_path,backup_root,*,installation_id,bootstrap_operation,
        operation_id,expected_version,username,password,reason):
    """Authenticate under exclusive control and atomically record the open epoch.

    The child audits the existing business database read-only and exits before
    the external gate commit. All accepted business access still depends on its
    current scoped review; device recovery remains provisional.
    """
    try:
        operation,installation,bootstrap=map(lambda value:str(uuid.UUID(str(value))),
            (operation_id,installation_id,bootstrap_operation))
        if (type(expected_version) is not int or not 1<=expected_version<9223372036854775807
                or not isinstance(reason,str) or not reason.strip() or len(reason)>2000
                or not isinstance(username,str) or not 1<=len(username)<=150
                or not isinstance(password,str) or not 1<=len(password)<=4096):
            raise RestoreActivationError("restore_activation_input_invalid")
        reason=reason.strip()
        database,vault,archives=map(_path,(database_path,vault_path,backup_root))
        with _connection(gate_path) as db:
            db.execute("BEGIN EXCLUSIVE")
            if db.execute("PRAGMA user_version").fetchone()[0]!=2:
                raise RestoreActivationError("restore_activation_installation_unavailable")
            state=_state(db);_require_no_installation(db)
            if state["database_path"]!=str(database) or not database.is_file():
                raise RestoreActivationError("restore_activation_state_conflict")
            previous=db.execute("SELECT receipt FROM gate_events WHERE operation_id=?",[operation]).fetchone()
            old=json.loads(previous[0]) if previous else None
            if old is not None and (old.get("action")!="activate" or old.get("expected_version")!=expected_version
                    or state["mode"]!="open" or state["version"]!=old.get("version")):
                raise RestoreActivationError("restore_activation_state_conflict")
            if old is None and (state["mode"]!="maintenance" or state["version"]!=expected_version):
                raise RestoreActivationError("restore_activation_state_conflict")
            installed=db.execute("SELECT * FROM gate_installations WHERE operation_id=?",[installation]).fetchone()
            if installed is None or installed["state"]!="installed" or installed["gate_version"]!=expected_version:
                raise RestoreActivationError("restore_activation_installation_unavailable")
            intent,outcome=json.loads(installed["request"]),json.loads(installed["receipt"])
            plan=intent["plan"]
            digest=plan.get("candidate_sha256")
            size=plan.get("candidate_bytes")
            if (not isinstance(digest,str) or len(digest)!=64 or set(digest)-set("0123456789abcdef")
                    or type(size) is not int or not 0<size<=MAX_SNAPSHOT_BYTES
                    or type(intent.get("expected_version")) is not int or intent["expected_version"]+1!=expected_version
                    or intent.get("operation_id")!=installation or outcome.get("gate_mode")!="maintenance"
                    or type(outcome.get("candidate_bytes")) is not int
                    or intent["database_path"]!=str(database) or outcome.get("gate_version")!=expected_version
                    or outcome.get("operation_id")!=installation or outcome.get("activation_allowed") is not False
                    or outcome.get("live_database_replaced") is not True or outcome.get("access_quarantined") is not True
                    or any(outcome.get(key)!=plan.get(key) for key in ("restore_generation_id","backup_id","candidate_sha256","candidate_bytes"))):
                raise RestoreActivationError("restore_activation_installation_unavailable")
            identity=_audit(database,vault,archives,username=username,password=password,bootstrap_operation=bootstrap,
                bootstrap_gate_version=expected_version,first_open=old is None)
            if (identity["generation_id"]!=plan["restore_generation_id"] or identity["backup_id"]!=plan["backup_id"]):
                raise RestoreActivationError("restore_activation_generation_conflict")
            request={"database_path":str(database),"vault_path":str(vault),"backup_root":str(archives),
                "installation_id":installation,"bootstrap_operation":bootstrap,"expected_version":expected_version,
                "requested_by":identity["user_id"],"reason":reason}
            if old is not None:
                if old.get("request")!=request:raise RestoreActivationError("restore_activation_operation_conflict")
                return old
            state.update(mode="open",version=expected_version+1)
            db.execute("UPDATE gate_state SET mode='open',version=?,fingerprint=NULL WHERE singleton=1",[state["version"]])
            receipt=_event(db,state,operation,identity["user_id"],reason,"activate",expected_version)
            receipt.update(request=request,installation_id=installation,bootstrap_operation=bootstrap,
                restore_generation_id=identity["generation_id"],backup_id=identity["backup_id"],
                schema_checked=True,current_technical_identity_confirmed=True,recovery_closed=False,
                recovery_state="recovering",archived_permissions_restored=False)
            db.execute("UPDATE gate_events SET receipt=? WHERE operation_id=?",[json.dumps(receipt,sort_keys=True,allow_nan=False),operation])
            db.commit()
            return receipt
    except RestoreActivationError:raise
    except RestoreGateError:
        raise RestoreActivationError("restore_activation_gate_unavailable") from None
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,sqlite3.Error,subprocess.SubprocessError):
        raise RestoreActivationError("restore_activation_failed") from None
