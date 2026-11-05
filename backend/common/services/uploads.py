"""Stop multipart file chunks before Django spools an oversized upload to disk."""
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from rest_framework.exceptions import ValidationError


class FileByteLimit(FileUploadHandler):
    def __init__(self, request, byte_limit):
        super().__init__(request)
        self.byte_limit = byte_limit
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.byte_limit:
            self.request._app_upload_limit_exceeded = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


class BoundedUploadMixin:
    """Apply a per-request file limit, independent of Django's buffering strategy."""
    def initialize_request(self, request, *args, **kwargs):
        request.upload_handlers.insert(0, FileByteLimit(request, self.upload_byte_limit))
        return super().initialize_request(request, *args, **kwargs)

    def bounded_payload(self, request):
        data = request.data
        if getattr(request._request, "_app_upload_limit_exceeded", False):
            raise ValidationError(self.upload_size_error)
        return data
