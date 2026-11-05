"""Snapshot potentially useful device identities before revoking archived rights."""
from datetime import timezone
import uuid


def known_device_rows(database):
    """Observe account scopes and historical device grants, including withdrawals."""
    return database.execute("""SELECT scopes.shop_id,i.id,i.user_id,i.label FROM core_installation i
        JOIN (SELECT id AS shop_id,owner_id AS user_id FROM core_shop
              UNION SELECT shop_id,user_id FROM core_membership) scopes ON scopes.user_id=i.user_id
        UNION SELECT a.shop_id,i.id,i.user_id,i.label FROM core_shopdeviceaccess a
              JOIN core_installation i ON i.id=a.installation_id
        ORDER BY 1,2""").fetchall()


def snapshot_recovery(database,generation_id,instant,*,snapshot_at=None):
    if snapshot_at is not None:
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("restore_snapshot_time_requires_timezone")
        snapshot_at=snapshot_at.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    generation=uuid.UUID(str(generation_id))
    session=uuid.uuid5(generation,"recovery-session")
    database.execute("INSERT INTO backups_restoresession "
        "(id,created_at,updated_at,generation_id,snapshot_at,inventory_recorded_at) VALUES(?,?,?,?,?,?)",
        [session.hex,instant,instant,generation.hex,snapshot_at,instant])
    for shop,installation,user,label in known_device_rows(database):
        identifier=uuid.uuid5(session,str(uuid.UUID(shop))+":"+str(uuid.UUID(installation)))
        database.execute("INSERT INTO backups_recoverydevice "
            "(id,created_at,updated_at,session_id,shop_id,origin_installation_id,origin_user_id,label) VALUES(?,?,?,?,?,?,?,?)",
            [identifier.hex,instant,instant,session.hex,shop,uuid.UUID(installation).hex,uuid.UUID(user).hex,label])
