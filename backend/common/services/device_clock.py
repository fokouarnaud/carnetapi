"""Device clocks run ahead of the server by a few seconds; both sides are UTC.

Decision of 13 November 2026: an instant recorded by a phone may be up to
five minutes ahead of the server before it is refused as in the future.
"""
from datetime import timedelta

DEVICE_CLOCK_TOLERANCE = timedelta(minutes=5)
