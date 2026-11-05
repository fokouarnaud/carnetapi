"""Read-only isolated authentication and restoration audit; never loads deployment settings."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import unicodedata
import uuid


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    os.environ["DJANGO_SETTINGS_MODULE"]="config.technical_recovery_settings"
    try:
        raw=sys.stdin.read(16385)
        if len(raw.encode("utf-8"))>16384:raise ValueError()
        values=json.loads(raw)
        if (not isinstance(values,dict) or set(values)!={"username","password","bootstrap_operation","bootstrap_gate_version","first_open"}
                or not isinstance(values["username"],str) or not 1<=len(values["username"])<=150
                or not isinstance(values["password"],str) or not 1<=len(values["password"])<=4096
                or type(values["first_open"]) is not bool):raise ValueError()
        import django
        django.setup()
        from django.contrib.auth.hashers import check_password
        from backups.services.recovery_web import recovery_identity
        from backups.services.recovery_snapshot import known_device_rows
        business=Path(sys.argv[1]).resolve()
        identity=recovery_identity(business,Path(sys.argv[2]),backup_root=Path(sys.argv[3]),
            bootstrap_operation=values["bootstrap_operation"],gate_version=values["bootstrap_gate_version"])
        user=uuid.UUID(identity["user_id"])
        generation=uuid.UUID(identity["generation_id"])
        with closing(sqlite3.connect(business.as_uri()+"?mode=ro",uri=True)) as db:
            row=db.execute("SELECT password FROM core_user WHERE id=? AND username=? AND is_active=1 AND is_staff=1 AND is_superuser=1",
                [user.hex,unicodedata.normalize("NFKC",values["username"])]).fetchone()
            if row is None or not check_password(values["password"],row[0]):raise ValueError()
            backup=db.execute("SELECT backup_id FROM backups_restoreaccessgeneration WHERE id=? AND current_slot=1",[generation.hex]).fetchone()
            session=db.execute("SELECT id,inventory_recorded_at FROM backups_restoresession WHERE generation_id=?",[generation.hex]).fetchone()
            if backup is None or session is None or session[1] is None:raise ValueError()
            if values["first_open"]:
                checks=(
                    ("SELECT 1 FROM core_user WHERE typeof(is_active)!='integer' OR is_active NOT IN (0,1) OR typeof(is_staff)!='integer' OR is_staff NOT IN (0,1) OR typeof(is_superuser)!='integer' OR is_superuser NOT IN (0,1) LIMIT 1",[]),
                    ("SELECT 1 FROM core_user WHERE id!=? AND (is_staff!=0 OR is_superuser!=0) LIMIT 1",[user.hex]),
                    ("SELECT 1 FROM core_user_groups LIMIT 1",[]),
                    ("SELECT 1 FROM core_user_user_permissions LIMIT 1",[]),
                    ("SELECT 1 FROM core_installation WHERE revoked_at IS NULL LIMIT 1",[]),
                    ("SELECT 1 FROM core_devicesession WHERE revoked_at IS NULL LIMIT 1",[]),
                    ("SELECT 1 FROM core_shopdeviceaccess WHERE revoked_at IS NULL OR verified_at IS NOT NULL OR offline_expires_at IS NOT NULL LIMIT 1",[]),
                )
                if any(db.execute(sql,params).fetchone() is not None for sql,params in checks):raise ValueError()
                expected={(shop,uuid.UUID(install).hex,uuid.UUID(owner).hex,label) for shop,install,owner,label in known_device_rows(db)}
                recorded=set(db.execute("SELECT shop_id,origin_installation_id,origin_user_id,label FROM backups_recoverydevice WHERE session_id=?",[session[0]]).fetchall())
                if expected!=recorded:raise ValueError()
        print(json.dumps({"user_id":str(user),"generation_id":str(generation),"backup_id":str(uuid.UUID(backup[0])),
            "schema_checked":True,"identity_confirmed":True,"first_open_checks_applied":values["first_open"]}))
        return 0
    except Exception:
        print(json.dumps({"error":"restore_activation_identity_unavailable"}));return 1


if __name__=="__main__":raise SystemExit(main())
