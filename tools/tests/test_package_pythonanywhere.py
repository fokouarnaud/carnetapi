"""The server source package has no path to local business data or client files."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "repository"
    files = {
        "backend/manage.py": "# management entry point\n",
        "backend/requirements.txt": "Django==5.2.18\n",
        "backend/backup_schema.json": "[]\n",
        "backend/restore_schema.json": "{}\n",
        "backend/config/settings.py": "# runtime settings without secret values\n",
        "backend/config/wsgi.py": "# WSGI entry point\n",
    }
    for app in ("backups", "cash", "catalogue", "common", "core", "customers", "identity", "purchasing", "sales", "stock"):
        files[f"backend/{app}/__init__.py"] = ""
    for name in ("__init__", "applications", "hosting_repair_settings", "mail", "recovery", "restore_access_settings",
            "restore_settings", "technical_recovery_settings", "urls"):
        files[f"backend/config/{name}.py"] = "# deployment or isolated recovery settings\n"
    for language in ("en", "fr"):
        for suffix in ("json", "mo"):
            files[f"backend/core/locale/{language}/LC_MESSAGES/django.{suffix}"] = "runtime translation catalogue"
    for name in (
        "activate_server_restore", "backup_schema_manifest", "compile_messages", "install_server_restore",
        "prepare_server_restore", "quarantine_server_restore", "repair_hosting_reminder", "restore_maintenance",
        "restore_schema_manifest", "review_restore_access", "technical_recovery", "verify_server_backup",
    ):
        files[f"tools/{name}.py"] = "# operational console\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def write(root, name, content=b"runtime asset"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_package_preserves_runtime_and_operational_files_and_hashes_every_member(source, tmp_path):
    from tools.package_pythonanywhere import build_package
    included = (
        "backend/backups/services/restore_migrations.py",
        "backend/backups/management/commands/backup_scheduler_step.py",
        "backend/catalogue/migrations/0004_product_price_conflicts.py",
        "backend/backups/templates/admin/backups/recovery_close.html",
        "backend/core/locale/fr/LC_MESSAGES/django.mo",
        "backend/core/locale/en/LC_MESSAGES/django.json",
        "backend/catalogue/static/catalogue/product.css",
    )
    excluded = (
        "client/lib/app.py", "analysis/private.py", ".venv/credentials.py",
        "backend/db.sqlite3", "backend/db.sqlite3-wal", "backend/media/photo.png",
        "backend/runtime-control/restore-gate.sqlite3", "backend/backup-files/part.axbkp",
        "backend/verification/smoke.py", "backend/backups/tests/test_private.py",
        "backend/backups/services/__pycache__/private.pyc", "backend/backups/services/.env",
        "backend/backups/services/smtp.json", "backend/backups/services/backup.key",
        "backend/backups/services/credentials.py", "backend/config/local_settings.py",
        "backend/config/test_settings.py", "backend/requirements-dev.txt",
        "backend/secrets.py", "tools/unlisted_private.py",
        "backend/catalogue/static/catalogue/secret.key",
    )
    for name in included:
        write(source, name)
    for name in excluded:
        write(source, name, b"DO NOT PACKAGE PRIVATE DATA")
    output = tmp_path / "server.zip"
    result = build_package(source, output)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert set(included) <= names
        assert not names.intersection(excluded)
        assert "backend/backup_schema.json" in names
        assert "backend/restore_schema.json" in names
        assert "tools/technical_recovery.py" in names
        assert all(not name.startswith("/") and "\\" not in name and ".." not in Path(name).parts for name in names)
        manifest = json.loads(archive.read("server-package-manifest.json"))
        assert manifest["format"] == "app-x-pythonanywhere-source"
        assert manifest["format_version"] == 1
        assert {row["path"] for row in manifest["files"]} == names - {"server-package-manifest.json"}
        for row in manifest["files"]:
            content = archive.read(row["path"])
            assert row["bytes"] == len(content)
            assert row["sha256"] == hashlib.sha256(content).hexdigest()
            assert b"DO NOT PACKAGE PRIVATE DATA" not in content
    assert result["archive_bytes"] == output.stat().st_size
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["source_bytes"] == sum(row["bytes"] for row in manifest["files"])


def test_missing_restore_reference_fails_before_creating_any_archive(source, tmp_path):
    from tools.package_pythonanywhere import PackageError, build_package
    (source / "backend/restore_schema.json").unlink()
    output = tmp_path / "server.zip"
    with pytest.raises(PackageError, match="required_source_missing"):
        build_package(source, output)
    assert not output.exists()


@pytest.mark.parametrize("missing", ["backend/config/urls.py", "backend/core/locale/en/LC_MESSAGES/django.mo",
    "backend/core/locale/fr/LC_MESSAGES/django.json"])
def test_missing_runtime_settings_or_language_catalogue_is_not_a_successful_package(source, tmp_path, missing):
    from tools.package_pythonanywhere import PackageError, build_package
    (source / missing).unlink()
    output = tmp_path / "server.zip"
    with pytest.raises(PackageError, match="required_source_missing"):
        build_package(source, output)
    assert not output.exists()


def test_existing_destination_is_never_overwritten(source, tmp_path):
    from tools.package_pythonanywhere import PackageError, build_package
    output = write(tmp_path, "server.zip", b"previous artifact")
    with pytest.raises(PackageError, match="destination_exists"):
        build_package(source, output)
    assert output.read_bytes() == b"previous artifact"


@pytest.mark.parametrize("linked", ["backend/backups/services", "backend/backups/services/safe.py"])
def test_windows_reparse_directory_or_file_is_rejected_without_reading_it(source, tmp_path, monkeypatch, linked):
    from tools.package_pythonanywhere import PackageError, build_package
    write(source, "backend/backups/services/safe.py")
    original = Path.lstat

    def reparse_stat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        if path == source / linked:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        return actual

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    output = tmp_path / "server.zip"
    with pytest.raises(PackageError, match="unsafe_source_path"):
        build_package(source, output)
    assert not output.exists()


def test_output_parent_reparse_is_rejected_and_source_is_not_changed(source, tmp_path, monkeypatch):
    from tools.package_pythonanywhere import PackageError, build_package
    destination = tmp_path / "linked-output"
    destination.mkdir()
    original = Path.lstat

    def reparse_stat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        if path == destination:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        return actual

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    with pytest.raises(PackageError, match="unsafe_destination_path"):
        build_package(source, destination / "server.zip")
    assert list(destination.iterdir()) == []


def test_identical_source_produces_identical_archives_without_local_times_or_permissions(source, tmp_path):
    from tools.package_pythonanywhere import build_package
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    build_package(source, first)
    build_package(source, second)
    assert first.read_bytes() == second.read_bytes()


def test_development_manifest_generator_cannot_import_test_settings_in_the_server_package(source, tmp_path):
    from tools.package_pythonanywhere import build_package
    write(source, "backend/config/test_settings.py", b"# test-only insecure signing keys and password hashing")
    write(source, "tools/backup_schema_manifest.py", b"# requires config.test_settings at build time")
    output = tmp_path / "server.zip"
    build_package(source, output)
    with zipfile.ZipFile(output) as archive:
        assert "backend/config/test_settings.py" not in archive.namelist()
        assert "tools/backup_schema_manifest.py" not in archive.namelist()
        assert "tools/restore_schema_manifest.py" in archive.namelist()
