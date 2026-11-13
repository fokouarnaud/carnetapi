"""Product conflicts retain every proposal while new resource pages stay bounded."""
from copy import deepcopy
import json
import uuid

from django.test import TestCase
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests.test_recovery_purchase_contract import nullable_schema
from catalogue.api.price_conflicts import PriceProposalSerializer
from catalogue.models import ProductPriceProposal
from catalogue.tests import test_price_conflicts_api as fixtures
from common.models import Activity


class ProductPriceProposalPageTests(TestCase):
    setUp = fixtures.PriceConflictAPITests.setUp
    authenticate = fixtures.PriceConflictAPITests.authenticate
    post_conflict = fixtures.PriceConflictAPITests.post_conflict

    def record_many(self):
        for price in range(1200, 1227):
            command = {**self.proposal, "operation_id": str(uuid.uuid4()), "proposed_price_fcfa": price}
            response = self.client.post(self.url, command, format="json")
            self.assertEqual(response.status_code, 201, response.content)
        return response, command

    def test_new_receipt_is_bounded_but_every_choice_remains_reachable_and_resolvable(self):
        last, command = self.record_many()
        self.assertEqual(len(last.data["proposals"]), 25)
        self.assertEqual(last.data["proposals_count"], 28)
        detail = self.url + last.data["id"] + "/"
        self.assertEqual(last.data["proposals_url"], detail + "proposals/")
        first = self.client.get(last.data["proposals_url"])
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual((first.data["count"], len(first.data["results"])), (28, 25))
        second = self.client.get(first.data["next"])
        self.assertEqual((second.status_code, len(second.data["results"])), (200, 3))
        self.assertEqual(first.data["results"] + second.data["results"], list(PriceProposalSerializer(
            ProductPriceProposal.objects.filter(conflict_id=last.data["id"]).order_by("created_at", "id"), many=True).data))
        self.assertEqual(self.client.get(detail + "proposals/?page_size=999").data["count"], 28)
        own = last.data["operation_snapshot"]["proposals_created"][0]["id"]
        self.assertNotIn(own, [row["id"] for row in last.data["proposals"]])
        preview = self.client.get(detail + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        resolved = self.client.post(detail + "resolve/", {"operation_id": str(uuid.uuid4()),
            "proposal_id": own, "preview_token": preview.data["preview_token"], "reason": "Choix hors première page"}, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.assertEqual(resolved.data["resolution"]["proposal_id"], own)
        replay = self.client.post(self.url, command, format="json")
        self.assertEqual((replay.status_code, replay.content), (200, last.content))

    def test_proposal_page_checks_current_role_shop_and_session(self):
        created = self.post_conflict()
        self.assertEqual(created.status_code, 201, created.content)
        detail = self.url + created.data["id"] + "/proposals/"
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.authenticate(self.owner)
        foreign = detail.replace(str(self.shop.id), str(self.other.id))
        self.assertEqual(self.client.get(foreign).status_code, 404)
        self.session.revoked_at = timezone.now()
        self.session.save(update_fields=["revoked_at"])
        self.assertEqual(self.client.get(detail).status_code, 401)

    def test_unbounded_legacy_receipt_is_not_truncated_or_backfilled_on_retry(self):
        last, command = self.record_many()
        legacy = deepcopy(last.data)
        legacy["proposals"] = list(PriceProposalSerializer(ProductPriceProposal.objects.filter(
            conflict_id=last.data["id"]).order_by("created_at", "id"), many=True).data)
        for key in ("proposals_count", "proposals_url", "operation_snapshot"):
            legacy.pop(key, None)
        event = Activity.objects.get(id=command["operation_id"])
        event.details.pop("operation_snapshot", None)
        event.details["response"] = json.loads(json.dumps(legacy, default=str))
        event.save(update_fields=["details", "updated_at"])
        replay = self.client.post(self.url, command, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, legacy))
        self.assertEqual(len(replay.data["proposals"]), 28)
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = "/api/v1/shops/{shop_id}/products/{product_id}/price-conflicts/"
        rule = schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        errors = list(Draft4Validator(nullable_schema({**rule, "components": schema["components"]}),
            format_checker=FormatChecker()).iter_errors(json.loads(replay.content)))
        self.assertEqual([error.message for error in errors], [])
