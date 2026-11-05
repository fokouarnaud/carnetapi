"""Read customer obligations using the current shop's debt permissions."""
from rest_framework import mixins, viewsets

from customers.models import CustomerCredit
from customers.services.payment_services import payment_context
from sales.api.sale_serializers import CustomerCreditSerializer


class CustomerCreditViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = CustomerCredit.objects.none()
    serializer_class = CustomerCreditSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CustomerCredit.objects.none()
        shop, customer = payment_context(
            self.kwargs["shop_id"], self.kwargs["client_id"], self.request.user,
            self.request.device_session.installation,
        )
        return CustomerCredit.objects.filter(shop=shop, customer=customer).order_by("occurred_at", "id")
