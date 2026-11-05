from customers.models import CustomerPaymentPart
from sales.models import SalePayment


def possible_duplicate_reference(shop, part):
    reference = part["external_reference"]
    if not reference:
        return False
    fields = {"external_reference": reference, "method": part["method"], "other_name": part["other_name"]}
    return (SalePayment.objects.filter(sale__shop=shop, **fields).exists()
            or CustomerPaymentPart.objects.filter(payment__shop=shop, **fields).exists())
