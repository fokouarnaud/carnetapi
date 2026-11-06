"""Pack price decisions expose only scoped public metadata and current authority."""
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalogue.models import ProductPack, PackPriceConflict
from catalogue.tests import test_pack_price_conflicts as fixtures


class PackPriceConflictAPITests(TestCase):
    setUp = fixtures.PackPriceConflictTests.setUp
    authenticate = fixtures.PackPriceConflictTests.authenticate
    url = fixtures.PackPriceConflictTests.url
    payload = fixtures.PackPriceConflictTests.payload
    create = fixtures.PackPriceConflictTests.create
    pack_url = fixtures.PackPriceConflictTests.pack_url
    conflicts_url = fixtures.PackPriceConflictTests.conflicts_url
    command = fixtures.PackPriceConflictTests.command
    record = fixtures.PackPriceConflictTests.record
    preview = fixtures.PackPriceConflictTests.preview

    def test_strict_input_rejects_extra_factor_money_coercion_and_unbounded_values(self):
        for changes in ({"proposed_price_fcfa": True}, {"proposed_price_fcfa": 1.5},
                {"proposed_price_fcfa": -1}, {"proposed_price_fcfa": 9223372036854775808},
                {"base_version": 1.0}, {"base_version": 0}, {"reason": " "},
                {"base_quantity_milli": 12000}, {"unknown": 1}):
            with self.subTest(changes=changes):
                response = self.manager_api.post(self.conflicts_url(), self.command(**changes), format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PackPriceConflict.objects.exists())
        self.assertEqual(ProductPack.objects.get(id=self.pack_id).sale_price_fcfa, 5200)

    def test_seller_is_denied_all_review_routes_and_parent_shop_pack_are_scoped(self):
        conflict = self.record()
        self.authenticate(self.seller)
        routes = [("get", self.conflicts_url(), None), ("get", self.conflicts_url(conflict["id"]), None),
            ("get", self.conflicts_url(conflict["id"], "preview/"), None),
            ("post", self.conflicts_url(), self.command()),
            ("post", self.conflicts_url(conflict["id"], "resolve/"), {})]
        for method, url, body in routes:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, body, format="json") if body is not None else self.client.get(url)
                self.assertEqual(response.status_code, 403, response.content)
        for url in (self.conflicts_url().replace(str(self.shop.id), str(self.other.id)),
                self.conflicts_url().replace(str(self.product.id), str(self.weight.id)),
                self.conflicts_url().replace(self.pack_id, str(uuid.uuid4()))):
            with self.subTest(url=url):
                self.assertEqual(self.manager_api.get(url).status_code, 404)
                self.assertEqual(self.manager_api.post(url, self.command(), format="json").status_code, 404)
        self.b_session.revoked_at = timezone.now()
        self.b_session.save(update_fields=["revoked_at"])
        self.assertEqual(self.manager_api.get(self.conflicts_url()).status_code, 401)
        self.assertEqual(self.manager_api.post(self.conflicts_url(), self.command(), format="json").status_code, 401)

    def test_public_pack_metadata_has_no_proposals_actor_or_cost_and_clears_on_resolution(self):
        conflict = self.record()
        self.authenticate(self.seller)
        response = self.client.get(self.pack_url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["sale_price_fcfa"], 4800)
        self.assertEqual(response.data["price_conflict"], {
            "id": conflict["id"], "state": "open", "common_price_fcfa": 4800})
        for name in ("proposals", "resolution", "actor_id", "installation_id", "reason", "purchase_price_fcfa"):
            self.assertNotIn(name, response.data)
            self.assertNotIn(name, response.data["price_conflict"])
        preview = self.preview(conflict["id"])
        selected = next(row for row in conflict["proposals"] if row["proposed_price_fcfa"] == 5000)
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "proposal_id": selected["id"], "reason": "Choix explicite du prix du carton"}
        resolved = self.manager_api.post(self.conflicts_url(conflict["id"], "resolve/"), command, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.assertEqual(self.client.get(self.pack_url()).data["sale_price_fcfa"], 5000)
        self.assertIsNone(self.client.get(self.pack_url()).data["price_conflict"])
        self.assertEqual(self.client.get(self.pack_url() + "history/").status_code, 403)
        self.authenticate(self.owner)
        history = self.client.get(self.pack_url() + "history/")
        self.assertEqual(history.status_code, 200, history.content)
        actions = [row["action"] for row in history.data["results"]]
        self.assertIn("catalogue_pack_price_conflict_recorded", actions)
        self.assertIn("catalogue_pack_price_resolved", actions)

    def test_signed_preview_is_not_interchangeable_between_two_packs_or_operation_namespaces(self):
        first_command = self.command()
        first = self.record(first_command)
        another = self.create(self.payload(name="Autre carton"))
        self.assertEqual(another.status_code, 201, another.content)
        second_pack = another.data["id"]
        second_url = self.url() + second_pack + "/"
        change = {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "sale_price_fcfa": 5300, "reason": "Tarif du second conditionnement"}
        self.assertEqual(self.client.patch(second_url, change, format="json").status_code, 200)
        second_conflicts = second_url + "price-conflicts/"
        second = self.manager_api.post(second_conflicts,
            self.command(source_change_id=change["operation_id"]), format="json")
        self.assertEqual(second.status_code, 201, second.content)
        second_preview = self.manager_api.get(second_conflicts + second.data["id"] + "/preview/")
        self.assertEqual(second_preview.status_code, 200, second_preview.content)
        endpoint = self.conflicts_url(first["id"], "resolve/")
        body = {"operation_id": str(uuid.uuid4()), "preview_token": second_preview.data["preview_token"],
            "proposal_id": None, "reason": "Token d'un autre conditionnement"}
        self.assertEqual(self.manager_api.post(endpoint, body, format="json").status_code, 409)
        self.assertEqual(self.manager_api.post(endpoint, {**body, "preview_token": "x" * 513}, format="json").status_code, 400)
        replay_elsewhere = self.manager_api.post(second_conflicts, first_command, format="json")
        self.assertEqual(replay_elsewhere.status_code, 409, replay_elsewhere.content)
        self.assertEqual(self.manager_api.get(second_conflicts + first["id"] + "/").status_code, 404)
        self.assertEqual(PackPriceConflict.objects.filter(state="open").count(), 2)

    def test_public_pack_page_adds_no_per_row_conflict_queries_or_private_cost_select(self):
        self.record()
        ProductPack.objects.bulk_create([ProductPack(product=self.product, name=f"Format {index}",
            base_quantity_milli=1000, sale_price_fcfa=250) for index in range(24)])
        self.authenticate(self.seller)
        query_counts = []
        for page_size in (1, 25):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url() + f"?page_size={page_size}")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual((response.data["count"], len(response.data["results"])), (25, page_size))
            self.assertTrue(any(row["price_conflict"] for row in response.data["results"]))
            query_counts.append(sum(PackPriceConflict._meta.db_table in row["sql"] for row in queries.captured_queries))
            pages = [row["sql"] for row in queries.captured_queries
                if 'FROM "' + ProductPack._meta.db_table + '"' in row["sql"] and " LIMIT " in row["sql"]]
            self.assertTrue(pages)
            self.assertTrue(all('"purchase_price_fcfa"' not in query for query in pages))
        self.assertEqual(query_counts[0], query_counts[1], query_counts)
        self.assertLessEqual(query_counts[1], 2, query_counts)

    def test_review_list_is_bounded_in_sql_after_shop_product_pack_authorization(self):
        first = self.record()
        for _ in range(2):
            preview = self.preview(first["id"])
            response = self.manager_api.post(self.conflicts_url(first["id"], "resolve/"), {
                "operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
                "proposal_id": None, "reason": "Clôture explicite pour l'historique"}, format="json")
            self.assertEqual(response.status_code, 200, response.content)
            pack = ProductPack.objects.get(id=self.pack_id)
            change = {"operation_id": str(uuid.uuid4()), "base_version": pack.version,
                "sale_price_fcfa": 5200, "reason": "Nouvelle proposition historique"}
            self.assertEqual(self.client.patch(self.pack_url(), change, format="json").status_code, 200)
            first = self.record(self.command(source_change_id=change["operation_id"], base_version=pack.version))
        with CaptureQueriesContext(connection) as queries:
            response = self.manager_api.get(self.conflicts_url() + "?page_size=1")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["count"], len(response.data["results"])), (3, 1))
        self.assertIsNotNone(response.data["next"])
        table = PackPriceConflict._meta.db_table
        pages = [row["sql"] for row in queries.captured_queries if 'FROM "' + table + '"' in row["sql"] and " LIMIT 1" in row["sql"]]
        self.assertTrue(pages)
        for query in pages:
            for field in ("shop_id", "product_id", "pack_id"):
                self.assertIn('"' + table + '"."' + field + '"', query)
        self.assertEqual(len(self.manager_api.get(self.conflicts_url() + "?page_size=999").data["results"]), 3)

    def test_proposals_are_bounded_in_receipts_and_page_two_can_be_selected_without_a_quota(self):
        conflict = self.record()
        last_operation = None
        for index in range(26):
            command = self.command(proposed_price_fcfa=5001 + index)
            last_operation = command["operation_id"]
            response = self.manager_api.post(self.conflicts_url(), command, format="json")
            self.assertEqual(response.status_code, 201, response.content)
        detail = self.conflicts_url(conflict["id"])
        with CaptureQueriesContext(connection) as queries:
            receipt = self.manager_api.get(detail)
        self.assertEqual(receipt.status_code, 200, receipt.content)
        self.assertEqual((receipt.data["proposals_count"], len(receipt.data["proposals"])), (28, 25))
        endpoint = receipt.data["proposals_url"]
        self.assertEqual(endpoint, detail + "proposals/")
        proposal_queries = [row["sql"] for row in queries.captured_queries
            if "catalogue_packpriceproposal" in row["sql"]]
        self.assertTrue(proposal_queries)
        self.assertTrue(any("ROW_NUMBER" in query.upper() or " LIMIT 25" in query for query in proposal_queries))
        first_page = self.manager_api.get(endpoint)
        self.assertEqual((first_page.status_code, first_page.data["count"], len(first_page.data["results"])), (200, 28, 25))
        self.assertIsNotNone(first_page.data["next"])
        with CaptureQueriesContext(connection) as queries:
            second_page = self.manager_api.get(first_page.data["next"])
        self.assertEqual((second_page.status_code, len(second_page.data["results"])), (200, 3))
        self.assertTrue(any(" LIMIT " in row["sql"] and " OFFSET 25" in row["sql"] for row in queries.captured_queries))
        self.assertTrue(set(row["id"] for row in first_page.data["results"]).isdisjoint(row["id"] for row in second_page.data["results"]))
        chosen = next(row for row in second_page.data["results"] if row["operation_id"] == last_operation)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        foreign = endpoint.replace(str(self.product.id), str(self.weight.id))
        self.assertEqual(self.manager_api.get(foreign).status_code, 404)
        preview = self.preview(conflict["id"])
        resolved = self.manager_api.post(detail + "resolve/", {"operation_id": str(uuid.uuid4()),
            "preview_token": preview["preview_token"], "proposal_id": chosen["id"],
            "reason": "Choix explicite d'une proposition de la seconde page"}, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.assertEqual(resolved.data["resolution"]["proposal_id"], chosen["id"])
        self.assertEqual(resolved.data["resolution"]["price_fcfa"], chosen["proposed_price_fcfa"])
