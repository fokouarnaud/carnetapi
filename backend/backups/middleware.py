"""WSGI maintenance barrier, before sessions and authentication touch SQLite."""
from django.conf import settings
from django.db import connections
from django.http import JsonResponse,HttpResponseRedirect
import re

from .services.restore_gate import acquire_request, acquire_recovery_request, RestoreGateError,RestoreGateClosed
from .services.recovery_web import recovery_identity


def recovery_path_allowed(request):
    path=request.path_info
    if path in ("/admin/","/admin/backups/restoreaccessreview/",
            "/admin/backups/restoresession/","/admin/backups/recoverydevice/"):
        return request.method=="GET"
    if path=="/admin/login/":
        return request.method in ("GET","POST")
    if path=="/admin/logout/":
        return request.method=="POST"
    return request.method in ("GET","POST") and re.fullmatch(
        r"/admin/backups/restoreaccessreview/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/control/",path) is not None


class RestoreGateMiddleware:
    """Keep the lease through response and stream cleanup, including disconnects."""
    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = settings.RESTORE_GATE_PATH
        if path is None:  # Explicit isolated-test setting; no environment bypass.
            return self.get_response(request)
        try:
            database=settings.DATABASES["default"]["NAME"]
            try:
                lease = acquire_request(path,database)
            except RestoreGateClosed:
                options=getattr(settings,"RESTORE_RECOVERY",None)
                if not isinstance(options,dict) or not recovery_path_allowed(request):
                    raise
                try:
                    lease=acquire_recovery_request(path,database,expected_version=options["gate_version"])
                except (KeyError,TypeError):
                    raise RestoreGateError("restore_recovery_configuration_invalid") from None
                try:
                    identity=recovery_identity(database,options["vault"],bootstrap_operation=options["bootstrap_operation"],
                        gate_version=options["gate_version"],backup_root=settings.BACKUP_ROOT)
                except BaseException as error:
                    lease.close()
                    if isinstance(error,(KeyError,TypeError)):
                        raise RestoreGateError("restore_recovery_configuration_invalid") from None
                    raise
                request.restore_recovery_user_id=identity["user_id"]
                request.restore_recovery_generation_id=identity["generation_id"]
        except RestoreGateError:
            response = JsonResponse({"code": "service_unavailable", "detail": {
                "fr": "Service temporairement indisponible.",
                "en": "Service temporarily unavailable."}}, status=503)
            response["Cache-Control"] = "no-store"
            response["Retry-After"] = "30"
            return response

        def release():
            try:
                # Do not leave reusable connections pointing at an old inode.
                connections.close_all()
            finally:
                lease.close()

        try:
            if getattr(request,"restore_recovery_user_id",None) is not None and request.path_info=="/admin/":
                response=HttpResponseRedirect("/admin/backups/restoreaccessreview/")
            else:
                response = self.get_response(request)
            # Existing resource closers (notably stream generators) finish first.
            response._resource_closers.append(release)
            return response
        except BaseException:
            release()
            raise
