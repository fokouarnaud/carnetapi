
from django.db import models
from common.model_base import UUIDRecord


class Activity(UUIDRecord):
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="activities")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="activities")
    action = models.CharField(max_length=80)
    target_id = models.UUIDField()
    reason = models.TextField()
    details = models.JSONField(default=dict)

    class Meta:
        db_table = "core_activity"
        app_label = "core"
        ordering = ["created_at", "id"]
