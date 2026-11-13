"""G26: the server attributes installation codes; rows are never deleted, so numbers never return."""
from django.db import transaction
from django.db.models import Max

from identity.models import ShopInstallationCode


@transaction.atomic
def assign_installation_code(shop, installation):
    existing = ShopInstallationCode.objects.filter(shop=shop, installation=installation).first()
    if existing is not None:
        return existing
    last = ShopInstallationCode.objects.filter(shop=shop).aggregate(last=Max("number"))["last"] or 0
    return ShopInstallationCode.objects.create(shop=shop, installation=installation, number=last + 1)
