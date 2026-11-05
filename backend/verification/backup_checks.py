"""Real file-backed SQLite: archive work must not block ordinary sales."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from backups.models import BackupJob
from backups.services.archive import build_archive, verify_bundle
from catalogue.models import Product
from identity.models import DeviceSession, Installation, Shop, User
from sales.models import Sale
from stock.models import StockMovement


class BackupOperationalChecks(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="backup-lock-check-")
        self.addCleanup(self.directory.cleanup)
        self.key = Fernet.generate_key()
        self.options = override_settings(BACKUP_ROOT=Path(self.directory.name) / "archives",
            BACKUP_ENCRYPTION_KEY=self.key, BACKUP_KEY_ID="test-primary", BACKUP_PART_MAX_BYTES=32768)
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.technical = User.objects.create_superuser(username="backup-lock-technical", password=None)
        self.owner = User.objects.create_user(username="backup-lock-owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Boutique")
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=500)

    def bearer(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        return str(token)

    def request(self, path, body, bearer):
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            response = client.post(path, body, format="json")
            return response.status_code, response.json()
        finally:
            connections.close_all()

    def backup_body(self):
        return {"operation_id": str(uuid.uuid4()), "recipient": "backup@example.test", "reason": "Contrôle SQLite"}

    def test_health_read_during_smtp_does_not_hold_up_sales(self):
        from django.test import Client
        from django.urls import reverse
        from backups.models import SendAttempt
        technical,owner=self.bearer(self.technical),self.bearer(self.owner)
        status,receipt=self.request("/api/v1/backups/",self.backup_body(),technical)
        self.assertEqual(status,201,receipt)
        status,command=self.request(f"/api/v1/backups/{receipt['id']}/dispatches/",
            {"operation_id":str(uuid.uuid4()),"reason":"Lecture de santé pendant SMTP"},technical)
        self.assertEqual(status,201,command)
        client=Client()
        client.force_login(self.technical)
        entered,release=Event(),Event()
        submissions=[]
        def paused(message):
            submissions.append(message)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
        def read_health():
            try:
                response=client.get(reverse("admin:backups_backupconfiguration_configure"))
                health=response.context["backup_health"]
                return response.status_code,health["blocked_count"],health["last_accepted_at"]
            finally:
                connections.close_all()
        sale={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":1,"quantity_milli":1000}],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":500}]}
        endpoint=f"/api/v1/backups/{receipt['id']}/dispatches/{command['id']}/advance/"
        with override_settings(EMAIL_HOST_USER="sender@example.test"),patch("backups.services.sending.submit_smtp",side_effect=paused),ThreadPoolExecutor(max_workers=2) as executor:
            sending=executor.submit(self.request,endpoint,{},technical)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(executor.submit(read_health).result(timeout=5),(200,1,None))
                written=executor.submit(self.request,f"/api/v1/shops/{self.shop.id}/sales/",sale,owner).result(timeout=5)
                self.assertEqual(written[0],201,written)
            finally:
                release.set()
            self.assertEqual(sending.result(timeout=15)[0],200)
        self.assertEqual(BackupJob.objects.count(),1)
        self.assertEqual(SendAttempt.objects.count(),1)
        self.assertEqual(Sale.objects.count(),1)
        self.assertEqual(len(submissions),1)

    def test_scheduler_overlaps_are_busy_while_sale_commits_and_only_one_snapshot_is_created(self):
        from datetime import datetime,timezone as utc_timezone
        from backups.models import BackupScheduleRun,BackupSchedulerState
        trigger="file-backed-scheduler-test-only-secret-0001"
        owner=self.bearer(self.owner)
        entered,release=Event(),Event()
        def paused(*args,**kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
            return build_archive(*args,**kwargs)
        sale={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":1,"quantity_milli":1000}],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":500}]}
        with override_settings(BACKUP_SCHEDULER_TOKEN=trigger,BACKUP_SCHEDULER_ACTOR_ID=str(self.technical.id),
                EMAIL_HOST_USER="sender@example.test"),patch("django.utils.timezone.now",return_value=datetime(2026,10,19,9,tzinfo=utc_timezone.utc)),\
                patch("backups.services.jobs.build_archive",side_effect=paused),ThreadPoolExecutor(max_workers=2) as executor:
            pending=executor.submit(self.request,"/api/v1/backups/trigger/",{},trigger)
            try:
                self.assertTrue(entered.wait(10))
                blocked=executor.submit(self.request,"/api/v1/backups/trigger/",{},trigger).result(timeout=5)
                self.assertEqual(blocked,(200,{"status":"busy"}))
                written=executor.submit(self.request,f"/api/v1/shops/{self.shop.id}/sales/",sale,owner).result(timeout=5)
                self.assertEqual(written[0],201,written)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=30),(200,{"status":"prepared"}))
        self.assertEqual(BackupJob.objects.count(),1)
        self.assertEqual(BackupScheduleRun.objects.count(),1)
        self.assertEqual(Sale.objects.count(),1)
        self.assertIsNone(BackupSchedulerState.objects.get().lease_id)

    def test_sale_commits_during_encryption_and_snapshot_keeps_its_earlier_consistent_state(self):
        technical = self.bearer(self.technical)
        owner = self.bearer(self.owner)
        entered, release = Event(), Event()

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
            return build_archive(*args, **kwargs)

        sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}]}
        with patch("backups.services.jobs.build_archive", side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            backup = executor.submit(self.request, "/api/v1/backups/", self.backup_body(), technical)
            try:
                self.assertTrue(entered.wait(10), "Snapshot never reached the archive step")
                written = executor.submit(self.request, f"/api/v1/shops/{self.shop.id}/sales/", sale, owner)
                status, body = written.result(timeout=5)
                self.assertEqual(status, 201, body)
            finally:
                release.set()
            status, receipt = backup.result(timeout=30)
        self.assertEqual(status, 201, receipt)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -1000)
        root = Path(self.directory.name) / "archives" / receipt["id"]
        verified = verify_bundle([root / part["filename"] for part in receipt["parts"]], self.key,
            Path(self.directory.name) / "check")
        with closing(sqlite3.connect(verified.path)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM core_sale").fetchone()[0], 0)
            self.assertEqual(snapshot.execute("SELECT count(*) FROM core_stockmovement").fetchone()[0], 0)
            self.assertEqual(snapshot.execute("SELECT count(*) FROM core_cashmovement").fetchone()[0], 0)

    def test_another_preparation_is_refused_while_archive_work_is_in_progress(self):
        technical = self.bearer(self.technical)
        entered, release = Event(), Event()

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
            return build_archive(*args, **kwargs)

        with patch("backups.services.jobs.build_archive", side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.request, "/api/v1/backups/", self.backup_body(), technical)
            try:
                self.assertTrue(entered.wait(10))
                second = executor.submit(self.request, "/api/v1/backups/", self.backup_body(), technical)
                self.assertEqual(second.result(timeout=5)[0], 409)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=30)[0], 201)
        self.assertEqual(BackupJob.objects.count(), 1)
        self.assertEqual(BackupJob.objects.get().state, "ready")


    def test_smtp_does_not_hold_sqlite_lock_and_same_part_has_one_submission(self):
        from backups.models import SendAttempt
        technical = self.bearer(self.technical)
        owner = self.bearer(self.owner)
        status, receipt = self.request("/api/v1/backups/", self.backup_body(), technical)
        self.assertEqual(status, 201, receipt)
        entered, release = Event(), Event()
        submissions = []
        def paused(message):
            submissions.append(message)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
        endpoint = f"/api/v1/backups/{receipt['id']}/parts/1/send/"
        operation = {"operation_id": str(uuid.uuid4())}
        sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}]}
        with override_settings(EMAIL_HOST_USER="sender@example.test"), patch("backups.services.sending.submit_smtp", side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.request, endpoint, operation, technical)
            try:
                self.assertTrue(entered.wait(10))
                same = executor.submit(self.request, endpoint, operation, technical).result(timeout=5)
                self.assertEqual(same[0], 200, same)
                self.assertEqual(same[1]["state"], "sending")
                other = executor.submit(self.request, endpoint, {"operation_id": str(uuid.uuid4())}, technical).result(timeout=5)
                self.assertEqual(other[0], 409, other)
                written = executor.submit(self.request, f"/api/v1/shops/{self.shop.id}/sales/", sale, owner).result(timeout=5)
                self.assertEqual(written[0], 201, written)
            finally:
                release.set()
            status, attempt = first.result(timeout=15)
        self.assertEqual(status, 201, attempt)
        self.assertEqual(attempt["state"], "accepted")
        self.assertEqual(SendAttempt.objects.count(), 1)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(Sale.objects.count(), 1)


    def test_cleanup_replay_does_not_block_sales_and_records_one_completion(self):
        from backups.models import BackupEvent, SendAttempt
        technical=self.bearer(self.technical)
        owner=self.bearer(self.owner)
        status,receipt=self.request("/api/v1/backups/",self.backup_body(),technical)
        self.assertEqual(status,201,receipt)
        job=BackupJob.objects.get(id=receipt["id"])
        parts=list(job.parts.order_by("number"))
        entered,release=Event(),Event()
        original_unlink=Path.unlink
        def paused_unlink(path,*args,**kwargs):
            if path.name==parts[0].filename and not entered.is_set():
                entered.set()
                if not release.wait(15):
                    raise TimeoutError()
            return original_unlink(path,*args,**kwargs)
        with override_settings(EMAIL_HOST_USER="sender@example.test"),patch("backups.services.sending.submit_smtp",return_value=None):
            for part in parts[:-1]:
                status,body=self.request(f"/api/v1/backups/{job.id}/parts/{part.number}/send/",{"operation_id":str(uuid.uuid4())},technical)
                self.assertEqual(status,201,body)
            endpoint=f"/api/v1/backups/{job.id}/parts/{parts[-1].number}/send/"
            operation={"operation_id":str(uuid.uuid4())}
            sale={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
                "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":1,"quantity_milli":1000}],
                "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":500}]}
            with patch.object(Path,"unlink",autospec=True,side_effect=paused_unlink),ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(self.request,endpoint,operation,technical)
                try:
                    self.assertTrue(entered.wait(10))
                    written=executor.submit(self.request,f"/api/v1/shops/{self.shop.id}/sales/",sale,owner).result(timeout=5)
                    self.assertEqual(written[0],201,written)
                    replay=executor.submit(self.request,endpoint,operation,technical).result(timeout=5)
                    self.assertEqual(replay[0],200,replay)
                    self.assertEqual(replay[1]["state"],"accepted")
                finally:
                    release.set()
                status,body=first.result(timeout=15)
        self.assertEqual(status,201,body)
        job.refresh_from_db()
        self.assertEqual(job.cleanup_state,"cleaned")
        self.assertEqual(BackupEvent.objects.filter(job=job,kind="cleanup_completed").count(),1)
        self.assertEqual(SendAttempt.objects.filter(part__job=job).count(),len(parts))
        directory=Path(self.directory.name)/"archives"/str(job.id)
        self.assertFalse(list(directory.glob("*.axbkp")))
        self.assertTrue((directory/"manifest.json").is_file())
        self.assertEqual(Sale.objects.count(),1)


    def test_abandonment_during_smtp_preserves_late_result_and_blocks_next_submission(self):
        from backups.models import BackupEvent,SendAttempt
        technical=self.bearer(self.technical)
        status,receipt=self.request("/api/v1/backups/",self.backup_body(),technical)
        self.assertEqual(status,201,receipt)
        entered,release=Event(),Event()
        submissions=[]
        def paused(message):
            submissions.append(message)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
        endpoint=f"/api/v1/backups/{receipt['id']}/parts/1/send/"
        with override_settings(EMAIL_HOST_USER="sender@example.test"),patch("backups.services.sending.submit_smtp",side_effect=paused),ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(self.request,endpoint,{"operation_id":str(uuid.uuid4())},technical)
            try:
                self.assertTrue(entered.wait(10))
                abandoned=executor.submit(self.request,f"/api/v1/backups/{receipt['id']}/abandon/",
                    {"operation_id":str(uuid.uuid4()),"reason":"Arrêt pendant SMTP"},technical).result(timeout=5)
                self.assertEqual(abandoned[0],200,abandoned)
                blocked=executor.submit(self.request,endpoint,{"operation_id":str(uuid.uuid4())},technical).result(timeout=5)
                self.assertEqual(blocked[0],409,blocked)
            finally:
                release.set()
            status,result=first.result(timeout=15)
        self.assertEqual(status,201,result)
        self.assertEqual(result["state"],"accepted")
        job=BackupJob.objects.get(id=receipt["id"])
        self.assertIsNotNone(job.abandoned_at)
        self.assertEqual(job.cleanup_state,"waiting")
        self.assertEqual(SendAttempt.objects.filter(part__job=job).count(),1)
        self.assertEqual(BackupEvent.objects.filter(job=job,kind="abandoned").count(),1)
        self.assertEqual(len(submissions),1)
        directory=Path(self.directory.name)/"archives"/str(job.id)
        self.assertEqual(len(list(directory.glob("*.axbkp"))),job.part_count)


    def test_dispatch_lease_prevents_overlap_while_sale_can_commit(self):
        from backups.models import BackupDispatch,SendAttempt
        technical=self.bearer(self.technical)
        owner=self.bearer(self.owner)
        status,receipt=self.request("/api/v1/backups/",self.backup_body(),technical)
        self.assertEqual(status,201,receipt)
        status,dispatch=self.request(f"/api/v1/backups/{receipt['id']}/dispatches/",
            {"operation_id":str(uuid.uuid4()),"reason":"Envoi de l’archive"},technical)
        self.assertEqual(status,201,dispatch)
        entered,release=Event(),Event()
        submissions=[]
        def paused(message):
            submissions.append(message)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
        endpoint=f"/api/v1/backups/{receipt['id']}/dispatches/{dispatch['id']}/advance/"
        sale={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),"product_version":1,"quantity_milli":1000}],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":500}]}
        with override_settings(EMAIL_HOST_USER="sender@example.test"),patch("backups.services.sending.submit_smtp",side_effect=paused),ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(self.request,endpoint,{},technical)
            try:
                self.assertTrue(entered.wait(10))
                blocked=executor.submit(self.request,endpoint,{},technical).result(timeout=5)
                self.assertEqual(blocked[0],409,blocked)
                written=executor.submit(self.request,f"/api/v1/shops/{self.shop.id}/sales/",sale,owner).result(timeout=5)
                self.assertEqual(written[0],201,written)
            finally:
                release.set()
            status,result=first.result(timeout=15)
        self.assertEqual(status,200,result)
        self.assertEqual(result["accepted_parts"],1)
        command=BackupDispatch.objects.get(id=dispatch["id"])
        self.assertIsNone(command.lease_id)
        self.assertEqual(SendAttempt.objects.filter(dispatch=command).count(),1)
        self.assertEqual(len(submissions),1)
        self.assertEqual(Sale.objects.count(),1)


    def test_unfold_concurrent_start_posts_replay_one_durable_command(self):
        from django.test import Client
        from django.urls import reverse
        from threading import Barrier
        from backups.models import BackupDispatch,BackupEvent,SendAttempt
        technical=self.bearer(self.technical)
        status,receipt=self.request("/api/v1/backups/",self.backup_body(),technical)
        self.assertEqual(status,201,receipt)
        clients=[Client(),Client()]
        for client in clients:
            client.force_login(self.technical)
        url=reverse("admin:backups_backupjob_dispatch_start",args=[receipt["id"]])
        body={"operation_id":str(uuid.uuid4()),"reason":"Double clic et reprise de formulaire"}
        ready=Barrier(2)
        def posted(client):
            try:
                ready.wait(timeout=10)
                response=client.post(url,body)
                return response.status_code,getattr(response,"url",None)
            finally:
                connections.close_all()
        with patch("backups.services.sending.submit_smtp",side_effect=AssertionError("Creation must not submit SMTP")),ThreadPoolExecutor(max_workers=2) as executor:
            pending=[executor.submit(posted,client) for client in clients]
            results=[future.result(timeout=15) for future in pending]
        self.assertEqual([status for status,_ in results],[302,302],results)
        self.assertEqual(results[0][1],results[1][1])
        command=BackupDispatch.objects.get()
        self.assertEqual(command.state,"active")
        self.assertEqual(BackupEvent.objects.filter(kind="dispatch_requested").count(),1)
        self.assertEqual(SendAttempt.objects.count(),0)


    def test_configuration_concurrent_versions_preserve_one_change_without_backup_execution(self):
        from threading import Barrier
        from backups.models import BackupConfiguration,BackupConfigurationChange,SendAttempt
        technical=self.bearer(self.technical)
        ready=Barrier(2)
        def changed(hour):
            try:
                client=APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {technical}")
                ready.wait(timeout=10)
                response=client.patch("/api/v1/backups/configuration/",{
                    "operation_id":str(uuid.uuid4()),"expected_version":0,"enabled":True,
                    "local_hour":hour,"local_minute":15,"reason":"Modification concurrente"},format="json")
                return response.status_code,response.json()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending=[executor.submit(changed,hour) for hour in (7,9)]
            results=[future.result(timeout=15) for future in pending]
        self.assertEqual(sorted(status for status,_ in results),[200,409],results)
        saved=BackupConfiguration.objects.get()
        winner=next(body for status,body in results if status==200)
        self.assertEqual((saved.version,saved.local_hour,saved.local_minute),(1,winner["local_hour"],15))
        self.assertEqual(BackupConfigurationChange.objects.count(),1)
        self.assertEqual(BackupConfigurationChange.objects.get().after,winner)
        self.assertEqual(BackupJob.objects.count(),0)
        self.assertEqual(SendAttempt.objects.count(),0)

    def _restore_upload_fixture(self):
        import os
        source = Path(self.directory.name) / 'import-source.sqlite3'
        with closing(sqlite3.connect(source)) as database:
            database.execute('CREATE TABLE django_migrations (app TEXT, name TEXT)')
            database.execute("INSERT INTO django_migrations VALUES ('core','0001_initial')")
            database.execute('CREATE TABLE contents (photo BLOB)')
            database.execute('INSERT INTO contents VALUES (?)', [os.urandom(60000)])
            database.commit()
        bundle = build_archive(source, Path(self.directory.name) / 'input-parts', self.key,
            backup_id=uuid.uuid4(), key_id='file-check', max_part_bytes=32768)
        return Path(self.directory.name) / 'input-parts' / bundle.parts[0]['filename']

    def test_restore_import_concurrent_same_part_has_one_receipt_and_one_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import Client
        from django.urls import reverse
        from threading import Barrier
        from backups.models import RestoreImport, RestoreImportPart
        from backups.services.restore_imports import imported_part_path
        incoming = self._restore_upload_fixture().read_bytes()
        batch_id = uuid.uuid4()
        ready = Barrier(2)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.technical)
        def posted(client):
            try:
                ready.wait(timeout=10)
                response = client.post(reverse('admin:backups_restoreimport_start'), {
                    'batch_id': str(batch_id), 'reason': 'Même partie simultanée',
                    'encryption_key': self.key.decode(), 'part': SimpleUploadedFile('part', incoming)})
                return response.status_code, getattr(response, 'url', None)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(posted, client) for client in clients]
            results = [future.result(timeout=20) for future in pending]
        self.assertEqual([status for status, _ in results], [302,302], results)
        self.assertEqual(results[0][1], results[1][1])
        batch, part = RestoreImport.objects.get(), RestoreImportPart.objects.get()
        self.assertEqual(imported_part_path(batch, part).read_bytes(), incoming)
        self.assertEqual(len(list((Path(self.directory.name) / 'archives' / 'restore-imports').rglob('*.axbkp'))), 1)

    def test_restore_import_authentication_does_not_hold_up_a_sale(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import Client
        from django.urls import reverse
        from backups.models import RestoreImportPart
        from backups.services.archive import inspect_part
        incoming = self._restore_upload_fixture().read_bytes()
        owner = self.bearer(self.owner)
        client = Client()
        client.force_login(self.technical)
        entered, release = Event(), Event()
        def paused(*args, **kwargs):
            self.assertFalse(connections['default'].in_atomic_block)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
            return inspect_part(*args, **kwargs)
        def upload():
            try:
                return client.post(reverse('admin:backups_restoreimport_start'), {
                    'batch_id': str(uuid.uuid4()), 'reason': 'Vente pendant contrôle',
                    'encryption_key': self.key.decode(), 'part': SimpleUploadedFile('part', incoming)}).status_code
            finally:
                connections.close_all()
        sale = {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'lines':[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),'product_version':1,'quantity_milli':1000}],
            'payments':[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':500}]}
        with patch('backups.services.restore_imports.inspect_part', side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(upload)
            try:
                self.assertTrue(entered.wait(10))
                written = executor.submit(self.request, f'/api/v1/shops/{self.shop.id}/sales/', sale, owner).result(timeout=5)
                self.assertEqual(written[0], 201, written)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=15), 302)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(RestoreImportPart.objects.count(), 1)

    def test_import_preparation_replay_and_sale_during_worker_use_only_one_copy(self):
        from django.test import Client
        from django.urls import reverse
        from backups.models import ImportPreparation
        from backups.services.jobs import job_directory
        from backups.services.restore_imports import collect_part
        from backups.services.restore_preparation import prepare_restore
        from backups.services.import_preparation import preparation_directory
        from django.core.files.uploadedfile import SimpleUploadedFile
        technical, owner = self.bearer(self.technical), self.bearer(self.owner)
        status, receipt = self.request('/api/v1/backups/', self.backup_body(), technical)
        self.assertEqual(status, 201, receipt)
        batch_id, operation_id = uuid.uuid4(), uuid.uuid4()
        for part in receipt['parts']:
            path = job_directory(receipt['id']) / part['filename']
            collect_part(self.technical, batch_id, 'Import de contrôle', SimpleUploadedFile('part', path.read_bytes()), self.key)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.technical)
        entered, release = Event(), Event()
        calls = []
        def paused(*args, **kwargs):
            self.assertFalse(connections['default'].in_atomic_block)
            calls.append(True)
            entered.set()
            if not release.wait(15):
                raise TimeoutError()
            return prepare_restore(*args, **kwargs)
        body = {'operation_id':str(operation_id), 'reason':'Copie unique', 'encryption_key':self.key.decode()}
        url = reverse('admin:backups_restoreimport_prepare', args=[batch_id])
        def posted(client, data):
            try:
                response = client.post(url, data)
                return response.status_code, getattr(response, 'url', None)
            finally:
                connections.close_all()
        sale = {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'lines':[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),'product_version':1,'quantity_milli':1000}],
            'payments':[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':500}]}
        with patch('backups.services.import_preparation.prepare_restore', side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(posted, clients[0], body)
            try:
                self.assertTrue(entered.wait(10))
                replay = executor.submit(posted, clients[1], body).result(timeout=5)
                self.assertEqual(replay[0], 302, replay)
                other = executor.submit(posted, clients[1], {**body,'operation_id':str(uuid.uuid4())}).result(timeout=5)
                self.assertEqual(other[0], 200, other)
                written = executor.submit(self.request, f'/api/v1/shops/{self.shop.id}/sales/', sale, owner).result(timeout=5)
                self.assertEqual(written[0], 201, written)
            finally:
                release.set()
            completed = pending.result(timeout=60)
        self.assertEqual(completed, replay)
        self.assertEqual(len(calls), 1)
        record = ImportPreparation.objects.get()
        self.assertEqual(record.state, 'prepared', record.failure_code)
        self.assertEqual(Sale.objects.count(), 1)
        with closing(sqlite3.connect(preparation_directory(record) / 'candidate.sqlite3')) as candidate:
            self.assertEqual(candidate.execute('SELECT COUNT(*) FROM core_sale').fetchone()[0], 0)

    def test_quarantine_replay_blocks_other_copy_work_but_allows_an_ordinary_sale(self):
        from django.test import Client
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from backups.models import ImportQuarantine
        from backups.services.jobs import job_directory
        from backups.services.restore_imports import collect_part
        from backups.services.import_preparation import prepare_import
        from backups.services.import_quarantine import quarantine_directory
        from backups.services.restore_quarantine import quarantine_restore
        technical, owner = self.bearer(self.technical), self.bearer(self.owner)
        status, receipt = self.request('/api/v1/backups/', self.backup_body(), technical)
        self.assertEqual(status, 201, receipt)
        batch_id = uuid.uuid4()
        for part in receipt['parts']:
            path = job_directory(receipt['id'])/part['filename']
            collect_part(self.technical, batch_id, 'Archive de contrôle', SimpleUploadedFile('part',path.read_bytes()), self.key)
        source, _created = prepare_import(self.technical,batch_id,uuid.uuid4(),'Copie de contrôle',self.key)
        self.assertEqual(source.state,'prepared',source.failure_code)
        clients = [Client(),Client()]
        for client in clients:
            client.force_login(self.technical)
        entered, release, calls = Event(), Event(), []
        def paused(*args,**kwargs):
            self.assertFalse(connections['default'].in_atomic_block)
            calls.append(True)
            entered.set()
            if not release.wait(20):
                raise TimeoutError()
            return quarantine_restore(*args,**kwargs)
        def posted(client,url,data):
            try:
                response=client.post(url,data)
                return response.status_code,getattr(response,'url',None)
            finally:
                connections.close_all()
        url=reverse('admin:backups_importquarantine_start',args=[source.id])
        body={'operation_id':str(uuid.uuid4()),'reason':'Suspension unique'}
        sale={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'lines':[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),'product_version':1,'quantity_milli':1000}],
            'payments':[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':500}]}
        with patch('backups.services.import_quarantine.quarantine_restore',side_effect=paused), ThreadPoolExecutor(max_workers=2) as executor:
            pending=executor.submit(posted,clients[0],url,body)
            try:
                self.assertTrue(entered.wait(10))
                replay=executor.submit(posted,clients[1],url,body).result(timeout=5)
                self.assertEqual(replay[0],302,replay)
                busy=executor.submit(posted,clients[1],url,{**body,'operation_id':str(uuid.uuid4())}).result(timeout=5)
                self.assertEqual(busy[0],200,busy)
                preparation_url=reverse('admin:backups_restoreimport_prepare',args=[batch_id])
                other=executor.submit(posted,clients[1],preparation_url,{'operation_id':str(uuid.uuid4()),'reason':'Autre copie','encryption_key':self.key.decode()}).result(timeout=5)
                self.assertEqual(other[0],200,other)
                written=executor.submit(self.request,f'/api/v1/shops/{self.shop.id}/sales/',sale,owner).result(timeout=5)
                self.assertEqual(written[0],201,written)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=60),replay)
        self.assertEqual(len(calls),1)
        self.assertEqual(ImportQuarantine.objects.count(),1)
        record=ImportQuarantine.objects.get()
        self.assertEqual(record.state,'quarantined',record.failure_code)
        self.assertEqual(Sale.objects.count(),1)
        with closing(sqlite3.connect(quarantine_directory(record)/'candidate.sqlite3')) as candidate:
            self.assertEqual(candidate.execute('SELECT COUNT(*) FROM core_sale').fetchone()[0],0)
            self.assertEqual(candidate.execute('SELECT COUNT(*) FROM core_user WHERE is_active!=0').fetchone()[0],0)
