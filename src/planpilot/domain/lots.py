"""Fixed max-size lot decomposition; solver-chosen splitting is deliberately absent."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Lot:
    order_id: str
    lot_no: int
    quantity: int


def split_lots(order_id: str, quantity: int, max_lot_size: int) -> tuple[Lot, ...]:
    """Return a stable 1..N decomposition whose quantities reconcile exactly."""
    if not isinstance(order_id, str) or not order_id or type(quantity) is not int or quantity <= 0 or type(max_lot_size) is not int or max_lot_size <= 0:
        raise ValueError("order_id, quantity and max_lot_size must be valid positive values")
    count = (quantity + max_lot_size - 1) // max_lot_size
    return tuple(Lot(order_id, index, min(max_lot_size, quantity - (index - 1) * max_lot_size)) for index in range(1, count + 1))
