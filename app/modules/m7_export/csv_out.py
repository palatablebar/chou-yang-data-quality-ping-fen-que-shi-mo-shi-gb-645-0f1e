"""CSV 导出：页面上的每个数都能在这里找到对应的原始行与原因码。

约定：UTF-8 带 BOM（Excel 直接打开不乱码）、CRLF、Decimal 按列刻度输出为
字符串、行号是源文件物理行号。
"""
from __future__ import annotations

import csv
import io

BOM = "\ufeff"


def csv_text(header: list[str], rows: list[list]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return BOM + buffer.getvalue()


def report_csv(result: dict) -> str:
    score = result["score"]
    counters = result["manifest"]["counters"]
    assessable = score.get("assessable", {})
    notation = result.get("profile", {}).get("notation") or {}
    rows = [
        ["总分", score["score"], "分", "刻度 0.01；仅覆盖可评格"],
        ["等级", score["grade"], "", score["grade_label"]],
        ["结论覆盖", "完整" if assessable.get("qualified", True) else "仅覆盖可评格", "",
         "；".join(score.get("qualifiers", [])) or "可评覆盖率达标"],
        ["扣分合计", score["penalty_sum"], "分", "总分 + 扣分 = 100"],
    ]
    for item in score["dims"]:
        rows.append([f"{item['label']}扣分", item["penalty"], "分",
                     f"比率 {item['rate']} 容忍度 {item['tolerance']} 封顶 {item['cap']}"])
    rows.extend([
        ["原始记录", counters["raw_records"], "行", "含空行与隔离行"],
        ["采纳行", counters["accepted"], "行", ""],
        ["唯一行", counters["rows_unique"], "行", "全行哈希去重后"],
        ["重复行", counters["rows_dup"], "行", ""],
        ["隔离行", counters["quarantine"], "行", ""],
        ["空行", counters["blank"], "行", ""],
        ["单元格", counters["cells_total"], "格", ""],
        ["可评单元格", counters.get("cells_assessable", assessable.get("cells_assessable", "")),
         "格", "单元格 − 写法未定"],
        ["排除单元格", counters.get("cells_excluded", ""), "格",
         "写法未定；缺失仍按缺失维计入，不在此排除"],
        ["缺失单元格", counters["cells_missing"], "格", ""],
        ["写法未定单元格", counters.get("cells_notation_unknown", 0), "格",
         f"写法未定，未参与评分；裁定规则 {assessable.get('notation_policy', '')}"],
        ["离群单元格", counters["outlier_cells"], "格", "有值的子集"],
    ])
    return csv_text(["项目", "数值", "单位", "备注"], rows)


def notation_csv(result: dict) -> str:
    """写法未定的逐格证据：行号、列、原值、两种读法，能直接翻回源文件。"""
    notation = result.get("profile", {}).get("notation") or {}
    cells = notation.get("cells") or []
    if not cells:
        return csv_text(["列名", "列标题", "行号", "原值", "原因码", "说明"], [])
    rows = []
    for item in cells:
        rows.append([item["column"], item["label"], item["lineno"], item["raw"],
                     item["code"], item["detail"]])
    return csv_text(["列名", "列标题", "行号", "原值", "原因码", "说明"], rows)


def profile_csv(result: dict) -> str:
    rows = []
    for item in result["profile"]["profile"]:
        rows.append([
            item["name"], item["label"], item["kind"], item["total"],
            item["filled"], item["missing"], item["missing_rate"],
            item["format_bad"], item["format_rate"], item["distinct"],
            item.get("notation_unknown", 0),
            item.get("notation_unknown_rate", "0.0000"),
            item.get("min") or "", item.get("max") or "",
        ])
    return csv_text(["列名", "列标题", "类型", "单元格", "有值", "缺失", "缺失率",
                     "格式不合格", "格式率", "基数", "写法未定", "写法未定率",
                     "最小", "最大"], rows)


def missing_csv(result: dict) -> str:
    summary = {item["column"]: item for item in result["missing"]["run_summary"]}
    rows = []
    for item in result["missing"]["account"]["by_column"]:
        extra = summary.get(item["name"], {})
        rows.append([item["name"], item["label"], item["missing"], item["filled"],
                     item["missing_rate"], extra.get("runs", 0),
                     extra.get("longest", 0), item.get("notation_unknown", 0)])
    return csv_text(["列名", "列标题", "缺失", "有值", "缺失率", "连续段数",
                     "最长段", "写法未定"], rows)


def assessable_csv(result: dict) -> str:
    """本批可评/排除范围：一行一项，带分子分母，报告页与导出同源。"""
    assessable = result["score"].get("assessable", {})
    notation = result.get("profile", {}).get("notation") or {}
    rows = [
        ["单元格总数", assessable.get("cells_total", 0), ""],
        ["可评单元格", assessable.get("cells_assessable", 0),
         "单元格 − 写法未定；缺失维与格式维的分母都从这里算"],
        ["其中缺失", assessable.get("cells_missing", 0), "缺失维分子，仍属可评范围"],
        ["其中参检离群", assessable.get("tested_cells", 0), "离群维分母（已排除退化列与写法未定格）"],
        ["排除·写法未定", assessable.get("cells_notation_unknown", 0),
         "两种读法都成立且不同，未证实解释未进入任何分数"],
        ["裁定规则", assessable.get("notation_policy", ""), notation.get("rule", "")],
        ["可评覆盖率", assessable.get("coverage", ""),
         f"下限 {assessable.get('coverage_min', '')}；低于下限时结论标注「仅覆盖可评格」"],
        ["结论覆盖", "完整" if assessable.get("qualified", True) else "仅覆盖可评格", ""],
    ]
    for item in notation.get("by_column", []):
        rows.append([f"写法未定·{item['label']}", item["count"],
                     f"未定率 {item['rate']}（分母为列内非缺失格）"])
    return csv_text(["项目", "数值", "备注"], rows)


def duplicates_csv(result: dict) -> str:
    duplicates = result["duplicates"]
    rows = []
    for item in duplicates["hash_groups"]:
        rows.append(["全行哈希", item["hash"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]), ""])
    for item in duplicates["key_repeats"]:
        rows.append(["业务键重复", item["key"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]), ""])
    for item in duplicates["conflicts"]:
        rows.append(["业务键冲突", item["key"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]),
                     " ".join(item["fields"])])
    return csv_text(["账", "组或键", "行数", "行号", "冲突列"], rows)


def outliers_csv(result: dict) -> str:
    stats = {item["name"]: item for item in result["outliers"]["columns"]}
    rows = []
    for item in result["outliers"]["cells"]:
        column = stats.get(item["column"], {})
        rows.append([item["column"], column.get("label", item["column"]),
                     item["lineno"], item["raw"], item["value"], item["z"],
                     column.get("method", ""), "K_OUTLIER"])
    return csv_text(["列名", "列标题", "行号", "原值", "归一值", "稳健z", "方法",
                     "原因码"], rows)


def quarantine_csv(result: dict) -> str:
    rows = []
    for item in result["dataset"]["quarantine"]:
        rows.append([item["lineno"], item["segment"], item["code"],
                     item["field"], item["raw_value"], item["detail"]])
    return csv_text(["行号", "段", "原因码", "字段", "原值", "说明"], rows)


def flags_csv(result: dict) -> str:
    rows = []
    for item in result["dataset"]["flags"]:
        rows.append([item["code"], item["label"], item["level"], item["count"]])
    return csv_text(["原因码", "含义", "级别", "次数"], rows)
