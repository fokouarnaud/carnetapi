"""Authenticate first without Django, then migrate only an isolated temporary copy."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid
from backups.services.archive import MAX_SNAPSHOT_BYTES, validate_snapshot, verify_bundle


class RestorePreparationError(ValueError):
    """A stable diagnostic without archive values, host secrets or worker output."""


@dataclass(frozen=True)
class PreparedRestore:
    path: Path
    manifest: dict


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _write_json(path, value, *, owned=None):
    with path.open("x", encoding="utf-8") as target:
        if owned is not None:
            owned.append(path)
        os.chmod(path, 0o600)
        json.dump(value, target, sort_keys=True, ensure_ascii=True)
        target.flush()
        os.fsync(target.fileno())


def prepare_restore(paths, key, output_dir, *, expected_migrations, timeout_seconds=120):
    if (not isinstance(expected_migrations, list) or not 0 < len(expected_migrations) <= 2048 or any(
            not isinstance(row, list) or len(row) != 2 or any(not isinstance(item, str) or not 0 < len(item) <= 255 for item in row)
            for row in expected_migrations) or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600):
        raise RestorePreparationError("restore_configuration_invalid")
    output = Path(output_dir).absolute()
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise RestorePreparationError("restore_output_exists") from None
    published = []
    try:
        with TemporaryDirectory(prefix="restore-work-", dir=output) as directory:
            work = Path(directory).resolve()
            if not work.is_relative_to(output.resolve()):
                raise RestorePreparationError("restore_work_directory_invalid")
            verified = verify_bundle(paths, key, work, expected_migrations=expected_migrations)
            before = verified.manifest["migrations"]
            if len(before) != len({tuple(row) for row in before}):
                raise RestorePreparationError("restore_migration_history_invalid")
            candidate = work / "candidate.sqlite3"
            verified.path.rename(candidate)
            nonce = str(uuid.uuid4())
            _write_json(work / "request.json", {"nonce": nonce, "snapshot_sha256": verified.manifest["snapshot_sha256"],
                "migrations_before": before, "expected_migrations": expected_migrations})
            environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH") if name in os.environ}
            environment.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work), LANG="C.UTF-8")
            try:
                completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("restore_migrations.py")),
                    str(work), nonce], env=environment, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout_seconds, check=False)
            except subprocess.TimeoutExpired:
                raise RestorePreparationError("restore_migration_timeout") from None
            report_path = work / "worker_report.json"
            if not report_path.is_file() or report_path.stat().st_size > 150000:
                raise RestorePreparationError("restore_worker_failed")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise RestorePreparationError("restore_worker_failed")
            if completed.returncode:
                allowed = {"restore_migration_history_invalid", "restore_runtime_schema_mismatch", "restore_schema_incomplete",
                    "restore_migration_graph_conflict", "restore_backward_migration_forbidden", "restore_runtime_unavailable",
                    "restore_schema_mismatch", "restore_schema_reference_missing", "restore_schema_reference_stale",
                    "restore_schema_reference_invalid", "restore_schema_unreadable"}
                raise RestorePreparationError(report.get("error") if report.get("error") in allowed else "restore_migration_failed")
            after = validate_snapshot(candidate)
            if (after != report.get("migrations_after") or report.get("migrations_before") != before
                    or {tuple(row) for row in after} != {tuple(row) for row in expected_migrations}):
                raise RestorePreparationError("restore_schema_incomplete")
            if report.get("schema_audit_checked") is not True:
                raise RestorePreparationError("restore_schema_reference_invalid")
            size = candidate.stat().st_size
            if not 0 < size <= MAX_SNAPSHOT_BYTES:
                raise RestorePreparationError("restore_candidate_size_limit")
            manifest = {"format": "app-x-prepared-restore", "format_version": 1,
                "backup_id": verified.manifest["backup_id"], "source_snapshot_sha256": verified.manifest["snapshot_sha256"],
                "backup_created_at": verified.manifest["created_at"],
                "candidate_sha256": _digest(candidate), "candidate_bytes": size, **report,
                "prepared_at": datetime.now(timezone.utc).isoformat(), "migration_execution_checked": True,
                "activation_allowed": False, "permissions_revalidated": False, "live_database_replaced": False}
            with candidate.open("r+b") as target:
                target.flush()
                os.fsync(target.fileno())
            os.chmod(candidate, 0o600)
            final = output / "candidate.sqlite3"
            # Same filesystem: atomic publication with no overwrite or second plaintext allocation.
            os.link(candidate, final)
            published.append(final)
            receipt = output / "prepare_manifest.json"
            _write_json(receipt, manifest, owned=published)
        return PreparedRestore(final, manifest)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass
        raise
