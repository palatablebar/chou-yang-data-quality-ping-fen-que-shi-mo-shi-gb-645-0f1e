"""评分：四个维度各自封顶，总分 = 100 − 各维扣分。

每维扣分 = 封顶分 × min(1, 实际比率 / 容忍度)，量化到 0.01（ROUND_HALF_UP）。
封顶是硬上限：比率再高也不会把该维扣成负分，所以总分恒在 [0, 100]，
且「总分 + 扣分之和 = 100」逐位成立。

写法未定的格不进任何维度的分子或分母（TRAP-27）：它既不算缺失、也不算
格式错误、更不参与离群。可评格 = 单元格 − 写法未定，缺失维与格式维的分母
都是可评格，这样分数只描述已证实的样本。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config
from . import grade

ZERO = Decimal("0")


def rate(numerator: int, denominator: int) -> Decimal:
    if denominator is None or denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def assessable_cells(counts: dict) -> int:
    """可评格：单元格总数 − 写法未定。缺失格仍属可评，只是按缺失维计。"""
    total = counts.get("cells_total", 0)
    unknown = counts.get("cells_notation_unknown", 0)
    return max(total - unknown, 0)


def format_cells(counts: dict) -> int:
    """格式维分母：可评且非缺失的格。"""
    return max(assessable_cells(counts) - counts.get("cells_missing", 0), 0)


def ratios(counts: dict) -> dict:
    return {
        "missing": rate(counts.get("cells_missing", 0), assessable_cells(counts)),
        "duplicate": rate(counts.get("rows_dup", 0), counts.get("rows_accepted", 0)),
        "outlier": rate(counts.get("outlier_cells", 0), counts.get("tested_cells", 0)),
        "format": rate(counts.get("format_bad", 0), format_cells(counts)),
    }


def penalties(counts: dict) -> list[dict]:
    actual = ratios(counts)
    numerators = {
        "missing": counts.get("cells_missing", 0),
        "duplicate": counts.get("rows_dup", 0),
        "outlier": counts.get("outlier_cells", 0),
        "format": counts.get("format_bad", 0),
    }
    denominators = {
        "missing": assessable_cells(counts),
        "duplicate": counts.get("rows_accepted", 0),
        "outlier": counts.get("tested_cells", 0),
        "format": format_cells(counts),
    }
    rows = []
    for item in config.WEIGHTS:
        key = item["key"]
        cap = Decimal(item["cap"])
        tolerance = Decimal(item["tolerance"])
        value = actual[key]
        ratio = (value / tolerance) if tolerance > 0 else ZERO
        capped = ratio > 1
        ratio = min(ratio, Decimal("1"))
        penalty = (cap * ratio).quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)
        severity = grade.severity(penalty, cap)
        rows.append({
            "key": key,
            "label": item["label"],
            "cap": str(cap),
            "tolerance": str(tolerance),
            "numerator": numerators[key],
            "denominator": denominators[key],
            "rate": str(value),
            "ratio": str(ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "penalty": str(penalty),
            "capped": capped,
            "severity": severity["key"],
            "severity_label": severity["label"],
        })
    return rows


def evaluate(counts: dict) -> dict:
    dims = penalties(counts)
    penalty_sum = sum((Decimal(item["penalty"]) for item in dims), ZERO)
    penalty_sum = penalty_sum.quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)
    total = (config.HUNDRED - penalty_sum).quantize(config.SCORE_QUANT,
                                                   rounding=ROUND_HALF_UP)
    total = min(max(total, ZERO), config.HUNDRED)
    graded = grade.of(total)
    total_cells = counts.get("cells_total", 0)
    unknown = counts.get("cells_notation_unknown", 0)
    assessable = max(total_cells - unknown, 0)
    coverage = rate(assessable, total_cells)
    qualified = coverage >= config.NOTATION_COVERAGE_MIN
    return {
        "score": str(total),
        "grade": graded["grade"],
        "grade_label": graded["label"],
        "grade_floor": graded["floor"],
        "penalty_sum": str(penalty_sum),
        "dims": dims,
        "bands": grade.bands(dims),
        "closed": (total + penalty_sum == config.HUNDRED),
        "qualified": qualified,
        "qualifiers": ([] if qualified else [
            f"可评覆盖率 {coverage} 低于下限 {config.NOTATION_COVERAGE_MIN}，"
            f"{unknown} 格写法未定未计分"]),
        "assessable": {
            "cells_total": total_cells,
            "cells_assessable": assessable,
            "cells_notation_unknown": unknown,
            "cells_missing": counts.get("cells_missing", 0),
            "tested_cells": counts.get("tested_cells", 0),
            "coverage": str(coverage),
            "coverage_min": str(config.NOTATION_COVERAGE_MIN),
            "qualified": qualified,
            "notation_policy": config.NOTATION_POLICY,
            "note": "缺失维分母 = 可评格（单元格 − 写法未定）；格式维分母 = 可评且"
                    "非缺失格。写法未定的格不计入任何比率，评分只覆盖已证实的样本",
        },
        "weights": [{"key": item["key"], "label": item["label"], "cap": item["cap"],
                     "tolerance": item["tolerance"]} for item in config.WEIGHTS],
        "formula": "100 − 缺失30 − 重复25 − 离群20 − 格式25，每维按容忍度封顶",
    }
