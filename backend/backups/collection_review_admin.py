"""Technical inspection never changes business review decisions."""
import json
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from backups.models import RecoveryCollectionReview,RecoveryCollectionReviewDecision
from backups.recovery_admin import RecoveryReadOnlyAdmin


class CollectionReviewReadOnlyAdmin(RecoveryReadOnlyAdmin):
    generation_lookup="capture__session__generation_id"

    def review_for(self,obj):return obj

    def generation_for(self,obj):return obj.capture.session.generation

    @admin.display(description=_("Shop"))
    def shop_display(self,obj):return self.review_for(obj).capture.shop.name

    @admin.display(description=_("Source operation UUID"))
    def source_display(self,obj):return self.review_for(obj).source_operation_id

    @admin.display(description=_("Customer UUID"))
    def customer_display(self,obj):return self.review_for(obj).customer_id

    @admin.display(description=_("State"),ordering="state")
    def state_display(self,obj):return {"pending":_("Pending"),"refused":_("Refused"),"validated":_("Validated")}[obj.state]

    @admin.display(description=_("Version"),ordering="version")
    def version_display(self,obj):return obj.version

    @admin.display(description=_("Created at (UTC)"),ordering="created_at")
    def created_display(self,obj):return obj.created_at


@admin.register(RecoveryCollectionReview)
class RecoveryCollectionReviewAdmin(CollectionReviewReadOnlyAdmin):
    list_display=["id","shop_display","source_display","state_display","version_display","amounts_display","history_display"]
    readonly_fields=[*list_display,"generation_display","customer_display","created_display"]
    fields=readonly_fields

    def get_queryset(self,request):
        return super().get_queryset(request).select_related("capture__session__generation","capture__shop")

    @admin.display(description=_("Source received / applied / unallocated (FCFA)"))
    def amounts_display(self,obj):return f"{obj.amount_fcfa} / {obj.original_applied_fcfa} / {obj.original_unallocated_fcfa}"

    @admin.display(description=_("Review history"))
    def history_display(self,obj):
        return format_html('<a href="{}?review__id__exact={}">{}</a>',
            reverse("admin:backups_recoverycollectionreviewdecision_changelist"),obj.id,_("Review history"))


@admin.register(RecoveryCollectionReviewDecision)
class RecoveryCollectionReviewDecisionAdmin(CollectionReviewReadOnlyAdmin):
    list_display=["id","shop_display","source_display","state_display","version_display","reviewer_display","created_display"]
    readonly_fields=[*list_display,"generation_display","customer_display","reason_display","integration_display","request_display","receipt_display"]
    fields=readonly_fields

    def review_for(self,obj):return obj.review

    def get_queryset(self,request):
        return super().get_queryset(request).select_related("review__capture__shop","capture__session__generation")

    @admin.display(description=_("Reviewer at decision time"))
    def reviewer_display(self,obj):return obj.reviewed_by_id

    @admin.display(description=_("Reason"))
    def reason_display(self,obj):return obj.reason

    @admin.display(description=_("Integration decision UUID"))
    def integration_display(self,obj):return obj.integration_decision_id

    @admin.display(description=_("Request"))
    def request_display(self,obj):return json.dumps(obj.request,ensure_ascii=False,indent=2)

    @admin.display(description=_("Receipt"))
    def receipt_display(self,obj):return json.dumps(obj.receipt,ensure_ascii=False,indent=2)
