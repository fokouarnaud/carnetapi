"""Reject forbidden and oversized restore uploads before temporary body storage."""
from types import SimpleNamespace
from django.core.exceptions import PermissionDenied, RequestDataTooBig
from django.core.files.uploadhandler import StopUpload
from django.test import TestCase, RequestFactory
from identity.models import User


class RestoreUploadLimitTests(TestCase):
    def test_owner_is_rejected_before_body_parsing_even_with_a_large_declared_length(self):
        from backups.uploads import RestoreUploadMiddleware
        owner = User.objects.create_user(username="limited-owner", is_staff=True)
        request = RequestFactory().post("/admin/backups/restoreimport/receive/", data=b"", content_type="multipart/form-data; boundary=test")
        request.user = owner
        request.resolver_match = SimpleNamespace(url_name="backups_restoreimport_start")
        request.META["CONTENT_LENGTH"] = "999999999"
        class Unreadable:
            def read(self, *args, **kwargs):
                raise AssertionError("body read before permission")
        request._stream = Unreadable()
        with self.assertRaises(PermissionDenied):
            RestoreUploadMiddleware(lambda value: None).process_view(request, None, (), {})

    def test_declared_body_limit_and_streamed_file_limit_are_independent(self):
        from backups.uploads import RestorePartUploadHandler
        handler = RestorePartUploadHandler()
        with self.assertRaises(RequestDataTooBig):
            handler.handle_raw_input(None, {}, 15065537, None)
        self.assertIsNone(handler.handle_raw_input(None, {}, 15065536, None))
        handler.new_file("part", "ignored", "application/octet-stream", None)
        block = b"x"*7500000
        self.assertIs(handler.receive_data_chunk(block, 0), block)
        self.assertIs(handler.receive_data_chunk(block, len(block)), block)
        with self.assertRaises(StopUpload):
            handler.receive_data_chunk(b"x", 15000000)

    def test_a_second_file_is_refused_and_other_admin_routes_are_unaffected(self):
        from backups.uploads import RestorePartUploadHandler, RestoreUploadMiddleware
        handler = RestorePartUploadHandler()
        handler.new_file("part", "first", "application/octet-stream", 1)
        with self.assertRaises(StopUpload):
            handler.new_file("part", "second", "application/octet-stream", 1)
        request = RequestFactory().post("/admin/login/")
        request.resolver_match = SimpleNamespace(url_name="login")
        self.assertIsNone(RestoreUploadMiddleware(lambda value: None).process_view(request, None, (), {}))
