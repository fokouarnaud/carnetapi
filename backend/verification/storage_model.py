"""Interpret measured bytes without conflating initial budget and annual hosting."""
from dataclasses import dataclass

INITIAL_BUDGET_BYTES = 500000000


def _bytes(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("invalid_byte_measurement")
    return value


def initial_budget_status(known_bytes, *, inventory_complete):
    if type(inventory_complete) is not bool:
        raise ValueError("invalid_inventory_state")
    if known_bytes is None:
        return "not_demonstrated"
    if _bytes(known_bytes) >= INITIAL_BUDGET_BYTES:
        return "exceeds_limit"
    return "within_limit" if inventory_complete else "not_demonstrated"


def annual_floor_status(database_bytes, *, host_limit_bytes):
    _bytes(database_bytes)
    if host_limit_bytes is None:
        return "host_limit_unknown"
    if database_bytes > _bytes(host_limit_bytes, positive=True):
        return "floor_exceeds_host_limit"
    return "capacity_not_demonstrated"


@dataclass(frozen=True)
class Workload:
    shops: int = 2
    products_per_shop: int = 1000
    devices_per_shop: int = 3
    sales_per_shop_day: int = 100
    lines_per_sale: int = 5
    days: int = 365

    def __post_init__(self):
        for value in self.__dict__.values():
            _bytes(value, positive=True)

    @property
    def sales(self):
        return self.shops*self.sales_per_shop_day*self.days

    @property
    def lines(self):
        return self.sales*self.lines_per_sale

    @property
    def products(self):
        return self.shops*self.products_per_shop

    @property
    def devices(self):
        return self.shops*self.devices_per_shop
