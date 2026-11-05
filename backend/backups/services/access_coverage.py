"""Validate that every archived account/shop pair has its own current hold."""
import uuid


def checked_generation(database, *, prefix="", require_pending=False):
    if prefix not in ("", "restored."):
        raise ValueError("restore_access_coverage_invalid")
    row=database.execute(f"SELECT id FROM {prefix}backups_restoreaccessgeneration WHERE current_slot=1").fetchone()
    if row is None:
        raise ValueError("restore_access_coverage_invalid")
    identifier=uuid.UUID(row[0])
    missing=database.execute(f"""SELECT 1 FROM (
        SELECT id AS shop_id,owner_id AS user_id FROM {prefix}core_shop
        UNION SELECT shop_id,user_id FROM {prefix}core_membership
        ) AS pair WHERE NOT EXISTS (
            SELECT 1 FROM {prefix}backups_restoreaccessreview AS review
            WHERE review.generation_id=? AND review.shop_id=pair.shop_id AND review.user_id=pair.user_id
        ) LIMIT 1""",[identifier.hex]).fetchone()
    if missing or (require_pending and database.execute(f"""SELECT 1
        FROM {prefix}backups_restoreaccessdecision AS decision
        JOIN {prefix}backups_restoreaccessreview AS review ON review.id=decision.review_id
        WHERE review.generation_id=? LIMIT 1""",[identifier.hex]).fetchone()):
        raise ValueError("restore_access_coverage_invalid")
    return str(identifier)
