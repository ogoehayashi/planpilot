from planpilot.domain.lots import split_lots


def test_lots_reconcile_without_optional_solver_splitting():
    lots = split_lots("O-1", 25, 10)
    assert [(lot.lot_no, lot.quantity) for lot in lots] == [(1, 10), (2, 10), (3, 5)]
    assert sum(lot.quantity for lot in lots) == 25
