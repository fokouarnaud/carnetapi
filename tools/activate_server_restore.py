"""Open an installed restoration after current technical authentication; recovery stays provisional."""
import argparse
from getpass import getpass,GetPassWarning
import json
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
            password=getpass("Mot de passe technique actuel / Current technical password: ")
    if not isinstance(password,str) or not 1<=len(password)<=4096:raise ValueError()
    return password


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("gate","database","vault","backup-root"):parser.add_argument("--"+name,required=True,type=Path)
    for name in ("installation-id","bootstrap-operation","operation-id"):parser.add_argument("--"+name,required=True,type=uuid.UUID)
    parser.add_argument("--expected-version",required=True,type=int)
    parser.add_argument("--username",required=True)
    parser.add_argument("--reason",required=True)
    parser.add_argument("--acknowledge-provisional",required=True,action="store_true")
    parser.add_argument("--stdin-json",action="store_true")
    args=parser.parse_args(argv)
    try:password=_password(args.stdin_json)
    except (ValueError,UnicodeError,EOFError,GetPassWarning):
        print(json.dumps({"error":"restore_activation_input_invalid"}),file=sys.stderr);return 1
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"backend"))
    from backups.services.restore_activation import activate_restore,RestoreActivationError
    try:
        receipt=activate_restore(args.gate,args.database,args.vault,args.backup_root,
            installation_id=args.installation_id,bootstrap_operation=args.bootstrap_operation,operation_id=args.operation_id,
            expected_version=args.expected_version,username=args.username,password=password,reason=args.reason)
    except RestoreActivationError as error:
        print(json.dumps({"error":str(error)}),file=sys.stderr);return 1
    print(json.dumps(receipt,ensure_ascii=True));return 0


if __name__=="__main__":raise SystemExit(main())
