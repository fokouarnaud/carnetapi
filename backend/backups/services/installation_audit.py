"""Isolated readonly audit; never loads deployment settings or the target DB."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    os.environ["DJANGO_SETTINGS_MODULE"]="config.technical_recovery_settings"
    try:
        import django
        django.setup()
        from backups.services.technical_recovery import _schema
        from backups.services.restore_quarantine import _COUNTS
        from backups.services.access_coverage import checked_generation
        from backups.services.recovery_snapshot import known_device_rows
        candidate=Path(sys.argv[1]).resolve()
        _schema(candidate)
        with closing(sqlite3.connect(candidate.as_uri()+"?mode=ro",uri=True)) as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0]!="delete" or any(db.execute(sql).fetchone()[0] for sql in _COUNTS.values()):
                raise ValueError()
            generation=checked_generation(db,require_pending=True)
            session=db.execute("SELECT id,inventory_recorded_at FROM backups_restoresession WHERE generation_id=?",
                [uuid.UUID(generation).hex]).fetchone()
            if session is None or session[1] is None:
                raise ValueError()
            expected={(shop,uuid.UUID(install).hex,uuid.UUID(user).hex,label)
                for shop,install,user,label in known_device_rows(db)}
            recorded=set(db.execute("SELECT shop_id,origin_installation_id,origin_user_id,label FROM backups_recoverydevice WHERE session_id=?",
                [session[0]]).fetchall())
            if expected!=recorded:
                raise ValueError()
            backup=db.execute("SELECT backup_id FROM backups_restoreaccessgeneration WHERE current_slot=1").fetchone()[0]
        print(json.dumps({"generation_id":generation,"backup_id":str(uuid.UUID(backup)),"schema_checked":True}))
        return 0
    except Exception:
        print(json.dumps({"error":"restore_installation_candidate_invalid"}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
