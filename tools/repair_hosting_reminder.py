"""Classify an interrupted hosting reminder under exclusive normal gate control.

Never sends mail, changes hosting expiration, or opens a maintenance gate.
"""
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
        if sys.stdin.isatty():raise ValueError()
        raw=sys.stdin.read(16385)
        if len(raw.encode("utf-8"))>16384:raise ValueError()
        data=json.loads(raw)
        if not isinstance(data,dict) or set(data)!={"password"}:raise ValueError()
        password=data["password"]
    else:
        if not sys.stdin.isatty():raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error",GetPassWarning)
            password=getpass("Mot de passe technique / Technical password: ")
    if not isinstance(password,str) or not 1<=len(password)<=4096:raise ValueError()
    return password


def _action(args,password,control):
    from django.contrib.auth.hashers import check_password
    from django.db import transaction
    from backups.services.hosting_repair import HostingRepairError, _repair_under_control
    from identity.models import User
    with transaction.atomic():
        actor=User.objects.filter(username=User.normalize_username(args.username),is_active=True,is_staff=True,is_superuser=True).first()
        if actor is None or not check_password(password,actor.password):raise HostingRepairError("hosting_repair_forbidden")
        return _repair_under_control(actor,attempt_id=args.attempt_id,operation_id=args.operation_id,reason=args.reason,control=control)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("gate","database"):parser.add_argument("--"+name,type=Path,required=True)
    for name in ("attempt-id","operation-id"):parser.add_argument("--"+name,type=uuid.UUID,required=True)
    parser.add_argument("--gate-version",type=int,required=True)
    parser.add_argument("--username",required=True)
    parser.add_argument("--reason",required=True)
    parser.add_argument("--acknowledge-uncertain",action="store_true",required=True)
    parser.add_argument("--stdin-json",action="store_true")
    args=parser.parse_args(argv)
    try:
        password=_password(args.stdin_json)
        if not args.reason.strip() or len(args.reason)>2000:raise ValueError()
    except (ValueError,UnicodeError,EOFError,GetPassWarning):
        print(json.dumps({"error":"hosting_repair_input_invalid"}),file=sys.stderr);return 1
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"backend"))
    from backups.services.restore_gate import hold_control, RestoreGateError
    try:
        with hold_control(args.gate,args.database,required_mode="open",expected_version=args.gate_version) as control:
            os.environ["DJANGO_SETTINGS_MODULE"]="config.hosting_repair_settings"
            os.environ["APP_HOSTING_REPAIR_DATABASE"]=str(args.database.resolve())
            import django
            from django.db import connections
            try:
                django.setup()
                from django.conf import settings
                binding=connections["default"].settings_dict
                if (settings.SETTINGS_MODULE!="config.hosting_repair_settings" or binding["ENGINE"]!="django.db.backends.sqlite3"
                        or binding["NAME"]!=args.database.resolve().as_uri()+"?mode=rw"):raise ValueError()
                from backups.services.technical_recovery import _schema, TechnicalRecoveryError
                try:_schema(args.database)
                except TechnicalRecoveryError:
                    print(json.dumps({"error":"hosting_repair_schema_invalid"}),file=sys.stderr);return 1
                result=_action(args,password,control)
            finally:connections.close_all()
    except RestoreGateError:
        print(json.dumps({"error":"hosting_repair_gate_unavailable"}),file=sys.stderr);return 1
    except Exception as error:
        error_class=getattr(sys.modules.get("backups.services.hosting_repair"),"HostingRepairError",None)
        code=str(error) if error_class is not None and isinstance(error,error_class) else "hosting_repair_failed"
        print(json.dumps({"error":code}),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=True));return 0


if __name__=="__main__":raise SystemExit(main())
