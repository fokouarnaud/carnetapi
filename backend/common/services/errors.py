from rest_framework.exceptions import APIException

class OperationConflict(APIException):
    status_code = 409
    default_detail = "Cet UUID d'opération est déjà associé à une autre demande."
    default_code = "operation_conflict"
