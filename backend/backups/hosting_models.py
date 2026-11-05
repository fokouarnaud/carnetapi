"""Operator reports and SMTP evidence never constitute automatic hosting renewal."""
from django.db import models
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns
from backups.recovery_models import _json_kind


class HostingRecord(UUIDRecord):
    created_at=models.DateTimeField(_("Created at (UTC)"),auto_now_add=True)
    updated_at=models.DateTimeField(_("Updated at (UTC)"),auto_now=True)
    class Meta:
        abstract=True


class HostingConfiguration(HostingRecord):
    singleton=models.PositiveSmallIntegerField(default=1,unique=True)
    version=models.PositiveBigIntegerField(_("Version"),default=1)
    enabled=models.BooleanField(_("Hosting reminders enabled"),default=True)
    expires_at=models.DateTimeField(_("Reported expiration (UTC)"))
    recipient=models.EmailField(_("Reminder recipient"))
    language=models.CharField(_("Reminder language"),max_length=2,choices=[("fr","Français"),("en","English")])
    evidence_reference=models.CharField(_("Verification reference"),max_length=500)
    updated_by=models.ForeignKey("core.User",verbose_name=_("Requested by"),on_delete=models.PROTECT,related_name="hosting_configurations")
    verified_generation_id=models.UUIDField(null=True,blank=True)
    last_reported_renewal_at=models.DateTimeField(null=True,blank=True)

    class Meta:
        verbose_name=_("Hosting configuration")
        verbose_name_plural=_("Hosting configurations")
        constraints=[models.CheckConstraint(condition=models.Q(singleton=1,version__gt=0)&_integer_columns("singleton","version"),name="hosting_singleton_version"),
            models.CheckConstraint(condition=models.Q(language__in=["fr","en"]),name="hosting_supported_language")]


class HostingConfigurationChange(HostingRecord):
    configuration=models.ForeignKey(HostingConfiguration,on_delete=models.PROTECT,related_name="changes")
    operation_id=models.UUIDField(unique=True)
    actor=models.ForeignKey("core.User",verbose_name=_("Operator"),on_delete=models.PROTECT,related_name="hosting_changes")
    previous_version=models.PositiveBigIntegerField()
    version=models.PositiveBigIntegerField()
    action=models.CharField(max_length=24,choices=[("verify_expiration",_("Verify reported expiration")),("confirm_renewal",_("Report manual renewal"))])
    reason=models.TextField(_("Reason"))
    payload=models.JSONField()
    before=models.JSONField()
    after=models.JSONField()

    class Meta:
        verbose_name=_("Hosting configuration change")
        verbose_name_plural=_("Hosting configuration changes")
        ordering=["version","id"]
        constraints=[models.UniqueConstraint(fields=["configuration","version"],name="hosting_change_version_once"),
            models.CheckConstraint(condition=models.Q(version=models.F("previous_version")+1)&_integer_columns("version","previous_version"),name="hosting_change_versions"),
            models.CheckConstraint(condition=_json_kind("payload","object")&_json_kind("before","object")&_json_kind("after","object"),name="hosting_change_json")]


class HostingReminder(HostingRecord):
    expires_at=models.DateTimeField(_("Reported expiration (UTC)"))
    lead_days=models.PositiveSmallIntegerField(_("Reminder lead days"))
    state=models.CharField(max_length=12,default="pending")
    version=models.PositiveBigIntegerField(default=0)
    current_attempt=models.ForeignKey("backups.HostingReminderAttempt",on_delete=models.PROTECT,null=True,blank=True,related_name="current_for_reminders")

    class Meta:
        verbose_name=_("Hosting reminder")
        verbose_name_plural=_("Hosting reminders")
        ordering=["expires_at","lead_days","id"]
        constraints=[models.UniqueConstraint(fields=["expires_at","lead_days"],name="hosting_expiration_reminder_once"),
            models.CheckConstraint(condition=models.Q(lead_days__in=[2,7])&_integer_columns("lead_days","version"),name="hosting_reminder_numbers"),
            models.CheckConstraint(condition=models.Q(state="pending",version=0,current_attempt__isnull=True)
                |models.Q(state__in=["sending","accepted","failed","uncertain"],version__gt=0,current_attempt__isnull=False),name="hosting_reminder_attempt_link")]


