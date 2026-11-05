"""Synthetic mandatory-row storage floor; not a sales/API/load qualification."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event, Thread
import time
import uuid


def identifier(kind, number):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"app-x-storage-floor/{kind}/{number}").hex


def visible_bytes(work):
    total = 0
    for path in work.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def measure(work):
    if (not work.is_absolute() or work.is_symlink() or not work.name.startswith("restore-work-")
            or (work / "candidate.sqlite3").is_symlink() or (work / "candidate.sqlite3").stat().st_size != 0):
        raise ValueError("invalid_storage_work_directory")
    request = json.loads((work / "request.json").read_text(encoding="utf-8"))
    if request.get("purpose") != "storage-floor":
        raise ValueError("invalid_storage_work_directory")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from verification.storage_model import Workload, annual_floor_status
    target = Workload(**request["workload"])
    maximum = request["max_database_bytes"]
    reserve = 1024**3
    if type(maximum) is not int or maximum <= 0 or shutil.disk_usage(work).free < 2*maximum+reserve:
        raise ValueError("insufficient_measurement_space")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.restore_settings"
    os.environ["APP_RESTORE_WORKDIR"] = str(work)
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connection, connections, transaction
    from identity.models import User, Shop, Installation
    from catalogue.models import Product
    from backups.services.schema_audit import schema_profile
    started = time.perf_counter()
    stop = Event()
    peak = [0]
    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], visible_bytes(work))
            stop.wait(0.02)
    sampler = Thread(target=sample, daemon=True)
    sampler.start()
    try:
        # Local fixture generation only; preserve SQLite's durability settings.
        # A tiny default cache otherwise rewrites random UUID index pages in each batch.
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA cache_size = -131072")
            cursor.execute("PRAGMA cache_size")
            sqlite_cache_size = cursor.fetchone()[0]
            cursor.execute("PRAGMA synchronous")
            sqlite_synchronous = cursor.fetchone()[0]
        call_command("migrate", verbosity=0, interactive=False)
        owner = User.objects.create_user(id=uuid.UUID(identifier("owner", 0)), username="storage-floor-owner")
        shops = [Shop(id=uuid.UUID(identifier("shop", index)), owner=owner, name="S") for index in range(target.shops)]
        Shop.objects.bulk_create(shops)
        devices = [Installation(id=uuid.UUID(identifier("device", index)), user=owner, label="D") for index in range(target.devices)]
        Installation.objects.bulk_create(devices)
        products = [Product(id=uuid.UUID(identifier("product", index)), shop=shops[index//target.products_per_shop],
            name="P", unit_kind="piece", unit_label="u", sale_price_fcfa=500) for index in range(target.products)]
        Product.objects.bulk_create(products, batch_size=500)
        schema_sha256 = schema_profile(work / "candidate.sqlite3")["sha256"]
        activity_sql = 'INSERT INTO core_activity (id,created_at,updated_at,shop_id,actor_id,action,target_id,reason,details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        sale_sql = 'INSERT INTO core_sale (id,created_at,updated_at,mode,customer_id,shop_id,actor_id,installation_id,operation_id,occurred_at,reference_total_fcfa,discount_total_fcfa,net_total_fcfa,stock_requires_regularization) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        line_sql = 'INSERT INTO core_saleline (id,created_at,updated_at,sale_id,product_id,pack_id,position,product_version,product_name,unit_kind,unit_label,quantity_milli,factor_milli,base_quantity_milli,unit_price_fcfa,reference_fcfa,own_discount_fcfa,global_discount_fcfa,net_fcfa,unit_cost_micros) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        base = datetime(2025, 10, 20, tzinfo=timezone.utc)
        generated, stopped_reason = 0, None
        data_started = time.perf_counter()
        # One fixture transaction; bounded Python batches, no per-sale business-flow claim.
        with transaction.atomic():
            for first in range(0, target.sales, 1000):
                activities, sales, lines = [], [], []
                for index in range(first, min(first+1000, target.sales)):
                    day, offset = divmod(index, target.shops*target.sales_per_shop_day)
                    shop_number, shop_sale = divmod(offset, target.sales_per_shop_day)
                    instant = (base+timedelta(days=day, seconds=shop_sale*86400//target.sales_per_shop_day)).strftime("%Y-%m-%d %H:%M:%S")
                    shop = shops[shop_number].id.hex
                    device = devices[shop_number*target.devices_per_shop+shop_sale%target.devices_per_shop].id.hex
                    sale_id, operation = identifier("sale", index), identifier("activity", index)
                    amount = 500*target.lines_per_sale
                    activities.append((operation, instant, instant, shop, owner.id.hex, "sale_created", sale_id, "", "{}"))
                    sales.append((sale_id, instant, instant, "cash", None, shop, owner.id.hex, device, operation, instant, amount, 0, amount, True))
                    for position in range(target.lines_per_sale):
                        product = products[shop_number*target.products_per_shop+(shop_sale*target.lines_per_sale+position)%target.products_per_shop].id.hex
                        lines.append((identifier("line", index*target.lines_per_sale+position), instant, instant, sale_id, product,
                            None, position, 1, "P", "piece", "u", 1000, 1000, 1000, 500, 500, 0, 0, 500, None))
                with connection.cursor() as cursor:
                    cursor.executemany(activity_sql, activities)
                    cursor.executemany(sale_sql, sales)
                    cursor.executemany(line_sql, lines)
                generated += len(sales)
                if generated % 10000 == 0 or generated == target.sales:
                    print(json.dumps({"stage": "storage_floor_rows", "sales": generated, "lines": generated*target.lines_per_sale,
                        "database_bytes": (work / "candidate.sqlite3").stat().st_size}), flush=True)
                if (work / "candidate.sqlite3").stat().st_size > maximum:
                    stopped_reason = "database_byte_bound"
                    break
                if shutil.disk_usage(work).free < reserve:
                    stopped_reason = "disk_reserve_reached"
                    break
        generation_seconds = time.perf_counter()-data_started
        with connection.cursor() as cursor:
            before_vacuum = (work / "candidate.sqlite3").stat().st_size
            vacuum_started = time.perf_counter()
            vacuumed = stopped_reason != "disk_reserve_reached" and shutil.disk_usage(work).free > before_vacuum+reserve
            if vacuumed:
                cursor.execute("VACUUM")
            vacuum_seconds = time.perf_counter()-vacuum_started
            actual = {}
            for label, table in (("shops", "core_shop"), ("products", "core_product"), ("devices", "core_installation"),
                    ("sales", "core_sale"), ("lines", "core_saleline"), ("activities", "core_activity")):
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                actual[label] = cursor.fetchone()[0]
            cursor.execute("PRAGMA foreign_key_check")
            foreign_keys_valid = cursor.fetchone() is None
            cursor.execute("PRAGMA integrity_check")
            integrity_valid = cursor.fetchall() == [("ok",)]
            cursor.execute("SELECT 1 FROM core_sale s LEFT JOIN (SELECT sale_id,SUM(net_fcfa) AS total,COUNT(*) AS lines FROM core_saleline GROUP BY sale_id) l ON l.sale_id=s.id WHERE l.total != s.net_total_fcfa OR l.lines != %s OR l.total IS NULL LIMIT 1", [target.lines_per_sale])
            matching_totals = cursor.fetchone() is None
            cursor.execute("PRAGMA page_count")
            page_count = cursor.fetchone()[0]
            cursor.execute("PRAGMA page_size")
            page_size = cursor.fetchone()[0]
            try:
                cursor.execute("SELECT m.tbl_name,SUM(d.pgsize) FROM dbstat d JOIN sqlite_master m ON m.name=d.name GROUP BY m.tbl_name ORDER BY SUM(d.pgsize) DESC")
                allocation = [{"table": table, "bytes_with_indexes": amount} for table, amount in cursor.fetchall()]
            except Exception:
                allocation = None
        if not foreign_keys_valid or not integrity_valid or not matching_totals:
            raise ValueError("invalid_storage_fixture")
        connections.close_all()
        measured = (work / "candidate.sqlite3").stat().st_size
        peak[0] = max(peak[0], visible_bytes(work))
        expected = {"shops": target.shops, "products": target.products, "devices": target.devices,
            "sales": target.sales, "lines": target.lines, "activities": target.sales}
        return {"measurement": "synthetic_minimal_sales_tables_storage_floor", "workload": asdict(target),
            "target_counts": expected, "actual_counts": actual, "target_row_counts_reached": actual == expected,
            "stopped_reason": stopped_reason, "database_bytes": measured, "before_vacuum_bytes": before_vacuum,
            "vacuum_completed": vacuumed, "page_count": page_count, "page_size": page_size, "table_allocation": allocation,
            "fixture_data_transaction": "single", "fixture_sqlite_cache_size": sqlite_cache_size, "fixture_sqlite_synchronous": sqlite_synchronous,
            "sampled_visible_temporary_peak_bytes": peak[0], "sampling_interval_ms": 20,
            "generation_seconds": generation_seconds, "vacuum_seconds": vacuum_seconds, "total_seconds": time.perf_counter()-started,
            "foreign_keys_valid": foreign_keys_valid, "integrity_valid": integrity_valid, "sale_totals_match_lines": matching_totals,
            "schema_sha256": schema_sha256, "initial_budget_comparison": "not_applicable_to_annual_dataset",
            "host_limit_bytes": request.get("host_limit_bytes"),
            "annual_floor_status": annual_floor_status(measured, host_limit_bytes=request.get("host_limit_bytes")),
            "qualifies_pilot_load": False, "api_business_flows_exercised": False,
            "omitted": ["photos", "stock movements", "sale payments", "cash movements", "cost snapshots",
                "activity request/response payloads", "purchases/returns/inventories", "runtime and code", "backup/restore staging"],
            "limitations": ["One-character product names and unit labels; timestamps only to whole seconds.",
                "Empty activity details; direct SQL inserts, not complete business operations.",
                "Visible-file sampling misses unlinked SQLite temporary files; no RAM, concurrency or hosting proof.",
                "Fixture generation uses one data transaction and a 128 MiB page cache, not production hosting configuration.",
                "Initial installed budget and annual hosting capacity are separate PRD gates."]}
    finally:
        stop.set()
        sampler.join(timeout=5)
        connections.close_all()


def main():
    work = Path(sys.argv[1]).resolve()
    try:
        report = measure(work)
        (work / "measurement.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        print(json.dumps({"error": "storage_floor_measurement_failed"}), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
