"""Only a bounded recovery interface may cross an explicitly configured closed gate."""
import uuid
from django.http import HttpResponse
from django.test import RequestFactory,TransactionTestCase,override_settings
from backups.tests import test_technical_recovery as fixtures
from backups.services.restore_gate import hold_control,RestoreGateError,read_gate


class RecoveryWebMiddlewareTests(TransactionTestCase):
    setUp=fixtures.TechnicalRecoveryTests.setUp
    provision=fixtures.TechnicalRecoveryTests.provision
    suspend=fixtures.TechnicalRecoveryTests.suspend
    bootstrap=fixtures.TechnicalRecoveryTests.bootstrap

    def prepare(self):
        issued=self.provision()
        self.suspend()
        self.operation=uuid.uuid4()
        self.receipt=self.bootstrap(issued.secret,operation_id=self.operation)
        self.options={"vault":self.vault,"bootstrap_operation":str(self.operation),"gate_version":2}

    def settings_for_source(self,options):
        return override_settings(RESTORE_GATE_PATH=self.gate,RESTORE_RECOVERY=options,BACKUP_ROOT=self.root/"stored",
            DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":str(self.source)}})

    def test_explicit_external_proof_allows_only_recovery_and_keeps_lease_until_response_close(self):
        from backups.middleware import RestoreGateMiddleware
        self.prepare()
        captured=[]
        def handler(request):
            captured.append((request.restore_recovery_user_id,request.restore_recovery_generation_id))
            return HttpResponse("recovery")
        request=RequestFactory().get("/admin/login/")
        with self.settings_for_source(None):
            self.assertEqual(RestoreGateMiddleware(handler)(request).status_code,503)
        with self.settings_for_source(self.options):
            response=RestoreGateMiddleware(handler)(request)
            self.addCleanup(response.close)
            self.assertEqual(response.status_code,200)
            self.assertEqual(captured,[(self.receipt["user_id"],self.receipt["restore_generation_id"])])
            with self.assertRaises(RestoreGateError):
                with hold_control(self.gate,self.source,required_mode="maintenance",expected_version=2):
                    self.fail("Recovery response still owns its lease")
            response.close()
        with hold_control(self.gate,self.source,required_mode="maintenance",expected_version=2) as state:
            self.assertEqual(state["mode"],"maintenance")

    def test_business_other_admin_paths_and_non_recovery_methods_remain_503(self):
        from backups.middleware import RestoreGateMiddleware
        self.prepare()
        for method,path in (("GET","/api/v1/shops/"),("GET","/api/v1/schema/"),("GET","/admin/core/user/"),
                            ("POST",f"/api/v1/shops/{uuid.uuid4()}/recovery/journals/"),
                            ("GET",f"/api/v1/shops/{uuid.uuid4()}/recovery/operations/"),
                            ("GET","/admin/backups/backupjob/"),("POST","/admin/backups/restoreaccessreview/"),
                            ("DELETE","/admin/login/"),("GET","/admin/backups/restoreaccessreview/../")):
            with self.subTest(method=method,path=path),self.settings_for_source(self.options):
                request=RequestFactory().generic(method,path)
                response=RestoreGateMiddleware(lambda request:self.fail("Forbidden path reached Django"))(request)
                self.assertEqual(response.status_code,503)

    def test_current_proof_allows_only_get_on_recovery_inventory_lists_and_keeps_the_lease(self):
        from backups.middleware import RestoreGateMiddleware
        self.prepare()
        for model in ("restoresession","recoverydevice"):
            path=f"/admin/backups/{model}/"
            with self.subTest(model=model),self.settings_for_source(self.options):
                response=RestoreGateMiddleware(lambda request:HttpResponse(request.restore_recovery_user_id))(
                    RequestFactory().get(path))
                self.addCleanup(response.close)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,self.receipt["user_id"])
                with self.assertRaises(RestoreGateError):
                    with hold_control(self.gate,self.source,required_mode="maintenance",expected_version=2):
                        self.fail("Inventory response released its lease too early")
                response.close()
                for method in ("POST","PATCH","DELETE"):
                    denied=RestoreGateMiddleware(lambda request:self.fail("Inventory write reached Django"))(
                        RequestFactory().generic(method,path))
                    self.assertEqual(denied.status_code,503)
                child=RestoreGateMiddleware(lambda request:self.fail("Generic edit reached Django"))(
                    RequestFactory().get(path+str(uuid.uuid4())+"/change/"))
                self.assertEqual(child.status_code,503)

    def test_wrong_proof_or_version_is_closed_and_handler_failure_releases_recovery_lease(self):
        from backups.middleware import RestoreGateMiddleware
        self.prepare()
        for options in ({**self.options,"gate_version":1},{**self.options,"bootstrap_operation":str(uuid.uuid4())},{}):
            with self.subTest(options=options),self.settings_for_source(options):
                response=RestoreGateMiddleware(lambda request:self.fail("Invalid permit reached Django"))(RequestFactory().get("/admin/login/"))
                self.assertEqual(response.status_code,503)
        with self.settings_for_source(self.options):
            def fail(request):
                raise RuntimeError("recovery handler failed")
            with self.assertRaisesMessage(RuntimeError,"recovery handler failed"):
                RestoreGateMiddleware(fail)(RequestFactory().get("/admin/login/"))
        with hold_control(self.gate,self.source,required_mode="maintenance",expected_version=2):
            pass
