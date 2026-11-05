from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from customers.models import CustomerPayment
from customers.api.payment_serializers import CreateCustomerPaymentSerializer, CustomerPaymentSerializer, PaymentPreviewRequestSerializer, PaymentPreviewSerializer
from customers.services.payment_services import create_customer_payment, payment_context, payment_preview
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError


class CustomerPaymentViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = CustomerPayment.objects.none()
    serializer_class = CustomerPaymentSerializer

    def context(self, write=False):
        return payment_context(self.kwargs["shop_id"], self.kwargs["client_id"], self.request.user,
                               self.request.device_session.installation, write=write)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CustomerPayment.objects.none()
        shop, customer = self.context()
        return with_public_origin(CustomerPayment.objects.filter(shop=shop, customer=customer)).prefetch_related("parts", "allocations").order_by("occurred_at", "id")

    @extend_schema(responses={200:CustomerPaymentSerializer(many=True),409:OriginReferenceError})
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(request=CreateCustomerPaymentSerializer, responses={200: CustomerPaymentSerializer, 201: CustomerPaymentSerializer})
    def create(self, request, *args, **kwargs):
        self.context(write=True)
        serializer = CreateCustomerPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_customer_payment(
            self.kwargs["shop_id"], self.kwargs["client_id"], request.user, request.device_session.installation,
            request.device_session.id, serializer.validated_data,
        )
        return Response(receipt, status=201 if created else 200)

    @extend_schema(parameters=[PaymentPreviewRequestSerializer], responses=PaymentPreviewSerializer)
    def preview(self, request, *args, **kwargs):
        shop, customer = self.context(write=True)
        serializer = PaymentPreviewRequestSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(PaymentPreviewSerializer(payment_preview(shop, customer, serializer.validated_data["amount_fcfa"])).data)
