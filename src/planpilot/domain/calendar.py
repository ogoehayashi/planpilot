"""Deterministic minute-level coverage and overtime accounting for the solver."""
from __future__ import annotations


def covered(rows: list[dict], start: int, end: int) -> bool:
    cursor = start
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        if row["end"] <= cursor:
            continue
        if row["start"] > cursor:
            return False
        cursor = max(cursor, row["end"])
        if cursor >= end:
            return True
    return cursor >= end


def merged_intervals(rows: list[dict]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        start, end = row["start"], row["end"]
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def overtime_by_day(shifts: tuple[dict, ...], worker_id: str | None,
                    start: int, end: int) -> dict[int, int]:
    if worker_id is None or not shifts:
        return {}
    rows = [row for row in shifts if row.get("worker_id") == worker_id]
    regular = [row for row in rows if row.get("window_type", "REGULAR") == "REGULAR"]
    overtime = [row for row in rows if row.get("window_type") == "OVERTIME"]
    counts: dict[int, int] = {}
    for minute in range(start, end):
        if any(row["start"] <= minute < row["end"] for row in regular):
            continue
        if any(row["start"] <= minute < row["end"] for row in overtime):
            day = minute // 1440
            counts[day] = counts.get(day, 0) + 1
    return counts

