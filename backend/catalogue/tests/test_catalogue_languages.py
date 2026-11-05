"""US-08: catalogue error messages follow the request language, keeping stable codes."""
import uuid
from django.test import TestCase
from catalogue.models import Product
from stock.tests import test_stock_thresholds as threshold_tests


class CatalogueLanguageTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def test_archive_conflict_is_localized_per_request_without_sticky_language(self):
        product, _ = self.create()
        Product.objects.filter(id=product["id"]).update(is_active=False)
        endpoint = self.url(product["id"]) + "packs/"
        for language, expected in [("en", "This product is archived."), ("fr", "Ce produit est archivé.")]:
            body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                    "base_quantity_milli": 24000, "sale_price_fcfa": 4800}
            response = self.client.post(endpoint, body, format="json", HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(str(response.data["detail"]), expected)
            self.assertEqual(response.data["detail"].code, "product_archived")

    def test_unknown_field_and_piece_threshold_are_localized(self):
        invalid = self.client.post(self.url(), self.payload(stock_threshold_milli=1500), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(str(invalid.data["stock_threshold_milli"][0]), "A piece quantity must be a whole number.")
        unknown = self.client.post(self.url(), self.payload(unrecognized=True), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(str(unknown.data["unrecognized"]), "Unknown field.")

    def test_user_entered_names_are_preserved_across_languages(self):
        body = self.payload(name="Savon de Douala")
        created = self.client.post(self.url(), body, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["name"], "Savon de Douala")
        self.assertEqual(self.client.get(self.url(body["id"]), HTTP_ACCEPT_LANGUAGE="fr").data["name"], "Savon de Douala")

    def test_business_choice_labels_use_french_and_english_without_changing_values(self):
        from django.utils import translation
        from identity.models import Membership
        product, _ = self.create()
        entity = Product.objects.get(id=product["id"])
        self.member.role = Membership.Role.MANAGER
        self.member.save()
        with translation.override("fr"):
            self.assertEqual(entity.get_unit_kind_display(), "Pièce")
            self.assertEqual(self.member.get_role_display(), "Responsable")
        with translation.override("en"):
            self.assertEqual(entity.get_unit_kind_display(), "Piece")
            self.assertEqual(self.member.get_role_display(), "Manager")
        self.assertEqual(entity.unit_kind, "piece")
        self.assertEqual(self.member.role, "manager")

    def test_catalogue_version_conflict_is_localized_with_stable_code(self):
        product, _ = self.create()
        response = self.client.patch(self.url(product["id"]), {"operation_id": str(uuid.uuid4()),
            "base_version": 2, "reason": "Motif utilisateur", "name": "Autre nom"}, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(str(response.data["detail"]), "The product has changed. Read its current version before editing.")
        self.assertEqual(response.data["detail"].code, "product_version_conflict")

    def test_seller_denial_uses_english_and_does_not_change_entered_data(self):
        self.authenticate(self.seller)
        response = self.client.post(self.url(), self.payload(name="Donnée saisie"), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["detail"]), "Only a manager can create a product.")
        self.assertEqual(Product.objects.count(), 0)

    def test_photo_version_and_decode_errors_are_localized(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        product, _ = self.create()
        endpoint = self.url(product["id"]) + "photo/"
        stale = self.client.delete(endpoint, {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "reason": "Motif utilisateur"}, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(str(stale.data["detail"]), "The photo has changed. Read its current version.")
        invalid = self.client.post(endpoint, {"operation_id": str(uuid.uuid4()), "base_version": 0,
            "reason": "Motif utilisateur", "image": SimpleUploadedFile("photo.jpg", b"not an image")},
            format="multipart", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(str(invalid.data["image"]), "Invalid image or image too large to decode.")

    def test_revoked_session_error_is_localized_and_keeps_machine_code(self):
        from identity.models import DeviceSession
        from django.utils import timezone
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        response = self.client.get(self.url(), HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(str(response.data["detail"]), "Session expired or revoked.")
        self.assertEqual(response.data["detail"].code, "invalid_session")

    def test_error_code_is_available_in_rendered_json_in_both_languages(self):
        product, _ = self.create()
        Product.objects.filter(id=product["id"]).update(is_active=False)
        for language in ("fr", "en"):
            response = self.client.post(self.url(product["id"]) + "packs/", {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                "base_quantity_milli": 24000, "sale_price_fcfa": 4800}, format="json", HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "product_archived")
            self.assertIn("detail", response.json())

    def test_editing_piece_threshold_reports_english_validation_without_modification(self):
        product, _ = self.create()
        response = self.client.patch(self.url(product["id"]), {"operation_id": str(uuid.uuid4()),
            "base_version": 1, "reason": "Seuil vérifié", "stock_threshold_milli": 1500},
            format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data["stock_threshold_milli"]), "A piece quantity must be a whole number.")
        self.assertEqual(Product.objects.get(id=product["id"]).version, 1)

    def test_pack_permission_and_piece_factor_messages_are_localized(self):
        product, _ = self.create()
        endpoint = self.url(product["id"]) + "packs/"
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                "base_quantity_milli": 1500, "sale_price_fcfa": 4800}
        invalid = self.client.post(endpoint, data, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(str(invalid.data["base_quantity_milli"]), "A pack must contain a whole number of pieces.")
        self.authenticate(self.seller)
        denied = self.client.post(endpoint, {**data, "base_quantity_milli": 24000}, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(str(denied.data["detail"]), "Only a manager can create a pack.")

    def test_stale_barcode_assignment_explains_conflict_in_english(self):
        product, _ = self.create()
        response = self.client.patch(self.url(product["id"]) + "barcode/", {
            "operation_id": str(uuid.uuid4()), "base_version": 1, "code": "CODE",
            "reason": "Code vérifié"}, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "barcode_version_conflict")
        self.assertEqual(response.json()["detail"], "The barcode assignment has changed. Read its current version.")

    def test_openapi_validates_without_enum_warning_in_both_languages(self):
        from io import StringIO
        import json
        from django.core.management import call_command
        for language in ("fr", "en"):
            with self.subTest(language=language):
                output = StringIO()
                call_command("spectacular", lang=language, format="openapi-json", validate=True,
                             fail_on_warn=True, stdout=output, stderr=StringIO())
                schema = json.loads(output.getvalue())
                roles = schema["components"]["schemas"]["MembershipRoleEnum"]
                self.assertEqual(roles["enum"], ["manager", "seller"])
