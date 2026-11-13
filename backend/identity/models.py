import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(
        _("phone"), max_length=16, unique=True, null=True, blank=True,
        validators=[RegexValidator(r"^\+[1-9][0-9]{7,14}$")],
    )
    language = models.CharField(_("Language"), max_length=2, choices=[("fr", "Français"), ("en", "English")], default="fr")
    language_version = models.PositiveBigIntegerField(default=1)

    class Meta(AbstractUser.Meta):
        db_table = "core_user"
        app_label = "core"
        abstract = False
        constraints = [
            models.CheckConstraint(condition=models.Q(language__in=["fr", "en"]), name="user_supported_language"),
            models.CheckConstraint(condition=models.Q(language_version__gt=0) & _integer_columns("language_version"),
                                   name="user_positive_language_version"),
        ]


class LanguagePreferenceChange(UUIDRecord):
    """An account preference receipt is independent of shop activity and rights."""
    user = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="language_changes")
    before_language = models.CharField(max_length=2)
    after_language = models.CharField(max_length=2)
    version = models.PositiveBigIntegerField()
    request = models.JSONField()
    response = models.JSONField()

    class Meta:
        db_table = "core_languagepreferencechange"
        constraints = [
            models.UniqueConstraint(fields=["user", "version"], name="language_change_user_version_once"),
            models.CheckConstraint(condition=models.Q(before_language__in=["fr", "en"], after_language__in=["fr", "en"]),
                                   name="language_change_supported_values"),
            models.CheckConstraint(condition=models.Q(version__gt=1) & _integer_columns("version"), name="language_change_positive_version"),
        ]


class Shop(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    name = models.CharField(_("name"), max_length=150)
    owner = models.ForeignKey('core.User', verbose_name=_("Owner"), on_delete=models.PROTECT, related_name="owned_shops")
    timezone = models.CharField(_("Time zone"), max_length=64, default="Africa/Douala")
    is_active = models.BooleanField(_("Active"), default=True)
    seller_discount_cap_basis_points = models.PositiveSmallIntegerField(_("Seller discount cap (basis points)"), default=0)
    discount_policy_version = models.PositiveBigIntegerField(_("Discount policy version"), default=1)

    class Meta:
        db_table = "core_shop"
        ordering = ["created_at", "id"]
        verbose_name = _("Shop")
        verbose_name_plural = _("Shops")
        constraints = [
            models.CheckConstraint(condition=_integer_columns("seller_discount_cap_basis_points", "discount_policy_version"),
                                   name="shop_integral_discount_policy"),
            models.CheckConstraint(condition=models.Q(seller_discount_cap_basis_points__lte=10000), name="shop_discount_cap_range"),
            models.CheckConstraint(condition=models.Q(discount_policy_version__gt=0), name="discount_policy_positive_version"),
        ]

    def __str__(self):
        return self.name


class Membership(UUIDRecord):
    class Role(models.TextChoices):
        MANAGER = "manager", _("Manager")
        SELLER = "seller", _("Seller")

    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    shop = models.ForeignKey('identity.Shop', verbose_name=_("Shop"), on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey('core.User', verbose_name=_("User"), on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(_("Role"), max_length=10, choices=Role.choices, default=Role.SELLER)
    is_active = models.BooleanField(_("Active"), default=True)
    can_grant_credit = models.BooleanField(_("Can grant credit"), default=False)
    can_collect_debt = models.BooleanField(_("Can collect debts"), default=True)
    can_read_debt = models.BooleanField(_("Can view debts"), default=False)
    can_prepare_reminder = models.BooleanField(_("Can prepare reminders"), default=False)
    can_read_cost = models.BooleanField(_("Can view costs"), default=False)
    can_read_cash_balance = models.BooleanField(_("Can view total cash balance"), default=False)
    version = models.PositiveBigIntegerField(_("Version"), default=1)

    def __str__(self):
        return f"{self.user} — {self.shop}"

    class Meta:
        db_table = "core_membership"
        verbose_name = _("Team member")
        verbose_name_plural = _("Team members")
        constraints = [
            models.CheckConstraint(condition=_integer_columns("version"), name="membership_integral_version"),
            models.UniqueConstraint(fields=["shop", "user"], name="unique_shop_membership"),
            models.CheckConstraint(condition=models.Q(role__in=["manager", "seller"]), name="membership_supported_role"),
            models.CheckConstraint(condition=models.Q(version__gt=0), name="membership_positive_version"),
        ]
        indexes = [models.Index(fields=["user", "is_active", "shop"], name="core_member_user_id_f362ea_idx")]


class Installation(UUIDRecord):
    """An account's installation identity, retained across token rotations."""

    user = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="installations")
    label = models.CharField(max_length=120, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_installation"


class DeviceSession(UUIDRecord):
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="sessions")
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    refresh_jti = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "core_devicesession"


class ShopDeviceAccess(UUIDRecord):
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="device_accesses")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="shop_accesses")
    verified_at = models.DateTimeField(null=True, blank=True)
    offline_expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_shopdeviceaccess"
        constraints = [models.UniqueConstraint(fields=["shop", "installation"], name="unique_shop_installation")]


