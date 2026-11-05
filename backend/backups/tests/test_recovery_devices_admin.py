"""Technical inspection stays readonly, translated and bound to recovery proof."""
from html.parser import HTMLParser
import uuid
from django.contrib import admin
from django.test import Client,RequestFactory,TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from backups import models as backups
from backups.tests import test_recovery_session as fixtures


class ResultRows(HTMLParser):
    def __init__(self,page):
        super().__init__();self.table=False;self.cell=None;self.row=[];self.rows=[]
        self.feed(page.content.decode())
    def handle_starttag(self,tag,attrs):
        if tag=="table" and dict(attrs).get("id")=="result_list":self.table=True
        if self.table and tag=="tr":self.row=[]
        if self.table and tag in ("td","th"):self.cell=[]
    def handle_data(self,data):
        if self.table and self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if self.table and tag in ("td","th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()));self.cell=None
        if self.table and tag=="tr":self.rows.append(self.row)
        if tag=="table":self.table=False


class RecoveryDeviceAdminTests(TransactionTestCase):
    generation=fixtures.RecoverySessionTests.generation
    record=fixtures.RecoverySessionTests.record
    device=fixtures.RecoverySessionTests.device

    def setUp(self):
        fixtures.RecoverySessionTests.setUp(self)
        self.old_generation=self.generation()
        self.old_registry=self.record(self.old_generation)
        self.old_device=self.device(self.old_registry,self.a,label="Historical observation")
        self.old_generation.current_slot=None;self.old_generation.save()
        self.current=self.generation()
        self.registry=self.record(self.current,inventory_recorded_at=timezone.now())
        first=self.device(self.registry,self.a,label="Current observation")
        self.second=self.device(self.registry,self.b,origin_installation_id=first.origin_installation_id)
        self.client=Client()
        self.client.force_login(self.technical)

    def url(self,model):
        return reverse(f"admin:backups_{model}_changelist")

    def test_readonly_lists_translate_and_distinguish_phone_count_from_observations(self):
        for language,title,unknown in (("en","Restoration recovery sessions","Inventory not recorded"),
                ("fr","Sessions de récupération après restauration","Inventaire non enregistré")):
            with self.subTest(language=language):
                page=self.client.get(self.url("restoresession"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(page.status_code,200)
                self.assertContains(page,title)
                rows=ResultRows(page).rows
                current=next(row for row in rows if str(self.registry.id) in row)
                historical=next(row for row in rows if str(self.old_registry.id) in row)
                self.assertEqual(current[-2:],["1","2"])
                self.assertEqual(historical[-2:],[unknown,unknown])
        devices=self.client.get(self.url("recoverydevice"))
        self.assertEqual(devices.status_code,200)
        self.assertContains(devices,"Current observation")
        self.assertNotContains(devices,'name="action"')
        self.assertNotContains(devices,'class="deletelink"')
        for model in ("recoverydevice","restoresession"):
            self.assertEqual(self.client.get(reverse(f"admin:backups_{model}_add")).status_code,403)
        self.assertEqual(self.client.post(reverse("admin:backups_recoverydevice_delete",args=[self.second.id]),{"post":"yes"}).status_code,403)
        self.assertEqual(backups.RecoveryDevice.objects.count(),3)

    def test_business_staff_and_a_withdrawn_technical_account_cannot_view_observations(self):
        self.owner.is_staff=True;self.owner.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url("recoverydevice")).status_code,403)
        self.client.force_login(self.technical)
        self.technical.is_superuser=False;self.technical.save()
        self.assertEqual(self.client.get(self.url("restoresession")).status_code,403)

    def test_recovery_context_binds_identity_and_current_generation_before_querying_rows(self):
        request=RequestFactory().get("/admin/")
        request.user=self.technical
        request.restore_recovery_user_id=str(self.technical.id)
        request.restore_recovery_generation_id=str(self.current.id)
        sessions=admin.site._registry[backups.RestoreSession]
        devices=admin.site._registry[backups.RecoveryDevice]
        self.assertEqual(list(sessions.get_queryset(request).values_list("id",flat=True)),[self.registry.id])
        self.assertEqual(devices.get_queryset(request).count(),2)
        self.assertFalse(devices.get_queryset(request).filter(id=self.old_device.id).exists())
        request.restore_recovery_generation_id=None
        self.assertEqual(devices.get_queryset(request).count(),0)
        request.restore_recovery_generation_id="invalid-generation"
        self.assertEqual(devices.get_queryset(request).count(),0)
        request.restore_recovery_generation_id=str(self.old_generation.id)
        self.assertEqual(devices.get_queryset(request).count(),0)
        request.restore_recovery_generation_id=str(self.current.id)
        request.restore_recovery_user_id=str(uuid.uuid4())
        self.assertFalse(devices.has_view_permission(request))
        self.assertEqual(devices.get_queryset(request).count(),0)
