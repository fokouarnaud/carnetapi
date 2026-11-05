"""Stable public errors for contradictory protected origin references."""
from rest_framework import serializers


class OriginReferenceError(serializers.Serializer):
    detail=serializers.CharField()
    code=serializers.ChoiceField(choices=["origin_reference_conflict"])