class ShopInstallationCode(UUIDRecord):
    """G26: readable code of an installation in one shop; numbers are never reused."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="installation_codes")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="shop_codes")
    number = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shop", "number"], name="installation_code_number_once"),
            models.UniqueConstraint(fields=["shop", "installation"], name="installation_code_once_per_shop"),
            models.CheckConstraint(condition=models.Q(number__gt=0) & _integer_columns("number"), name="installation_code_positive"),
        ]

    @property
    def label(self):
        return f"T{self.number:02d}"


class OwnershipTransfer(UUIDRecord):
    """US-47 / G24: superadmin-only reassignment; history stays attributed to its authors."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="ownership_transfers")
    previous_owner = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="+")
    new_owner = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    proofs = models.JSONField()

    class Meta:
        constraints = [models.CheckConstraint(condition=~models.Q(reason=""), name="ownership_transfer_reason")]


class AccountRecoveryCase(UUIDRecord):
    """US-07 / G22: exceptional recovery; only a reference of the identity document is kept."""
    account = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="recovery_cases")
    new_phone = models.CharField(max_length=16, validators=[RegexValidator(r"^\+[1-9][0-9]{7,14}$")])
    identity_reference = models.CharField(max_length=120)
    facts_checked = models.PositiveSmallIntegerField()
    confirming_member = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    effective_after = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(facts_checked__gte=3), name="recovery_case_three_facts"),
                       models.CheckConstraint(condition=~models.Q(identity_reference="") & ~models.Q(reason=""),
                                              name="recovery_case_reference_and_reason")]


class OtpChallenge(UUIDRecord):
    """US-01 / G4: one SMS budget admission per challenge; only an HMAC of the code is kept."""
    class Purpose(models.TextChoices):
        SIGNUP = "signup", _("Sign-up")
        NEW_DEVICE = "new_device", _("New device")
        RECOVERY = "recovery", _("Recovery")

    operation_id = models.UUIDField(unique=True)
    request_digest = models.CharField(max_length=64)
    phone = models.CharField(max_length=16, validators=[RegexValidator(r"^\+237[0-9]{9}$")])
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    send_status = models.CharField(max_length=12, blank=True, default="")
    budget_date = models.DateField()
    # Recovery only: the account moving to ``phone`` and the code reserved for it.
    account = models.ForeignKey('core.User', null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    recovery_code = models.ForeignKey('identity.RecoveryCode', null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="challenges")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(purpose__in=["signup", "new_device", "recovery"]),
                                   name="otp_challenge_purpose"),
            models.CheckConstraint(
                condition=(models.Q(purpose="recovery", account__isnull=False, recovery_code__isnull=False)
                           | (~models.Q(purpose="recovery") & models.Q(account__isnull=True, recovery_code__isnull=True))),
                name="otp_challenge_recovery_binding"),
            models.CheckConstraint(condition=models.Q(attempts__lte=5) & _integer_columns("attempts"), name="otp_challenge_attempts"),
        ]
        indexes = [models.Index(fields=["phone", "created_at"], name="otp_challenge_phone_idx")]


class RecoveryCode(UUIDRecord):
    """US-07 / Q154: one-time code kept by the holder off the phone; only a keyed hash is stored."""
    account = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="recovery_codes")
    batch_id = models.UUIDField()
    code_hash = models.CharField(max_length=64, unique=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "batch_id"], name="recovery_code_batch_idx")]
