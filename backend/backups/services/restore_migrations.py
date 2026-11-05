"""Internal subprocess. Only the coordinator's disposable candidate may be migrated."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys


class RestoreMigrationError(ValueError):
    pass


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_report(path, data):
    with path.open("x", encoding="utf-8") as target:
        json.dump(data, target, sort_keys=True)
        target.flush()
        os.fsync(target.fileno())


def configure_work(work, nonce):
    candidate = work / "candidate.sqlite3"
    request_path = work / "request.json"
    if (work.is_symlink() or not work.name.startswith("restore-work-") or candidate.is_symlink()
            or candidate.resolve().parent != work.resolve() or request_path.stat().st_size > 150000):
        raise RestoreMigrationError("restore_worker_request_invalid")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("nonce") != nonce or digest(candidate) != request.get("snapshot_sha256"):
        raise RestoreMigrationError("restore_worker_request_invalid")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.restore_settings"
    os.environ["APP_RESTORE_WORKDIR"] = str(work.resolve())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import django
    django.setup()
    return request


def check_current_tables(connection, apps):
    tables = set(connection.introspection.table_names())
    with connection.cursor() as cursor:
        for model in apps.get_models(include_auto_created=True):
            if not model._meta.managed or model._meta.proxy or model._meta.swapped:
                continue
            table = model._meta.db_table
            if table not in tables:
                raise RestoreMigrationError("restore_schema_incomplete")
            # Only names are needed here. Read SQLite metadata directly: Django's
            # collation parser splits CHECK expressions on commas and can fail on
            # nested JSON constraints. Full SQL is audited separately below.
            cursor.execute("SELECT name FROM pragma_table_xinfo(%s)", [table])
            actual = {row[0] for row in cursor.fetchall()}
            if not {field.column for field in model._meta.local_concrete_fields}.issubset(actual):
                raise RestoreMigrationError("restore_schema_incomplete")


def schema_metadata(executor, migrations):
    import django
    import sqlparse
    from backups.services import schema_audit
    sources = [[app, name, digest(inspect.getfile(type(executor.loader.disk_migrations[(app, name)])))]
        for app, name in sorted(executor.loader.disk_migrations)]
    return {"migration_code_sha256": hashlib.sha256(json.dumps(sources, separators=(",", ":")).encode()).hexdigest(),
        "worker_sha256": digest(Path(__file__)), "normalizer_sha256": digest(Path(schema_audit.__file__)),
        "migrations": migrations, "runtime": {"django": django.get_version(),
            "sqlite": sqlite3.sqlite_version, "sqlparse": sqlparse.__version__}}


def build_schema_reference(work, nonce):
    request = configure_work(work, nonce)
    candidate = work / "candidate.sqlite3"
    if request.get("purpose") != "schema-reference" or candidate.stat().st_size != 0:
        raise RestoreMigrationError("restore_worker_request_invalid")
    from django.apps import apps
    from django.core.management import call_command
    from django.db import connection, connections
    from django.db.migrations.executor import MigrationExecutor
    from backups.services.archive import validate_snapshot
    from backups.services.schema_audit import schema_profile, SchemaAuditError
    try:
        call_command("migrate", interactive=False, verbosity=0)
        check_current_tables(connection, apps)
        executor = MigrationExecutor(connection)
        connections.close_all()
        migrations = validate_snapshot(candidate)
        if sorted(tuple(row) for row in migrations) != sorted(executor.loader.disk_migrations):
            raise RestoreMigrationError("restore_schema_incomplete")
        return {"format": "app-x-sqlite-schema-reference", "format_version": 1,
            **schema_metadata(executor, migrations), "schema": schema_profile(candidate)}
    except SchemaAuditError as error:
        raise RestoreMigrationError(str(error)) from None
    finally:
        connections.close_all()


def migrate_candidate(work, nonce):
    request = configure_work(work, nonce)
    candidate = work / "candidate.sqlite3"
    from django.apps import apps
    from django.core.management import call_command
    from django.db import connection, connections
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.exceptions import InconsistentMigrationHistory
    from backups.services.archive import validate_snapshot
    from backups.services.schema_audit import audit_schema, SchemaAuditError
    try:
        before = validate_snapshot(candidate)
        if before != request["migrations_before"]:
            raise RestoreMigrationError("restore_worker_request_invalid")
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=DELETE")
        executor = MigrationExecutor(connection)
        known = sorted(executor.loader.disk_migrations)
        if known != sorted(tuple(row) for row in request["expected_migrations"]):
            raise RestoreMigrationError("restore_runtime_schema_mismatch")
        try:
            executor.loader.check_consistent_history(connection)
        except InconsistentMigrationHistory:
            raise RestoreMigrationError("restore_migration_history_invalid") from None
        if executor.loader.detect_conflicts():
            raise RestoreMigrationError("restore_migration_graph_conflict")
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if any(backwards for _migration, backwards in plan):
            raise RestoreMigrationError("restore_backward_migration_forbidden")
        call_command("migrate", interactive=False, verbosity=0)
        check_current_tables(connection, apps)
        connections.close_all()
        after = validate_snapshot(candidate)
        if sorted(tuple(row) for row in after) != known:
            raise RestoreMigrationError("restore_schema_incomplete")
        metadata = schema_metadata(executor, after)
        audit = audit_schema(candidate, Path(__file__).resolve().parents[2] / "restore_schema.json", metadata)
        return {"migrations_before": before, "migrations_after": after,
            "applied_migrations": [row for row in after if row not in before],
            "migration_code_sha256": metadata["migration_code_sha256"], **audit,
            "runtime": {"python": platform.python_version(), **metadata["runtime"]}}
    except SchemaAuditError as error:
        raise RestoreMigrationError(str(error)) from None
    finally:
        connections.close_all()


def main():
    work = Path(sys.argv[1]).absolute()
    report = work / "worker_report.json"
    try:
        if len(sys.argv) == 4 and sys.argv[3] == "--schema-reference":
            data = build_schema_reference(work, sys.argv[2])
        elif len(sys.argv) == 3:
            data = migrate_candidate(work, sys.argv[2])
        else:
            raise RestoreMigrationError("restore_worker_request_invalid")
    except RestoreMigrationError as error:
        write_report(report, {"error": str(error)})
        return 1
    except ImportError:
        write_report(report, {"error": "restore_runtime_unavailable"})
        return 1
    except Exception:
        write_report(report, {"error": "restore_migration_failed"})
        return 1
    write_report(report, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
