"""One fee interpretation for broker reports and the analytical journal."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def commission_amount(raw: object) -> Decimal | None:
    """Preserve unknown fees; an explicitly encoded numeric zero is evidence.

    Sandbox can return currency='' with units='0', nano=0. Zero has no FX
    conversion, unlike a nonzero amount with an unspecified/foreign currency.
    A missing message or one without numeric fields is NOT this case.
    """
    if not isinstance(raw, Mapping) or not {'units', 'nano'}.intersection(raw):
        return None
    try:
        units = Decimal(str(raw.get('units', '0')))
        nano = Decimal(str(raw.get('nano', 0)))
        if (not units.is_finite() or not nano.is_finite()
                or units != units.to_integral_value() or nano != nano.to_integral_value()
                or not -(2**63) <= units < 2**63 or abs(nano) >= 1_000_000_000
                or units * nano < 0):
            return None
        amount = units + nano / 1_000_000_000
    except (InvalidOperation, ValueError, TypeError):
        return None
    currency = raw.get('currency', 'rub')
    if not isinstance(currency, str):
        return None
    if currency.lower() != 'rub' and not (currency == '' and amount == 0):
        return None
    return abs(amount)
