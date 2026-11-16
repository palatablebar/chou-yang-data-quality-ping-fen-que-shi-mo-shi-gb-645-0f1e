"""字段画像：把采纳行原文逐格解析成四态单元格，再按列汇总。

输出结构是下游共享契约：
- rows[i]["cells"][column] = {state, code, raw, value, detail, notation}
- columns 为契约顺序，绝不按输入表头顺序漂移
- counts 给出 单元格 = 有值 + 写法未定 + 缺失 的分子分母；
  有值 = 合格 + 格式不合格。写法未定的格既不判合格也不判不合格。
"""
from __future__ import annotations

from ... import config
from . import column_stats, formats


def build(ingest: dict) -> dict:
    columns = list(config.COLUMNS)
    rows = []
    for row in ingest["rows"]:
        cells = {}
        for column in columns:
            parsed = formats.parse_cell(column, row["cells"].get(column.name, ""))
            cells[column.name] = parsed.as_dict()
        rows.append({"lineno": row["lineno"], "cells": cells})

    per_column = [
        column_stats.describe(column, [row["cells"][column.name] for row in rows])
        for column in columns
    ]
    missing = sum(item["missing"] for item in per_column)
    unknown = sum(item["notation_unknown"] for item in per_column)
    bad = sum(item["format_bad"] for item in per_column)
    total = len(rows) * len(columns)
    counts = {
        "rows": len(rows),
        "cells_total": total,
        "cells_missing": missing,
        # 写法未定：原值存在但解释不唯一，不进任何比率的分子分母
        "cells_notation_unknown": unknown,
        # 有值 = 非缺失且写法已定（含格式不合格）；合格 = 有值且格式合格
        "cells_value": total - missing - unknown,
        "cells_ok": total - missing - unknown - bad,
        "cells_format_bad": bad,
        # 排除 = 写法未定；缺失仍属于评分范围（按缺失维计）
        "cells_excluded": unknown,
        "cells_assessable": total - unknown,
        "columns": len(columns),
        "empty_columns": [item["name"] for item in per_column
                          if item["status"] == "empty_column"],
    }
    return {
        "columns": [column.as_dict() for column in columns],
        "rows": rows,
        "profile": per_column,
        "counts": counts,
        "notation": notation_evidence(rows, per_column),
    }


def notation_evidence(rows: list[dict], per_column: list[dict]) -> dict:
    """写法未定格的逐格证据：行号 + 列 + 原值 + 两种读法，页面与导出共用。"""
    labels = {item["name"]: item["label"] for item in per_column}
    cells = []
    by_column: dict[str, int] = {}
    by_value: dict[str, int] = {}
    for row in rows:
        for name, cell in row["cells"].items():
            if cell["state"] != formats.NOTATION_UNKNOWN:
                continue
            by_column[name] = by_column.get(name, 0) + 1
            by_value[cell["raw"]] = by_value.get(cell["raw"], 0) + 1
            cells.append({
                "lineno": row["lineno"],
                "column": name,
                "label": labels.get(name, name),
                "raw": cell["raw"],
                "code": cell["code"],
                "detail": cell["detail"],
            })
    cells.sort(key=lambda item: (item["column"], item["lineno"]))
    columns = []
    for item in per_column:
        if not item["notation_unknown"]:
            continue
        columns.append({"name": item["name"], "label": item["label"],
                        "count": item["notation_unknown"],
                        "rate": item["notation_unknown_rate"]})
    values = [{"raw": raw, "count": count}
              for raw, count in sorted(by_value.items(),
                                       key=lambda pair: (-pair[1], pair[0]))]
    return {
        "policy": config.NOTATION_POLICY,
        "rule": "语法无法唯一确定小数位置的原值不取值：不进缺失、格式、离群与评分，"
                "只保留原值与行号供人工复核",
        "count": len(cells),
        "cells": cells,
        "by_column": columns,
        "by_value": values,
    }


