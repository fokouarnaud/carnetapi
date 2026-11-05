"""Install a quarantined copy under maintenance; keep an exact old file bundle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from .archive import MAX_SNAPSHOT_BYTES
from .installation_journal import installation_control
from .restore_gate import _connection, _path, _identity, _fingerprint, read_gate, RestoreGateError


class RestoreInstallationError(ValueError):
    """A stable diagnostic; an unfinished intent always remains in maintenance."""


SUFFIXES=("","-wal","-journal","-shm")


def _digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def _file(path):
    path=_path(path)
    if not path.is_file():
        raise RestoreInstallationError("restore_installation_file_missing")
    return {"sha256":_digest(path),"bytes":path.stat().st_size}


def _checked_candidate(directory):
    directory=_path(directory)
    candidate=directory/"candidate.sqlite3"
    manifest_path=directory/"quarantine_manifest.json"
    if (manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size>150000
            or any(Path(str(candidate)+suffix).exists() for suffix in SUFFIXES[1:])):
        raise RestoreInstallationError("restore_installation_candidate_invalid")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest,dict) or manifest.get("format")!="app-x-quarantined-restore"
            or type(manifest.get("format_version")) is not int or manifest["format_version"]!=1
            or manifest.get("access_quarantined") is not True
            or any(manifest.get(name) is not False for name in ("activation_allowed","permissions_revalidated","live_database_replaced"))
            or type(manifest.get("candidate_bytes")) is not int or not 0<manifest["candidate_bytes"]<=MAX_SNAPSHOT_BYTES
            or _file(candidate)!={"sha256":manifest.get("candidate_sha256"),"bytes":manifest["candidate_bytes"]}
            or _digest(Path(__file__).resolve().parents[2]/"restore_schema.json")!=manifest.get("schema_reference_sha256")):
        raise RestoreInstallationError("restore_installation_candidate_invalid")
    environment={key:os.environ[key] for key in ("SYSTEMROOT","WINDIR","PATH","TEMP","TMP") if key in os.environ}
    completed=subprocess.run([sys.executable,"-I","-X","utf8",str(Path(__file__).with_name("installation_audit.py")),str(candidate)],
        env=environment,cwd=directory,capture_output=True,timeout=60)
    if completed.returncode or len(completed.stdout)>4096:
        raise RestoreInstallationError("restore_installation_candidate_invalid")
    audit=json.loads(completed.stdout)
    if (audit.get("schema_checked") is not True or audit.get("generation_id")!=manifest.get("restore_generation_id")
            or audit.get("backup_id")!=manifest.get("backup_id") or _digest(candidate)!=manifest["candidate_sha256"]):
        raise RestoreInstallationError("restore_installation_candidate_invalid")
    return manifest


def _cache_file(source,destination,expected):
    """Publish an fsynced cache file; retry only the operation-owned partial file."""
    destination=_path(destination)
    if destination.exists():
        if _file(destination)!=expected:
            raise RestoreInstallationError("restore_installation_cached_file_changed")
        return
    if _file(source)!=expected:
        raise RestoreInstallationError("restore_installation_source_changed")
    if shutil.disk_usage(destination.parent).free<expected["bytes"]+15000000:
        raise RestoreInstallationError("restore_installation_space_insufficient")
    partial=destination.with_name(destination.name+".partial")
    if partial.is_symlink():
        raise RestoreInstallationError("restore_installation_path_invalid")
    partial.unlink(missing_ok=True)
    with Path(source).open("rb") as incoming,partial.open("xb") as target:
        os.chmod(partial,0o600)
        shutil.copyfileobj(incoming,target,1024*1024)
        target.flush()
        os.fsync(target.fileno())
    if _file(partial)!=expected:
        raise RestoreInstallationError("restore_installation_source_changed")
    os.replace(partial,destination)
    _sync_directory(destination.parent)


def _sync_directory(directory):
    if os.name!="nt":
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish(database,candidate,plan):
    rollback=_path(plan["rollback_directory"])
    if rollback.parent!=database.parent or rollback==candidate.parent or candidate.is_relative_to(rollback):
        raise RestoreInstallationError("restore_installation_path_invalid")
    rollback.mkdir(mode=0o700,exist_ok=True)
    if rollback.is_symlink() or rollback.resolve().parent!=database.parent:
        raise RestoreInstallationError("restore_installation_path_invalid")
    _sync_directory(rollback.parent)
    originals=plan["original_files"]
    incoming=rollback/"incoming.sqlite3"
    wanted={"sha256":plan["candidate_sha256"],"bytes":plan["candidate_bytes"]}
    current=_file(database) if database.exists() else None
    if current not in (originals[""],wanted):
        raise RestoreInstallationError("restore_installation_target_changed")
    for suffix,expected in originals.items():
        if expected is not None:
            _cache_file(Path(str(database)+suffix),rollback/("original.sqlite3"+suffix),expected)
    # All originals are durable before removing any SQLite sidecar or main file.
    for suffix in SUFFIXES[1:]:
        target=Path(str(database)+suffix)
        if target.exists() and (originals[suffix] is None or _file(target)!=originals[suffix]):
            raise RestoreInstallationError("restore_installation_target_changed")
    if current!=wanted:
        _cache_file(candidate,incoming,wanted)
        if (_file(database) if database.exists() else None)!=originals[""]:
            raise RestoreInstallationError("restore_installation_target_changed")
        for suffix in SUFFIXES[1:]:
            target=_path(str(database)+suffix)
            if target.exists():
                target.unlink()
        os.replace(incoming,database)
        _sync_directory(database.parent)
    elif any(Path(str(database)+suffix).exists() for suffix in SUFFIXES[1:]):
        raise RestoreInstallationError("restore_installation_target_changed")
    if _file(database)!=wanted:
        raise RestoreInstallationError("restore_installation_target_changed")


def install_restore(gate_path,database_path,candidate_dir,*,operation_id,requested_by,reason,expected_version):
    """Resume one installation; technical bootstrap and activation remain separate."""
    try:
        operation,actor,reason=_identity(operation_id,requested_by,reason)
        database,candidate_dir=_path(database_path),_path(candidate_dir)
        if database==candidate_dir/"candidate.sqlite3":
            raise RestoreInstallationError("restore_installation_path_invalid")
        with _connection(gate_path) as db:
            if db.execute("PRAGMA user_version").fetchone()[0]!=2:
                raise RestoreInstallationError("restore_installation_gate_upgrade_required")
            existing=db.execute("SELECT request FROM gate_installations WHERE operation_id=?",[operation]).fetchone()
        if existing is not None:
            plan=json.loads(existing[0])["plan"]
            if plan["candidate_dir"]!=str(candidate_dir):
                raise RestoreInstallationError("restore_installation_request_conflict")
        else:
            state=read_gate(gate_path)
            if (state["mode"]!="maintenance" or state["version"]!=expected_version
                    or state["database_path"]!=str(database) or _fingerprint(database,allow_missing=True)!=state["fingerprint"]):
                raise RestoreInstallationError("restore_installation_state_conflict")
            manifest=_checked_candidate(candidate_dir)
            plan={"candidate_dir":str(candidate_dir),"candidate_sha256":manifest["candidate_sha256"],
                "candidate_bytes":manifest["candidate_bytes"],"restore_generation_id":manifest["restore_generation_id"],
                "backup_id":manifest["backup_id"],"source_fingerprint":state["fingerprint"],
                "original_files":{suffix:_file(str(database)+suffix) if Path(str(database)+suffix).exists() else None for suffix in SUFFIXES},
                "rollback_directory":str(database.parent/("restore-install-"+uuid.UUID(operation).hex))}
        with installation_control(gate_path,database,operation_id=operation,requested_by=actor,reason=reason,
                expected_version=expected_version,plan=plan) as installation:
            if installation.state=="installed":
                return installation.receipt
            manifest=_checked_candidate(candidate_dir)
            if any(manifest[name]!=plan[name] for name in ("candidate_sha256","candidate_bytes","restore_generation_id","backup_id")):
                raise RestoreInstallationError("restore_installation_candidate_invalid")
            _publish(database,candidate_dir/"candidate.sqlite3",plan)
            return installation.complete({"candidate_sha256":plan["candidate_sha256"],"candidate_bytes":plan["candidate_bytes"],
                "rollback_directory":plan["rollback_directory"],"original_files":plan["original_files"],
                "restore_generation_id":plan["restore_generation_id"],"backup_id":plan["backup_id"],
                "activation_allowed":False,"live_database_replaced":True,"access_quarantined":True,
                "previous_database_missing":plan["original_files"][""] is None})
    except RestoreInstallationError:
        raise
    except RestoreGateError as error:
        raise RestoreInstallationError(str(error)) from None
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,sqlite3.Error,subprocess.SubprocessError):
        raise RestoreInstallationError("restore_installation_failed") from None
