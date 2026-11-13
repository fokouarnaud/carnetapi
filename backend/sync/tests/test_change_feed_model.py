"""US-48 / G6: each shop has a monotone change sequence and an append-only feed."""
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase

from identity.models import Shop, User


class ChangeFeedModelTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username="feed-owner")
        self.shop = Shop.objects.create(name="A", owner=owner)
        self.other = Shop.objects.create(name="B", owner=owner)

    def test_record_change_assigns_consecutive_sequences_per_shop(self):
        from sync.services import record_change
        first = record_change(self.shop, "catalogue.product", uuid.uuid4(), "upsert")
        second = record_change(self.shop, "sales.sale", uuid.uuid4(), "upsert")
        elsewhere = record_change(self.other, "catalogue.product", uuid.uuid4(), "upsert")
        self.assertEqual((first.seq, second.seq, elsewhere.seq), (1, 2, 1))

    def test_sequence_is_unique_within_a_shop(self):
        from sync.models import ChangeFeedEntry
        ChangeFeedEntry.objects.create(shop=self.shop, seq=1, object_type="catalogue.product",
                                       object_id=uuid.uuid4(), kind="upsert")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChangeFeedEntry.objects.create(shop=self.shop, seq=1, object_type="sales.sale",
                                           object_id=uuid.uuid4(), kind="upsert")

    def test_unknown_kind_and_non_positive_sequence_are_rejected_by_database(self):
        from sync.models import ChangeFeedEntry
        for changes in ({"seq": 0, "kind": "upsert"}, {"seq": 5, "kind": "explode"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ChangeFeedEntry.objects.create(shop=self.shop, object_type="catalogue.product",
                                               object_id=uuid.uuid4(), **changes)
