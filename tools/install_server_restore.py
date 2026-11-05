"""Install one quarantined restore under maintenance; never reopen the service."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_dir",type=Path)
    for name in ("gate","database"):
        parser.add_argument("--"+name,required=True,type=Path)
    for name in ("operation-id","requested-by","reason"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--expected-version",required=True,type=int)
    args=parser.parse_args(argv)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"backend"))
    from backups.services.restore_installation import install_restore,RestoreInstallationError
    try:
        result=install_restore(args.gate,args.database,args.candidate_dir,operation_id=args.operation_id,
            requested_by=args.requested_by,reason=args.reason,expected_version=args.expected_version)
    except RestoreInstallationError as error:
        print(json.dumps({"error":str(error)}),file=sys.stderr)
        return 1
    except (OSError,ValueError,TypeError):
        print(json.dumps({"error":"restore_installation_failed"}),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
