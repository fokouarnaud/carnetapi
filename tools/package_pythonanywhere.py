"""Package allowlisted server sources for manual transfer; never deploy or copy data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile


APPLICATIONS = ("backups", "cash", "catalogue", "common", "core", "customers", "identity", "purchasing", "sales", "stock")
CODE_SUBDIRECTORIES = ("", "api", "services", "migrations", "management", "management/commands")
CONFIG_MODULES = (
    "__init__", "applications", "hosting_repair_settings", "mail", "recovery", "restore_access_settings",
    "restore_settings", "settings", "technical_recovery_settings", "urls", "wsgi",
)
OPERATIONAL_TOOLS = (
    "activate_server_restore", "compile_messages", "install_server_restore",
    "prepare_server_restore", "quarantine_server_restore", "repair_hosting_reminder", "restore_maintenance",
    "restore_schema_manifest", "review_restore_access", "technical_recovery", "verify_server_backup",
)
ROOT_FILES = (
    "backend/manage.py", "backend/requirements.txt", "backend/backup_schema.json", "backend/restore_schema.json",
)
DOCUMENTS = ("README.md", "BACKUPS.md", "STORAGE.md", "ARCHITECTURE.md")
STATIC_SUFFIXES = {".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2", ".ttf"}
MODULE_NAME = re.compile(r"[A-Za-z0-9_]+\.py\Z")
PRIVATE_MODULE = re.compile(r"(?:secrets?|credentials?|private|local_settings|settings_local)(?:_|\.|$)", re.IGNORECASE)
MANIFEST_NAME = "server-package-manifest.json"


class PackageError(ValueError):
    """The source or destination does not satisfy the source-package contract."""


def _safe_path(path, *, destination=False):
    """Inspect lexical ancestors before resolution, including Windows junctions."""
    code = "unsafe_destination_path" if destination else "unsafe_source_path"
    current = None
    for part in (*reversed(path.parents), path):
        try:
            current = part.lstat()
        except FileNotFoundError:
            current = None
            continue
        if stat.S_ISLNK(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400:
            raise PackageError(code)
    return current


def _read_source(root, relative):
    path = root / relative
    metadata = _safe_path(path)
    if metadata is None:
        raise PackageError("required_source_missing")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PackageError("unsafe_source_path")
    # Compare the opened handle before reading, so replacement with another file
    # or a link cannot silently change which source is being packaged.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        _safe_path(path)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PackageError("source_changed")
        content = source.read()
        after = os.fstat(source.fileno())
    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or len(content) != opened.st_size:
        raise PackageError("source_changed")
    return content


def _asset_files(root, relative, suffixes):
    directory = root / relative
    metadata = _safe_path(directory)
    if metadata is None:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise PackageError("unsafe_source_path")
    for child in sorted(directory.iterdir()):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        child_relative = child.relative_to(root).as_posix()
        child_metadata = _safe_path(child)
        if stat.S_ISDIR(child_metadata.st_mode):
            yield from _asset_files(root, child_relative, suffixes)
        elif child.suffix.lower() in suffixes:
            yield child_relative


def _source_paths(root):
    required = (*ROOT_FILES, *(f"backend/config/{name}.py" for name in CONFIG_MODULES),
        *(f"backend/{app}/__init__.py" for app in APPLICATIONS),
        *(f"tools/{name}.py" for name in OPERATIONAL_TOOLS),
        *(f"backend/core/locale/{language}/LC_MESSAGES/django.{suffix}"
            for language in ("en", "fr") for suffix in ("json", "mo")))
    paths = set(required)
    for app in APPLICATIONS:
        for subdirectory in CODE_SUBDIRECTORIES:
            directory = root / "backend" / app / subdirectory
            metadata = _safe_path(directory)
            if metadata is None:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise PackageError("unsafe_source_path")
            for file in directory.iterdir():
                if (MODULE_NAME.fullmatch(file.name) and not PRIVATE_MODULE.match(file.name)
                        and not file.name.startswith("test_")):
                    paths.add(file.relative_to(root).as_posix())
        paths.update(_asset_files(root, f"backend/{app}/templates", {".html"}))
        paths.update(_asset_files(root, f"backend/{app}/static", STATIC_SUFFIXES))
    for name in DOCUMENTS:
        relative = f"backend/{name}"
        if _safe_path(root / relative) is not None:
            paths.add(relative)
    return sorted(paths)


def _member(archive, name, content):
    # Reproducible archives do not expose the workstation's file times/ACLs.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, content, compresslevel=9)


def build_package(source_root, output):
    """Write a new ZIP of server code and a hash manifest, without importing Django."""
    root = Path(os.path.abspath(source_root))
    output = Path(os.path.abspath(output))
    root_metadata = _safe_path(root)
    if root_metadata is None or not stat.S_ISDIR(root_metadata.st_mode):
        raise PackageError("required_source_missing")
    if _safe_path(output, destination=True) is not None:
        raise PackageError("destination_exists")
    contents = {name: _read_source(root, name) for name in _source_paths(root)}
    manifest = {"format": "app-x-pythonanywhere-source", "format_version": 1,
        "files": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in contents.items()]}
    source_bytes = sum(row["bytes"] for row in manifest["files"])
    manifest["source_bytes"] = source_bytes
    output.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(output, destination=True)
    created = False
    try:
        with output.open("xb") as target:
            created = True
            with zipfile.ZipFile(target, "w") as archive:
                for name, content in contents.items():
                    _member(archive, name, content)
                _member(archive, MANIFEST_NAME, (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        return {"package": str(output), "files": len(contents), "source_bytes": source_bytes,
            "archive_bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    except FileExistsError:
        raise PackageError("destination_exists") from None
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_package(Path(__file__).absolute().parent.parent, args.output)
    except (PackageError, OSError, ValueError):
        print(json.dumps({"error": "server_source_package_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
