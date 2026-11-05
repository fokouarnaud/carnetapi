"""Authorize restore forms and bound file bodies before Django stores uploads."""
from django.core.exceptions import PermissionDenied, RequestDataTooBig
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.utils.deprecation import MiddlewareMixin
from backups.services.archive import MAX_PART_BYTES
from identity.models import User


class RestorePartUploadHandler(FileUploadHandler):
    chunk_size = 65536

    def __init__(self, request=None, *, allow_files=True):
        super().__init__(request)
        self.allow_files = allow_files
        self.maximum_body = MAX_PART_BYTES+65536 if allow_files else 65536
        self.received = 0
        self.files = 0

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        if content_length is not None and not 0 <= content_length <= self.maximum_body:
            raise RequestDataTooBig("Restore upload body exceeds its bound.")

    def _stop(self):
        if self.request is not None:
            self.request.restore_upload_rejected = True
        raise StopUpload(connection_reset=True)

    def new_file(self, *args, **kwargs):
        self.files += 1
        if not self.allow_files or self.files > 1:
            self._stop()
        super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > MAX_PART_BYTES:
            self._stop()
        return raw_data

    def file_complete(self, file_size):
        return None


class RestoreUploadMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if (request.method != "POST" or match is None
                or match.url_name not in {"backups_restoreimport_start", "backups_restoreimport_receive", "backups_restoreimport_prepare", "backups_importpreparation_result", "backups_importquarantine_start", "backups_restoreaccessreview_control"}):
            return None
        if not User.objects.filter(id=request.user.pk, is_active=True, is_superuser=True, is_staff=True).exists():
            raise PermissionDenied()
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except (ValueError, TypeError):
            raise RequestDataTooBig("Invalid restore upload length.") from None
        allow_files = match.url_name in {"backups_restoreimport_start", "backups_restoreimport_receive"}
        maximum = MAX_PART_BYTES+65536 if allow_files else 65536
        if not 0 <= length <= maximum:
            raise RequestDataTooBig("Restore upload body exceeds its bound.")
        request.upload_handlers.insert(0, RestorePartUploadHandler(request, allow_files=allow_files))
        return None
