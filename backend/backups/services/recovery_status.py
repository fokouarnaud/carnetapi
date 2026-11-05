"""Explain the current restore without claiming unknown devices are recovered."""
from django.db.models import BooleanField, IntegerField
from django.db.models.expressions import RawSQL
from backups.models import RestoreAccessGeneration,RestoreSession,RecoveryDevice,RecoveryClosure,RecoveryJournalOperation


def recovery_status_for_shop(shop):
    """Return only the authorized shop's inventory; the caller authorizes scope.

    The registry is an observation of known devices, never evidence that every
    surviving phone is known or recovered. Closure is a separate technical act.
    """
    generation=RestoreAccessGeneration.objects.filter(current_slot=1).first()
    if generation is None:
        return None
    session=RestoreSession.objects.filter(generation=generation).first()
    recorded=session is not None and session.inventory_recorded_at is not None
    # Only scoped counts leave SQLite; a shop read never loads foreign origins.
    matching_devices="FROM json_each(backups_recoveryclosure.missing_devices) device WHERE json_extract(device.value,'$.shop_id')=%s"
    closure=(RecoveryClosure.objects.filter(session=session).annotate(scoped_missing=RawSQL(
        "(SELECT COUNT(*) "+matching_devices+")",[str(shop.id)],output_field=IntegerField()),
        scoped_captures=RawSQL("CASE WHEN json_type(snapshot,'$.captured_operation_counts')='object' "
            "THEN coalesce(json_extract(snapshot,%s),0) ELSE NULL END",
            ['$.captured_operation_counts."'+str(shop.id)+'"'],output_field=IntegerField()))
        .values("id","created_at","scoped_missing","scoped_captures").first()) if session else None
    historical=(RecoveryClosure.objects.exclude(session=session).alias(scoped=RawSQL(
        "EXISTS(SELECT 1 "+matching_devices+")",[str(shop.id)],output_field=BooleanField()))
        .filter(scoped=True).count())
    missing=closure["scoped_missing"] if closure else None
    late=0
    if closure:
        # Captures are append-only. Frozen scoped counts survive equal timestamps
        # or a server clock adjustment; a legacy missing count remains unknown.
        before=closure["scoped_captures"]
        current=RecoveryJournalOperation.objects.filter(session=session,shop=shop).count()
        late=current-before if type(before) is int and 0<=before<=current else None
    return {"generation_id":str(generation.id),"session_id":str(session.id) if session else None,
        "state":"closed_partial" if closure else "recovering","snapshot_at":session.snapshot_at if session else None,
        "started_at":generation.created_at,"inventory_recorded_at":session.inventory_recorded_at if session else None,
        "known_devices":RecoveryDevice.objects.filter(session=session,shop=shop).count() if recorded else None,
        "closure_id":str(closure["id"]) if closure else None,"closed_at":closure["created_at"] if closure else None,
        "missing_devices":missing,"historical_missing_declarations":historical,
        "missing_data":True if historical else missing>0 if missing is not None else None,
        "late_operations":late,"late_review_required":late is None or late>0}
