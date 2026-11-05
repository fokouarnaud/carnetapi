"""Relocating URL modules preserves existing public paths, names and actions."""
import json
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLResolver


def http_actions(actions):
    # DRF binds HEAD to GET on first dispatch, mutating its original dict.
    # Compare the same effective HTTP contract before and after dispatch.
    normalized = dict(actions)
    if "get" in normalized:
        normalized.setdefault("head", normalized["get"])
    return normalized


def api_routes(patterns, prefix=""):
    result = []
    for item in patterns:
        full = prefix + str(item.pattern)
        if isinstance(item, URLResolver):
            result.extend(api_routes(item.url_patterns, full))
        elif full.startswith("api/v1/"):
            cls = getattr(item.callback, "cls", None)
            result.append([full, item.name, cls.__name__ if cls else None, http_actions(getattr(item.callback, "actions", {}))])
    return result


class ApiRouteCompatibilityTests(SimpleTestCase):
    def test_existing_api_routes_keep_their_paths_names_and_actions(self):
        baseline = Path(__file__).resolve().parents[3] / "analysis/backend-route-baseline.json"
        existing = json.loads(baseline.read_text(encoding="utf-8"))
        actual = api_routes(get_resolver().url_patterns)
        for route in existing:
            route[3] = http_actions(route[3])
            with self.subTest(route=route[0]):
                self.assertIn(route, actual)

    def test_shop_router_and_documentation_keep_their_reverse_names(self):
        self.assertEqual(reverse("shop-list"), "/api/v1/shops/")
        self.assertEqual(reverse("schema"), "/api/v1/schema/")
        self.assertEqual(reverse("swagger"), "/api/v1/docs/")
        self.assertEqual(reverse("redoc"), "/api/v1/redoc/")
