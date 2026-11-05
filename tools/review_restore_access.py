"""Inspect or decide restored responsible access from the hosting console."""
import argparse
from getpass import getpass, GetPassWarning
import json
import os
from pathlib import Path
import sys
import uuid
import warnings


def _password(stdin_json):
    if stdin_json:
        if sys.stdin.isatty():
            raise ValueError()
        raw = sys.stdin.read(16385)
        if len(raw.encode("utf-8")) > 16384:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"password"}:
            raise ValueError()
        password = data["password"]
    else:
        if not sys.stdin.isatty():
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("Mot de passe technique / Technical password: ")
    if not isinstance(password, str) or not 1 <= len(password) <= 4096:
        raise ValueError()
    return password


def _action(args, password):
    from django.contrib.auth.hashers import check_password
    from django.db import transaction
    from rest_framework.exceptions import PermissionDenied
    from backups.models import RestoreAccessReview
    from backups.services.access_decisions import decide_responsible_access
    from backups.services.access_inspection import inspect_access_reviews
    from common.services.errors import OperationConflict
    from identity.models import User

    with transaction.atomic():
        actor = User.objects.filter(username=User.normalize_username(args.username), is_active=True,
            is_staff=True, is_superuser=True).first()
        if actor is None or not check_password(password, actor.password):
            raise PermissionDenied()
        if args.action == "inspect":
            return inspect_access_reviews(actor, page=args.page, page_size=args.page_size,
                state=args.state, shop_id=args.shop_id)
        review = RestoreAccessReview.objects.filter(id=args.review_id,
            generation_id=args.generation_id, generation__current_slot=1).first()
        if review is None:
            raise OperationConflict()
        decision, _created = decide_responsible_access(actor=actor, review_id=review.id,
            operation_id=args.operation_id, previous_version=args.previous_version,
            state=args.state, reason=args.reason, evidence_ref=args.evidence_ref)
        return {"generation_id":str(review.generation_id), "decision":{
            "id":str(decision.id), "operation_id":str(decision.operation_id), "review_id":str(review.id),
            "shop_id":str(review.shop_id), "user_id":str(review.user_id), "requested_by":str(decision.requested_by_id),
            "state":decision.state, "version":decision.version, "reason":decision.reason,
            "evidence_ref":decision.evidence_ref}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("inspect", "decide"):
        command = commands.add_parser(action)
        for name in ("gate", "database"):
            command.add_argument("--"+name, required=True, type=Path)
        command.add_argument("--gate-version", required=True, type=int)
        command.add_argument("--username", required=True)
        command.add_argument("--stdin-json", action="store_true")
        if action == "inspect":
            command.add_argument("--page", type=int, default=1)
            command.add_argument("--page-size", type=int, default=25)
            command.add_argument("--state", choices=("all", "pending", "confirmed", "refused"), default="all")
            command.add_argument("--shop-id", type=uuid.UUID)
        else:
            for name in ("review-id", "generation-id", "operation-id"):
                command.add_argument("--"+name, required=True, type=uuid.UUID)
            command.add_argument("--previous-version", required=True, type=int)
            command.add_argument("--state", required=True, choices=("confirmed", "refused"))
            command.add_argument("--reason", required=True)
            command.add_argument("--evidence-ref", default="")
    args = parser.parse_args(argv)
    try:
        password = _password(args.stdin_json)
    except (ValueError, UnicodeError, EOFError, GetPassWarning):
        print(json.dumps({"error":"restore_access_input_invalid"}), file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent/"backend"))
    from backups.services.restore_gate import hold_control, RestoreGateError
    try:
        with hold_control(args.gate, args.database, required_mode="maintenance", expected_version=args.gate_version):
            os.environ["DJANGO_SETTINGS_MODULE"] = "config.restore_access_settings"
            os.environ["APP_RESTORE_ACCESS_DATABASE"] = str(args.database.resolve())
            import django
            django.setup()
            from django.db import connections
            try:
                from django.conf import settings
                binding = connections["default"].settings_dict
                if (settings.SETTINGS_MODULE != "config.restore_access_settings"
                        or binding["ENGINE"] != "django.db.backends.sqlite3"
                        or binding["NAME"] != args.database.resolve().as_uri()+"?mode=rw"):
                    raise ValueError("restore_access_binding_invalid")
                from backups.services.technical_recovery import _schema
                _schema(args.database)
                result = _action(args, password)
            finally:
                connections.close_all()
            result.update(activation_allowed=False, gate_mode="maintenance", gate_version=args.gate_version)
    except RestoreGateError:
        print(json.dumps({"error":"restore_access_gate_unavailable"}), file=sys.stderr)
        return 1
    except Exception as error:
        from rest_framework.exceptions import APIException
        code = str(error.get_codes()) if isinstance(error, APIException) and isinstance(error.get_codes(), str) else "restore_access_console_failed"
        print(json.dumps({"error":code}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
