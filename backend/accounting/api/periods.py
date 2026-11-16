"""Q108: an export period is a range of local days, both included, as a UTC interval."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework import serializers

from common.api.fields import StrictSerializer

MAX_PERIOD_DAYS = 366


class PeriodQuerySerializer(StrictSerializer):
    start = serializers.DateField()
    end = serializers.DateField()
    as_of = serializers.DateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def validate_as_of(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Instantané futur non autorisé.")
        return value

    def validate(self, data):
        if data["end"] < data["start"]:
            raise serializers.ValidationError({"end": "La fin précède le début."})
        if (data["end"] - data["start"]).days >= MAX_PERIOD_DAYS:
            raise serializers.ValidationError({"end": "Période limitée à 366 jours."})
        return data


def utc_bounds(shop, start, end):
    """First local day at 00:00 included, the day after the last one excluded."""
    zone = ZoneInfo(shop.timezone)
    begin = datetime.combine(start, time.min, tzinfo=zone)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
    return begin.astimezone(ZoneInfo("UTC")), finish.astimezone(ZoneInfo("UTC"))


def local_date(shop, instant):
    return instant.astimezone(ZoneInfo(shop.timezone)).date().isoformat()
