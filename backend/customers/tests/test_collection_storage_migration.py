"""Stored legacy money is audited before SQLite can coerce values in a rebuild."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from customers.tests import test_collection_storage as fixtures


class CollectionStorageMigrationTests(TransactionTestCase):
    def test_valid_collections_survive_and_fractional_legacy_money_stops_atomically(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("customers", "0002_customer_customer_shop_name_match_and_more")])
            fixtures.CollectionStorageTests.setUp(self)
            with connection.cursor() as cursor:
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500.5 WHERE id=%s", [self.payment.id.hex])
            with self.assertRaisesMessage(ValueError, "Non-integral"):
                MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                cursor.execute("SELECT unallocated_fcfa FROM core_customerpayment WHERE id=%s", [self.payment.id.hex])
                self.assertEqual(cursor.fetchone()[0], 500.5)
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500 WHERE id=%s", [self.payment.id.hex])
            MigrationExecutor(connection).migrate(latest)
            self.payment.refresh_from_db(); self.debt.refresh_from_db(); self.part.refresh_from_db()
            self.assertEqual((self.payment.amount_fcfa, self.payment.unallocated_fcfa, self.debt.remaining_fcfa,
                              self.part.received_fcfa, self.part.returned_fcfa), (2000, 500, 3500, 2500, 500))
        finally:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500 WHERE unallocated_fcfa=500.5")
            MigrationExecutor(connection).migrate(latest)
