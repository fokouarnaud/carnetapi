"""Initial customer/opening-debt facts are independent from mutable balances."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import uuid
from django.test import SimpleTestCase


class RecoveryCustomerSnapshotTests(SimpleTestCase):
    def customer_source(self):
        customer,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,22,9,tzinfo=timezone.utc)
        return SimpleNamespace(operation_type="customer_creation",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,
                "original_request":{"id":str(customer),"operation_id":str(operation),"name":"Client retrouvé","phone":"+237600000001"},
                "customer":{"id":str(customer),"name":"Client retrouvé","phone":"+237600000001","version":1,
                    "created_at":occurred.isoformat(),"updated_at":occurred.isoformat()}})

    def debt_source(self,customer_id=None):
        debt,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        customer_id=customer_id or uuid.uuid4();occurred=datetime(2026,10,22,9,5,tzinfo=timezone.utc)
        return SimpleNamespace(operation_type="customer_opening_debt",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,
                "original_request":{"id":str(debt),"operation_id":str(operation),"customer_id":str(customer_id),
                    "installation_id":str(device),"amount_fcfa":8000,"occurred_at":occurred.isoformat(),
                    "reason":"Solde antérieur confirmé","due_date":"2026-10-31"},
                "opening_debt":{"id":str(debt),"customer_id":str(customer_id),"sale_id":None,"amount_fcfa":8000,
                    "remaining_fcfa":8000,"occurred_at":occurred.isoformat(),"due_at":"2026-10-30T23:00:00Z","due_version":1}})

    def test_customer_initial_fields_and_native_canonical_request_are_preserved(self):
        from backups.services.recovery_customer_snapshots import decode_customer_creation
        source=self.customer_source();decoded=decode_customer_creation(source)
        self.assertEqual(decoded.snapshot["created_at"],source.occurred_at)
        self.assertEqual(decoded.canonical_request,source.payload["original_request"])
        self.assertEqual(decoded.snapshot["version"],1)

    def test_opening_debt_preserves_full_initial_balance_and_frozen_local_deadline(self):
        from backups.services.recovery_customer_snapshots import decode_opening_debt
        source=self.debt_source();decoded=decode_opening_debt(source,timezone_name="Africa/Douala")
        self.assertEqual(decoded.snapshot["remaining_fcfa"],8000)
        self.assertEqual(decoded.snapshot["due_at"].isoformat(),"2026-10-30T23:00:00+00:00")
        self.assertEqual(decoded.canonical_request["installation_id"],str(source.origin_installation_id))

    def test_customer_update_or_inconsistent_identity_dates_and_phone_are_not_creation_facts(self):
        from backups.services.recovery_customer_snapshots import decode_customer_creation,RecoveryCustomerSnapshotInvalid
        for case in ("version","name","phone","id","created","updated","naive","operation","device"):
            with self.subTest(case=case):
                source=self.customer_source();snapshot=source.payload["customer"];raw=source.payload["original_request"]
                if case=="version":snapshot["version"]=2
                elif case=="name":snapshot["name"]="Autre nom"
                elif case=="phone":snapshot["phone"]="600000001"
                elif case=="id":snapshot["id"]=str(uuid.uuid4())
                elif case=="created":snapshot["created_at"]="2026-10-21T09:00:00Z"
                elif case=="updated":snapshot["updated_at"]="2026-10-21T09:00:00Z"
                elif case=="naive":snapshot["created_at"]="2026-10-22T09:00:00"
                elif case=="operation":raw["operation_id"]=str(uuid.uuid4())
                else:raw["installation_id"]=str(uuid.uuid4())
                with self.assertRaises(RecoveryCustomerSnapshotInvalid):decode_customer_creation(source)

    def test_paid_or_revised_debt_receipt_cannot_replace_the_initial_opening(self):
        from backups.services.recovery_customer_snapshots import decode_opening_debt,RecoveryCustomerSnapshotInvalid
        for case in ("paid","version","sale","deadline","customer","amount","float","bool","string","naive"):
            with self.subTest(case=case):
                source=self.debt_source();snapshot=source.payload["opening_debt"];raw=source.payload["original_request"]
                if case=="paid":snapshot["remaining_fcfa"]=5000
                elif case=="version":snapshot["due_version"]=2
                elif case=="sale":snapshot["sale_id"]=str(uuid.uuid4())
                elif case=="deadline":snapshot["due_at"]="2026-10-31T00:00:00Z"
                elif case=="customer":raw["customer_id"]=str(uuid.uuid4())
                elif case=="amount":snapshot["amount_fcfa"]=8001
                elif case=="float":raw["amount_fcfa"]=8000.0
                elif case=="bool":raw["amount_fcfa"]=True
                elif case=="string":raw["amount_fcfa"]="8000"
                else:raw["occurred_at"]="2026-10-22T09:05:00"
                with self.assertRaises(RecoveryCustomerSnapshotInvalid):decode_opening_debt(source,timezone_name="Africa/Douala")
