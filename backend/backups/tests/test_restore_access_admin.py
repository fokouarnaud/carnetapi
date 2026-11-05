"""Motivated Unfold controls preserve scope, history and server-side authority."""
import uuid
from html.parser import HTMLParser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase,RequestFactory
from django.urls import reverse
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from identity.models import Shop, User


class RestoreAccessAdministrationTests(TestCase):
    def test_list_identifies_its_generation_and_links_to_control_instead_of_generic_change(self):
        response=self.client.get(reverse("admin:backups_restoreaccessreview_changelist"))
        class ResultText(HTMLParser):
            def __init__(self):
                super().__init__();self.result=False;self.text=[]
            def handle_starttag(self,tag,attrs):
                if tag=="table" and dict(attrs).get("id")=="result_list":self.result=True
            def handle_endtag(self,tag):
                if tag=="table":self.result=False
            def handle_data(self,data):
                if self.result:self.text.append(data)
        rendered=ResultText();rendered.feed(response.content.decode())
        self.assertIn(str(self.generation.id)," ".join(rendered.text))
        generic=reverse("admin:backups_restoreaccessreview_change",args=[self.reviews[0].id])
        self.assertNotContains(response,f'href="{generic}"')
        self.assertContains(response,f'href="{self.url()}"')

    def test_recovery_login_accepts_only_the_identity_attested_by_the_external_proof(self):
        from backups.recovery_forms import RecoveryAuthenticationForm
        password="fixture-recovery-form-42!"
        self.technical.set_password(password)
        self.technical.save()
        other=User.objects.create_superuser(username="different-technical",password=password)
        request=RequestFactory().post("/admin/login/")
        request.restore_recovery_user_id=str(self.technical.id)
        self.assertTrue(RecoveryAuthenticationForm(request,data={"username":self.technical.username,"password":password}).is_valid())
        self.assertFalse(RecoveryAuthenticationForm(request,data={"username":other.username,"password":password}).is_valid())

    def test_recovery_context_rejects_a_different_technical_identity(self):
        from backups.admin_base import technical
        request=RequestFactory().get("/admin/")
        request.user=self.technical
        request.restore_recovery_user_id=str(uuid.uuid4())
        self.assertFalse(technical(request))

    def setUp(self):
        self.technical = User.objects.create_superuser(username="access-admin", password=None)
        self.owner = User.objects.create_user(username="access-owner", is_active=False)
        self.a, self.b = [Shop.objects.create(name=name,owner=self.owner) for name in ("A","B")]
        self.generation = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=uuid.uuid4())
        flags = dict.fromkeys(("can_grant_credit","can_collect_debt","can_read_debt","can_prepare_reminder","can_read_cost","can_read_cash_balance"),True)
        self.reviews = [RestoreAccessReview.objects.create(generation=self.generation,shop=shop,user=self.owner,
            archived_role="owner",was_active=True,archived_permissions=flags) for shop in (self.a,self.b)]
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.technical)
        self.body = {"operation_id":str(uuid.uuid4()),"generation_id":str(self.generation.id),
            "previous_version":0,"state":"confirmed","reason":"Contrôle humain actuel","evidence_ref":""}

    def url(self,index=0):
        return reverse("admin:backups_restoreaccessreview_control",args=[self.reviews[index].id])

    def token(self):
        self.client.get(self.url())
        return self.client.cookies["csrftoken"].value

    def test_bilingual_form_requires_csrf_then_records_only_the_url_scope_and_server_actor(self):
        self.assertContains(self.client.get(reverse("admin:backups_restoreaccessreview_changelist")),str(self.reviews[0].id))
        for language,label in (("fr","Contrôler les accès restaurés"),("en","Review restored access")):
            with self.subTest(language=language):
                self.assertContains(self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE=language),label)
        self.assertContains(self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE="fr"),"Rôle archivé : Propriétaire")
        self.assertFalse(RestoreAccessDecision.objects.exists())
        self.assertEqual(self.client.post(self.url(),self.body).status_code,403)
        payload={**self.body,"csrfmiddlewaretoken":self.token(),"requested_by":str(self.owner.id),"shop_id":str(self.b.id)}
        response=self.client.post(self.url(),payload)
        self.assertEqual(response.status_code,302)
        decision=RestoreAccessDecision.objects.get()
        self.assertEqual((decision.review_id,decision.requested_by_id,decision.reason,decision.evidence_ref),
            (self.reviews[0].id,self.technical.id,"Contrôle humain actuel",""))
        self.assertEqual(self.client.post(self.url(),payload).url,response.url)
        self.assertEqual(RestoreAccessDecision.objects.count(),1)
        page=self.client.get(response.url,HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page,"Décision enregistrée")
        self.assertContains(page,"Les opérations commerciales restent bloquées pendant la maintenance.")
        self.assertNotContains(page,'name="activate"')
        self.assertFalse(RestoreAccessDecision.objects.filter(review=self.reviews[1]).exists())
        self.assertFalse(User.objects.get(id=self.owner.id).is_superuser)

    def test_empty_reason_and_old_generation_do_not_grant_access_and_conflicts_preserve_history(self):
        token=self.token()
        body={**self.body,"csrfmiddlewaretoken":token}
        invalid=self.client.post(self.url(),{**body,"reason":" "})
        self.assertEqual(invalid.status_code,200)
        self.assertFalse(RestoreAccessDecision.objects.exists())
        invalid=self.client.post(self.url(),{**body,"generation_id":str(uuid.uuid4())})
        self.assertEqual(invalid.status_code,409)
        self.assertFalse(RestoreAccessDecision.objects.exists())
        self.assertEqual(self.client.post(self.url(),body).status_code,302)
        stale=self.client.post(self.url(),{**body,"operation_id":str(uuid.uuid4())})
        self.assertEqual(stale.status_code,409)
        refused=self.client.post(self.url(),{**body,"operation_id":str(uuid.uuid4()),"previous_version":1,"state":"refused","reason":"Contrôle non concluant"})
        self.assertEqual(refused.status_code,302)
        page=self.client.get(refused.url)
        self.assertContains(page,"Contrôle humain actuel")
        self.assertContains(page,"Contrôle non concluant")
        self.assertEqual(list(RestoreAccessDecision.objects.order_by("version").values_list("state",flat=True)),["confirmed","refused"])

    def test_business_staff_and_demoted_technical_cannot_read_or_write_and_generic_mutations_are_disabled(self):
        token=self.token()
        root=reverse("admin:backups_restoreaccessreview_changelist")
        for action in ("add","delete","change"):
            args=[] if action=="add" else [self.reviews[0].id]
            url=reverse(f"admin:backups_restoreaccessreview_{action}",args=args)
            self.assertEqual(self.client.post(url,{"csrfmiddlewaretoken":token}).status_code,403)
        User.objects.filter(id=self.owner.id).update(is_active=True,is_staff=True)
        self.client.force_login(self.owner)
        for url in (root,self.url()):
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.generic("POST",self.url(),data=b"",CONTENT_LENGTH="999999999").status_code,403)
        self.client.force_login(self.technical)
        User.objects.filter(id=self.technical.id).update(is_superuser=False)
        self.assertEqual(self.client.get(self.url()).status_code,403)
        self.assertFalse(RestoreAccessDecision.objects.exists())

    def test_files_and_large_bodies_are_rejected_before_any_decision(self):
        token=self.token()
        self.assertEqual(self.client.generic("POST",self.url(),data=b"",CONTENT_LENGTH="65537").status_code,400)
        response=self.client.post(self.url(),{**self.body,"csrfmiddlewaretoken":token,
            "file":SimpleUploadedFile("unexpected",b"no-file-expected")})
        self.assertEqual(response.status_code,200)
        self.assertFalse(RestoreAccessDecision.objects.exists())
