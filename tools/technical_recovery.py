"""Hosting-console recovery with an external one-use code; never opens the HTTP gate."""
import argparse
from getpass import getpass, GetPassWarning
import json
import os
from pathlib import Path
import sys
import warnings


def _secrets_from_input(stdin_json):
    if stdin_json:
        if sys.stdin.isatty():
            raise ValueError()
        raw = sys.stdin.read(16385)
        if len(raw.encode("utf-8")) > 16384:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"code", "password", "password_confirmation"}:
            raise ValueError()
        code, password, confirmation = data["code"], data["password"], data["password_confirmation"]
    else:
        if not sys.stdin.isatty():
            raise ValueError()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                code = getpass("Code de secours / Recovery code: ")
                password = getpass("Nouveau mot de passe / New password: ")
                confirmation = getpass("Confirmation / Confirm password: ")
        except GetPassWarning:
            raise ValueError() from None
    if (not all(isinstance(value, str) for value in (code, password, confirmation))
            or not code or not password or password != confirmation):
        raise ValueError()
    return code, password


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("provision", "bootstrap"):
        command = commands.add_parser(action)
        for path in ("gate", "vault", "database"):
            command.add_argument("--" + path, required=True, type=Path)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--requested-by", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--gate-version", required=True, type=int)
        if action == "provision":
            command.add_argument("--backup-root", required=True, type=Path)
            command.add_argument("--show-code", action="store_true")
        else:
            command.add_argument("--username", required=True)
            command.add_argument("--language", choices=("fr", "en"), default="fr")
            command.add_argument("--stdin-json", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "provision" and not (args.show_code and sys.stdin.isatty() and sys.stdout.isatty()):
        print(json.dumps({"error": "technical_recovery_interactive_required"}), file=sys.stderr)
        return 1
    try:
        secret, password = _secrets_from_input(args.stdin_json) if args.action == "bootstrap" else (None, None)
    except (ValueError, TypeError, UnicodeError, EOFError):
        print(json.dumps({"error": "technical_recovery_input_invalid"}), file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    # Isolated settings intentionally ignore broken web settings and production
    # secret files. No ORM database is configured; services use explicit paths.
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.technical_recovery_settings"
    try:
        import django
        django.setup()
        from backups.services.technical_recovery import provision_technical_recovery, bootstrap_technical_recovery
        from backups.services.recovery_proof import RecoveryProofError
    except Exception:
        print(json.dumps({"error": "technical_recovery_runtime_unavailable"}), file=sys.stderr)
        return 1
    try:
        identity = dict(operation_id=args.operation_id, requested_by=args.requested_by,
                        reason=args.reason, gate_version=args.gate_version)
        if args.action == "provision":
            issued = provision_technical_recovery(args.gate, args.vault, args.database, args.backup_root, **identity)
            print("Code technique : conserver hors serveur et hors journaux. / Keep off-server and out of logs.")
            print(issued.secret)
            result = {"proof_id": issued.id, "code_delivered_to_console": True}
        else:
            result = bootstrap_technical_recovery(args.gate, args.vault, args.database, secret,
                username=args.username, password=password, language=args.language, **identity)
    except RecoveryProofError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "technical_recovery_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
