"""Frozen collections preserve original allocations rather than recreating them."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import uuid
from django.test import SimpleTestCase


class RecoveryCollectionSnapshotTests(SimpleTestCase):
    def source(self):
        payment,operation,customer,part,allocation,debt,device,actor=[uuid.uuid4() for _ in range(8)]
        declared={"id":str(part),"method":"cash","amount_fcfa":3000,"other_name":"","external_reference":"",
            "received_fcfa":6000,"returned_fcfa":3000}
        requested={"id":str(allocation),"debt_id":str(debt),"amount_fcfa":3000}
        snapshot={"id":str(payment),"customer_id":str(customer),"amount_fcfa":3000,"unallocated_fcfa":1000,
            "requires_regularization":True,"parts":[deepcopy(declared)],"allocations":[{
                "id":str(allocation),"debt_id":str(debt),"requested_amount_fcfa":3000,"applied_amount_fcfa":2000}]}
        raw={"id":str(payment),"operation_id":str(operation),"customer_id":str(customer),"installation_id":str(device),
            "parts":[declared],"allocations":[requested]}
        return SimpleNamespace(operation_id=operation,origin_user_id=actor,origin_installation_id=device,
            occurred_at=datetime(2026,10,22,9,tzinfo=timezone.utc),payload={"schema_version":1,"original_request":raw,"collection":snapshot})

    def test_exact_source_request_and_initial_applied_values_are_both_kept(self):
        from backups.services.recovery_collection_snapshot import decode_collection_snapshot
        source=self.source();decoded=decode_collection_snapshot(source)
        self.assertEqual(decoded.snapshot["unallocated_fcfa"],1000)
        self.assertEqual(decoded.snapshot["allocations"][0]["applied_amount_fcfa"],2000)
        self.assertEqual(decoded.request["allocations"][0]["amount_fcfa"],3000)
        self.assertEqual(decoded.canonical_request["installation_id"],str(source.origin_installation_id))

    def test_inconsistent_facts_noninteger_money_and_different_origin_are_refused(self):
        from backups.services.recovery_collection_snapshot import decode_collection_snapshot,RecoveryCollectionSnapshotInvalid
        for case in ("sum","applied","requested","actor_device","customer","change","version","bool","float","string","regularization"):
            with self.subTest(case=case):
                source=self.source();snapshot=source.payload["collection"];raw=source.payload["original_request"]
                if case=="sum":snapshot["unallocated_fcfa"]=1001
                elif case=="applied":snapshot["allocations"][0]["applied_amount_fcfa"]=3001
                elif case=="requested":snapshot["allocations"][0]["requested_amount_fcfa"]=2500
                elif case=="actor_device":raw["installation_id"]=str(uuid.uuid4())
                elif case=="customer":raw["customer_id"]=str(uuid.uuid4())
                elif case=="change":snapshot["parts"][0]["returned_fcfa"]=2999
                elif case=="version":source.payload["schema_version"]=2
                elif case=="bool":snapshot["amount_fcfa"]=True
                elif case=="float":snapshot["amount_fcfa"]=3000.0
                elif case=="string":snapshot["amount_fcfa"]="3000"
                else:snapshot["requires_regularization"]=False
                with self.assertRaises(RecoveryCollectionSnapshotInvalid):decode_collection_snapshot(source)

    def test_duplicate_debt_references_must_have_possible_sequential_original_allocations(self):
        from backups.services.recovery_collection_snapshot import decode_collection_snapshot,RecoveryCollectionSnapshotInvalid
        source=self.source();snapshot=source.payload["collection"];raw=source.payload["original_request"]
        debt=raw["allocations"][0]["debt_id"]
        first,second=uuid.uuid4(),uuid.uuid4()
        raw["allocations"]=[{"id":str(first),"debt_id":debt,"amount_fcfa":1500},{"id":str(second),"debt_id":debt,"amount_fcfa":1500}]
        snapshot["allocations"]=[{"id":str(first),"debt_id":debt,"requested_amount_fcfa":1500,"applied_amount_fcfa":1000},
            {"id":str(second),"debt_id":debt,"requested_amount_fcfa":1500,"applied_amount_fcfa":1000}]
        with self.assertRaises(RecoveryCollectionSnapshotInvalid):decode_collection_snapshot(source)

    def test_omitted_declared_change_is_distinct_from_an_invented_zero(self):
        from backups.services.recovery_collection_snapshot import decode_collection_snapshot
        source=self.source()
        for key in ("received_fcfa","returned_fcfa"):
            source.payload["original_request"]["parts"][0].pop(key)
            source.payload["collection"]["parts"][0][key]=None
        decoded=decode_collection_snapshot(source)
        self.assertIsNone(decoded.snapshot["parts"][0]["received_fcfa"])
