"""Request lifetimes must remain inside the external maintenance barrier."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import uuid

from django.http import HttpResponse, StreamingHttpResponse
from django.test import TransactionTestCase, RequestFactory, override_settings

from backups.services.restore_gate import initialize_gate, enter_maintenance, RestoreGateBusy


class RestoreGateMiddlewareTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="restore-http-gate-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.business, self.gate = self.root / "business.sqlite3", self.root / "gate.sqlite3"
        with closing(sqlite3.connect(self.business)) as db:
            db.execute("CREATE TABLE business (amount INTEGER)")
        self.actor = uuid.uuid4()
        self.settings = override_settings(RESTORE_GATE_PATH=self.gate, DATABASES={"default": {
            "ENGINE": "django.db.backends.sqlite3", "NAME": str(self.business)}})
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.request = RequestFactory().get("/api/v1/products/")

    def initialize(self):
        initialize_gate(self.gate, self.business, operation_id=uuid.uuid4(), requested_by=self.actor, reason="Installation")

    def enter(self):
        return enter_maintenance(self.gate, operation_id=uuid.uuid4(), requested_by=self.actor,
                                 reason="Restauration", expected_version=1, timeout=0)

    def test_missing_and_closed_gates_return_503_before_the_handler(self):
        from backups.middleware import RestoreGateMiddleware
        called = []
        middleware = RestoreGateMiddleware(lambda request: called.append(request))
        response = middleware(self.request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(called, [])
        self.assertFalse(self.gate.exists())
        self.initialize()
        self.enter()
        response = middleware(self.request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(called, [])

    def test_ordinary_response_holds_lease_until_close(self):
        from backups.middleware import RestoreGateMiddleware
        self.initialize()
        response = RestoreGateMiddleware(lambda request: HttpResponse("ok"))(self.request)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(RestoreGateBusy):
            self.enter()
        response.close()
        response.close()
        self.assertEqual(self.enter()["mode"], "maintenance")

    def test_incomplete_gate_schema_returns_503_without_running_the_handler(self):
        from backups.middleware import RestoreGateMiddleware
        broken = self.root / "incomplete.sqlite3"
        with closing(sqlite3.connect(broken)) as db:
            db.execute("PRAGMA user_version=1")
        with override_settings(RESTORE_GATE_PATH=broken):
            response = RestoreGateMiddleware(lambda request: self.fail("handler must not run"))(self.request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_stream_and_its_cleanup_finish_before_the_lease_is_released(self):
        from backups.middleware import RestoreGateMiddleware
        self.initialize()
        closed_while_locked = []
        def stream():
            try:
                yield b"first"
                yield b"second"
            finally:
                try:
                    self.enter()
                except RestoreGateBusy:
                    closed_while_locked.append(True)
        response = RestoreGateMiddleware(lambda request: StreamingHttpResponse(stream()))(self.request)
        self.addCleanup(response.close)
        self.assertEqual(next(iter(response.streaming_content)), b"first")
        with self.assertRaises(RestoreGateBusy):
            self.enter()
        response.close()
        self.assertEqual(closed_while_locked, [True])
        self.assertEqual(self.enter()["mode"], "maintenance")

    def test_handler_failure_releases_lease_without_hiding_the_failure(self):
        from backups.middleware import RestoreGateMiddleware
        self.initialize()
        def fail(request):
            raise RuntimeError("handler failed")
        with self.assertRaisesMessage(RuntimeError, "handler failed"):
            RestoreGateMiddleware(fail)(self.request)
        self.assertEqual(self.enter()["mode"], "maintenance")
