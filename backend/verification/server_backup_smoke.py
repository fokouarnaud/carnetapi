"""Real HTTP backup preparation/download and independent console verification."""
from contextlib import ExitStack, closing
from datetime import timedelta
from io import BytesIO
from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from urllib.parse import urlencode, urlparse
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def main():
    with TemporaryDirectory(prefix="server-backup-http-") as directory, ExitStack() as cleanup:
        root = Path(directory)
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(root / "source.sqlite3")
        import django
        django.setup()
        from cryptography.fernet import Fernet
        from django.core import mail
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        cleanup.callback(connections.close_all)
        from django.test import override_settings
        from django.utils import timezone
        from PIL import Image
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import BackupEvent, BackupJob
        from catalogue.models import Product, ProductPhoto
        from identity.models import DeviceSession, Installation, Shop, User

        call_command("migrate", verbosity=0)
        key = Fernet.generate_key()
        technical = User.objects.create_superuser(username="backup-http-technical", password="isolated-loopback-test-password")
        owner = User.objects.create_user(username="backup-http-owner")
        shop = Shop.objects.create(owner=owner, name="Boutique sauvegardée")
        product = Product.objects.create(shop=shop, name="Photo cohérente", unit_kind="piece", unit_label="pièce", sale_price_fcfa=500)
        photo = BytesIO()
        Image.new("RGB", (32, 32), "blue").save(photo, format="JPEG")
        ProductPhoto.objects.create(product=product, data=photo.getvalue(), width=32, height=32)

        def token(user):
            installation = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=1))
            access = AccessToken.for_user(user)
            access["session_id"] = str(session.id)
            return str(access)

        technical_token, owner_token = token(technical), token(owner)
        from backups.services.restore_gate import initialize_gate, enter_maintenance, abort_maintenance
        gate = root / "restore-gate.sqlite3"
        initialize_gate(gate, root / "source.sqlite3", operation_id=uuid.uuid4(),
                        requested_by=technical.id, reason="Installation du contrôle HTTP local")
        stopped = Event()
        peak = [0]

        def sample():
            while not stopped.is_set():
                total = 0
                for path in root.rglob("*"):
                    try:
                        if path.is_file():
                            total += path.stat().st_size
                    except FileNotFoundError:
                        pass  # A scratch file was removed between sampling calls.
                peak[0] = max(peak[0], total)
                stopped.wait(0.005)

        sampler = Thread(target=sample, daemon=True)
        with override_settings(BACKUP_ROOT=root / "archives", BACKUP_ENCRYPTION_KEY=key,
                BACKUP_KEY_ID="demo-primary", BACKUP_PART_MAX_BYTES=32768, RESTORE_GATE_PATH=gate):
            server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
            thread = Thread(target=server.serve_forever, daemon=True)
            sampler.start()
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}/api/v1/backups/"

            def request(bearer, endpoint=base, body=None, binary=False):
                req = Request(endpoint, data=json.dumps(body).encode() if body is not None else None,
                    headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
                try:
                    response = urlopen(req, timeout=30)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, response.read() if binary else json.load(response)

            origin = f"http://127.0.0.1:{server.server_port}"
            cookie_jar = CookieJar()
            admin_browser = build_opener(HTTPCookieProcessor(cookie_jar))

            def admin_request(path, form=None):
                data = None
                if form is not None:
                    token = next(cookie.value for cookie in cookie_jar if cookie.name == "csrftoken")
                    data = urlencode({**form, "csrfmiddlewaretoken": token}).encode("ascii")
                req = Request(origin + path, data=data, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept-Language": "fr"})
                with admin_browser.open(req, timeout=30) as response:
                    return response.status, urlparse(response.geturl()).path, response.read().decode("utf-8")

            def admin_upload(path, fields, attachment):
                boundary = "restore-part-"+uuid.uuid4().hex
                token = next(cookie.value for cookie in cookie_jar if cookie.name == "csrftoken")
                blocks = []
                for name, value in {**fields, "csrfmiddlewaretoken": token}.items():
                    blocks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8"))
                blocks.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="part"; filename="received.axbkp"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n').encode("ascii"), attachment.read_bytes(),
                    (f'\r\n--{boundary}--\r\n').encode("ascii")])
                req = Request(origin+path, data=b"".join(blocks), headers={
                    "Content-Type": "multipart/form-data; boundary="+boundary, "Accept-Language": "fr"})
                with admin_browser.open(req, timeout=30) as response:
                    return response.status, urlparse(response.geturl()).path, response.read().decode("utf-8")

            try:
                body = {"operation_id": str(uuid.uuid4()), "recipient": "backup@example.test", "reason": "Vérification HTTP isolée"}
                created, receipt = request(technical_token, body=body)
                retried, retry = request(technical_token, body=body)
                assert created == 201 and retried == 200 and receipt == retry
                assert receipt["state"] == "ready" and receipt["part_count"] > 0
                downloaded = []
                for part in receipt["parts"]:
                    status, data = request(technical_token, base + receipt["id"] + f"/parts/{part['number']}/", binary=True)
                    assert status == 200 and len(data) == part["size_bytes"] <= 32768
                    assert hashlib.sha256(data).hexdigest() == part["sha256"]
                    target = root / part["filename"]
                    target.write_bytes(data)
                    downloaded.append(target)
                from backups.services.email_parts import build_part_email
                from email import policy
                from email.parser import BytesParser
                prepared_job = BackupJob.objects.get(id=receipt["id"])
                mime_sizes = []
                for saved_part in prepared_job.parts.all():
                    prepared = build_part_email(prepared_job, saved_part,
                        sender="sender@example.test", max_message_bytes=25000000)
                    parsed = BytesParser(policy=policy.SMTP).parsebytes(prepared.wire_bytes)
                    attached = list(parsed.iter_attachments())
                    assert len(attached) == 1
                    assert hashlib.sha256(attached[0].get_payload(decode=True)).hexdigest() == saved_part.sha256
                    assert str(parsed["To"]) == "backup@example.test"
                    assert key not in prepared.wire_bytes
                    mime_sizes.append(len(prepared.wire_bytes))
                from verification.smtp_test_server import LocalSMTPServer, LocalSMTPHandler
                smtp_server = LocalSMTPServer(("127.0.0.1", 0), LocalSMTPHandler)
                smtp_server.mode, smtp_server.received = "accept", []
                smtp_worker = Thread(target=smtp_server.serve_forever, daemon=True)
                smtp_worker.start()
                try:
                    with override_settings(EMAIL_HOST="127.0.0.1", EMAIL_PORT=smtp_server.server_address[1],
                            EMAIL_USE_TLS=False, EMAIL_USE_SSL=False, EMAIL_HOST_USER="sender@example.test",
                            EMAIL_HOST_PASSWORD="", EMAIL_TIMEOUT=2):
                        send_body = {"operation_id": str(uuid.uuid4())}
                        sent, send_receipt = request(technical_token, base + receipt["id"] + "/parts/1/send/", body=send_body)
                        replayed, replay_receipt = request(technical_token, base + receipt["id"] + "/parts/1/send/", body=send_body)
                        assert sent == 201 and replayed == 200 and send_receipt == replay_receipt
                        assert send_receipt["state"] == "accepted"
                        history_status, history = request(technical_token, base + receipt["id"] + "/parts/1/attempts/")
                        assert history_status == 200 and history["count"] == 1
                        denied_send, _ = request(owner_token, base + receipt["id"] + "/parts/1/send/", body={"operation_id": str(uuid.uuid4())})
                        assert denied_send == 403
                        from django.urls import reverse
                        login_path = reverse("admin:login")
                        assert admin_request(login_path)[0] == 200
                        login_status, login_result, _ = admin_request(login_path,
                            {"username": technical.username, "password": "isolated-loopback-test-password", "next": reverse("admin:index")})
                        assert login_status == 200 and login_result == reverse("admin:index")
                        from backups.models import RestoreImport
                        import_id = uuid.uuid4()
                        import_start = reverse("admin:backups_restoreimport_start")
                        assert admin_request(import_start)[0] == 200
                        fields = {"batch_id": str(import_id), "reason": "Import HTTP contrôlé", "encryption_key": key.decode("ascii")}
                        for attachment in reversed(downloaded):
                            import_status, inventory_path, inventory_html = admin_upload(import_start, fields, attachment)
                            assert import_status == 200 and inventory_path == reverse("admin:backups_restoreimport_inventory", args=[import_id])
                            assert "Vérification complète encore requise" in inventory_html
                            assert key.decode("ascii") not in inventory_html
                        admin_upload(import_start, fields, downloaded[0])
                        imported = RestoreImport.objects.get(id=import_id)
                        assert imported.parts.count() == len(downloaded)
                        assert imported.header["backup_id"] == receipt["id"]
                        from backups.models import ImportPreparation
                        preparation_form = reverse("admin:backups_restoreimport_prepare", args=[import_id])
                        assert admin_request(preparation_form)[0] == 200
                        preparation_operation = uuid.uuid4()
                        preparation_body = {"operation_id": str(preparation_operation), "reason": "Copie vérifiée via HTTP",
                            "encryption_key": key.decode("ascii")}
                        prepare_status, prepare_path, prepare_html = admin_request(preparation_form, preparation_body)
                        assert prepare_status == 200 and "Copie préparée" in prepare_html
                        assert "Cette copie ne peut pas encore être activée." in prepare_html
                        repeated_status, repeated_path, _repeated_html = admin_request(preparation_form, preparation_body)
                        assert repeated_status == 200 and repeated_path == prepare_path
                        prepared_import = ImportPreparation.objects.get(operation_id=preparation_operation)
                        assert prepared_import.state == "prepared" and prepared_import.manifest["activation_allowed"] is False
                        inspection_status, inspection_path, inspection_html = admin_request(prepare_path, {})
                        assert inspection_status == 200 and inspection_path == prepare_path
                        assert "Intégrité de la copie vérifiée" in inspection_html
                        assert "Cette copie ne peut pas encore être activée." in inspection_html
                        assert key.decode("ascii") not in inspection_html
                        assert key.decode("ascii") not in prepare_html
                        from backups.models import ImportQuarantine
                        quarantine_form=reverse("admin:backups_importquarantine_start",args=[prepared_import.id])
                        assert admin_request(quarantine_form)[0]==200
                        quarantine_body={"operation_id":str(uuid.uuid4()),"reason":"Suspendre les anciens droits via Unfold"}
                        quarantine_status,quarantine_path,quarantine_html=admin_request(quarantine_form,quarantine_body)
                        assert quarantine_status==200 and "Copie en quarantaine" in quarantine_html
                        assert "Cette copie ne peut pas encore être activée." in quarantine_html
                        repeated_quarantine_status,repeated_quarantine_path,_quarantine_html=admin_request(quarantine_form,quarantine_body)
                        assert repeated_quarantine_status==200 and repeated_quarantine_path==quarantine_path
                        quarantined_import=ImportQuarantine.objects.get(operation_id=quarantine_body["operation_id"])
                        assert quarantined_import.state=="quarantined" and quarantined_import.manifest["activation_allowed"] is False
                        assert Product.objects.get(id=product.id).sale_price_fcfa == 500
                        assert bytes(ProductPhoto.objects.get(product_id=product.id).data) == photo.getvalue()
                        configuration_url = base + "configuration/"
                        config_status, defaults = request(technical_token, configuration_url)
                        assert config_status == 200 and defaults["enabled"] is True
                        assert (defaults["local_hour"], defaults["local_minute"], defaults["version"]) == (2, 0, 0)
                        config_path = reverse("admin:backups_backupconfiguration_configure")
                        form_status, _, config_html = admin_request(config_path)
                        assert form_status == 200 and "02:00" in config_html and "Africa/Douala" in config_html
                        assert "Nouvelles sauvegardes automatiques bloquées" in config_html
                        assert "Aucune acceptation SMTP complète connue." in config_html
                        saved_status, saved_path, _ = admin_request(config_path, {"operation_id": str(uuid.uuid4()),
                            "expected_version": 0, "enabled": "on", "local_hour": 6, "local_minute": 15, "reason": "Horaire validé en HTTP"})
                        assert saved_status == 200 and saved_path == config_path
                        _, configured = request(technical_token, configuration_url)
                        assert configured["version"] == 1 and configured["automatic_recipient"] == "sender@example.test"
                        assert (configured["local_hour"], configured["local_minute"]) == (6, 15)
                        disabled_status, _, _ = admin_request(config_path, {"operation_id": str(uuid.uuid4()),
                            "expected_version": 1, "local_hour": 6, "local_minute": 15, "reason": "Désactivation du scénario"})
                        assert disabled_status == 200
                        _, final_config = request(technical_token, configuration_url)
                        assert final_config["version"] == 2 and final_config["enabled"] is False and final_config["next_run_at"] is None
                        denied_configuration, _ = request(owner_token, configuration_url)
                        assert denied_configuration == 403
                        start_path = reverse("admin:backups_backupjob_dispatch_start", args=[receipt["id"]])
                        assert admin_request(start_path)[0] == 200
                        started_status, progress_path, started_html = admin_request(start_path,
                            {"operation_id": str(uuid.uuid4()), "reason": "Envoi du reste de l’archive depuis Unfold"})
                        assert started_status == 200 and "1 / " + str(receipt["part_count"]) in started_html
                        command_id = str(uuid.UUID(progress_path.rstrip("/").split("/")[-1]))
                        dispatch_url = base + receipt["id"] + f"/dispatches/{command_id}/advance/"
                        for remaining in receipt["parts"][1:]:
                            step_status, current_path, step_html = admin_request(progress_path, {"action": "advance"})
                            assert step_status == 200 and current_path == progress_path
                            assert f"{remaining['number']} / {receipt['part_count']}" in step_html
                        assert "Acceptée par SMTP, livraison non confirmée" in step_html
                        assert "Fichiers de l’archive nettoyés" in step_html
                        assert 'name="action"' not in step_html
                        assert admin_request(progress_path)[0] == 200
                        repeated_status, progress = request(technical_token, dispatch_url, body={})
                        assert repeated_status == 200 and progress["state"] == "accepted"
                        detail_status, final_detail = request(technical_token, base + receipt["id"] + "/")
                        assert detail_status == 200 and final_detail["cleanup_state"] == "cleaned"
                        assert final_detail["cleaned_at"] is not None and final_detail["manifest"] == receipt["manifest"]
                        gone_status, gone = request(technical_token, base + receipt["id"] + "/parts/1/")
                        assert gone_status == 410 and gone["code"] == "backup_archive_cleaned"
                        smtp_before_health = len(smtp_server.received)
                        _, _, health_html = admin_request(config_path)
                        assert "Aucune archive incomplète ne bloque" in health_html
                        assert "Dernière acceptation SMTP complète" in health_html
                        assert "Âge lors de ce contrôle" in health_html
                        assert "L’acceptation SMTP ne confirme pas la livraison." in health_html
                        assert len(smtp_server.received) == smtp_before_health
                        extra_status, extra = request(technical_token, body={"operation_id": str(uuid.uuid4()),
                            "recipient": "backup@example.test", "reason": "Contrôle abandon incomplet"})
                        assert extra_status == 201 and extra["part_count"] > 1
                        partial_status, partial_receipt = request(technical_token, base + extra["id"] + "/parts/1/send/",
                            body={"operation_id": str(uuid.uuid4())})
                        assert partial_status == 201 and partial_receipt["state"] == "accepted"
                        _, _, health_html = admin_request(config_path)
                        assert "Nouvelles sauvegardes automatiques bloquées" in health_html
                        assert "Dernière acceptation SMTP complète" in health_html
                        assert extra["id"] in health_html and receipt["id"] in health_html
                        abandon_body = {"operation_id": str(uuid.uuid4()), "reason": "Arrêt explicite du scénario"}
                        abandoned_status, abandoned = request(technical_token, base + extra["id"] + "/abandon/", body=abandon_body)
                        abandon_retry_status, abandon_retry = request(technical_token, base + extra["id"] + "/abandon/", body=abandon_body)
                        assert abandoned_status == abandon_retry_status == 200 and abandoned == abandon_retry
                        assert abandoned["abandoned_at"] is not None and abandoned["cleanup_state"] == "waiting"
                        blocked_status, _ = request(technical_token, base + extra["id"] + "/parts/2/send/",
                            body={"operation_id": str(uuid.uuid4())})
                        assert blocked_status == 409
                        _, _, health_html = admin_request(config_path)
                        assert "Aucune archive incomplète ne bloque" in health_html
                        assert "Dernière acceptation SMTP complète" in health_html
                        assert len(list((root / "archives" / extra["id"]).glob("*.axbkp"))) == extra["part_count"]
                        partial_wire = smtp_server.received[-1]
                        from backups.services.configuration import change_configuration
                        from backups.models import BackupScheduleRun
                        change_configuration(technical, uuid.uuid4(), 2, True, 0, 0, "Activation du déclencheur loopback")
                        trigger = "http-loopback-dedicated-trigger-test-secret-0001"
                        with override_settings(BACKUP_SCHEDULER_TOKEN=trigger, BACKUP_SCHEDULER_ACTOR_ID=str(technical.id)):
                            wrong_status, _ = request(owner_token, base + "trigger/", body={})
                            assert wrong_status == 401
                            prepared_status, prepared = request(trigger, base + "trigger/", body={})
                            assert prepared_status == 200 and prepared == {"status": "prepared"}
                            automatic = BackupScheduleRun.objects.select_related("job").get()
                            assert automatic.job.recipient == "sender@example.test"
                            automatic_parts = automatic.job.part_count
                            for _ in range(automatic_parts):
                                step_status, step = request(trigger, base + "trigger/", body={})
                                assert step_status == 200 and step["status"] in {"progressed", "accepted"}
                            assert step == {"status": "accepted"}
                            assert request(trigger, base + "trigger/", body={}) == (200, {"status": "idle"})
                            assert request(trigger)[0] == 401
                            automatic.job.refresh_from_db()
                            assert automatic.job.cleanup_state == "cleaned"
                            _, _, health_html = admin_request(config_path)
                            assert "Aucune archive incomplète ne bloque" in health_html
                            assert str(automatic.job_id) in health_html
                finally:
                    smtp_server.shutdown()
                    smtp_server.server_close()
                    smtp_worker.join(timeout=3)
                assert len(smtp_server.received) == receipt["part_count"] + 1 + automatic_parts
                delivered_part = next(BytesParser(policy=policy.SMTP).parsebytes(smtp_server.received[0]).iter_attachments())
                assert hashlib.sha256(delivered_part.get_payload(decode=True)).hexdigest() == receipt["parts"][0]["sha256"]
                assert key not in smtp_server.received[0]
                expected_parts = {part["filename"]: part["sha256"] for part in receipt["parts"]}
                for wire in smtp_server.received[:receipt["part_count"]]:
                    attachment = next(BytesParser(policy=policy.SMTP).parsebytes(wire).iter_attachments())
                    assert hashlib.sha256(attachment.get_payload(decode=True)).hexdigest() == expected_parts[attachment.get_filename()]
                    assert key not in wire
                private_folder = root / "archives" / receipt["id"]
                assert not list(private_folder.glob("*.axbkp"))
                assert (private_folder / "manifest.json").is_file()
                denied, _response = request(owner_token)
                denied_part, _response = request(owner_token, base + receipt["id"] + "/parts/1/")
                assert denied == 403 and denied_part == 403
                separate_key = root / "separate.key"
                separate_key.write_bytes(key)
                repository = Path(__file__).resolve().parents[2]
                result = subprocess.run([sys.executable, "-X", "utf8", str(repository / "tools/verify_server_backup.py"),
                    "--key-file", str(separate_key), "--expected-migrations", str(repository / "backend/backup_schema.json"),
                    "--output-dir", str(root / "verified"), *map(str, downloaded)], text=True, capture_output=True, timeout=30)
                assert result.returncode == 0, result.stderr
                checked = json.loads(result.stdout)
                assert checked["live_database_replaced"] is False
                live_before_prepare = hashlib.sha256((root / "source.sqlite3").read_bytes()).hexdigest()
                prepared_copy = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "tools/prepare_server_restore.py"),
                    "--key-file", str(separate_key), "--expected-migrations", str(repository / "backend/backup_schema.json"),
                    "--output-dir", str(root / "prepared-restore"), *map(str, downloaded)], text=True, capture_output=True, timeout=120)
                assert prepared_copy.returncode == 0, prepared_copy.stderr
                prepared_restore = json.loads(prepared_copy.stdout)
                assert prepared_restore["migration_execution_checked"] is True
                assert prepared_restore["schema_audit_checked"] is True
                assert prepared_restore["schema_reference_sha256"] == hashlib.sha256((repository / "backend/restore_schema.json").read_bytes()).hexdigest()
                assert prepared_restore["activation_allowed"] is False
                assert prepared_restore["permissions_revalidated"] is False
                assert prepared_restore["live_database_replaced"] is False
                assert hashlib.sha256((root / "source.sqlite3").read_bytes()).hexdigest() == live_before_prepare
                assert request(technical_token, base + receipt["id"] + "/")[0] == 200
                with closing(sqlite3.connect(prepared_restore["candidate"])) as candidate:
                    assert candidate.execute("SELECT name FROM core_shop WHERE id=?", (shop.id.hex,)).fetchone()[0] == shop.name
                    assert bytes(candidate.execute("SELECT data FROM core_productphoto WHERE product_id=?", (product.id.hex,)).fetchone()[0]) == photo.getvalue()
                prepared_digest = hashlib.sha256(Path(prepared_restore["candidate"]).read_bytes()).hexdigest()
                quarantined_copy = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "tools/quarantine_server_restore.py"),
                    str(root / "prepared-restore"), "--output-dir", str(root / "quarantined-restore"),
                    "--operation-id", str(uuid.uuid4()), "--requested-by", str(technical.id),
                    "--reason", "Suspension des accès archivés, contrôle HTTP local"], text=True, capture_output=True, timeout=60)
                assert quarantined_copy.returncode == 0, quarantined_copy.stderr
                quarantined_restore = json.loads(quarantined_copy.stdout)
                assert quarantined_restore["access_quarantined"] is True
                assert quarantined_restore["activation_allowed"] is False and quarantined_restore["permissions_revalidated"] is False
                assert quarantined_restore["live_database_replaced"] is False
                assert hashlib.sha256(Path(prepared_restore["candidate"]).read_bytes()).hexdigest() == prepared_digest
                assert hashlib.sha256((root / "source.sqlite3").read_bytes()).hexdigest() == live_before_prepare
                assert request(technical_token, base + receipt["id"] + "/")[0] == 200
                with closing(sqlite3.connect(quarantined_restore["candidate"])) as candidate:
                    assert candidate.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0] == 0
                    assert candidate.execute("SELECT COUNT(*) FROM core_user WHERE is_superuser!=0 OR is_staff!=0").fetchone()[0] == 0
                    assert candidate.execute("SELECT COUNT(*) FROM core_membership WHERE is_active!=0").fetchone()[0] == 0
                    assert candidate.execute("SELECT COUNT(*) FROM core_user_groups").fetchone()[0] == 0
                    assert candidate.execute("SELECT COUNT(*) FROM core_user_user_permissions").fetchone()[0] == 0
                    assert candidate.execute("SELECT name FROM core_shop WHERE id=?", (shop.id.hex,)).fetchone()[0] == shop.name
                    assert bytes(candidate.execute("SELECT data FROM core_productphoto WHERE product_id=?", (product.id.hex,)).fetchone()[0]) == photo.getvalue()
                # A second, private target simulates the recovery sequence. The
                # SQLite serving the HTTP application is never replaced.
                from backups.services.technical_recovery import provision_technical_recovery
                from backups.services.restore_gate import hold_control, read_gate
                recovery_target = root / "recovery-target.sqlite3"
                recovery_gate = root / "recovery-gate.sqlite3"
                proof_vault = root / "private-control" / "proof.sqlite3"
                with closing(sqlite3.connect((root / "source.sqlite3").as_uri()+"?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(recovery_target)) as destination:
                        source.backup(destination)
                initialize_gate(recovery_gate, recovery_target, operation_id=uuid.uuid4(),
                                requested_by=technical.id, reason="Cible privée de contrôle de secours")
                recovery_code = provision_technical_recovery(recovery_gate, proof_vault, recovery_target,
                    root / "archives", operation_id=uuid.uuid4(), requested_by=technical.id,
                    reason="Préparation avant panne simulée", gate_version=1)
                enter_maintenance(recovery_gate, operation_id=uuid.uuid4(), requested_by=technical.id,
                                  reason="Incident simulé sur cible privée", expected_version=1)
                with hold_control(recovery_gate, recovery_target, required_mode="maintenance", expected_version=2):
                    shutil.copyfile(quarantined_restore["candidate"], recovery_target)
                bootstrap_operation = uuid.uuid4()
                bootstrap_command = [sys.executable, "-I", "-X", "utf8", str(repository / "tools/technical_recovery.py"),
                    "bootstrap", "--gate", str(recovery_gate), "--vault", str(proof_vault),
                    "--database", str(recovery_target), "--operation-id", str(bootstrap_operation),
                    "--requested-by", str(technical.id), "--reason", "Accès technique neuf, contrôle privé",
                    "--gate-version", "2", "--username", "recovered-technical", "--language", "en", "--stdin-json"]
                recovery_password = "secours-fixture-password-8!"
                bootstrap_input = json.dumps({"code": recovery_code.secret, "password": recovery_password,
                                              "password_confirmation": recovery_password})
                bootstrap_results = []
                for _ in range(2):
                    completed = subprocess.run(bootstrap_command, input=bootstrap_input, text=True,
                        capture_output=True, timeout=60, env={**os.environ, "DJANGO_SETTINGS_MODULE": "intentionally.invalid"})
                    assert completed.returncode == 0, "Technical bootstrap failed"
                    assert recovery_code.secret not in completed.stdout + completed.stderr
                    assert recovery_password not in completed.stdout + completed.stderr
                    bootstrap_results.append(json.loads(completed.stdout))
                assert bootstrap_results[0] == bootstrap_results[1]
                assert bootstrap_results[0]["activation_allowed"] is False
                assert bootstrap_results[0]["permissions_revalidated"] is False
                assert read_gate(recovery_gate)["mode"] == "maintenance"
                with closing(sqlite3.connect(proof_vault)) as vault:
                    assert vault.execute("SELECT state FROM recovery_proof").fetchone()[0] == "consumed"
                    assert vault.execute("SELECT COUNT(*) FROM recovery_events").fetchone()[0] == 1
                with closing(sqlite3.connect(recovery_target)) as target:
                    assert target.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0] == 1
                    assert target.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?", (technical.id.hex,)).fetchone() == (0,0,0)
                    assert target.execute("SELECT COUNT(*) FROM core_membership WHERE is_active=1").fetchone()[0] == 0
                    assert target.execute("SELECT name FROM core_shop WHERE id=?", (shop.id.hex,)).fetchone()[0] == shop.name
                review_common = ["--gate", str(recovery_gate), "--database", str(recovery_target),
                    "--gate-version", "2", "--username", "recovered-technical", "--stdin-json"]
                review_script = repository/"tools/review_restore_access.py"
                review_input = json.dumps({"password":recovery_password})
                inspected = subprocess.run([sys.executable,"-I","-X","utf8",str(review_script),
                    "inspect",*review_common,"--state","pending","--shop-id",str(shop.id)],
                    input=review_input,text=True,capture_output=True,timeout=60)
                assert inspected.returncode == 0, "Console inspection failed"
                assert recovery_password not in inspected.stdout+inspected.stderr
                review_page = json.loads(inspected.stdout)
                assert review_page["count"] == 1 and review_page["results"][0]["user_id"] == str(owner.id)
                assert review_page["results"][0]["state"] == "pending"
                review_operation = uuid.uuid4()
                review_command = [sys.executable,"-I","-X","utf8",str(review_script),"decide",*review_common,
                    "--review-id",review_page["results"][0]["id"],"--generation-id",review_page["generation_id"],
                    "--operation-id",str(review_operation),"--previous-version","0","--state","confirmed",
                    "--reason","Contrôle manuel privé de la qualité actuelle"]
                review_results = []
                for _ in range(2):
                    reviewed = subprocess.run(review_command,input=review_input,text=True,capture_output=True,timeout=60)
                    assert reviewed.returncode == 0, "Console responsible confirmation failed"
                    assert recovery_password not in reviewed.stdout+reviewed.stderr
                    review_results.append(json.loads(reviewed.stdout))
                assert review_results[0] == review_results[1]
                assert review_results[0]["decision"]["evidence_ref"] == ""
                assert review_results[0]["activation_allowed"] is False
                assert read_gate(recovery_gate)["mode"] == "maintenance"
                with closing(sqlite3.connect(recovery_target)) as target:
                    assert target.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision WHERE operation_id=?",[review_operation.hex]).fetchone()[0] == 1
                    assert target.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",[owner.id.hex]).fetchone() == (1,0,0)
                    assert target.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",[technical.id.hex]).fetchone() == (0,0,0)
                assert hashlib.sha256((root / "source.sqlite3").read_bytes()).hexdigest() == live_before_prepare
                incomplete_attachment = next(BytesParser(policy=policy.SMTP).parsebytes(partial_wire).iter_attachments())
                incomplete_path = root / incomplete_attachment.get_filename()
                incomplete_path.write_bytes(incomplete_attachment.get_payload(decode=True))
                incomplete = subprocess.run([sys.executable, "-X", "utf8", str(repository / "tools/verify_server_backup.py"),
                    "--key-file", str(separate_key), "--expected-migrations", str(repository / "backend/backup_schema.json"),
                    "--output-dir", str(root / "incomplete-refused"), str(incomplete_path)], text=True, capture_output=True, timeout=30)
                assert incomplete.returncode != 0 and "backup_parts_missing" in incomplete.stderr
                assert not list((root / "incomplete-refused").rglob("*.sqlite3"))
                with closing(sqlite3.connect(checked["snapshot"])) as snapshot:
                    assert snapshot.execute("SELECT name FROM core_shop WHERE id=?", (shop.id.hex,)).fetchone()[0] == shop.name
                    assert bytes(snapshot.execute("SELECT data FROM core_productphoto WHERE product_id=?", (product.id.hex,)).fetchone()[0]) == photo.getvalue()
                assert BackupJob.objects.count() == 3 and BackupEvent.objects.filter(job_id=receipt["id"]).count() == 6 + 2 * receipt["part_count"]
                assert len(getattr(mail, "outbox", [])) == 0
                from backups.services.import_preparation import preparation_directory
                private_candidate = preparation_directory(prepared_import) / "candidate.sqlite3"
                from backups.services.import_quarantine import quarantine_directory
                private_quarantined=quarantine_directory(quarantined_import)/"candidate.sqlite3"
                assert set((root / "archives").rglob("*.sqlite3")) == {private_candidate,private_quarantined}
                assert hashlib.sha256(private_quarantined.read_bytes()).hexdigest()==quarantined_import.candidate_sha256
                assert hashlib.sha256(private_candidate.read_bytes()).hexdigest() == prepared_import.candidate_sha256
                assert prepared_import.manifest["permissions_revalidated"] is False
                assert prepared_import.manifest["live_database_replaced"] is False
                connections.close_all()
                enter_maintenance(gate, operation_id=uuid.uuid4(), requested_by=technical.id,
                                  reason="Contrôle HTTP de maintenance", expected_version=1)
                assert request(technical_token, base + receipt["id"] + "/")[0] == 503
                abort_maintenance(gate, operation_id=uuid.uuid4(), requested_by=technical.id,
                                  reason="Abandon sans remplacement", expected_version=2)
                assert request(technical_token, base + receipt["id"] + "/")[0] == 200
                print(json.dumps({"http_server_backup": "passed", "creation": created, "retry": retried,
                    "external_maintenance_gate_http": True, "maintenance_http_503": True,
                    "unchanged_database_abort_http_200": True,
                    "technical_recovery_code_prepared": True, "technical_bootstrap_console": True,
                    "technical_bootstrap_replay_without_duplicate": True, "technical_bootstrap_gate_closed": True,
                    "external_proof_consumed": True,
                    "responsible_review_console": True, "responsible_review_replay_without_duplicate": True,
                    "responsible_review_gate_still_closed": True,
                    "part_count": receipt["part_count"], "archive_bytes": receipt["archive_bytes"],
                    "database_and_photo_consistent": True, "owner_denied": denied, "owner_part_denied": denied_part,
                    "console_verification": True, "known_schema_identifiers": checked["known_migration_identifiers"],
                    "migration_execution_checked": False, "live_database_replaced": False, "external_emails_sent": 0, "local_smtp_submissions": len(smtp_server.received), "whole_archive_dispatch": True, "unfold_login": True, "unfold_dispatch_workflow": True, "daily_configuration_unfold": True, "daily_configuration_disabled": True, "configuration_versions": final_config["version"], "dispatch_steps": receipt["part_count"] - 1, "abandonment_replay": True, "abandoned_send_blocked": True, "partial_archive_refused": True, "cleanup_state": "cleaned", "archive_files_removed": True, "encoded_email_bytes": mime_sizes,
                    "automatic_daily_trigger": True, "automatic_parts": automatic_parts, "backup_health_unfold": True,
                    "restore_copy_migrated": True, "restore_schema_audited": True, "live_database_unchanged_by_preparation": True,
                    "restore_parts_collected_unfold": imported.parts.count(), "restore_import_replay_without_duplicate": True,
                    "restore_copy_prepared_unfold": True, "restore_preparation_replayed_without_duplicate": True,
                    "prepared_copy_integrity_rechecked_unfold": True,
                    "restore_copy_quarantined_console": True,
                    "restore_copy_quarantined_unfold": True, "restore_quarantine_replayed_without_duplicate": True,
                    "total_local_smtp_submissions": len(smtp_server.received),
                    "sampled_temporary_peak_bytes": peak[0], "sampling_interval_ms": 5}))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                stopped.set()
                sampler.join(timeout=5)
                connections.close_all()


if __name__ == "__main__":
    main()
