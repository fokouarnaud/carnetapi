"""Stored collection/debt money must remain integral even outside API serializers."""
from datetime import datetime,timezone

from django.db import connection,IntegrityError,transaction
from django.test import TestCase

from common.models import Activity
from customers.models import Customer,CustomerDebt,CustomerPayment,CustomerPaymentPart,DebtAllocation
from identity.models import User,Shop,Installation


class CollectionStorageTests(TestCase):
    def setUp(self):
        owner=User.objects.create_user(username="collection-storage-owner")
        shop=Shop.objects.create(owner=owner,name="A")
        customer=Customer.objects.create(shop=shop,name="Client")
        installation=Installation.objects.create(user=owner)
        instant=datetime(2026,10,22,12,tzinfo=timezone.utc)
        opening=Activity.objects.create(shop=shop,actor=owner,action="opening_debt",target_id=customer.id,reason="Ouverture")
        self.debt=CustomerDebt.objects.create(shop=shop,customer=customer,actor=owner,installation=installation,
            operation=opening,amount_fcfa=5000,remaining_fcfa=3500,occurred_at=instant)
        event=Activity.objects.create(shop=shop,actor=owner,action="customer_payment_created",target_id=customer.id,reason="Encaissement")
        self.payment=CustomerPayment.objects.create(shop=shop,customer=customer,actor=owner,installation=installation,
            operation=event,amount_fcfa=2000,unallocated_fcfa=500,requires_regularization=True,occurred_at=instant)
        self.part=CustomerPaymentPart.objects.create(payment=self.payment,position=0,method="cash",amount_fcfa=2000,
            received_fcfa=2500,returned_fcfa=500)
        self.allocation=DebtAllocation.objects.create(payment=self.payment,debt=self.debt,position=0,
            requested_amount_fcfa=2000,applied_amount_fcfa=1500)

    def test_fractional_debt_collection_or_allocation_cannot_enter_sqlite(self):
        fields=((self.debt,"amount_fcfa",5000.5),(self.debt,"remaining_fcfa",3500.5),(self.debt,"due_version",1.5),
            (self.payment,"amount_fcfa",2000.5),(self.payment,"unallocated_fcfa",500.5),
            (self.part,"position",0.5),(self.allocation,"position",0.5),
            (self.allocation,"requested_amount_fcfa",2000.5),(self.allocation,"applied_amount_fcfa",1500.5))
        for record,column,value in fields:
            with self.subTest(model=type(record).__name__,column=column),self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
                cursor.execute(f'UPDATE "{record._meta.db_table}" SET "{column}"=%s WHERE id=%s',[value,record.id.hex])

    def test_fractional_payment_parts_cannot_hide_behind_an_exact_change_equality(self):
        with self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
            cursor.execute('UPDATE core_customerpaymentpart SET amount_fcfa=2000.5,received_fcfa=2500.5 WHERE id=%s',[self.part.id.hex])
        with self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
            cursor.execute('UPDATE core_customerpaymentpart SET received_fcfa=2500.5,returned_fcfa=500.5 WHERE id=%s',[self.part.id.hex])

    def test_valid_money_and_optional_unspecified_change_are_preserved(self):
        self.part.received_fcfa=None;self.part.returned_fcfa=None
        self.part.save(update_fields=["received_fcfa","returned_fcfa"])
        self.part.refresh_from_db();self.payment.refresh_from_db();self.allocation.refresh_from_db()
        self.assertEqual((self.part.amount_fcfa,self.payment.unallocated_fcfa,self.allocation.applied_amount_fcfa),(2000,500,1500))
        self.assertIsNone(self.part.received_fcfa)
