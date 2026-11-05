"""Execute one hosting reminder under the normal database maintenance barrier."""
import json
from django.core.management.base import BaseCommand, CommandError
from backups.services.hosting import hosting_tick, HostingUnavailable


class Command(BaseCommand):
    help="Run one protected hosting-expiration reminder step; never renew hosting."
    requires_system_checks=[]

    def handle(self,*args,**options):
        try:status=hosting_tick()
        except HostingUnavailable:raise CommandError("hosting_reminder_unavailable") from None
        self.stdout.write(json.dumps({"status":status}))
