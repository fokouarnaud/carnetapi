"""Bind recovery UI authorization to a bootstrap event outside restored archives."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid
from .access_coverage import checked_generation
from .restore_gate import RestoreGateError


def recovery_identity(database_path,vault_path,*,bootstrap_operation,gate_version,backup_root):
    """Read only under a recovery lease, before sessions or other ORM work."""
    from .technical_recovery import _schema, TechnicalRecoveryError
    try:
        business,vault,archives=Path(database_path).resolve(),Path(vault_path),Path(backup_root).resolve()
        operation=str(uuid.UUID(str(bootstrap_operation)))
        if (type(gate_version) is not int or gate_version<1 or vault.is_symlink() or not vault.is_file()
                or vault.resolve().is_relative_to(archives) or vault.resolve()==business or vault.stat().st_size>1000000):
            raise ValueError()
        with closing(sqlite3.connect(vault.resolve().as_uri()+"?mode=ro",uri=True)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0]!=1:
                raise ValueError()
            row=db.execute("SELECT state,consumed_operation,database_path,backup_root FROM recovery_proof WHERE singleton=1").fetchone()
            if (row is None or row[:3]!=("consumed",operation,str(business))
                    or vault.resolve().is_relative_to(Path(row[3]).resolve())):
                raise ValueError()
            event=db.execute("""SELECT CASE WHEN length(request)<=65536 THEN request END,
                CASE WHEN length(receipt)<=65536 THEN receipt END
                FROM recovery_events WHERE operation_id=?""",[operation]).fetchone()
            if event is None:
                raise ValueError()
            request,receipt=map(json.loads,event)
        if (not isinstance(request,dict) or not isinstance(receipt,dict) or request.get("database_path")!=str(business)
                or request.get("request",{}).get("action")!="create_fresh_technical_account"
                or type(request["request"].get("gate_version")) is not int or request["request"]["gate_version"]!=gate_version
                or receipt.get("operation_id")!=operation or type(receipt.get("gate_version")) is not int
                or receipt["gate_version"]!=gate_version or receipt.get("technical_account_created") is not True
                or any(receipt.get(key) is not False for key in ("activation_allowed","permissions_revalidated","business_operations_allowed"))):
            raise ValueError()
        user_id=uuid.UUID(receipt["user_id"])
        generation_id=str(uuid.UUID(receipt["restore_generation_id"]))
        audit=_schema(business)
        if audit["schema_sha256"]!=receipt.get("schema_sha256"):
            raise ValueError()
        with closing(sqlite3.connect(business.as_uri()+"?mode=ro",uri=True)) as db:
            if checked_generation(db)!=generation_id or db.execute("""SELECT 1 FROM core_user
                WHERE id=? AND is_active=1 AND is_staff=1 AND is_superuser=1""",[user_id.hex]).fetchone() is None:
                raise ValueError()
        return {"user_id":str(user_id),"generation_id":generation_id}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error,TechnicalRecoveryError):
        raise RestoreGateError("restore_recovery_identity_unavailable") from None
