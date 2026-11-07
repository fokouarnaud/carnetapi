"""Load private Cooms settings explicitly, without an implicit credential file."""
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


COOMS_KEYS = frozenset({"COOMS_API_KEY", "COOMS_SMS_SEND_URL"})
MAX_PRIVATE_CONFIG_BYTES = 16_384
_SEND_URL = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+cooms\.app"
    r"(?::443)?/api/v1/sms/send",
    flags=re.ASCII,
)


@dataclass(frozen=True, slots=True)
class CoomsConfiguration:
    """Keep the credential out of representations and restrict the live endpoint."""

    api_key: str = field(repr=False)
    send_url: str

    def __post_init__(self):
        if (type(self.api_key) is not str or not 1 <= len(self.api_key) <= 2048
                or any(not 33 <= ord(character) <= 126 for character in self.api_key)):
            raise ImproperlyConfigured("Invalid private Cooms credential.")
        if (type(self.send_url) is not str or len(self.send_url) > 2048
                or _SEND_URL.fullmatch(self.send_url) is None):
            raise ImproperlyConfigured("Invalid Cooms HTTPS send endpoint.")


def _unique_object(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate configuration key.")
        values[key] = value
    return values


def cooms_settings(environ: Mapping, *, secret_path=None) -> CoomsConfiguration | None:
    """Read an explicit JSON file, then environment overrides; absent means disabled.

    ``APP_OTP_COOMS_CONFIG_FILE`` selects the private JSON file. No file in the
    current directory is discovered automatically. Only the two documented
    Cooms keys are allowed in JSON; incomplete configuration fails closed.
    """
    values = {}
    path = secret_path if secret_path is not None else environ.get("APP_OTP_COOMS_CONFIG_FILE")
    if path is not None:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_PRIVATE_CONFIG_BYTES + 1)
            if len(raw) > MAX_PRIVATE_CONFIG_BYTES:
                raise ValueError("Configuration exceeds the bound.")
            values = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
            if not isinstance(values, dict) or set(values) - COOMS_KEYS:
                raise ValueError("Unsupported configuration structure.")
        except (OSError, ValueError, TypeError):
            raise ImproperlyConfigured("Cannot read private Cooms configuration.") from None
    values = {**values, **{key: environ[key] for key in COOMS_KEYS if key in environ}}
    if not values:
        return None
    if set(values) != COOMS_KEYS:
        raise ImproperlyConfigured("Both Cooms configuration fields are required.")
    return CoomsConfiguration(api_key=values["COOMS_API_KEY"], send_url=values["COOMS_SMS_SEND_URL"])
