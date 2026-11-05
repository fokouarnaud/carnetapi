"""Bound JSON reads before decoding and reject excessive container nesting."""
from io import BytesIO
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import APIException, ParseError
from rest_framework.parsers import JSONParser


class JSONBodyTooLarge(APIException):
    status_code = 413
    default_code = "json_body_too_large"
    default_detail = _("Corps de requête JSON trop volumineux.")


class BoundedJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.API_JSON_MAX_BYTES
        body = bytearray()
        # Short reads do not imply EOF. Never trust Content-Length for the bound.
        while len(body) <= limit:
            chunk = stream.read(min(65536, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > limit:
            raise JSONBodyTooLarge()
        try:
            value = super().parse(BytesIO(body), media_type, parser_context)
        except RecursionError as exc:
            raise ParseError(_("JSON trop profondément imbriqué.")) from exc
        # Store iterators, so a wide array does not allocate one work item per child.
        pending = [(iter((value,)), 0)]
        while pending:
            nodes, depth = pending[-1]
            try:
                node = next(nodes)
            except StopIteration:
                pending.pop()
                continue
            if isinstance(node, (dict, list)):
                if depth >= settings.API_JSON_MAX_DEPTH:
                    raise ParseError(_("JSON trop profondément imbriqué."))
                children = node.values() if isinstance(node, dict) else node
                pending.append((iter(children), depth + 1))
        return value
