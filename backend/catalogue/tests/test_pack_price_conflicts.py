"""A price-only pack conflict preserves billed factors and the common tariff."""
from copy import deepcopy
from datetime import timedelta
import uuid

from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from catalogue.models import Product, ProductPack
from catalogue.tests import test_packs as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, User
from sales.models import SaleLine
from stock.models import StockMovement


class PackPriceConflictTests(TestCase):
    authenticate = fixtures.PackTests.authenticate
    url = fixtures.PackTests.url
    payload = fixtures.PackTests.payload
    create = fixtures.PackTests.create

    def setUp(self):
        fixtures.PackTests.setUp(self)
        created = self.create()
        self.assertEqual(created.status_code, 201, created.content)
        self.pack_id = created.data["id"]
        self.initial = deepcopy(created.data)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_opened", target_id=self.product.id, reason="Stock physique")
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=opening, kind="opening",
            quantity_milli=100000, unit_cost_micros=200000000, occurred_at=timezone.now(), reason="Stock physique")
        command = {"operation_id": str(uuid.uuid4()), "base_version": 1, "sale_price_fcfa": 5200, "reason": "Tarif carton confirmé sur A"}
        response = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.source = Activity.objects.get(id=command["operation_id"])
        self.manager = User.objects.create_user(username="pack-price-manager")
        self.member = Membership.objects.create(shop=self.shop, user=self.manager, role="manager")
        self.b_install = Installation.objects.create(user=self.manager)
        self.b_session = DeviceSession.objects.create(installation=self.b_install, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(self.manager); token["session_id"] = str(self.b_session.id)
        self.manager_api = APIClient(); self.manager_api.credentials(HTTP_AUTHORIZATION="Bearer " + str(token))

    def pack_url(self):
        return self.url() + self.pack_id + "/"

    def conflicts_url(self, conflict=None, suffix=""):
        return self.pack_url() + "price-conflicts/" + (str(conflict) + "/" if conflict else "") + suffix

    def command(self, **values):
        return {"operation_id": str(uuid.uuid4()), "source_change_id": str(self.source.id), "base_version": 1,
            "proposed_price_fcfa": 5000, "reason": "Proposition concurrente du carton sur B", **values}

    def record(self, command=None):
        response = self.manager_api.post(self.conflicts_url(), command or self.command(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.data

    def preview(self, conflict):
        response = self.manager_api.get(self.conflicts_url(conflict, "preview/"))
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def sale(self, amount):
        self.product.refresh_from_db()
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": self.product.version,
                "pack_id": self.pack_id, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": amount}]}
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.data

    def test_received_pack_conflict_uses_common_price_and_preserves_old_sale_and_factor(self):
        before = self.sale(5200)
        counts = (StockMovement.objects.count(), CashMovement.objects.count())
        receipt = self.record()
        self.assertEqual((receipt["state"], receipt["version"], receipt["common_price_fcfa"], receipt["base_quantity_milli"]), ("open", 1, 4800, 24000))
        self.assertEqual((receipt["pack_id"], receipt["pack_version"], receipt["product_version"]), (self.pack_id, 3, 3))
        self.assertEqual(sorted(row["proposed_price_fcfa"] for row in receipt["proposals"]), [5000, 5200])
        native = next(row for row in receipt["proposals"] if row["operation_id"] == str(self.source.id))
        self.assertEqual(native["actor_id"], str(self.owner.id))
        self.assertIsNone(native["installation_id"])
        pack = ProductPack.objects.get(id=self.pack_id); self.product.refresh_from_db()
        self.assertEqual((pack.sale_price_fcfa, pack.base_quantity_milli, self.product.sale_price_fcfa), (4800, 24000, 250))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), counts)
        after = self.sale(4800)
        self.assertEqual((before["lines"][0]["unit_price_fcfa"], after["lines"][0]["unit_price_fcfa"]), (5200, 4800))
        self.assertEqual(list(SaleLine.objects.order_by("created_at").values_list("factor_milli", "base_quantity_milli")), [(24000, 24000)] * 2)
        self.source.refresh_from_db(); self.assertEqual(self.source.details["response"]["sale_price_fcfa"], 5200)
        self.assertNotIn("purchase_price_fcfa", str(receipt))

    def test_resolution_selects_preserved_price_and_original_uuids_retry_exactly(self):
        registration = self.command()
        first = self.record(registration)
        chosen = next(row for row in first["proposals"] if row["proposed_price_fcfa"] == 5000)
        preview = self.preview(first["id"])
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "proposal_id": chosen["id"], "reason": "Tarif carton de 5000 confirmé"}
        resolved = self.manager_api.post(self.conflicts_url(first["id"], "resolve/"), command, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.assertEqual((resolved.data["state"], resolved.data["version"], resolved.data["pack_version"], resolved.data["product_version"]), ("resolved", 2, 4, 4))
        self.assertEqual((resolved.data["resolution"]["proposal_id"], resolved.data["resolution"]["price_fcfa"]), (chosen["id"], 5000))
        self.assertEqual(self.manager_api.post(self.conflicts_url(first["id"], "resolve/"), command, format="json").data, resolved.data)
        replay = self.manager_api.post(self.conflicts_url(), registration, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, first))
        self.assertEqual((ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, StockMovement.objects.count(), CashMovement.objects.count()), (5000, 1, 0))

    def test_additional_proposal_changes_only_conflict_revision_and_invalidates_preview(self):
        registration = self.command(); first = self.record(registration); old = self.preview(first["id"])
        another = self.record(self.command(proposed_price_fcfa=5100))
        self.assertEqual((another["id"], another["version"], another["pack_version"], another["product_version"]), (first["id"], 2, 3, 3))
        self.assertEqual(sorted(row["proposed_price_fcfa"] for row in another["proposals"]), [5000, 5100, 5200])
        command = {"operation_id": str(uuid.uuid4()), "preview_token": old["preview_token"], "proposal_id": None, "reason": "Choix ancien"}
        response = self.manager_api.post(self.conflicts_url(first["id"], "resolve/"), command, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        replay = self.manager_api.post(self.conflicts_url(), registration, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, first))

    def test_common_price_resolution_is_explicit_and_never_accepts_an_arbitrary_amount(self):
        conflict = self.record(); preview = self.preview(conflict["id"])
        body = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Prix commun maintenu"}
        endpoint = self.conflicts_url(conflict["id"], "resolve/")
        self.assertEqual(self.manager_api.post(endpoint, {**body, "price_fcfa": 1}, format="json").status_code, 400)
        self.assertEqual(self.manager_api.post(endpoint, {**body, "proposal_id": str(uuid.uuid4())}, format="json").status_code, 409)
        accepted = self.manager_api.post(endpoint, body, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual((accepted.data["resolution"]["price_fcfa"], accepted.data["resolution"]["proposal_id"]), (4800, None))
        self.assertEqual(self.preview(conflict["id"])["can_resolve"], False)

    def test_source_must_prove_this_pack_price_transition_without_a_factor_change(self):
        for case in ("action", "target", "base", "price", "factor", "parent", "response",
                "missing_name", "factor_request", "reason_request"):
            with self.subTest(case=case), transaction.atomic():
                event = Activity.objects.get(id=self.source.id)
                if case == "action": event.action = "product_updated"
                elif case == "target": event.target_id = uuid.uuid4()
                elif case == "base": event.details["before"]["version"] = 2
                elif case == "price": event.details["request"]["sale_price_fcfa"] = 1
                elif case == "factor": event.details["before"]["base_quantity_milli"] = 12000
                elif case == "parent": event.details["after"]["product_id"] = str(self.foreign.id)
                elif case == "response": event.details["response"] = {}
                elif case == "missing_name":
                    for key in ("before", "after", "response"): event.details[key].pop("name")
                elif case == "factor_request": event.details["request"]["base_quantity_milli"] = 12000
                else: event.details["request"]["reason"] = "Motif contredit"
                event.save()
                response = self.manager_api.post(self.conflicts_url(), self.command(), format="json")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 5200)
                transaction.set_rollback(True)

    def test_a_real_mixed_factor_and_price_source_stays_unresolved_without_fallback(self):
        command = {"operation_id": str(uuid.uuid4()), "base_version": 2, "base_quantity_milli": 12000,
            "sale_price_fcfa": 3000, "reason": "Autre définition du format"}
        changed = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        conflict = self.manager_api.post(self.conflicts_url(), self.command(source_change_id=command["operation_id"], base_version=2), format="json")
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual((ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, ProductPack.objects.get(id=self.pack_id).base_quantity_milli), (3000, 12000))

    def test_an_older_price_source_cannot_rewind_the_current_pack(self):
        changed = self.client.patch(self.pack_url(), {"operation_id": str(uuid.uuid4()), "base_version": 2,
            "sale_price_fcfa": 5400, "reason": "Autre prix confirmé depuis"}, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        response = self.manager_api.post(self.conflicts_url(), self.command(), format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 5400)

    def test_current_role_and_installation_are_checked_before_both_original_receipts(self):
        registration = self.command(); receipt = self.record(registration)
        preview = self.preview(receipt["id"])
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Choix contrôlé"}
        endpoint = self.conflicts_url(receipt["id"], "resolve/")
        self.assertEqual(self.manager_api.post(endpoint, command, format="json").status_code, 200)
        self.member.is_active = False; self.member.save(update_fields=["is_active"])
        self.assertEqual(self.manager_api.post(self.conflicts_url(), registration, format="json").status_code, 404)
        self.assertEqual(self.manager_api.post(endpoint, command, format="json").status_code, 404)
        self.member.is_active = True; self.member.save(update_fields=["is_active"])
        Installation.objects.filter(id=self.b_install.id).update(revoked_at=timezone.now())
        self.assertEqual(self.manager_api.post(endpoint, command, format="json").status_code, 401)

    def test_preview_is_bound_to_current_actor_device_parent_and_pack_version(self):
        receipt = self.record(); preview = self.preview(receipt["id"])
        body = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Révision contrôlée"}
        endpoint = self.conflicts_url(receipt["id"], "resolve/")
        self.assertEqual(self.client.post(endpoint, body, format="json").status_code, 409)
        renamed = self.client.patch(self.pack_url(), {"operation_id": str(uuid.uuid4()), "base_version": 3,
            "name": "Carton renommé", "reason": "Libellé relu"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        self.assertEqual(self.manager_api.post(endpoint, body, format="json").status_code, 409)
        body["preview_token"] = self.preview(receipt["id"])["preview_token"]
        self.assertEqual(self.manager_api.post(endpoint, body, format="json").status_code, 200)

    def test_preserved_source_proposals_and_resolution_are_immutable_in_sql(self):
        from catalogue.models import PackPriceConflict, PackPriceProposal, PackPriceResolution
        receipt = self.record(); preview = self.preview(receipt["id"])
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Décision finale"}
        self.assertEqual(self.manager_api.post(self.conflicts_url(receipt["id"], "resolve/"), command, format="json").status_code, 200)
        mutations = [lambda: PackPriceConflict.objects.filter(id=receipt["id"]).update(source_snapshot={}),
            lambda: PackPriceConflict.objects.filter(id=receipt["id"]).delete(),
            lambda: PackPriceProposal.objects.update(proposed_price_fcfa=1), lambda: PackPriceProposal.objects.all().delete(),
            lambda: PackPriceResolution.objects.update(price_fcfa=1), lambda: PackPriceResolution.objects.all().delete()]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic(): change()

    def test_failed_resolution_rolls_back_price_parent_versions_and_decision(self):
        from catalogue.models import PackPriceResolution
        receipt = self.record(); preview = self.preview(receipt["id"])
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Décision incertaine"}
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_pack_resolution BEFORE INSERT ON catalogue_packpriceresolution BEGIN SELECT RAISE(ABORT,'injected pack decision failure'); END")
        try:
            with self.assertRaises(DatabaseError):
                self.manager_api.post(self.conflicts_url(receipt["id"], "resolve/"), command, format="json")
        finally:
            with connection.cursor() as cursor: cursor.execute("DROP TRIGGER fail_pack_resolution")
        self.assertFalse(PackPriceResolution.objects.exists())
        self.assertFalse(Activity.objects.filter(id=command["operation_id"]).exists())
        self.product.refresh_from_db()
        self.assertEqual((ProductPack.objects.get(id=self.pack_id).version, self.product.version), (3, 3))

    def recovered_source(self):
        """Rebuild the native update through the real catalogue recovery writer."""
        from backups import models as backups
        from backups.services.recovery_catalogue_revisions import persist_catalogue_revision
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision, immutable_catalogue_state
        from django.utils.dateparse import parse_datetime
        source_id = self.source.id
        details = deepcopy(self.source.details)
        before = immutable_catalogue_state(details["before"], pack=True)
        after = immutable_catalogue_state(details["after"], pack=True)
        self.source.delete()
        ProductPack.objects.filter(id=self.pack_id).update(sale_price_fcfa=before["sale_price_fcfa"],
            name=before["name"], is_active=before["is_active"], base_quantity_milli=before["base_quantity_milli"],
            version=before["version"], updated_at=parse_datetime(before["updated_at"]))
        Product.objects.filter(id=self.product.id).update(version=before["product_version"],
            updated_at=parse_datetime(before["product_updated_at"]))
        generation = backups.RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),
            backup_id=uuid.uuid4(), initiated_by=self.owner.id)
        session = backups.RestoreSession.objects.create(generation=generation)
        deposit = backups.RecoveryJournalDeposit.objects.create(session=session, shop=self.shop,
            operation_id=uuid.uuid4(), submitted_by=self.manager, submitted_installation=self.b_install,
            reason="Journal conservé du format", format_version=1, content_sha256="a" * 64, receipt={})
        capture = backups.RecoveryJournalOperation.objects.create(session=session, shop=self.shop,
            first_deposit=deposit, operation_id=source_id, origin_user_id=uuid.uuid4(), origin_installation_id=uuid.uuid4(),
            operation_type="pack_update", occurred_at=parse_datetime(after["updated_at"]),
            origin_recorded_at=timezone.now(), payload={"schema_version": 1, "original_request": details["request"],
                "catalogue_revision": {"before": before, "after": after}}, dependencies=[], content_sha256="b" * 64)
        decoded = decode_catalogue_revision(capture)
        persist_catalogue_revision(shop=self.shop, actor=self.manager, installation=self.b_install,
            decoded=decoded, origin_capture=capture, recovery_review={"reason": "Source vérifiée"})
        self.source = Activity.objects.get(id=source_id)
        return capture

    def test_recovered_pack_price_source_keeps_origin_and_still_supports_a_conflict(self):
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        capture = self.recovered_source()
        receipt, created = record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id,
            self.manager, self.b_install, self.command(), self.b_session.id)
        self.assertTrue(created)
        native = next(row for row in receipt["proposals"] if row["operation_id"] == str(self.source.id))
        self.assertEqual((native["actor_id"], native["installation_id"]),
            (str(capture.origin_user_id), str(capture.origin_installation_id)))
        self.assertEqual((receipt["common_price_fcfa"], ProductPack.objects.get(id=self.pack_id).sale_price_fcfa), (4800, 4800))
        self.assertFalse(User.objects.filter(id=capture.origin_user_id).exists())
        self.assertFalse(Installation.objects.filter(id=capture.origin_installation_id).exists())

    def test_exact_large_proposal_and_invalid_integer_inputs_never_round(self):
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict, preview_pack_price_conflict, resolve_pack_price_conflict
        from rest_framework.exceptions import ValidationError
        for invalid in (True, 1.0, "5000", -1, 9223372036854775808):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, self.manager,
                    self.b_install, self.command(proposed_price_fcfa=invalid), self.b_session.id)
        receipt, _ = record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, self.manager,
            self.b_install, self.command(proposed_price_fcfa=9223372036854775807), self.b_session.id)
        proposal = next(row for row in receipt["proposals"] if row["proposed_price_fcfa"] == 9223372036854775807)
        preview = preview_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, receipt["id"],
            self.manager, self.b_install, self.b_session.id)
        resolved, _ = resolve_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, receipt["id"],
            self.manager, self.b_install, {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
                "proposal_id": proposal["id"], "reason": "Prix entier exact"}, self.b_session.id)
        self.assertEqual(resolved["resolution"]["price_fcfa"], 9223372036854775807)
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 9223372036854775807)

    def test_many_proposals_have_a_bounded_receipt_and_a_complete_collection_reference(self):
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        for price in range(5000, 5027):
            receipt, _ = record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id,
                self.manager, self.b_install, self.command(proposed_price_fcfa=price), self.b_session.id)
        self.assertEqual(len(receipt["proposals"]), 25)
        self.assertEqual(receipt["proposals_count"], 28)
        self.assertEqual(receipt["proposals_url"], self.conflicts_url(receipt["id"], "proposals/"))

    def test_received_proposal_failure_rolls_back_the_fallback_and_source_copy_together(self):
        from catalogue.models import PackPriceConflict, PackPriceProposal
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        command = self.command()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_pack_received_proposal BEFORE INSERT ON catalogue_packpriceproposal WHEN NEW.is_native=0 BEGIN SELECT RAISE(ABORT,'injected received proposal failure'); END")
        try:
            with self.assertRaises(DatabaseError):
                record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, self.manager,
                    self.b_install, command, self.b_session.id)
        finally:
            with connection.cursor() as cursor: cursor.execute("DROP TRIGGER fail_pack_received_proposal")
        self.assertEqual((PackPriceConflict.objects.count(), PackPriceProposal.objects.count()), (0, 0))
        self.product.refresh_from_db()
        self.assertEqual((ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, self.product.version), (5200, 2))
        self.assertFalse(Activity.objects.filter(id=command["operation_id"]).exists())

    def test_contradictory_recovered_origin_is_rejected_before_any_fallback(self):
        from backups.models import RecoveryJournalOperation
        from catalogue.models import PackPriceConflict
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        from common.services.errors import OperationConflict
        capture = self.recovered_source()
        RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.other)
        with self.assertRaises(OperationConflict):
            record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, self.manager,
                self.b_install, self.command(), self.b_session.id)
        self.assertFalse(PackPriceConflict.objects.exists())
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 5200)

    def test_price_conflict_preserves_an_already_confirmed_name_with_the_same_factor(self):
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        for recovered in (False, True):
            with self.subTest(recovered=recovered), transaction.atomic():
                changed = {"operation_id": str(uuid.uuid4()), "base_version": 2, "sale_price_fcfa": 5300,
                    "name": "Carton renommé confirmé", "reason": "Prix et libellé confirmés"}
                response = self.client.patch(self.pack_url(), changed, format="json")
                self.assertEqual(response.status_code, 200, response.content)
                self.source = Activity.objects.get(id=changed["operation_id"])
                if recovered: self.recovered_source()
                receipt, _ = record_pack_price_conflict(self.shop.id, self.product.id, self.pack_id, self.manager,
                    self.b_install, self.command(base_version=2), self.b_session.id)
                pack = ProductPack.objects.get(id=self.pack_id)
                self.assertEqual((pack.name, pack.base_quantity_milli, pack.sale_price_fcfa),
                    ("Carton renommé confirmé", 24000, 5200))
                self.assertEqual(receipt["common_price_fcfa"], 5200)
                transaction.set_rollback(True)
