"""Unfold closes partially only after a concrete, signed, CSRF-protected preview."""
import uuid

from django.test import Client, TestCase
from django.urls import reverse

from backups import models as backups
from backups.tests import test_recovery_closures as fixtures
from identity.models import User


class RecoveryClosureAdminTests(TestCase):
    setUp=fixtures.RecoveryClosureTests.setUp
    capture=fixtures.RecoveryClosureTests.capture

    def url(self):
        return reverse("admin:backups_restoresession_close",args=[self.registry.id])

    def browser(self,user=None):
        client=Client(enforce_csrf_checks=True)
        client.force_login(user or self.technical)
        return client

    def prepare(self,client):
        response=client.get(self.url())
        self.assertEqual(response.status_code,200)
        return {"csrfmiddlewaretoken":client.cookies["csrftoken"].value,"stage":"preview",
            "operation_id":str(uuid.uuid4()),"reason":"<script>perdu</script>","missing_device_ids":[str(self.lost.id)]}

    def test_preview_lists_exact_missing_observations_then_confirmation_records_once_without_granting_rights(self):
        client=self.browser();body=self.prepare(client)
        self.assertContains(client.get(self.url(),HTTP_ACCEPT_LANGUAGE="fr"),"Clôturer partiellement la récupération")
        self.assertContains(client.get(self.url(),HTTP_ACCEPT_LANGUAGE="en"),"Close recovery partially")
        self.assertEqual(client.post(self.url(),{**body,"csrfmiddlewaretoken":""}).status_code,403)
        page=client.post(self.url(),body)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,str(self.lost.origin_installation_id))
        self.assertContains(page,"&lt;script&gt;perdu&lt;/script&gt;")
        self.assertFalse(backups.RecoveryClosure.objects.exists())
        signed=page.context["confirmation_form"]["signed_request"].value()
        confirmation={"stage":"confirm","signed_request":signed,"confirmed":"on","csrfmiddlewaretoken":body["csrfmiddlewaretoken"]}
        response=client.post(self.url(),confirmation)
        self.assertEqual(response.status_code,302,response.content)
        event=backups.RecoveryClosure.objects.get()
        self.assertEqual(event.closed_by_id,self.technical.id)
        self.assertIsNone(event.closed_installation_id)
        self.assertEqual(event.reason,body["reason"])
        self.assertEqual([row["id"] for row in event.missing_devices],[str(self.lost.id)])
        replay=client.post(self.url(),confirmation)
        self.assertEqual(replay.status_code,302)
        self.assertEqual(replay.url,response.url)
        self.assertEqual(backups.RecoveryClosure.objects.count(),1)
        receipt=client.get(response.url)
        self.assertContains(receipt,"&lt;script&gt;perdu&lt;/script&gt;")
        self.assertContains(receipt,"closed_partial")
        self.assertFalse(User.objects.get(id=self.owner.id).is_superuser)

    def test_stale_or_tampered_confirmation_cannot_close_and_historical_generation_is_inaccessible(self):
        client=self.browser();body=self.prepare(client)
        preview=client.post(self.url(),body)
        signed=preview.context["confirmation_form"]["signed_request"].value()
        confirmation={"stage":"confirm","signed_request":signed,"confirmed":"on","csrfmiddlewaretoken":body["csrfmiddlewaretoken"]}
        invalid=client.post(self.url(),{**confirmation,"signed_request":signed+"tamper"})
        self.assertEqual(invalid.status_code,400)
        self.capture()
        stale=client.post(self.url(),confirmation)
        self.assertEqual(stale.status_code,409)
        self.assertFalse(backups.RecoveryClosure.objects.exists())
        self.generation.current_slot=None;self.generation.save()
        self.assertEqual(client.get(self.url()).status_code,404)

    def test_only_current_technical_can_open_or_confirm_and_confirmation_is_bound_to_preview_actor(self):
        client=self.browser();body=self.prepare(client)
        preview=client.post(self.url(),body)
        signed=preview.context["confirmation_form"]["signed_request"].value()
        confirmation={"stage":"confirm","signed_request":signed,"confirmed":"on","csrfmiddlewaretoken":body["csrfmiddlewaretoken"]}
        self.owner.is_staff=True;self.owner.save()
        self.assertEqual(self.browser(self.owner).get(self.url()).status_code,403)
        other=User.objects.create_superuser(username="closure-other-technical",password=None)
        client.force_login(other)
        self.assertEqual(client.post(self.url(),confirmation).status_code,403)
        client.force_login(self.technical)
        User.objects.filter(id=self.technical.id).update(is_superuser=False)
        self.assertEqual(client.get(self.url()).status_code,403)
        self.assertEqual(client.post(self.url(),confirmation).status_code,403)
        self.assertFalse(backups.RecoveryClosure.objects.exists())
