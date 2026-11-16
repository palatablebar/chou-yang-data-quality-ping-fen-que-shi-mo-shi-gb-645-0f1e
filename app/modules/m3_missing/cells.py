"""单元格账：把「单元格 = 有值 + 写法未定 + 缺失」拆到列与行两层。

缺失的定义只有一个入口（M2 formats 的四态判定）：空串、空白串、哨兵都是缺失，
`0` 与 `"0"` 是有值。写法未定（`1,204` 这类逗号既可能是千分位也可能是小数点）
单列一账：它既不是缺失也不是合格值，不参与缺失率与格式率，但占着单元格总数，
所以行账与列账必须把它算进分母，任何一边对不上都说明有单元格被重复计数。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config


def is_missing(cell: dict) -> bool:
    return cell["state"] == "missing"


def is_notation_unknown(cell: dict) -> bool:
    return cell["state"] == "notation_unknown"


def is_assessable(cell: dict) -> bool:
    """可评格：缺失与写法未定之外的格。"""
    return cell["state"] not in ("missing", "notation_unknown")


def account(profile: dict) -> dict:
    rows = profile["rows"]
    columns = [column["name"] for column in profile["columns"]]
    labels = {column["name"]: column["label"] for column in profile["columns"]}
    by_column: dict[str, dict] = {
        name: {"name": name, "label": labels[name], "missing": 0, "filled": 0,
               "notation_unknown": 0}
        for name in columns
    }
    by_row: list[dict] = []
    missing_total = 0
    unknown_total = 0
    for row in rows:
        row_missing = 0
        row_unknown = 0
        for name in columns:
            cell = row["cells"][name]
            if is_missing(cell):
                by_column[name]["missing"] += 1
                row_missing += 1
            elif is_notation_unknown(cell):
                by_column[name]["notation_unknown"] += 1
                row_unknown += 1
            else:
                by_column[name]["filled"] += 1
        missing_total += row_missing
        unknown_total += row_unknown
        by_row.append({"lineno": row["lineno"], "missing": row_missing,
                       "notation_unknown": row_unknown,
                       "total": len(columns)})

    total_cells = len(rows) * len(columns)
    column_rows = []
    for name in columns:
        item = by_column[name]
        item["total"] = (item["missing"] + item["filled"]
                         + item["notation_unknown"])
        item["missing_rate"] = _rate(item["missing"], item["total"])
        column_rows.append(item)
    row_missing_sum = sum(item["missing"] for item in by_row)
    row_unknown_sum = sum(item["notation_unknown"] for item in by_row)
    return {
        "cells_total": total_cells,
        "missing": missing_total,
        "notation_unknown": unknown_total,
        "filled": total_cells - missing_total - unknown_total,
        "assessable": total_cells - unknown_total,
        "by_column": column_rows,
        "by_row": by_row,
        "closed": (missing_total + unknown_total
                   + (total_cells - missing_total - unknown_total) == total_cells
                   and row_missing_sum == missing_total
                   and row_unknown_sum == unknown_total
                   and sum(item["missing"] for item in column_rows) == missing_total
                   and sum(item["notation_unknown"] for item in column_rows)
                   == unknown_total),
    }


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def cell_evidence(profile: dict, column: str, limit: int = 60) -> list[dict]:
    """逐格证据：行号 + 原值 + 原因码，导出与页面共用。"""
    evidence = []
    for row in profile["rows"]:
        cell = row["cells"][column]
        if is_missing(cell):
            evidence.append({"lineno": row["lineno"], "column": column,
                             "raw": cell["raw"], "code": cell["code"]})
        if len(evidence) >= limit:
            break
    return evidence
