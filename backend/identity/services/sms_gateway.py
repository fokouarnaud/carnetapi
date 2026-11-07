"""Injectable SMS submission boundary; no claim of provider acceptance or delivery."""
import io
import json
import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from http.client import HTTPException, HTTPSConnection
from typing import Literal, Protocol
from urllib.parse import urlsplit

from config.cooms import CoomsConfiguration


MAX_RESPONSE_BYTES = 4096
MAX_MESSAGE_BYTES = 4096
_CAMEROON_PHONE = re.compile(r"\+237[0-9]{9}", flags=re.ASCII)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("SMS transport deadline elapsed.")
    return seconds


class _DeadlineReader(io.RawIOBase):
    """Keep HTTP status/header/body reads under the same socket deadline."""

    def __init__(self, stream, network_socket, deadline):
        self._stream = stream
        self._socket = network_socket
        self._deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self._socket.settimeout(_remaining(self._deadline))
        return self._stream.readinto(buffer)

    def close(self):
        if not self.closed:
            try:
                self._stream.close()
            finally:
                super().close()


class _DeadlineSocket:
    def __init__(self, network_socket, deadline):
        self._socket = network_socket
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._socket, name)

    def sendall(self, data):
        self._socket.settimeout(_remaining(self._deadline))
        return self._socket.sendall(data)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("SMS HTTP response requires a binary reader.")
        # A real socket file retains its own descriptor reference when
        # HTTPConnection closes a nonpersistent connection before body reads.
        stream = self._socket.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(stream, self._socket, self._deadline))


class SmsSendStatus(str, Enum):
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"


SmsSendReason = Literal[
    "http_submission_only", "not_configured", "invalid_request", "connection_unavailable",
    "transport_uncertain", "redirect_refused", "response_too_large", "http_rejected", "http_uncertain",
]


@dataclass(frozen=True, slots=True)
class SmsSendResult:
    """Expose only a local classification and optional HTTP status, never payloads.

    ACCEPTED means an HTTP 2xx response was observed, not that the undocumented
    Cooms business result accepted the SMS, nor that a recipient received/read it.
    UNCERTAIN must not trigger an automatic resend.
    """

    status: SmsSendStatus
    reason: SmsSendReason
    http_status: int | None = None


class SmsGateway(Protocol):
    """Allow the OTP domain to inject a gateway without owning HTTP or credentials."""

    def send(self, *, phone: str, message: str) -> SmsSendResult: ...


class CoomsSmsGateway:
    """Submit once over verified HTTPS, with one I/O deadline and bounded bytes.

    The connection factory is an infrastructure seam: tests can route the
    already-validated HTTPS endpoint to loopback without adding any production
    HTTP URL allowance. The default never follows redirects or uses a proxy.
    Network exceptions and provider response bodies are intentionally discarded.
    """

    def __init__(self, configuration: CoomsConfiguration | None, *, connection_factory=None, timeout_seconds=10):
        if configuration is not None and not isinstance(configuration, CoomsConfiguration):
            raise TypeError("A validated Cooms configuration is required.")
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10):
            raise ValueError("SMS socket timeout must be positive and at most ten seconds.")
        self.configuration = configuration
        self.connection_factory = HTTPSConnection if connection_factory is None else connection_factory
        self.timeout_seconds = timeout_seconds

    def send(self, *, phone: str, message: str) -> SmsSendResult:
        if self.configuration is None:
            return SmsSendResult(SmsSendStatus.UNAVAILABLE, "not_configured")
        if (type(phone) is not str or _CAMEROON_PHONE.fullmatch(phone) is None
                or type(message) is not str or not message.strip()):
            return SmsSendResult(SmsSendStatus.UNAVAILABLE, "invalid_request")
        try:
            if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                return SmsSendResult(SmsSendStatus.UNAVAILABLE, "invalid_request")
            body = json.dumps({"phone": phone, "message": message, "priority": 1},
                ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeError:
            return SmsSendResult(SmsSendStatus.UNAVAILABLE, "invalid_request")

        connection = None
        response = None
        request_started = False
        status = None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            endpoint = urlsplit(self.configuration.send_url)
            connection = self.connection_factory(endpoint.hostname, port=443, timeout=self.timeout_seconds)
            connection.connect()
            connection.sock = _DeadlineSocket(connection.sock, deadline)
            # A write error can follow partial transmission; it is never a safe
            # instruction to retry automatically.
            request_started = True
            connection.request("POST", endpoint.path, body=body, headers={
                "x-api-key": self.configuration.api_key,
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            status = response.status
            if type(status) is not int or not 100 <= status <= 599:
                status = None
                return SmsSendResult(SmsSendStatus.UNCERTAIN, "http_uncertain")
            if 300 <= status <= 399:
                return SmsSendResult(SmsSendStatus.UNCERTAIN, "redirect_refused", status)
            # The provider's business JSON is undocumented: bound and discard
            # it, rather than inventing an accepted/delivered field mapping.
            if len(response.read(MAX_RESPONSE_BYTES + 1)) > MAX_RESPONSE_BYTES:
                return SmsSendResult(SmsSendStatus.UNCERTAIN, "response_too_large", status)
            if 200 <= status <= 299:
                return SmsSendResult(SmsSendStatus.ACCEPTED, "http_submission_only", status)
            if 400 <= status <= 499 and status != 408:
                return SmsSendResult(SmsSendStatus.UNAVAILABLE, "http_rejected", status)
            return SmsSendResult(SmsSendStatus.UNCERTAIN, "http_uncertain", status)
        except (OSError, HTTPException):
            if request_started:
                return SmsSendResult(SmsSendStatus.UNCERTAIN, "transport_uncertain", status)
            return SmsSendResult(SmsSendStatus.UNAVAILABLE, "connection_unavailable")
        finally:
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, HTTPException):
                        pass
