"""Durable external intent before any restore file replacement; never opens HTTP."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import uuid

from .restore_gate import (
    _connection, _identity, _path, _state, _event, _require_no_installation, _fingerprint,
    RestoreGateError, RestoreGateConflict,
)


def _json(value):
    try:
        result=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)
        if len(result)>100000:
            raise ValueError()
        return result
    except (ValueError,TypeError,RecursionError):
        raise RestoreGateError("restore_installation_request_invalid") from None


class Installation:
    """One exclusive controller resumes its immutable, previously committed plan."""
    def __init__(self,database,row,request):
        self.database=database
        self.request=request
        self.state=row["state"]
        self.receipt=json.loads(row["receipt"]) if row["receipt"] is not None else None
        self.gate_version=row["gate_version"]

    def complete(self,outcome):
        if self.state!="reserved" or not isinstance(outcome,dict) or outcome.get("activation_allowed") is not False:
            raise RestoreGateConflict("restore_installation_state_conflict")
        receipt={**outcome,"operation_id":self.request["operation_id"],"requested_by":self.request["requested_by"],
            "reason":self.request["reason"],"gate_version":self.gate_version,"gate_mode":"maintenance",
            "installed_at":datetime.now(timezone.utc).isoformat()}
        value=_json(receipt)
        self.database.execute("UPDATE gate_installations SET state='installed',receipt=? WHERE operation_id=? AND state='reserved'",
            [value,self.request["operation_id"]])
        _event(self.database,_state(self.database),str(uuid.uuid5(uuid.UUID(self.request["operation_id"]),"installed")),
            self.request["requested_by"],self.request["reason"],"install_completed",self.gate_version)
        self.database.commit()
        self.state,self.receipt="installed",receipt
        return receipt


@contextmanager
def installation_control(gate_path,database_path,*,operation_id,requested_by,reason,expected_version,plan):
    """Commit intent, then exclusively resume it; exceptions keep maintenance closed."""
    operation,actor,reason=_identity(operation_id,requested_by,reason)
    if type(expected_version) is not int or expected_version<1 or not isinstance(plan,dict):
        raise RestoreGateError("restore_installation_request_invalid")
    request={"operation_id":operation,"requested_by":actor,"reason":reason,"expected_version":expected_version,
        "database_path":str(_path(database_path)),"plan":plan}
    canonical=_json(request)
    with _connection(gate_path) as db:
        db.execute("BEGIN EXCLUSIVE")
        state=_state(db)
        if db.execute("PRAGMA user_version").fetchone()[0]!=2:
            raise RestoreGateError("restore_installation_gate_upgrade_required")
        row=db.execute("SELECT * FROM gate_installations WHERE operation_id=?",[operation]).fetchone()
        if row is not None and row["request"]!=canonical:
            raise RestoreGateConflict("restore_gate_operation_conflict")
        if row is None:
            _require_no_installation(db)
            if (state["mode"]!="maintenance" or state["version"]!=expected_version
                    or state["database_path"]!=request["database_path"]
                    or db.execute("SELECT 1 FROM gate_events WHERE operation_id IN (?,?)",
                        [operation,str(uuid.uuid5(uuid.UUID(operation),"installed"))]).fetchone()):
                raise RestoreGateConflict("restore_gate_state_conflict")
            if "source_fingerprint" in plan and (state["fingerprint"]!=plan["source_fingerprint"]
                    or _fingerprint(database_path,allow_missing=True)!=plan["source_fingerprint"]):
                raise RestoreGateConflict("restore_gate_database_changed")
            state["version"]+=1
            db.execute("INSERT INTO gate_installations VALUES(?,'reserved',?,?,NULL)",[operation,state["version"],canonical])
            db.execute("UPDATE gate_state SET version=? WHERE singleton=1",[state["version"]])
            _event(db,state,operation,actor,reason,"install_reserved",expected_version)
            db.commit()  # Survives a kill before any filesystem mutation.
            db.execute("BEGIN EXCLUSIVE")
            row=db.execute("SELECT * FROM gate_installations WHERE operation_id=?",[operation]).fetchone()
            state=_state(db)
        if (state["mode"]!="maintenance" or state["version"]!=row["gate_version"]
                or state["database_path"]!=request["database_path"]):
            raise RestoreGateConflict("restore_gate_state_conflict")
        yield Installation(db,row,request)
