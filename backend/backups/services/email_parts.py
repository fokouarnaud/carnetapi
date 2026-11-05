"""Prepare bounded SMTP wire messages; this module never opens a connection."""
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from backups.services.jobs import part_path


class BackupEmailError(ValueError):
    """Stable error code without recipient, file content or SMTP credentials."""


@dataclass(frozen=True)
class PreparedPartEmail:
    sender: str
    recipient: str
    wire_bytes: bytes


def _address(value):
    try:
        if not isinstance(value, str) or any(character in value for character in "\r\n,;"):
            raise ValidationError("invalid")
        validate_email(value)
        value.encode("ascii")
    except (ValidationError, UnicodeError):
        raise BackupEmailError("backup_email_address_invalid") from None
    return value


def build_part_email(job, part, *, sender, max_message_bytes, possible_duplicate=False):
    """Return exactly the bytes to submit to SMTP after integrity/size checks.

    A caller must persist the send attempt and recheck authorization before
    transmission. Creating these bytes does not mean SMTP accepted the part.
    """
    if job.state != "ready":
        raise BackupEmailError("backup_email_not_ready")
    if (type(job.part_count) is not int or type(part.number) is not int
            or part.job_id != job.id
            or not 1 <= part.number <= job.part_count
            or type(part.size_bytes) is not int or not 0 < part.size_bytes <= 15000000):
        raise BackupEmailError("backup_email_part_invalid")
    if type(max_message_bytes) is not int or max_message_bytes <= 0:
        raise BackupEmailError("backup_email_size_limit_invalid")
    sender, recipient = _address(sender), _address(job.recipient)
    try:
        with part_path(job, part).open("rb") as source:
            data = source.read(part.size_bytes + 1)
        if len(data) != part.size_bytes or hashlib.sha256(data).hexdigest() != part.sha256:
            raise BackupEmailError("backup_part_integrity_failed")
    except OSError:
        raise BackupEmailError("backup_part_integrity_failed") from None
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = sender, recipient
    message["Date"] = format_datetime(job.created_at)
    message["Message-ID"] = f"<backup-{job.id}-part-{part.number}@app-x.invalid>"
    message["Subject"] = f"App X backup {job.id} - {part.number}/{job.part_count}"
    warning = "Doublon possible / Possible duplicate: meme UUID et numero de partie. / Same backup UUID and part number.\n" if possible_duplicate else ""
    message.set_content(
        f"Sauvegarde / Backup: {job.id}\n"
        f"Date UTC: {job.created_at.isoformat()}\n"
        f"Partie / Part: {part.number}/{job.part_count}\n"
        f"SHA-256: {part.sha256}\n"
        f"{warning}"
        "Conservez toutes les parties et la cle separement. / Keep all parts and the key separately.\n"
        "Une acceptation SMTP ne prouve pas la livraison. / SMTP acceptance does not prove delivery.\n")
    message.add_attachment(data, maintype="application", subtype="octet-stream", filename=part.filename)
    wire_bytes = message.as_bytes()
    if len(wire_bytes) > max_message_bytes:
        raise BackupEmailError("backup_email_too_large")
    return PreparedPartEmail(sender, recipient, wire_bytes)
