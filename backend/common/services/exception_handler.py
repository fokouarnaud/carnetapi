"""Expose stable scalar error codes alongside translated DRF messages."""
from rest_framework.exceptions import ErrorDetail
from rest_framework.views import exception_handler
from rest_framework.exceptions import NotFound
from django.http import Http404


def localized_exception_handler(exc, context):
    if isinstance(exc,Http404):
        exc=NotFound()
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict):
        detail = response.data.get("detail")
        if isinstance(detail, ErrorDetail):
            response.data.setdefault("code", detail.code)
    return response
