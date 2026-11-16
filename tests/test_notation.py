"""数值写法裁定（TRAP-27）：语法唯一才取值，两种读法都成立则不计分。

规则：
- `1,204.5` 只有美式读法成立 → 1204.5；`1.204,5` 只有欧式读法成立 → 1204.5；
- `1,204` / `1.204` 两种读法都成立且数值不同 → 写法未定，不给值、不进
  缺失/格式/离群/评分，只留原值与行号；
- 全按一种写法解释会导致小数被放大/缩小，逐个猜测会让相似工单得到两个口径，
  两者都不允许。
"""
from __future__ import annotations

from decimal import Decimal

from app import config
from app.modules.m2_profile import formats
from app.modules.m5_outliers import flags
from app.modules.m6_score import score as score_module
from fixtures import dirty_cases


def col(name: str = "first_response_min") -> config.Column:
    return config.COLUMN_MAP[name]


def test_unambiguous_us_and_eu_land_on_same_value():
    """两种地区写法都能唯一确定小数位置，归一后必须相同。"""
    us = formats.parse_cell(col(), "1,204.5")
    eu = formats.parse_cell(col(), "1.204,5")
    assert us.state == eu.state == formats.VALUE
    assert us.value == eu.value == "1204.5"
    assert (us.notation, eu.notation) == (formats.NOTATION_US, formats.NOTATION_EU)
    assert us.as_dict()["notation"] == "us"


def test_ambiguous_grouped_value_is_notation_unknown():
    """`1,204` 可能是 1204 也可能是 1.204：不取值，两种读法都写进证据。"""
    for raw in ("1,204", "1.204"):
        cell = formats.parse_cell(col(), raw)
        assert cell.state == formats.NOTATION_UNKNOWN, raw
        assert cell.code == "K_NOTATION_UNKNOWN"
        assert cell.value is None
        assert "读作" in cell.detail


def test_ambiguous_readings_are_symmetric():
    """同一数值的两种写法得到同一结论，不因 token 是逗号还是点号而偏袒。"""
    comma = formats.parse_cell(col(), "1,204")
    dot = formats.parse_cell(col(), "1.204")
    assert comma.state == dot.state == formats.NOTATION_UNKNOWN
    assert "1204" in comma.detail and "1.204" in dot.detail


def test_one_reading_out_of_contract_does_not_pick_a_side():
    """一种读法合格、另一种越界，取值仍依赖未证实的写法 → 判写法未定。

    例：整数列 1-5 里的 `1.000` 可以读作 1，也可以读作千分位 1000（越界）。
    按列契约挑合格的那个就是替用户猜，这里一律不取值。
    """
    satisfaction = config.COLUMN_MAP["satisfaction"]
    for raw in ("1.000", "1,000"):
        cell = formats.parse_cell(satisfaction, raw)
        assert cell.state == formats.NOTATION_UNKNOWN, raw
        assert cell.value is None
    # 只有一种读法的普通整数照常取值
    plain = formats.parse_cell(satisfaction, "4.0")
    assert plain.state == formats.VALUE and plain.value == "4"


def test_both_readings_invalid_stays_format_bad():
    """两种读法都判不合格时，结论与写法无关 → 仍记格式不合格，不浪费样本。"""
    first = formats.parse_cell(col(), "-1.204")
    assert first.state == formats.FORMAT_BAD
    assert first.code == "K_OUT_OF_RANGE"


def test_unparseable_garbage_still_format_bad():
    cell = formats.parse_cell(col(), "12,34.5")
    assert cell.state == formats.FORMAT_BAD
    assert cell.code == "K_BAD_FORMAT"


def test_unknown_notation_excluded_from_all_score_dimensions():
    """未定值不进缺失、格式、离群的分母，也不当合格值。"""
    totals = {"cells_total": 100, "cells_missing": 10,
              "cells_notation_unknown": 5, "format_bad": 4,
              "rows_accepted": 10, "rows_dup": 1,
              "outlier_cells": 2, "tested_cells": 50}
    evaluated = score_module.evaluate(totals)
    dims = {item["key"]: item for item in evaluated["dims"]}
    assert dims["missing"]["denominator"] == 95   # 100 − 5
    assert dims["missing"]["numerator"] == 10
    assert dims["format"]["denominator"] == 85    # 95 − 10
    assert dims["outlier"]["denominator"] == 50
    assert evaluated["assessable"]["cells_assessable"] == 95
    assert evaluated["assessable"]["cells_notation_unknown"] == 5
    assert Decimal(evaluated["score"]) + Decimal(evaluated["penalty_sum"]) == 100


