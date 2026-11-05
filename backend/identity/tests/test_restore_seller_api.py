"""Connected responsible-only revalidation with explicit permissions and optimistic versions."""
from datetime import timedelta
import uuid
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from backups.services.access_decisions import decide_responsible_access
from common.models import Activity
from identity.models import User,Membership,Installation,DeviceSession
from identity.tests import test_restore_seller_access as fixtures


class RestoreSellerAPITests(TestCase):
    setUp=fixtures.RestoreSellerAccessTests.setUp
    decide=fixtures.RestoreSellerAccessTests.decide

    def test_owner_can_nominate_a_restored_seller_as_manager_then_technical_checks_the_current_role(self):
        client,_=self.api()
        nomination=client.patch(f"/api/v1/shops/{self.a.id}/team/{self.members[0].id}/",{
            "operation_id":str(uuid.uuid4()),"base_version":1,"reason":"Nomination actuelle du propriétaire","role":"manager"},format="json")
        self.assertEqual(nomination.status_code,200,nomination.data)
        self.assertFalse(nomination.data["is_active"])
        decision,_=decide_responsible_access(actor=self.technical,review_id=self.reviews[0].id,
            operation_id=uuid.uuid4(),previous_version=0,state="confirmed",reason="Qualité actuelle de responsable contrôlée")
        self.assertEqual(decision.state,"confirmed")
        self.members[0].refresh_from_db()
        self.assertEqual((self.members[0].role,self.members[0].is_active,self.members[0].version),("manager",True,3))
        preview=client.get(self.url())
        self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.data["archived_role"],"seller")
        self.assertEqual(preview.data["membership"]["role"],"manager")

    def test_responsible_conflict_and_missing_scope_messages_are_localized(self):
        client,_=self.api()
        self.assertEqual(client.post(self.url(),self.data,format="json").status_code,201)
        seller,_=self.api(self.seller)
        samples=[(seller.get(self.url(),HTTP_ACCEPT_LANGUAGE="en"),403,"Only a current shop responsible can perform this action."),
            (client.post(self.url(),{**self.data,"reason":"Different request"},format="json",HTTP_ACCEPT_LANGUAGE="en"),
             409,"The restoration context changed or this operation UUID belongs to another request."),
            (client.get(self.url(shop=self.b,member=self.members[1]),HTTP_ACCEPT_LANGUAGE="fr"),404,"Non trouvé.")]
        for response,status,message in samples:
            with self.subTest(status=status):
                self.assertEqual(response.status_code,status)
                self.assertEqual(str(response.data["detail"]),message)

    def test_new_endpoint_documents_observed_responses_and_translates_profile_validation(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import validate
        schema=SchemaGenerator().get_schema(request=None,public=True)
        path=next(path for path in schema["paths"] if path.endswith("/restore-access/"))
        def check(response,method):
            document=schema["paths"][path][method]["responses"][str(response.status_code)]
            validate(response.data,{**document["content"]["application/json"]["schema"],"components":schema["components"]})
        client,_=self.api()
        check(client.get(self.url()),"get")
        missing=dict(self.data);missing.pop("permissions")
        invalid=client.post(self.url(),missing,format="json",HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(invalid.status_code,400)
        check(invalid,"post")
        self.assertEqual(str(invalid.data["non_field_errors"][0]),
            "Une confirmation exige un profil complet de permissions ; un refus ne doit pas en contenir.")
        check(client.post(self.url(),self.data,format="json"),"post")
        check(client.post(self.url(),self.data,format="json"),"post")
        check(client.post(self.url(),{**self.data,"reason":"Autre motif"},format="json"),"post")
        check(client.get(self.url(shop=self.b,member=self.members[1])),"get")
        unsupported=client.generic("POST",self.url(),data=b"unsupported",content_type="application/octet-stream")
        self.assertEqual(unsupported.status_code,415)
        check(unsupported,"post")

    def api(self,user=None):
        user=user or self.owner
        user.refresh_from_db()
        installation=Installation.objects.create(user=user)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=30))
        token=AccessToken.for_user(user)
        token["session_id"]=str(session.id)
        client=APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client,session

    def url(self,shop=None,member=None):
        return f"/api/v1/shops/{(shop or self.a).id}/team/{(member or self.members[0]).id}/restore-access/"

    def test_preview_separates_archived_permissions_and_effective_access_then_post_is_scoped_and_idempotent(self):
        client,session=self.api()
        response=client.get(self.url())
        self.assertEqual(response.status_code,200)
        self.assertEqual((response.data["state"],response.data["version"]),("pending",0))
        self.assertEqual(response.data["archived_permissions"],self.flags)
        self.assertEqual(response.data["membership"]["permissions"],dict.fromkeys(self.flags,False))
        created=client.post(self.url(),self.data,format="json")
        self.assertEqual(created.status_code,201,created.data)
        self.assertEqual(created.data["membership"]["declared_permissions"],self.permissions)
        self.assertTrue(created.data["membership"]["permissions"]["can_read_debt"])
        replay=client.post(self.url(),self.data,format="json")
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.data,created.data)
        changed=client.post(self.url(),{**self.data,"reason":"Autre contrôle"},format="json")
        self.assertEqual(changed.status_code,409)
        self.assertEqual(client.get(self.url(shop=self.b,member=self.members[1])).status_code,404)
        self.assertEqual(client.get(self.url(member=self.members[1])).status_code,404)
        self.assertEqual(DeviceSession.objects.filter(installation__user=self.seller).count(),0)
        self.assertEqual(Activity.objects.count(),1)
        history=client.get(f"/api/v1/shops/{self.a.id}/team/{self.members[0].id}/history/")
        self.assertEqual(history.status_code,200)
        self.assertEqual(history.data["count"],1)
        self.assertEqual(history.data["results"][0]["action"],"restore_seller_access_decided")

    def test_seller_and_technical_without_business_membership_are_denied_but_a_current_manager_can_refuse(self):
        self.decide()
        seller,_=self.api(self.seller)
        self.assertEqual(seller.get(self.url()).status_code,403)
        self.assertEqual(seller.post(self.url(),self.data,format="json").status_code,403)
        technical,_=self.api(self.technical)
        self.assertEqual(technical.get(self.url()).status_code,404)
        manager=User.objects.create_user(username="new-current-manager")
        Membership.objects.create(user=manager,shop=self.a,role="manager")
        client,_=self.api(manager)
        refusal={**self.data,"operation_id":uuid.uuid4(),"previous_review_version":1,"membership_version":2,
            "state":"refused","reason":"Contrôle du responsable"}
        refusal.pop("permissions")
        response=client.post(self.url(),refusal,format="json")
        self.assertEqual(response.status_code,201,response.data)
        self.assertFalse(response.data["membership"]["is_active"])
        self.assertEqual(Activity.objects.order_by("created_at").last().actor_id,manager.id)

    def test_complete_strict_profile_is_required_without_client_supplied_authority(self):
        client,_=self.api()
        for changes in ({"permissions":{}},{"permissions":{**self.permissions,"can_read_cost":1}},
                        {"permissions":{**self.permissions,"unknown":False}}, {"membership_version":"1"},
                        {"previous_review_version":True},{"reason":" "},{"requested_by":str(self.technical.id)}):
            with self.subTest(changes=changes):
                response=client.post(self.url(),{**self.data,**changes},format="json")
                self.assertEqual(response.status_code,400,response.data)
        missing=dict(self.data);missing.pop("permissions")
        self.assertEqual(client.post(self.url(),missing,format="json").status_code,400)
        self.assertEqual(client.post(self.url(),{**self.data,"state":"refused"},format="json").status_code,400)
        self.assertFalse(Activity.objects.exists())

    def test_current_access_and_live_session_are_checked_before_cached_receipts(self):
        client,session=self.api()
        self.assertEqual(client.post(self.url(),self.data,format="json").status_code,201)
        decide_responsible_access(actor=self.technical,review_id=self.owner_reviews[0].id,operation_id=uuid.uuid4(),
            previous_version=1,state="refused",reason="Accès du responsable retiré")
        self.assertEqual(client.get(self.url()).status_code,404)
        self.assertEqual(client.post(self.url(),self.data,format="json").status_code,404)
        DeviceSession.objects.filter(id=session.id).update(revoked_at=timezone.now())
        self.assertEqual(client.get(self.url()).status_code,401)
        self.assertEqual(Activity.objects.count(),1)
