"""Execute one daily backup step while participating in restore maintenance."""
import json

from django.core.management.base import BaseCommand, CommandError

from backups.services.scheduler import scheduler_tick
from backups.services.scheduler_errors import SchedulerUnavailable


class Command(BaseCommand):
    help="Run one protected daily backup step, using the configured technical account."
    requires_system_checks=[]

    def handle(self,*args,**options):
        try:
            status=scheduler_tick()
        except SchedulerUnavailable:
            raise CommandError("backup_scheduler_unavailable") from None
        self.stdout.write(json.dumps({"status":status}))
