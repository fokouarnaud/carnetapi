"""Stable conflicts for attempts to append a second commercial reversal."""
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import APIException


class SaleCancelledConflict(APIException):
    status_code = 409
    default_code = "sale_cancelled"
    default_detail = _("La vente est déjà annulée.")
