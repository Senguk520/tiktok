"""Currency-aware Decimal profit calculations with explicit rounding rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

_CURRENCY_MINOR_UNITS = {
    "CNY": 2,
    "IDR": 0,
    "MYR": 2,
    "PHP": 2,
    "SGD": 2,
    "THB": 2,
    "USD": 2,
    "VND": 0,
}
_RATE_QUANTUM = Decimal("0.000001")
_RATIO_QUANTUM = Decimal("0.0001")
_HUNDRED = Decimal("100")
_ONE = Decimal("1")
_ZERO = Decimal("0")


class ProfitInputError(ValueError):
    pass


def _decimal(value: Decimal | str | int, *, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProfitInputError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise ProfitInputError(f"{field} must be finite")
    return parsed


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in _CURRENCY_MINOR_UNITS:
        raise ProfitInputError("currency is not enabled")
    return currency


def money_quantum(currency: str) -> Decimal:
    units = _CURRENCY_MINOR_UNITS[normalize_currency(currency)]
    return Decimal(1).scaleb(-units)


def round_money(value: Decimal, currency: str) -> Decimal:
    return value.quantize(money_quantum(currency), rounding=ROUND_HALF_UP)


def convert_currency(
    amount: Decimal | str | int,
    rate: Decimal | str | int,
    *,
    source_currency: str,
    target_currency: str,
) -> Decimal:
    source = normalize_currency(source_currency)
    target = normalize_currency(target_currency)
    parsed_amount = _decimal(amount, field="amount")
    parsed_rate = _decimal(rate, field="exchange_rate")
    if parsed_amount < _ZERO or parsed_rate <= _ZERO:
        raise ProfitInputError("amount must be non-negative and exchange rate positive")
    if source == target and parsed_rate != _ONE:
        raise ProfitInputError("same-currency exchange rate must equal one")
    return round_money(parsed_amount * parsed_rate, target)


@dataclass(frozen=True, slots=True)
class ProfitInputs:
    product_cost: Decimal
    source_currency: str
    settlement_currency: str
    exchange_rate: Decimal
    shipping_cost: Decimal
    other_fixed_cost: Decimal
    commission_rate: Decimal
    payment_fee_rate: Decimal
    target_margin_rate: Decimal

    def __post_init__(self) -> None:
        source = normalize_currency(self.source_currency)
        settlement = normalize_currency(self.settlement_currency)
        values = {
            "product_cost": _decimal(self.product_cost, field="product_cost"),
            "exchange_rate": _decimal(self.exchange_rate, field="exchange_rate"),
            "shipping_cost": _decimal(self.shipping_cost, field="shipping_cost"),
            "other_fixed_cost": _decimal(self.other_fixed_cost, field="other_fixed_cost"),
            "commission_rate": _decimal(self.commission_rate, field="commission_rate"),
            "payment_fee_rate": _decimal(self.payment_fee_rate, field="payment_fee_rate"),
            "target_margin_rate": _decimal(self.target_margin_rate, field="target_margin_rate"),
        }
        if values["product_cost"] < _ZERO:
            raise ProfitInputError("product cost cannot be negative")
        if values["exchange_rate"] <= _ZERO:
            raise ProfitInputError("exchange rate must be positive")
        if source == settlement and values["exchange_rate"] != _ONE:
            raise ProfitInputError("same-currency exchange rate must equal one")
        if values["shipping_cost"] < _ZERO or values["other_fixed_cost"] < _ZERO:
            raise ProfitInputError("fixed costs cannot be negative")
        for name in ("commission_rate", "payment_fee_rate", "target_margin_rate"):
            if not _ZERO <= values[name] < _ONE:
                raise ProfitInputError(f"{name} must be in the range [0, 1)")
        combined = (
            values["commission_rate"]
            + values["payment_fee_rate"]
            + values["target_margin_rate"]
        )
        if combined >= _ONE:
            raise ProfitInputError("combined fee and target margin rates must be below one")
        object.__setattr__(self, "source_currency", source)
        object.__setattr__(self, "settlement_currency", settlement)
        for name, value in values.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ProfitEstimate:
    settlement_currency: str
    converted_product_cost: Decimal
    total_fixed_cost: Decimal
    suggested_price: Decimal
    commission_amount: Decimal
    payment_fee_amount: Decimal
    estimated_profit: Decimal
    realized_margin_rate: Decimal
    exchange_rate: Decimal

    @property
    def realized_margin_percent(self) -> Decimal:
        return (self.realized_margin_rate * _HUNDRED).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


def estimate_profit(inputs: ProfitInputs) -> ProfitEstimate:
    with localcontext() as context:
        context.prec = 34
        converted = convert_currency(
            inputs.product_cost,
            inputs.exchange_rate,
            source_currency=inputs.source_currency,
            target_currency=inputs.settlement_currency,
        )
        fixed = round_money(
            converted + inputs.shipping_cost + inputs.other_fixed_cost,
            inputs.settlement_currency,
        )
        denominator = _ONE - (
            inputs.commission_rate + inputs.payment_fee_rate + inputs.target_margin_rate
        )
        suggested = round_money(fixed / denominator, inputs.settlement_currency)
        commission = round_money(
            suggested * inputs.commission_rate,
            inputs.settlement_currency,
        )
        payment_fee = round_money(
            suggested * inputs.payment_fee_rate,
            inputs.settlement_currency,
        )
        profit = round_money(
            suggested - fixed - commission - payment_fee,
            inputs.settlement_currency,
        )
        margin = (
            _ZERO
            if suggested == _ZERO
            else (profit / suggested).quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)
        )
    return ProfitEstimate(
        settlement_currency=inputs.settlement_currency,
        converted_product_cost=converted,
        total_fixed_cost=fixed,
        suggested_price=suggested,
        commission_amount=commission,
        payment_fee_amount=payment_fee,
        estimated_profit=profit,
        realized_margin_rate=margin,
        exchange_rate=inputs.exchange_rate.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP),
    )


def profit_for_price(inputs: ProfitInputs, selling_price: Decimal | str | int) -> ProfitEstimate:
    price = round_money(_decimal(selling_price, field="selling_price"), inputs.settlement_currency)
    if price < _ZERO:
        raise ProfitInputError("selling price cannot be negative")
    converted = convert_currency(
        inputs.product_cost,
        inputs.exchange_rate,
        source_currency=inputs.source_currency,
        target_currency=inputs.settlement_currency,
    )
    fixed = round_money(
        converted + inputs.shipping_cost + inputs.other_fixed_cost,
        inputs.settlement_currency,
    )
    commission = round_money(price * inputs.commission_rate, inputs.settlement_currency)
    payment_fee = round_money(price * inputs.payment_fee_rate, inputs.settlement_currency)
    profit = round_money(price - fixed - commission - payment_fee, inputs.settlement_currency)
    margin = (
        _ZERO
        if price == _ZERO
        else (profit / price).quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)
    )
    return ProfitEstimate(
        settlement_currency=inputs.settlement_currency,
        converted_product_cost=converted,
        total_fixed_cost=fixed,
        suggested_price=price,
        commission_amount=commission,
        payment_fee_amount=payment_fee,
        estimated_profit=profit,
        realized_margin_rate=margin,
        exchange_rate=inputs.exchange_rate.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP),
    )