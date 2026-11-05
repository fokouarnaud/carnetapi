"""A confirmed responsible chooses fresh seller permissions for one restored shop."""
from datetime import timedelta
import uuid
from django.test import TestCase
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError,PermissionDenied,NotFound,AuthenticationFailed
from backups.models import RestoreAccessDecision,RestoreAccessGeneration,RestoreAccessReview
from backups.services.access_decisions import decide_responsible_access
from common.models import Activity
from common.services.errors import OperationConflict
from identity.models import User,Shop,Membership,Installation,DeviceSession
from identity.services.access import accessible_shops


class RestoreSellerAccessTests(TestCase):
    def setUp(self):
        self.technical=User.objects.create_superuser(username="seller-review-technical",password=None)
        self.owner=User.objects.create_user(username="seller-review-owner",is_active=False)
        self.seller=User.objects.create_user(username="restored-seller",is_active=False)
        self.a,self.b=[Shop.objects.create(owner=self.owner,name=name) for name in ("A","B")]
        self.generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=self.technical.id)
        self.flags=dict.fromkeys(("can_grant_credit","can_collect_debt","can_read_debt","can_prepare_reminder","can_read_cost","can_read_cash_balance"),True)
        self.owner_reviews=[RestoreAccessReview.objects.create(generation=self.generation,shop=shop,user=self.owner,
            archived_role="owner",was_active=True,archived_permissions=self.flags) for shop in (self.a,self.b)]
        self.owner_decision,_=decide_responsible_access(actor=self.technical,review_id=self.owner_reviews[0].id,
            operation_id=uuid.uuid4(),previous_version=0,state="confirmed",reason="Propriétaire actuel contrôlé")
        self.members=[Membership.objects.create(shop=shop,user=self.seller,role="seller",is_active=False,**self.flags) for shop in (self.a,self.b)]
        self.reviews=[RestoreAccessReview.objects.create(generation=self.generation,shop=shop,user=self.seller,
            membership=member,archived_role="seller",was_active=True,archived_permissions=self.flags) for shop,member in zip((self.a,self.b),self.members)]
        self.installation=Installation.objects.create(user=self.owner)
        self.session=DeviceSession.objects.create(installation=self.installation,expires_at=timezone.now()+timedelta(days=30))
        self.old_device=Installation.objects.create(user=self.seller,revoked_at=timezone.now())
        self.permissions={"can_grant_credit":False,"can_collect_debt":True,"can_read_debt":False,
            "can_prepare_reminder":False,"can_read_cost":False,"can_read_cash_balance":False}
        self.data={"operation_id":uuid.uuid4(),"generation_id":self.generation.id,"previous_review_version":0,
            "membership_version":1,"state":"confirmed","reason":"Vendeur et permissions actuels contrôlés",
            "evidence_ref":"","permissions":self.permissions}

    def decide(self,index=0,actor=None,installation=None,session_id=None,data=None):
        from identity.services.restore_seller_access import revalidate_seller_access
        return revalidate_seller_access(shop_id=(self.a,self.b)[index].id,member_id=self.members[index].id,
            actor=actor or self.owner,installation=installation or self.installation,
            session_id=session_id or self.session.id,data=self.data if data is None else data)

    def test_fresh_profile_is_scoped_audited_and_idempotent_without_reviving_old_devices(self):
        result,created=self.decide()
        self.assertTrue(created)
        self.assertEqual(result["state"],"confirmed")
        self.assertEqual(result["version"],1)
        self.members[0].refresh_from_db();self.members[1].refresh_from_db();self.seller.refresh_from_db();self.old_device.refresh_from_db()
        self.assertEqual((self.members[0].is_active,self.members[0].version,self.members[0].can_read_cost),(True,2,False))
        self.assertFalse(self.members[1].is_active)
        self.assertTrue(self.seller.is_active)
        self.assertFalse(self.seller.is_staff or self.seller.is_superuser)
        self.assertIsNotNone(self.old_device.revoked_at)
        self.assertEqual(result["membership"]["declared_permissions"],self.permissions)
        self.assertEqual(result["membership"]["permissions"],{**self.permissions,"can_read_debt":True})
        device=Installation.objects.create(user=self.seller)
        self.assertEqual(list(accessible_shops(self.seller,device)),[self.a])
        event=Activity.objects.get(id=self.data["operation_id"])
        self.assertEqual((event.actor_id,event.shop_id,event.target_id),(self.owner.id,self.a.id,self.members[0].id))
        self.assertTrue(event.details["before"]["declared_permissions"]["can_read_cost"])
        self.assertFalse(event.details["after"]["declared_permissions"]["can_read_cost"])
        decision=RestoreAccessDecision.objects.get(operation_id=self.data["operation_id"])
        self.assertEqual(decision.review_id,self.reviews[0].id)
        replay,created=self.decide()
        self.assertFalse(created)
        self.assertEqual(replay,result)
        self.assertEqual(Activity.objects.count(),1)
        with self.assertRaises(OperationConflict):
            self.decide(data={**self.data,"permissions":{**self.permissions,"can_read_cost":True}})

    def test_pending_owner_other_shop_and_revoked_session_cannot_revalidate(self):
        with self.assertRaises(Http404):
            self.decide(index=1)
        DeviceSession.objects.filter(id=self.session.id).update(revoked_at=timezone.now())
        with self.assertRaises(AuthenticationFailed):
            self.decide()
        self.assertFalse(Activity.objects.exists())
        self.assertFalse(RestoreAccessDecision.objects.filter(review__in=self.reviews).exists())

    def test_reason_complete_native_boolean_permissions_and_current_versions_are_required(self):
        invalid=[{"reason":" "},{"permissions":{}},{"permissions":{**self.permissions,"can_read_cost":1}},
            {"previous_review_version":True},{"membership_version":False},
            {"permissions":{**self.permissions,"unknown":False}}]
        for changes in invalid:
            with self.subTest(changes=changes),self.assertRaises(ValidationError):
                self.decide(data={**self.data,**changes})
        for changes in ({"membership_version":2},{"previous_review_version":1},{"generation_id":uuid.uuid4()},
                        {"operation_id":self.owner_decision.operation_id}):
            with self.subTest(changes=changes),self.assertRaises(OperationConflict):
                self.decide(data={**self.data,**changes})
        self.assertFalse(Activity.objects.exists())
        self.assertFalse(User.objects.get(id=self.seller.id).is_active)

    def test_refusal_keeps_old_confirmation_and_another_confirmed_shop_without_global_deactivation(self):
        self.decide()
        decide_responsible_access(actor=self.technical,review_id=self.owner_reviews[1].id,operation_id=uuid.uuid4(),
            previous_version=0,state="confirmed",reason="Contrôle de B")
        self.decide(index=1,data={**self.data,"operation_id":uuid.uuid4()})
        refused={**self.data,"operation_id":uuid.uuid4(),"state":"refused","previous_review_version":1,
            "membership_version":2,"reason":"Revalidation refusée"}
        refused.pop("permissions")
        result,created=self.decide(data=refused)
        self.assertTrue(created)
        self.assertEqual((result["state"],result["version"]),("refused",2))
        self.assertEqual(list(RestoreAccessDecision.objects.filter(review=self.reviews[0]).order_by("version").values_list("state",flat=True)),["confirmed","refused"])
        replay,created=self.decide()
        self.assertFalse(created)
        self.assertEqual(replay["state"],"confirmed")
        self.members[0].refresh_from_db();self.seller.refresh_from_db()
        self.assertFalse(self.members[0].is_active)
        self.assertTrue(self.seller.is_active)
        device=Installation.objects.create(user=self.seller)
        self.assertEqual(list(accessible_shops(self.seller,device)),[self.b])

    def test_current_seller_role_matters_and_the_responsible_cannot_reactivate_a_later_disabled_account(self):
        Membership.objects.filter(id=self.members[0].id).update(role="manager")
        with self.assertRaises(ValidationError):
            self.decide()
        Membership.objects.filter(id=self.members[0].id).update(role="seller")
        RestoreAccessReview.objects.filter(id=self.reviews[0].id).update(archived_role="manager")
        self.decide()  # A manager demoted to seller follows current seller authority.
        User.objects.filter(id=self.seller.id).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.decide(data={**self.data,"operation_id":uuid.uuid4(),"previous_review_version":1,"membership_version":2})
        self.assertFalse(User.objects.get(id=self.seller.id).is_active)
        self.assertEqual(Activity.objects.count(),1)
