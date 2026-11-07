"""Exercise SMS HTTP boundaries against loopback, never a real provider."""
import io
import json
import logging
import ssl
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from config.cooms import CoomsConfiguration
from identity.services.sms_gateway import CoomsSmsGateway, SmsSendStatus


FAKE_KEY = "fixture-only-cooms-key-not-a-live-credential"
FAKE_PHONE = "+237699000000"
FAKE_MESSAGE = "Code fictif de test : 481205. Ne pas envoyer."
PUBLIC_URL = "https://gw-89.cooms.app/api/v1/sms/send"


@contextmanager
def local_provider(*, status=202, body=b'{"undocumented":true}', headers=None, delay=0,
        byte_delay=0, server_tls=None, client_tls=None):
    observations = []
    response_finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            observations.append({"method": self.command, "path": self.path,
                "headers": dict(self.headers.items()), "body": self.rfile.read(size)})
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if byte_delay:
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(byte_delay)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                response_finished.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if server_tls is not None:
        server.socket = server_tls.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def connection_factory(host, *, port, timeout):
        if host != "gw-89.cooms.app" or port != 443:
            raise AssertionError("The validated live endpoint was unexpectedly broadened.")
        if server_tls is not None:
            return HTTPSConnection("127.0.0.1", server.server_port, timeout=timeout, context=client_tls)
        return HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

    try:
        yield connection_factory, observations, response_finished
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def local_tls_contexts():
    """Generate an ephemeral loopback certificate; never load application secrets."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Loopback qualification only")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    with TemporaryDirectory() as directory:
        certificate_path = Path(directory) / "loopback.crt"
        key_path = Path(directory) / "loopback.key"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        client_context = ssl.create_default_context(cafile=str(certificate_path))
        yield server_context, client_context


class CoomsSmsGatewayTests(TestCase):
    def gateway(self, connection_factory, **options):
        return CoomsSmsGateway(CoomsConfiguration(api_key=FAKE_KEY, send_url=PUBLIC_URL),
            connection_factory=connection_factory, **options)

    def assert_sanitized(self, result):
        for value in (FAKE_KEY, FAKE_PHONE, FAKE_MESSAGE, "provider-private-body"):
            self.assertNotIn(value, repr(result))
            self.assertNotIn(value, str(result))

    def test_real_http_request_preserves_the_documented_post_contract(self):
        with local_provider() as (factory, observations, finished):
            result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual((result.status, result.reason, result.http_status),
            (SmsSendStatus.ACCEPTED, "http_submission_only", 202))
        self.assertEqual(len(observations), 1)
        request = observations[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/api/v1/sms/send"))
        self.assertEqual(request["headers"]["x-api-key"], FAKE_KEY)
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(request["body"]),
            {"phone": FAKE_PHONE, "message": FAKE_MESSAGE, "priority": 1})
        self.assert_sanitized(result)

    def test_verified_local_tls_keeps_the_deadline_reader_working_after_http_connection_close(self):
        with local_tls_contexts() as (server_tls, client_tls):
            with local_provider(server_tls=server_tls, client_tls=client_tls) as (factory, seen, finished):
                result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual((result.status, result.http_status), (SmsSendStatus.ACCEPTED, 202))
        self.assertEqual(len(seen), 1)
        self.assert_sanitized(result)

    def test_untrusted_tls_certificate_never_receives_the_request_or_api_key(self):
        with local_tls_contexts() as (server_tls, trusted_tls):
            with local_provider(server_tls=server_tls) as (factory, seen, finished):
                result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual((result.status, result.reason), (SmsSendStatus.UNAVAILABLE, "connection_unavailable"))
        self.assertEqual(seen, [])
        self.assert_sanitized(result)

    def test_2xx_is_only_http_submission_not_an_invented_business_or_delivery_status(self):
        for body in (b'{"success":false,"detail":"provider-private-body"}', b'not-json', b''):
            with self.subTest(body_size=len(body)):
                with local_provider(status=200, body=body) as (factory, observations, finished):
                    result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
                self.assertEqual((result.status, result.reason), (SmsSendStatus.ACCEPTED, "http_submission_only"))
                self.assert_sanitized(result)

    def test_redirects_are_never_followed_or_retried_with_the_api_key(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                with local_provider(status=status, headers={"Location": "https://outside.invalid/steal"}) as (factory, seen, finished):
                    result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
                self.assertEqual(result.status, SmsSendStatus.UNCERTAIN)
                self.assertEqual(result.reason, "redirect_refused")
                self.assertEqual(len(seen), 1)
                self.assert_sanitized(result)

    def test_http_rejections_and_uncertain_server_failures_do_not_retry_or_echo_body(self):
        for code, expected in ((400, SmsSendStatus.UNAVAILABLE), (401, SmsSendStatus.UNAVAILABLE),
                (403, SmsSendStatus.UNAVAILABLE), (429, SmsSendStatus.UNAVAILABLE),
                (408, SmsSendStatus.UNCERTAIN), (500, SmsSendStatus.UNCERTAIN), (503, SmsSendStatus.UNCERTAIN)):
            with self.subTest(code=code):
                with local_provider(status=code, body=b'provider-private-body') as (factory, seen, finished):
                    result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
                self.assertEqual((result.status, result.http_status), (expected, code))
                self.assertEqual(len(seen), 1)
                self.assert_sanitized(result)

    def test_response_body_larger_than_the_bound_is_uncertain_and_discarded(self):
        with local_provider(body=b"provider-private-body" * 500) as (factory, seen, finished):
            result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual((result.status, result.reason), (SmsSendStatus.UNCERTAIN, "response_too_large"))
        self.assertEqual(len(seen), 1)
        self.assert_sanitized(result)

    def test_real_socket_timeout_after_submission_is_uncertain_not_automatic_resend(self):
        with local_provider(delay=0.5) as (factory, seen, finished):
            started = time.monotonic()
            result = self.gateway(factory, timeout_seconds=0.05).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
            elapsed = time.monotonic() - started
            self.assertFalse(finished.is_set())
        self.assertLess(elapsed, 1)
        self.assertEqual((result.status, result.reason), (SmsSendStatus.UNCERTAIN, "transport_uncertain"))
        self.assertEqual(len(seen), 1)
        self.assert_sanitized(result)

    def test_no_configuration_and_invalid_message_never_open_a_connection(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("No connection is allowed for this case.")
        unconfigured = CoomsSmsGateway(None, connection_factory=forbidden)
        self.assertEqual(unconfigured.send(phone=FAKE_PHONE, message=FAKE_MESSAGE).status, SmsSendStatus.UNAVAILABLE)
        for phone, message in (("699000000", FAKE_MESSAGE), ("+23769900000x", FAKE_MESSAGE),
                (FAKE_PHONE, ""), (FAKE_PHONE, True), (FAKE_PHONE, "é" * 4097), (FAKE_PHONE, "\ud800")):
            with self.subTest(phone_valid=phone == FAKE_PHONE):
                result = self.gateway(forbidden).send(phone=phone, message=message)
                self.assertEqual(result.status, SmsSendStatus.UNAVAILABLE)
                self.assert_sanitized(result)

    def test_slow_drip_body_cannot_restart_the_timeout_for_every_received_byte(self):
        with local_provider(body=b"x" * 30, byte_delay=0.02) as (factory, seen, finished):
            result = self.gateway(factory, timeout_seconds=0.1).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
            self.assertEqual(result.status, SmsSendStatus.UNCERTAIN)
            self.assertFalse(finished.is_set())
        self.assertEqual(len(seen), 1)
        self.assert_sanitized(result)

    def test_connection_failure_is_unavailable_without_exception_content_or_retry(self):
        calls = []

        def refused(*args, **kwargs):
            calls.append(1)
            raise OSError(FAKE_KEY + FAKE_PHONE + FAKE_MESSAGE)
        result = self.gateway(refused).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual(result.status, SmsSendStatus.UNAVAILABLE)
        self.assertEqual(len(calls), 1)
        self.assert_sanitized(result)

    def test_explicit_falsey_transport_is_never_replaced_with_a_live_transport(self):
        class FalseyTransport:
            calls = 0

            def __bool__(self):
                return False

            def __call__(self, *args, **kwargs):
                self.calls += 1
                raise OSError("Fixture transport unavailable.")
        transport = FalseyTransport()
        with patch("identity.services.sms_gateway.HTTPSConnection",
                side_effect=AssertionError("Live transport must not be activated by this test.")):
            result = self.gateway(transport).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual(result.status, SmsSendStatus.UNAVAILABLE)
        self.assertEqual(transport.calls, 1)

    def test_provider_failure_cannot_echo_sensitive_values_in_result_or_logs(self):
        captured = io.StringIO()
        logger = logging.getLogger()
        previous_level = logger.level
        handler = logging.StreamHandler(captured)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            payload = (FAKE_KEY + FAKE_PHONE + FAKE_MESSAGE + "provider-private-body").encode("utf-8")
            with local_provider(status=500, body=payload) as (factory, seen, finished):
                with redirect_stdout(captured), redirect_stderr(captured):
                    result = self.gateway(factory).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.assertEqual(result.status, SmsSendStatus.UNCERTAIN)
        self.assert_sanitized(result)
        for value in (FAKE_KEY, FAKE_PHONE, FAKE_MESSAGE, "provider-private-body"):
            self.assertNotIn(value, captured.getvalue())

    def test_write_failure_is_uncertain_and_the_connection_is_closed(self):
        class BrokenConnection:
            closed = False
            sock = None

            def connect(self):
                pass

            def request(self, *args, **kwargs):
                raise OSError(FAKE_KEY + FAKE_PHONE + FAKE_MESSAGE)

            def close(self):
                self.closed = True
        connection = BrokenConnection()
        result = self.gateway(lambda *args, **kwargs: connection).send(phone=FAKE_PHONE, message=FAKE_MESSAGE)
        self.assertEqual(result.status, SmsSendStatus.UNCERTAIN)
        self.assertTrue(connection.closed)
        self.assert_sanitized(result)

    def test_socket_timeout_is_finite_positive_and_cannot_exceed_ten_seconds(self):
        for timeout in (None, False, 0, -1, 10.01, float("inf"), float("nan"), "5"):
            with self.subTest(timeout_type=type(timeout).__name__):
                with self.assertRaises(ValueError):
                    self.gateway(lambda *args, **kwargs: None, timeout_seconds=timeout)
