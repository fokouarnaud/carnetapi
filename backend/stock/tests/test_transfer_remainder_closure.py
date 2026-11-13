"""US-16 / G16: the destination manager closes a lost or damaged remainder; it never returns to the source."""
import uuid

from django.db import IntegrityError, transaction

from common.models import Activity
from identity.models import Installation
from stock.models import StockMovement
from stock.tests.test_transfers import TransferApiTests


class TransferRemainderClosureTests(TransferApiTests):
    def close(self, transfer, quantity, outcome="lost"):
        from stock.transfer_models import StockTransferLine, StockTransferLineClosure
        line = StockTransferLine.objects.get(id=transfer["lines"][0]["id"])
        event = Activity.objects.create(shop=self.destination, actor=self.destination_manager,
                                        action="stock_transfer_remainder_closed", target_id=line.id, reason="Colis perdu")
        return StockTransferLineClosure.objects.create(line=line, operation=event, quantity_milli=quantity, outcome=outcome,
                                                       installation=Installation.objects.create(user=self.destination_manager),
                                                       reason="Colis perdu en route")

    def test_closed_remainder_leaves_transit_and_blocks_a_later_receipt_without_source_return(self):
        from stock.services.transfers import transfer_lines
        transfer, _ = self.depart()
        self.client = self.authenticate(self.destination_manager)
        path = self.url(self.destination, transfer["id"] + "/receipts/")
        self.assertEqual(self.client.post(path, self.arrival(transfer, quantity=3000), format="json").status_code, 201)
        self.close(transfer, 2000)
        line = transfer_lines().get(id=transfer["lines"][0]["id"])
        self.assertEqual(line.in_transit_milli, "0")
        self.assertIn(self.client.post(path, self.arrival(transfer, quantity=1000), format="json").status_code, (400, 409))
        self.assertEqual((self.stock(self.product), self.stock(self.target)), ("5000", "3000"))
        self.assertFalse(StockMovement.objects.filter(kind="transfer_return").exists())

    def test_closure_outcome_and_quantity_are_constrained(self):
        transfer, _ = self.depart()
        for quantity, outcome in ((0, "lost"), (1000, "returned")):
            with self.subTest(quantity=quantity, outcome=outcome), self.assertRaises(IntegrityError), transaction.atomic():
                self.close(transfer, quantity, outcome)


class TransferRemainderClosureApiTests(TransferApiTests):
    def closure(self, transfer, shop, **changes):
        data = {"operation_id": str(uuid.uuid4()), "outcome": "lost", "reason": "Colis perdu en route", **changes}
        url = self.url(shop, f"{transfer['id']}/lines/{transfer['lines'][0]['id']}/close/")
        return self.client.post(url, data, format="json"), data, url

    def test_destination_manager_closes_the_whole_remainder_once(self):
        transfer, _ = self.depart()
        self.client = self.authenticate(self.destination_manager)
        self.client.post(self.url(self.destination, transfer["id"] + "/receipts/"), self.arrival(transfer, quantity=3000), format="json")
        response, data, url = self.closure(transfer, self.destination)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["quantity_milli"], 2000)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 200)
        self.assertEqual(self.closure(transfer, self.destination)[0].status_code, 409)

    def test_source_manager_cannot_close_the_remainder(self):
        transfer, _ = self.depart()
        self.assertIn(self.closure(transfer, self.source)[0].status_code, (403, 404))