def test_low_coverage_is_qualified_not_silently_passed():
    """排除格太多时分数照算，但结论必须降级为「仅覆盖可评格」。"""
    low = score_module.evaluate({
        "cells_total": 100, "cells_missing": 0, "cells_notation_unknown": 30,
        "format_bad": 0, "rows_accepted": 10, "rows_dup": 0,
        "outlier_cells": 0, "tested_cells": 60})
    assert low["qualified"] is False
    assert low["assessable"]["coverage"] == "0.7000"
    assert "写法未定" in low["qualifiers"][0]

    enough = score_module.evaluate({
        "cells_total": 100, "cells_missing": 0, "cells_notation_unknown": 2,
        "format_bad": 0, "rows_accepted": 10, "rows_dup": 0,
        "outlier_cells": 0, "tested_cells": 60})
    assert enough["qualified"] is True
    assert enough["qualifiers"] == []


def test_pipeline_partition_closes_with_unknown(settings):
    text = dirty_cases.frame([
        dirty_cases.make_row(ticket="T-2026-910001", customer="00901", response="1,204"),
        dirty_cases.make_row(ticket="T-2026-910002", customer="00902", response="1.204"),
        dirty_cases.make_row(ticket="T-2026-910003", customer="00903", response="20.0"),
        dirty_cases.make_row(ticket="T-2026-910004", customer="00904", response="30.0"),
        dirty_cases.make_row(ticket="T-2026-910005", customer="00905", response="40.0"),
        dirty_cases.make_row(ticket="T-2026-910006", customer="00906", response="50.0"),
    ])
    payload = dirty_cases.pipeline(settings, text)
    counts = payload["profile"]["counts"]
    assert counts["cells_notation_unknown"] == 2
    assert counts["cells_total"] == (counts["cells_missing"]
                                     + counts["cells_notation_unknown"]
                                     + counts["cells_value"])
    assert counts["cells_assessable"] == counts["cells_total"] - 2
    evidence = payload["profile"]["notation"]["cells"]
    assert [(item["lineno"], item["raw"]) for item in evidence] == [
        (2, "1,204"), (3, "1.204")]


def test_unknown_notation_not_tested_as_outlier(settings):
    text = dirty_cases.frame([
        dirty_cases.make_row(ticket=f"T-2026-92{index:04d}", customer=f"00{920 + index}",
                             response=f"{20 + index}.0")
        for index in range(6)
    ] + [dirty_cases.make_row(ticket="T-2026-929999", customer="00999",
                              response="1,204")])
    payload = dirty_cases.pipeline(settings, text)
    column = {item["name"]: item for item in payload["outliers"]["columns"]}[
        "first_response_min"]
    assert column["notation_unknown"] == 1
    assert column["values"] == 6
    assert column["tested"] == 6
    assert payload["outliers"]["counts"]["notation_excluded_cells"] == 1
    assert all(item["raw"] != "1,204" for item in payload["outliers"]["cells"])


def test_unknown_notation_keeps_duplicate_hash_distinct(settings):
    """都「未定」但原文不同，不能因为都不取值就被合并成同一行。"""
    base = dict(ticket="T-2026-930001", customer="00931")
    payload = dirty_cases.pipeline(settings, dirty_cases.frame([
        dirty_cases.make_row(**base, response="1,204"),
        dirty_cases.make_row(**base, response="1.204"),
        dirty_cases.make_row(**base, response="1,204"),
    ]))
    groups = payload["duplicates"]["hash_groups"]
    assert sorted(group["count"] for group in groups) == [2]
    assert payload["duplicates"]["duplicate_rows"] == 1


def test_profile_counts_unknown_separately(settings):
    text = dirty_cases.frame([
        dirty_cases.make_row(ticket="T-2026-940001", customer="00941", response="1,204"),
        dirty_cases.make_row(ticket="T-2026-940002", customer="00942", response="20.0"),
    ])
    profile = dirty_cases.profile(settings, text)
    column = {item["name"]: item for item in profile["profile"]}["first_response_min"]
    assert (column["present"], column["filled"], column["notation_unknown"]) == (2, 1, 1)
    assert column["notation_unknown_rate"] == Decimal("0.5000")
    assert column["format_rate"] == Decimal("0.0000")


def test_code_present_in_catalog():
    from app import reasons
    assert reasons.label("K_NOTATION_UNKNOWN")
