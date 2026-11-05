"""Shared recovery metadata for authorized pages, without device identities."""
from copy import deepcopy
from drf_spectacular.utils import extend_schema_field,extend_schema_serializer
from rest_framework import serializers
from common.services.pagination import BoundedPagination
from backups.services.recovery_status import recovery_status_for_shop


RECOVERY_SCHEMA={"type":"object","nullable":True,
    "description":"Known scoped devices are observations, not proof of exhaustive recovery. Null means no current restoration.",
    "required":["generation_id","session_id","state","snapshot_at","started_at","inventory_recorded_at","known_devices",
        "closure_id","closed_at","missing_devices","historical_missing_declarations","missing_data","late_operations","late_review_required"],
    "properties":{
        "generation_id":{"type":"string","format":"uuid"},
        "session_id":{"type":"string","format":"uuid","nullable":True},
        "state":{"type":"string","enum":["recovering","closed_partial"],"description":"A partial technical declaration never proves exhaustive recovery."},
        "snapshot_at":{"type":"string","format":"date-time","nullable":True,"description":"Actual archive snapshot date, unknown for legacy metadata."},
        "started_at":{"type":"string","format":"date-time"},
        "inventory_recorded_at":{"type":"string","format":"date-time","nullable":True},
        "known_devices":{"type":"integer","minimum":0,"nullable":True,
            "description":"Only this shop. Null means inventory not recorded; zero never proves there are no unknown phones."},
        "closure_id":{"type":"string","format":"uuid","nullable":True},
        "closed_at":{"type":"string","format":"date-time","nullable":True},
        "missing_devices":{"type":"integer","minimum":0,"nullable":True,"description":"Unavailable device observations explicitly declared in this shop in the current closure; null before declaration."},
        "historical_missing_declarations":{"type":"integer","minimum":0,"description":"Prior declarations of missing data in this shop remain visible across restore generations; this is not a count of unique phones."},
        "missing_data":{"type":"boolean","nullable":True,"description":"An explicit current or earlier missing-data declaration persists; null means no current declaration establishes completeness."},
        "late_operations":{"type":"integer","minimum":0,"nullable":True,"description":"This shop's append-only capture count minus its frozen count at closure, independent of server clock changes. Null means legacy or inconsistent evidence cannot establish the count; no implied business validation."},
        "late_review_required":{"type":"boolean"}}}


@extend_schema_serializer(many=False)
class RecoveryReportSerializer(serializers.Serializer):
    """Explain live restoration independently from a report's historical cutoff.

    Callers must authorize the shop before passing it in serializer context.
    This metadata never changes the report's financial or stock calculations.
    """
    restoration=serializers.SerializerMethodField()

    @extend_schema_field(RECOVERY_SCHEMA)
    def get_restoration(self,obj):
        return recovery_status_for_shop(self.context["shop"])


class RecoveryPagination(BoundedPagination):
    def paginate_queryset(self,queryset,request,view=None):
        self.restoration=recovery_status_for_shop(view.get_shop())
        return super().paginate_queryset(queryset,request,view)

    def get_paginated_response(self,data):
        response=super().get_paginated_response(data)
        response.data["restoration"]=self.restoration
        return response

    def get_paginated_response_schema(self,schema):
        result=super().get_paginated_response_schema(schema)
        result["properties"]["restoration"]=deepcopy(RECOVERY_SCHEMA)
        result["required"].append("restoration")
        return result
