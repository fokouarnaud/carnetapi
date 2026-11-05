"""Exercise ordered cost arithmetic and versioned snapshots on a fresh SQLite file."""
from fractions import Fraction
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid


def main():
    with TemporaryDirectory(prefix="cost-smoke-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory) / "costs.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connections
        from django.utils import timezone
        from common.models import Activity
        from catalogue.models import Product
        from identity.models import Shop, User
        from stock.models import StockMovement, StockCostSnapshot
        from stock.services.moving_average_cost import AverageCostState, returned_cost_nanos
        call_command("migrate", verbosity=0)
        try:
            state = AverageCostState().receive(10000, 10000000000000).receive(10000, 12000000000000)
            after, cost = state.remove(5000)
            assert cost.unit_cost_micros == Fraction(1100000000)
            assert cost.total_nanos_fcfa == 5500000000000 and after.quantity_milli == 15000
            owner = User.objects.create_user(username="cost-smoke-owner")
            shop = Shop.objects.create(name="Coûts", owner=owner)
            product = Product.objects.create(shop=shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1500)
            for kind, unit_cost, action in (("opening", 1000000000, "fixture_cost_opening"),
                    ("purchase_receipt", 1200000000, "fixture_cost_receipt")):
                incoming = Activity.objects.create(shop=shop, actor=owner, action=action, target_id=product.id, reason="Donnée de vérification")
                StockMovement.objects.create(product=product, actor=owner, operation=incoming, kind=kind, quantity_milli=10000,
                    unit_cost_micros=unit_cost, occurred_at=timezone.now(), reason="Donnée de vérification")
            operation = Activity.objects.create(shop=shop, actor=owner, action="stock_loss_recorded", target_id=uuid.uuid4(), reason="Sortie physique à valoriser")
            movement = StockMovement.objects.create(product=product, actor=owner, operation=operation, kind="loss",
                quantity_milli=-5000, occurred_at=timezone.now(), reason="Sortie physique à valoriser")
            initial = StockCostSnapshot.objects.create(movement=movement, trigger_operation=operation, version=1,
                cost_state="unknown", quantity_after_milli=15000, stock_value_after_nanos_fcfa=None)
            cause = Activity.objects.create(shop=shop, actor=owner, action="cost_recalculated", target_id=product.id, reason="Coût connu confirmé")
            revised = StockCostSnapshot.objects.create(movement=movement, trigger_operation=cause, version=2, supersedes=initial,
                cost_state=cost.state, total_cost_nanos_fcfa=cost.total_nanos_fcfa,
                unit_cost_micros_numerator=cost.unit_cost_micros.numerator, unit_cost_micros_denominator=cost.unit_cost_micros.denominator,
                quantity_after_milli=after.quantity_milli, stock_value_after_nanos_fcfa=after.stock_value_nanos_fcfa)
            assert sum(StockMovement.objects.filter(product=product).values_list("quantity_milli", flat=True)) == after.quantity_milli
            revised.refresh_from_db()
            initial.refresh_from_db()
            assert initial.total_cost_nanos_fcfa is None and revised.total_cost_nanos_fcfa == 5500000000000
            assert returned_cost_nanos(24000000000000, 24000, 0, 3000) == 3000000000000
            negative, provisional = state.remove(22000)
            assert negative.quantity_milli == -2000 and provisional.state == "provisional"
            recovered = negative.receive(5000, 6000000000000)
            assert recovered.quantity_milli == 3000
            assert recovered.average_unit_cost_micros == Fraction(1200000000)
            assert recovered.stock_value_nanos_fcfa == 3600000000000
            assert recovered.has_unresolved_shortage and provisional.state == "provisional"
            print(json.dumps({"cost_math": "passed", "file_sqlite_snapshots": 2,
                "average_fcfa": str(cost.unit_cost_micros / 1000000), "outflow_fcfa": 5500,
                "stock_value_fcfa": 16500, "original_cost": initial.total_cost_nanos_fcfa,
                "negative_stock_milli": negative.quantity_milli, "negative_sale_state": provisional.state,
                "negative_receipt": "passed", "recovered_quantity_milli": recovered.quantity_milli,
                "unresolved_shortage": recovered.has_unresolved_shortage,
                "demonstration": "isolated_math_and_manual_snapshots"}))
        finally:
            connections.close_all()


if __name__ == "__main__":
    main()