class HostingReminderAttempt(HostingRecord):
    reminder=models.ForeignKey(HostingReminder,on_delete=models.PROTECT,related_name="attempts")
    number=models.PositiveBigIntegerField()
    command_id=models.UUIDField(null=True,blank=True,unique=True)
    actor=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="hosting_reminder_attempts")
    state=models.CharField(max_length=12,default="sending")
    configuration_version=models.PositiveBigIntegerField()
    recipient=models.EmailField(_("Reminder recipient"))
    sender=models.EmailField()
    language=models.CharField(max_length=2)
    message_id=models.CharField(max_length=500)
    subject=models.CharField(max_length=255)
    body=models.TextField()
    reason=models.TextField(_("Reason"),blank=True)
    request=models.JSONField(default=dict)
    started_at=models.DateTimeField(_("Started at (UTC)"))
    finished_at=models.DateTimeField(_("Finished at (UTC)"),null=True,blank=True)
    failure_code=models.CharField(max_length=64,blank=True)

    class Meta:
        verbose_name=_("Hosting reminder attempt")
        verbose_name_plural=_("Hosting reminder attempts")
        ordering=["started_at","id"]
        constraints=[models.UniqueConstraint(fields=["reminder","number"],name="hosting_attempt_number_once"),
            models.CheckConstraint(condition=models.Q(number__gt=0,configuration_version__gt=0)&_integer_columns("number","configuration_version"),name="hosting_attempt_numbers"),
            models.CheckConstraint(condition=models.Q(state="sending",finished_at__isnull=True)
                |models.Q(state__in=["accepted","failed","uncertain"],finished_at__isnull=False),name="hosting_attempt_completion"),
            models.CheckConstraint(condition=models.Q(language__in=["fr","en"])&_json_kind("request","object"),name="hosting_attempt_language_request")]


class HostingObservation(HostingRecord):
    operation_id=models.UUIDField(unique=True)
    actor=models.ForeignKey("core.User",verbose_name=_("Operator"),on_delete=models.PROTECT,related_name="hosting_observations")
    observed_at=models.DateTimeField(_("Observation time"))
    installed_bytes=models.PositiveBigIntegerField(_("Reported installed bytes"),null=True,blank=True)
    temporary_peak_bytes=models.PositiveBigIntegerField(_("Reported temporary peak bytes"),null=True,blank=True)
    quota_used_bytes=models.PositiveBigIntegerField(_("Reported quota used bytes"),null=True,blank=True)
    quota_limit_bytes=models.PositiveBigIntegerField(_("Reported quota limit bytes"),null=True,blank=True)
    service_state=models.CharField(max_length=8,choices=[("up",_("Reported available")),("down",_("Reported unavailable")),("unknown",_("Unknown"))])
    evidence_reference=models.CharField(_("Verification reference"),max_length=500)
    reason=models.TextField(_("Reason"))
    request=models.JSONField()
    receipt=models.JSONField()

    class Meta:
        verbose_name=_("Hosting observation")
        verbose_name_plural=_("Hosting observations")
        ordering=["observed_at","id"]
        indexes=[models.Index(fields=["observed_at","id"],name="hosting_observation_page")]
        constraints=[models.CheckConstraint(condition=models.Q(service_state__in=["up","down","unknown"]),name="hosting_observation_state"),
            models.CheckConstraint(condition=_json_kind("request","object")&_json_kind("receipt","object"),name="hosting_observation_json")]+[
            models.CheckConstraint(condition=models.Q(**{name+"__isnull":True})|_integer_columns(name),name="hosting_"+name+"_integer")
            for name in ("installed_bytes","temporary_peak_bytes","quota_used_bytes","quota_limit_bytes")]


class HostingReminderRepair(HostingRecord):
    attempt=models.OneToOneField(HostingReminderAttempt,verbose_name=_("Hosting reminder attempt"),on_delete=models.PROTECT,related_name="interruption_review")
    operation_id=models.UUIDField(_("Operation UUID"),unique=True)
    actor=models.ForeignKey("core.User",verbose_name=_("Operator"),on_delete=models.PROTECT,related_name="hosting_reminder_repairs")
    gate_version=models.PositiveBigIntegerField(_("Maintenance gate version"))
    reason=models.TextField(_("Reason"))
    payload=models.JSONField(_("Request"))
    receipt=models.JSONField(_("Receipt"))

    class Meta:
        verbose_name=_("Hosting reminder interruption review")
        verbose_name_plural=_("Hosting reminder interruption reviews")
        ordering=["created_at","id"]
        constraints=[models.CheckConstraint(condition=models.Q(gate_version__gt=0)&_integer_columns("gate_version"),name="hosting_repair_gate_version"),
            models.CheckConstraint(condition=_json_kind("payload","object")&_json_kind("receipt","object"),name="hosting_repair_json")]
