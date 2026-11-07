"""Each new record receipt preserves its own proposal beyond the first page."""
from copy import deepcopy
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.renderers import JSONRenderer

from catalogue.models import PackPriceProposal
from catalogue.tests import test_pack_price_conflicts as fixtures
from common.models import Activity
from common.services.operations import json_value


class PackPriceRecordIdentityTests(TestCase):
    setUp = fixtures.PackPriceConflictTests.setUp
    authenticate = fixtures.PackPriceConflictTests.authenticate
    url = fixtures.PackPriceConflictTests.url
    payload = fixtures.PackPriceConflictTests.payload
    create = fixtures.PackPriceConflictTests.create
    pack_url = fixtures.PackPriceConflictTests.pack_url
    conflicts_url = fixtures.PackPriceConflictTests.conflicts_url
    command = fixtures.PackPriceConflictTests.command
    preview = fixtures.PackPriceConflictTests.preview

    def post_record(self, command):
        response = self.manager_api.post(self.conflicts_url(), command, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response

    def expected_proposal(self, operation_id):
        proposal = PackPriceProposal.objects.get(operation_id=operation_id)
        return json_value({"id": proposal.id, "operation_id": proposal.operation_id,
            "actor_id": proposal.actor_id, "installation_id": proposal.installation_id,
            "proposed_price_fcfa": proposal.proposed_price_fcfa, "reason": proposal.reason,
            "created_at": proposal.created_at})

    def freeze_legacy_receipt(self, command, response):
        # Represent a receipt already committed by 9c10edd, which had no field
        # for this operation's own proposal. Its persisted bytes are immutable.
        legacy = deepcopy(response.data)
        legacy.pop("recorded_proposal", None)
        event = Activity.objects.get(id=command["operation_id"])
        event.details["response"] = legacy
        event.save(update_fields=["details", "updated_at"])
        return legacy

    def test_record_beyond_page_one_exposes_own_proposal_without_a_followup_read(self):
        for price in range(5000, 5026):
            command = self.command(proposed_price_fcfa=price)
            response = self.post_record(command)
        initial = deepcopy(response.data)
        self.assertEqual((len(initial["proposals"]), initial["proposals_count"]), (25, 27))
        self.assertIn("recorded_proposal", initial)
        own = initial["recorded_proposal"]
        self.assertEqual(own, self.expected_proposal(command["operation_id"]))
        self.assertNotIn(own["id"], [row["id"] for row in initial["proposals"]])
        self.assertEqual(own["operation_id"], command["operation_id"])
        self.assertEqual((own["actor_id"], own["installation_id"]), (str(self.manager.id), str(self.b_install.id)))
        self.post_record(self.command(proposed_price_fcfa=5030))
        preview = self.preview(initial["id"])
        resolved = self.manager_api.post(self.conflicts_url(initial["id"], "resolve/"), {
            "operation_id": str(uuid.uuid4()), "proposal_id": own["id"],
            "preview_token": preview["preview_token"], "reason": "Proposition conservée dès son accusé"}, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.assertEqual(resolved.data["resolution"]["proposal_id"], own["id"])
        replay = self.manager_api.post(self.conflicts_url(), command, format="json")
        self.assertEqual((replay.status_code, replay.data, replay.content), (200, initial, response.content))
        self.assertEqual(Activity.objects.get(id=command["operation_id"]).details["response"], initial)

    def test_first_record_attests_its_received_proposal_without_changing_resource_projections(self):
        command = self.command()
        response = self.post_record(command)
        self.assertIn("recorded_proposal", response.data)
        self.assertEqual(response.data["recorded_proposal"], self.expected_proposal(command["operation_id"]))
        self.assertNotEqual(response.data["recorded_proposal"]["operation_id"], str(self.source.id))
        for url in (self.conflicts_url(response.data["id"]), self.conflicts_url(response.data["id"], "preview/"), self.pack_url()):
            with self.subTest(url=url):
                resource = self.manager_api.get(url)
                self.assertEqual(resource.status_code, 200, resource.content)
                self.assertNotIn("recorded_proposal", resource.data)

    def test_old_frozen_receipt_stays_exactly_unchanged_instead_of_being_backfilled(self):
        command = self.command()
        legacy = self.freeze_legacy_receipt(command, self.post_record(command))
        counts = PackPriceProposal.objects.count(), Activity.objects.count()
        replay = self.manager_api.post(self.conflicts_url(), command, format="json")
        self.assertEqual((replay.status_code, replay.data, replay.content), (200, legacy, JSONRenderer().render(legacy)))
        self.assertNotIn("recorded_proposal", replay.data)
        self.assertEqual(Activity.objects.get(id=command["operation_id"]).details["response"], legacy)
        self.assertEqual((PackPriceProposal.objects.count(), Activity.objects.count()), counts)

    def test_new_and_old_record_receipts_require_current_rights_before_identity_is_returned(self):
        new_command, old_command = self.command(), self.command(proposed_price_fcfa=5100)
        self.post_record(new_command)
        self.freeze_legacy_receipt(old_command, self.post_record(old_command))
        self.member.is_active = False
        self.member.save(update_fields=["is_active"])
        for command in (new_command, old_command):
            with self.subTest(operation=command["operation_id"], status=404):
                self.assertEqual(self.manager_api.post(self.conflicts_url(), command, format="json").status_code, 404)
        self.member.is_active = True
        self.member.save(update_fields=["is_active"])
        self.b_session.revoked_at = timezone.now()
        self.b_session.save(update_fields=["revoked_at"])
        for command in (new_command, old_command):
            with self.subTest(operation=command["operation_id"], status=401):
                self.assertEqual(self.manager_api.post(self.conflicts_url(), command, format="json").status_code, 401)
