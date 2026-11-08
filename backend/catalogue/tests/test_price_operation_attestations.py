"""Pending native attestations preserve source identities without aggregate history.

This next tranche is deliberately separate from the recorded_proposal fix.
No recovery adapter is specified or enabled by these tests.
"""
from copy import deepcopy
import json
import uuid

from django.test import TestCase
from django.db import transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests.test_recovery_purchase_contract import nullable_schema
from catalogue import models
from catalogue.tests import test_price_conflicts as product_fixtures
from catalogue.tests import test_pack_price_conflicts as pack_fixtures
from cash.models import CashMovement
from common.models import Activity
from common.services.operations import json_value
from stock.models import StockMovement


class AttestationChecks:
    def catalogue_state(self):
        self.product.refresh_from_db()
        product = json_value({name: getattr(self.product, name) for name in
            ("id", "version", "sale_price_fcfa", "updated_at")})
        pack = None
        if self.kind == "pack":
            row = models.ProductPack.objects.get(id=self.pack_id)
            pack = json_value({name: getattr(row, name) for name in
                ("id", "product_id", "version", "sale_price_fcfa", "base_quantity_milli", "updated_at")})
        return {"product": product, "pack": pack}

    def post_record(self, command=None):
        command = command or self.command()
        response = self.manager_api.post(self.endpoint, command, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response, command

    def preview(self, conflict_id):
        response = self.manager_api.get(self.endpoint + conflict_id + "/preview/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def resolve(self, conflict_id, proposal_id):
        preview = self.preview(conflict_id)
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "proposal_id": proposal_id, "reason": "Choix initial conservé pour reprise"}
        response = self.manager_api.post(self.endpoint + conflict_id + "/resolve/", command, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response, command

    def proposal_snapshot(self, row):
        return json_value({name: getattr(row, name) for name in ("id", "operation_id", "actor_id",
            "installation_id", "is_native", "proposed_price_fcfa", "reason", "created_at")})

    def resolution_snapshot(self, row):
        return json_value({name: getattr(row, name) for name in ("id", "operation_id", "proposal_id", "actor_id",
            "installation_id", "price_fcfa", "reason", "created_at")})

    def check_snapshot(self, response, command, before, conflict_before, created_operations, *, resolution=False):
        self.assertIn("operation_snapshot", response.data)
        snapshot = response.data["operation_snapshot"]
        event = Activity.objects.get(id=command["operation_id"])
        conflict = self.conflict_model.objects.get(id=response.data["id"])
        scope = {"shop_id": str(self.shop.id), "product_id": str(self.product.id),
            "pack_id": self.pack_id if self.kind == "pack" else None}
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["operation_type"], self.resolve_action if resolution else self.record_action)
        self.assertEqual(snapshot["operation_id"], str(event.id))
        self.assertEqual(snapshot["scope"], scope)
        self.assertEqual(snapshot["original_request"], event.details["request"])
        self.assertEqual((snapshot["actor_id"], snapshot["installation_id"]),
            (str(self.manager.id), str(self.b_install.id)))
        self.assertEqual(snapshot["recorded_at"], json_value(event.created_at))
        self.assertEqual(snapshot["catalogue_before"], before)
        self.assertEqual(snapshot["catalogue_after"], self.catalogue_state())
        self.assertEqual(snapshot["conflict_before"], conflict_before)
        self.assertEqual(snapshot["conflict_after"], {"state": conflict.state, "version": conflict.version})
        frozen = json_value({name: getattr(conflict, name) for name in ("id", "source_change_id",
            "opening_operation_id", "base_version", "common_price_fcfa", "created_at")})
        if self.kind == "pack": frozen["base_quantity_milli"] = conflict.base_quantity_milli
        self.assertEqual(snapshot["conflict"], frozen)
        expected = self.proposal_model.objects.filter(operation_id__in=created_operations).order_by("created_at", "id")
        self.assertEqual(snapshot["proposals_created"], [self.proposal_snapshot(row) for row in expected])
        self.assertLessEqual(len(snapshot["proposals_created"]), 2)
        decision = self.resolution_model.objects.get(operation_id=event.id) if resolution else None
        self.assertEqual(snapshot["resolution"], self.resolution_snapshot(decision) if decision else None)
        self.assertEqual(event.details["operation_snapshot"], snapshot)
        self.assertEqual(event.details["response"], response.data)
        if not hasattr(type(self), "receipt_schema"):
            type(self).receipt_schema = SchemaGenerator().get_schema(request=None, public=True)
        root = "/api/v1/shops/{shop_id}/products/{product_id}/"
        if self.kind == "pack":
            root += "packs/{pack_id}/"
        root += "price-conflicts/" + ("{id}/resolve/" if resolution else "")
        schema = type(self).receipt_schema
        rule = schema["paths"][root]["post"]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        errors = list(Draft4Validator(nullable_schema({**rule, "components": schema["components"]}),
            format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
        self.assertEqual([error.message for error in errors], [])
        return snapshot

    def test_opening_attests_exact_two_proposal_ids_canonical_request_and_actual_clocks(self):
        before = self.catalogue_state()
        counts = StockMovement.objects.count(), CashMovement.objects.count()
        response, command = self.post_record()
        self.check_snapshot(response, command, before, None, [self.source.id, command["operation_id"]])
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), counts)

    def test_additional_proposal_after_page_one_attests_only_its_own_delta(self):
        first, _ = self.post_record()
        for price in range(1201, 1226): self.post_record(self.command(proposed_price_fcfa=price))
        before = self.catalogue_state()
        conflict = self.conflict_model.objects.get(id=first.data["id"])
        previous = {"state": "open", "version": conflict.version}
        response, command = self.post_record(self.command(proposed_price_fcfa=1250))
        snapshot = self.check_snapshot(response, command, before, previous, [command["operation_id"]])
        self.assertEqual(len(snapshot["proposals_created"]), 1)
        self.assertEqual(snapshot["catalogue_before"], snapshot["catalogue_after"])

    def test_resolution_attests_its_real_entity_uuid_choice_and_catalogue_before_after(self):
        for choose_common in (False, True):
            with self.subTest(choose_common=choose_common), transaction.atomic():
                registered, _ = self.post_record()
                own = next(row for row in registered.data["proposals"] if row["operation_id"] != str(self.source.id))
                before = self.catalogue_state()
                resolved, command = self.resolve(registered.data["id"], None if choose_common else own["id"])
                snapshot = self.check_snapshot(resolved, command, before, {"state": "open", "version": 1}, [], resolution=True)
                self.assertEqual(snapshot["resolution"]["proposal_id"], None if choose_common else own["id"])
                self.assertEqual(snapshot["original_request"]["preview_token"], command["preview_token"])
                transaction.set_rollback(True)

    def test_retries_preserve_both_snapshots_after_later_state_changes(self):
        recorded, record_command = self.post_record()
        resolved, resolve_command = self.resolve(recorded.data["id"], None)
        self.assertIn("operation_snapshot", recorded.data)
        self.assertIn("operation_snapshot", resolved.data)
        record_retry = self.manager_api.post(self.endpoint, record_command, format="json")
        resolve_retry = self.manager_api.post(self.endpoint + recorded.data["id"] + "/resolve/", resolve_command, format="json")
        self.assertEqual((record_retry.status_code, record_retry.content), (200, recorded.content))
        self.assertEqual((resolve_retry.status_code, resolve_retry.content), (200, resolved.content))

    def test_old_record_and_resolution_receipts_are_never_attested_retroactively(self):
        recorded, record_command = self.post_record()
        resolved, resolve_command = self.resolve(recorded.data["id"], None)
        for source, command, url in ((recorded, record_command, self.endpoint),
                (resolved, resolve_command, self.endpoint + recorded.data["id"] + "/resolve/")):
            with self.subTest(operation=command["operation_id"]):
                legacy = deepcopy(source.data)
                legacy.pop("operation_snapshot", None)
                event = Activity.objects.get(id=command["operation_id"])
                event.details.pop("operation_snapshot", None)
                event.details["response"] = legacy
                event.save(update_fields=["details", "updated_at"])
                replay = self.manager_api.post(url, command, format="json")
                self.assertEqual((replay.status_code, replay.data), (200, legacy))
                self.assertNotIn("operation_snapshot", replay.data)
                self.assertNotIn("operation_snapshot", Activity.objects.get(id=event.id).details)

    def test_attestation_whitelist_excludes_costs_and_never_appears_in_resource_projections(self):
        response, _ = self.post_record()
        self.assertIn("operation_snapshot", response.data)
        snapshot = response.data["operation_snapshot"]
        self.assertEqual(set(snapshot), {"schema_version", "operation_type", "operation_id", "scope", "original_request",
            "actor_id", "installation_id", "recorded_at", "conflict", "conflict_before", "conflict_after",
            "catalogue_before", "catalogue_after", "proposals_created", "resolution"})
        forbidden = {"purchase_price_fcfa", "unit_cost_micros", "unit_cost_fcfa", "source_snapshot",
            "margin", "password", "pin", "jwt", "access_token", "refresh_token", "authorization", "otp"}
        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()
        self.assertFalse(keys(snapshot) & forbidden)
        for url in (self.endpoint + response.data["id"] + "/", self.endpoint + response.data["id"] + "/preview/", self.resource_url):
            resource = self.manager_api.get(url)
            self.assertEqual(resource.status_code, 200, resource.content)
            self.assertNotIn("operation_snapshot", resource.data)


class ProductPriceOperationAttestationTests(AttestationChecks, TestCase):
    command = product_fixtures.ProductPriceConflictTests.command

    def setUp(self):
        product_fixtures.ProductPriceConflictTests.setUp(self)
        self.kind = "product"
        self.endpoint = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/price-conflicts/"
        self.resource_url = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/"
        self.record_action, self.resolve_action = "catalogue_price_conflict_recorded", "catalogue_price_resolved"
        self.conflict_model, self.proposal_model, self.resolution_model = models.ProductPriceConflict, models.ProductPriceProposal, models.ProductPriceResolution
        token = AccessToken.for_user(self.manager)
        token["session_id"] = str(self.b_session.id)
        self.manager_api = APIClient()
        self.manager_api.credentials(HTTP_AUTHORIZATION="Bearer " + str(token))


class PackPriceOperationAttestationTests(AttestationChecks, TestCase):
    authenticate = pack_fixtures.PackPriceConflictTests.authenticate
    url = pack_fixtures.PackPriceConflictTests.url
    payload = pack_fixtures.PackPriceConflictTests.payload
    create = pack_fixtures.PackPriceConflictTests.create
    pack_url = pack_fixtures.PackPriceConflictTests.pack_url
    command = pack_fixtures.PackPriceConflictTests.command

    def setUp(self):
        pack_fixtures.PackPriceConflictTests.setUp(self)
        self.kind = "pack"
        self.endpoint = self.pack_url() + "price-conflicts/"
        self.resource_url = self.pack_url()
        self.record_action, self.resolve_action = "catalogue_pack_price_conflict_recorded", "catalogue_pack_price_resolved"
        self.conflict_model, self.proposal_model, self.resolution_model = models.PackPriceConflict, models.PackPriceProposal, models.PackPriceResolution
