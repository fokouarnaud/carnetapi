from rest_framework.exceptions import APIException


class SchedulerUnavailable(APIException):
    status_code = 503
    default_detail = {"code": "backup_scheduler_unavailable"}
    default_code = "backup_scheduler_unavailable"
