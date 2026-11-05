"""Price recovery from frozen source values; catalogue reads are forbidden here."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import uuid

from django.test import SimpleTestCase
from rest_framework.exceptions import APIException


class RecoverySaleSnapshotTests(SimpleTestCase):
    def source(self):
        operation,sale,line,product,payment=[uuid.uuid4() for _ in range(5)]
        original={"id":str(sale),"operation_id":str(operation),"mode":"cash",
            "lines":[{"id":str(line),"product_id":str(product),"product_version":7,"quantity_milli":1000}],
            "payments":[{"id":str(payment),"method":"cash","amount_fcfa":500,"received_fcfa":500,"returned_fcfa":0}]}
        frozen={"id":str(sale),"mode":"cash","customer_id":None,"due_at":None,
            "reference_total_fcfa":500,"discount_total_fcfa":0,"net_total_fcfa":500,
            "lines":[{"id":str(line),"product_id":str(product),"product_version":7,"pack_id":None,
                "product_name":"Nom et tarif d’origine","unit_kind":"piece","unit_label":"pièce",
                "quantity_milli":1000,"factor_milli":1000,"base_quantity_milli":1000,"unit_price_fcfa":500,
                "reference_fcfa":500,"own_discount_fcfa":0,"global_discount_fcfa":0,"net_fcfa":500}],
            "payments":[{**original["payments"][0],"other_name":"","external_reference":""}]}
        return SimpleNamespace(id=uuid.uuid4(),shop_id=uuid.uuid4(),operation_id=operation,
            origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),
            occurred_at=datetime(2026,10,22,9,tzinfo=timezone.utc),
            payload={"schema_version":1,"original_request":original,"sale":frozen})

    def decode(self,capture):
        from backups.services.recovery_sale_snapshot import decode_sale_snapshot
        return decode_sale_snapshot(capture,timezone_name="Africa/Douala")

    def test_frozen_carton_and_half_kilogram_preserve_their_original_prices_and_factors(self):
        for kind,quantity,factor,price,base,reference in (("piece",1000,24000,4800,24000,4800),("weight",500,1000,249,500,125)):
            with self.subTest(kind=kind):
                source=self.source()
                snapshot=source.payload["sale"]
                requested=source.payload["original_request"]
                row=snapshot["lines"][0]
                row.update(unit_kind=kind,quantity_milli=quantity,factor_milli=factor,base_quantity_milli=base,
                    unit_price_fcfa=price,reference_fcfa=reference,net_fcfa=reference)
                requested["lines"][0]["quantity_milli"]=quantity
                if factor!=1000:
                    pack=str(uuid.uuid4())
                    row["pack_id"]=pack
                    requested["lines"][0]["pack_id"]=pack
                snapshot.update(reference_total_fcfa=reference,net_total_fcfa=reference)
                for payment in (snapshot["payments"][0],requested["payments"][0]):
                    payment.update(amount_fcfa=reference,received_fcfa=reference)
                decoded=self.decode(source)
                self.assertEqual(decoded.basket.net_total_fcfa,reference)
                self.assertEqual(decoded.snapshot["lines"][0]["base_quantity_milli"],base)
                self.assertEqual(decoded.snapshot["lines"][0]["unit_price_fcfa"],price)
                self.assertEqual(decoded.canonical_request["installation_id"],str(source.origin_installation_id))

    def test_header_amounts_physical_conversion_original_request_and_exact_types_must_agree(self):
        for field,value in (("net_total_fcfa",499),("factor_milli",1500),("base_quantity_milli",1001),
                            ("unit_price_fcfa",501),("quantity_milli",1.5),("schema_version",True),("original_operation",str(uuid.uuid4()))):
            with self.subTest(field=field):
                source=self.source()
                if field=="net_total_fcfa":source.payload["sale"][field]=value
                elif field=="schema_version":source.payload[field]=value
                elif field=="original_operation":source.payload["original_request"]["operation_id"]=value
                else:source.payload["sale"]["lines"][0][field]=value
                with self.assertRaises(APIException) as caught:self.decode(source)
                self.assertEqual(caught.exception.status_code,400)
                self.assertEqual(caught.exception.get_codes(),"recovery_sale_snapshot_invalid")

    def test_global_discount_allocation_is_verified_instead_of_accepting_a_plausible_total(self):
        source=self.source()
        snapshot=source.payload["sale"]
        requested=source.payload["original_request"]
        line=snapshot["lines"][0]
        line.update(unit_price_fcfa=1000,reference_fcfa=1000,own_discount_fcfa=100,global_discount_fcfa=90,net_fcfa=810)
        requested["lines"][0]["own_discount_fcfa"]=100
        second={**line,"id":str(uuid.uuid4()),"product_id":str(uuid.uuid4()),"unit_price_fcfa":2000,
            "reference_fcfa":2000,"own_discount_fcfa":0,"global_discount_fcfa":200,"net_fcfa":1800}
        snapshot["lines"].append(second)
        requested["lines"].append({"id":second["id"],"product_id":second["product_id"],"product_version":7,"quantity_milli":1000})
        requested["global_discount_fcfa"]=290
        snapshot.update(reference_total_fcfa=3000,discount_total_fcfa=390,net_total_fcfa=2610)
        for payment in (snapshot["payments"][0],requested["payments"][0]):payment.update(amount_fcfa=2610,received_fcfa=2610)
        decoded=self.decode(source)
        self.assertEqual([row.global_discount_fcfa for row in decoded.basket.lines],[90,200])
        changed=deepcopy(source)
        changed.payload["sale"]["lines"][0].update(global_discount_fcfa=91,net_fcfa=809)
        changed.payload["sale"]["lines"][1].update(global_discount_fcfa=199,net_fcfa=1801)
        with self.assertRaises(APIException):self.decode(changed)

    def test_fully_paid_credit_has_no_debt_deadline_even_when_the_request_had_a_due_date(self):
        source=self.source()
        customer=str(uuid.uuid4())
        source.payload["original_request"].update(mode="credit",customer_id=customer,due_date="2026-10-31")
        source.payload["sale"].update(mode="credit",customer_id=customer,due_at=None)
        decoded=self.decode(source)
        self.assertEqual(decoded.basket.net_total_fcfa,500)
        self.assertIsNone(decoded.snapshot["due_at"])
        self.assertEqual(sum(row["amount_fcfa"] for row in decoded.snapshot["payments"]),500)
        source.payload["sale"]["due_at"]="2026-10-30T23:00:00Z"
        with self.assertRaises(APIException):self.decode(source)
