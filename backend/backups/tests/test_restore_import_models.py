"""US-43: collected encrypted parts never imply an activated restore."""
import uuid
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from identity.models import User


class RestoreImportModelTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_superuser(username="restore-model", password=None)

    def batch(self):
        from backups.models import RestoreImport
        return RestoreImport.objects.create(requested_by=self.operator, reason="Archive reçue",
            backup_id=uuid.uuid4(), part_count=3, snapshot_bytes=8192,
            header_sha256="a"*64, header={"total_parts": 3})

    def test_import_keeps_origin_and_author_without_claiming_activation(self):
        batch = self.batch()
        batch.refresh_from_db()
        self.assertEqual(batch.requested_by_id, self.operator.id)
        self.assertEqual(batch.reason, "Archive reçue")
        self.assertEqual(batch.part_count, 3)
        self.assertEqual(batch.parts.count(), 0)

    def test_parts_keep_a_single_immutable_slot_for_each_authenticated_number(self):
        from backups.models import RestoreImportPart
        batch = self.batch()
        RestoreImportPart.objects.create(batch=batch, uploaded_by=self.operator,
            number=2, size_bytes=120, sha256="b"*64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RestoreImportPart.objects.create(batch=batch, uploaded_by=self.operator,
                number=2, size_bytes=130, sha256="c"*64)

    def test_part_byte_and_number_bounds_cannot_be_bypassed_with_raw_sql(self):
        from backups.models import RestoreImportPart
        part = RestoreImportPart.objects.create(batch=self.batch(), uploaded_by=self.operator,
            number=2, size_bytes=15000000, sha256="b"*64)
        for column, value in (("size_bytes",0),("size_bytes",15000001),("size_bytes",1.5),
                ("number",0),("number",32769),("number",1.5)):
            with self.subTest(column=column, value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE backups_restoreimportpart SET {column}=%s WHERE id=%s', [value, part.id.hex])

    def test_archive_metadata_bounds_and_reason_are_required_in_storage(self):
        from backups.models import RestoreImport
        batch = self.batch()
        for values in ({"part_count":0},{"part_count":32769},{"snapshot_bytes":0},{"reason":""}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                RestoreImport.objects.filter(id=batch.id).update(**values)
