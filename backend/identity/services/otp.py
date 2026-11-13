"""US-01 / G4: OTP challenges bound to one global SMS budget admission each.

Policy (6 November 2026): 6 digits, valid 10 minutes, 5 verification attempts
per code; every send, resends included, consumes one of 10 daily slots.
The code is never stored, logged or returned in clear.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from config.cooms import cooms_settings
from identity.models import DeviceSession, Installation, OtpChallenge, RecoveryCode, User
from identity.services.sms_budget import SmsBudgetConflict, SmsBudgetStore, SmsBudgetUnavailable, SmsQuotaReached
from identity.services.sms_gateway import CoomsSmsGateway, SmsSendResult, SmsSendStatus

CODE_LIFETIME = timedelta(minutes=10)
MAX_ATTEMPTS = 5
PURPOSES = ("signup", "new_device", "recovery")
RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 12
CAMEROON_PHONE = re.compile(r"\+237[0-9]{9}", flags=re.ASCII)
MESSAGES = {
    "fr": "Code de vérification : {code}. Valable 10 minutes. Ne le partagez pas.",
    "en": "Verification code: {code}. Valid for 10 minutes. Do not share it.",
}
FAKE_TRANSPORT_ENVIRONMENTS = ("development", "test")

# G4: dev/test transport only; never a proof of OTP delivery (DOD-09).
fake_outbox = []


class OtpError(Exception):
    def __init__(self, code, **details):
        self.code = code
        self.details = details
        super().__init__(code)


class FakeSmsGateway:
    def send(self, *, phone, message):
        fake_outbox.append({"phone": phone, "message": message})
        return SmsSendResult(SmsSendStatus.ACCEPTED, "http_submission_only", None)


def sms_gateway():
    transport = getattr(settings, "OTP_SMS_TRANSPORT", "cooms")
    if transport == "fake":
        if getattr(settings, "APP_ENV", "production") not in FAKE_TRANSPORT_ENVIRONMENTS:
            raise ImproperlyConfigured("The fake SMS transport is restricted to development and test.")
        return FakeSmsGateway()
    if transport == "cooms":
        return CoomsSmsGateway(cooms_settings(os.environ))
    raise ImproperlyConfigured("Unknown OTP_SMS_TRANSPORT.")


def sms_budget():
    path = getattr(settings, "SMS_BUDGET_PATH", None)
    if not path:
        raise OtpError("sms_budget_unavailable")
    return SmsBudgetStore(path)


def _digest(operation_id, phone, purpose, language, old_phone=None, recovery_hash=None):
    canonical = json.dumps({"operation_id": str(operation_id), "phone": phone, "purpose": purpose,
                            "language": language, "old_phone": old_phone, "recovery_hash": recovery_hash},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _code_hash(challenge_id, code):
    return hmac.new(settings.SECRET_KEY.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _normalized_recovery_code(code):
    value = code.replace("-", "").replace(" ", "").upper() if isinstance(code, str) else ""
    if len(value) != RECOVERY_CODE_LENGTH or any(char not in RECOVERY_ALPHABET for char in value):
        return None
    return value


def _recovery_hash(normalized):
    return hmac.new(settings.SECRET_KEY.encode(), f"recovery:{normalized}".encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def generate_recovery_codes(user):
    """Return fresh plaintext codes ONCE; earlier unused codes stop working."""
    now = timezone.now()
    RecoveryCode.objects.filter(account=user, consumed_at__isnull=True, revoked_at__isnull=True).update(
        revoked_at=now, updated_at=now)
    batch = uuid.uuid4()
    codes = []
    while len(codes) < RECOVERY_CODE_COUNT:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        if raw in codes:
            continue
        codes.append(raw)
    RecoveryCode.objects.bulk_create(
        [RecoveryCode(account=user, batch_id=batch, code_hash=_recovery_hash(raw)) for raw in codes])
    return [f"{raw[:4]}-{raw[4:8]}-{raw[8:]}" for raw in codes]


def _usable_recovery_code(code_hash, old_phone):
    return (RecoveryCode.objects.select_related("account")
            .filter(code_hash=code_hash, consumed_at__isnull=True, revoked_at__isnull=True,
                    account__phone=old_phone, account__is_active=True).first())


def request_code(*, operation_id, phone, purpose, language="fr", old_phone=None, recovery_code=None):
    """Admit at most one provider call per operation; a replay returns the original challenge."""
    if purpose not in PURPOSES or not isinstance(phone, str) or not CAMEROON_PHONE.fullmatch(phone):
        raise OtpError("otp_request_invalid")
    recovery_fields = (old_phone is not None, recovery_code is not None)
    if recovery_fields != ((True, True) if purpose == "recovery" else (False, False)):
        raise OtpError("otp_request_invalid")
    language = language if language in MESSAGES else "fr"
    recovery_hash = None
    if purpose == "recovery":
        normalized = _normalized_recovery_code(recovery_code)
        recovery_hash = _recovery_hash(normalized) if normalized else ""
    digest = _digest(operation_id, phone, purpose, language, old_phone, recovery_hash)
    existing = OtpChallenge.objects.filter(operation_id=operation_id).first()
    if existing is not None:
        if existing.request_digest != digest:
            raise OtpError("otp_operation_conflict")
        return existing
    # Checked before admission so a pointless request never spends one of the 10 daily SMS.
    holder = User.objects.filter(phone=phone).first()
    account = code_row = None
    if purpose == "signup" and holder is not None:
        raise OtpError("otp_account_exists")
    if purpose == "new_device" and holder is None:
        raise OtpError("otp_account_unknown")
    if purpose == "recovery":
        code_row = _usable_recovery_code(recovery_hash, old_phone) if recovery_hash else None
        if code_row is None:
            raise OtpError("otp_recovery_unknown")
        account = code_row.account
        if holder is not None and holder.id != account.id:
            raise OtpError("otp_phone_taken")

    gateway = sms_gateway()
    try:
        receipt, admitted = sms_budget().reserve(operation_id=operation_id, request_digest=digest)
    except SmsQuotaReached as error:
        raise OtpError("sms_daily_quota_reached", next_allowed_at=error.next_allowed_at) from None
    except SmsBudgetConflict:
        raise OtpError("otp_operation_conflict") from None
    except SmsBudgetUnavailable:
        raise OtpError("sms_budget_unavailable") from None
    if not admitted:
        # The slot belongs to an interrupted earlier attempt: never resend under it.
        raise OtpError("otp_send_uncertain")

    code = f"{secrets.randbelow(10 ** 6):06d}"
    try:
        with transaction.atomic():
            challenge = OtpChallenge.objects.create(
                operation_id=operation_id, request_digest=digest, phone=phone, purpose=purpose,
                expires_at=timezone.now() + CODE_LIFETIME, budget_date=receipt.local_date,
                account=account, recovery_code=code_row,
            )
            challenge.code_hash = _code_hash(challenge.id, code)
            challenge.expires_at = challenge.created_at + CODE_LIFETIME
            challenge.save(update_fields=["code_hash", "expires_at", "updated_at"])
    except IntegrityError:
        raise OtpError("otp_operation_conflict") from None
    result = gateway.send(phone=phone, message=MESSAGES[language].format(code=code))
    challenge.send_status = result.status.value
    challenge.save(update_fields=["send_status", "updated_at"])
    if result.status is SmsSendStatus.UNAVAILABLE:
        raise OtpError("sms_transport_unavailable")
    return challenge


@dataclass(frozen=True)
class Activation:
    user: User
    installation: Installation
    access: str
    refresh: str


def _recover(challenge, now):
    """Consume the reserved code; the account keeps its UUID, history and current (not past) rights."""
    consumed = RecoveryCode.objects.filter(id=challenge.recovery_code_id, account_id=challenge.account_id,
                                           consumed_at__isnull=True, revoked_at__isnull=True).update(
        consumed_at=now, updated_at=now)
    if consumed != 1:
        raise OtpError("otp_challenge_closed")
    account = User.objects.select_for_update().get(id=challenge.account_id)
    if not account.is_active:
        raise OtpError("otp_account_unknown")
    if User.objects.filter(phone=challenge.phone).exclude(id=account.id).exists():
        raise OtpError("otp_phone_taken")
    account.phone = challenge.phone
    account.save(update_fields=["phone"])
    return account


def _activate(challenge, installation_label, now):
    if challenge.purpose == "recovery":
        user = _recover(challenge, now)
    elif challenge.purpose == "signup":
        if User.objects.filter(phone=challenge.phone).exists():
            raise OtpError("otp_account_exists")
        # Username derives from the stable UUID: a phone can later move to another account (recovery).
        account_id = uuid.uuid4()
        user = User.objects.create_user(id=account_id, username=str(account_id), phone=challenge.phone)
    else:
        user = User.objects.filter(phone=challenge.phone, is_active=True).first()
        if user is None:
            raise OtpError("otp_account_unknown")
    installation = Installation.objects.create(user=user, label=installation_label[:120])
    refresh = RefreshToken.for_user(user)
    session = DeviceSession.objects.create(
        installation=installation, expires_at=datetime.fromtimestamp(refresh["exp"], tz=dt_timezone.utc),
    )
    refresh["session_id"] = str(session.id)
    session.refresh_jti = refresh["jti"]
    session.save(update_fields=["refresh_jti", "updated_at"])
    return Activation(user, installation, str(refresh.access_token), str(refresh))


def verify_code(*, challenge_id, code, installation_label=""):
    """A correct live code creates or reuses the account and activates a NEW installation."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code, flags=re.ASCII):
        raise OtpError("otp_code_malformed")
    failure = None
    with transaction.atomic():
        challenge = OtpChallenge.objects.select_for_update().filter(id=challenge_id).first()
        now = timezone.now()
        if (challenge is None or challenge.consumed_at is not None or challenge.attempts >= MAX_ATTEMPTS
                or challenge.expires_at <= now or challenge.send_status in ("", "unavailable")
                or not challenge.code_hash):
            raise OtpError("otp_challenge_closed")
        if hmac.compare_digest(challenge.code_hash, _code_hash(challenge.id, code)):
            challenge.consumed_at = now
            challenge.save(update_fields=["consumed_at", "updated_at"])
            return _activate(challenge, installation_label or "", now)
        # The failed attempt must commit, so the error is raised after the block.
        challenge.attempts += 1
        challenge.save(update_fields=["attempts", "updated_at"])
        remaining = MAX_ATTEMPTS - challenge.attempts
        failure = (OtpError("otp_code_invalid", attempts_remaining=remaining) if remaining
                   else OtpError("otp_challenge_closed"))
    raise failure
