"""单元格解析：四态判定、数值写法裁定、时间归一、格式与范围校验。

优先级（TRAP-19 / TRAP-27）：缺失 > 格式不合格 > 写法未定 > 离群。
一个单元格只会拿到一个状态与一个原因码，绝不同时计入两维，否则评分分项之和
永远对不上总分。

数值唯一入口：NFKC 折全角 → 按语法裁定小数写法 → Decimal → 按列刻度量化。
- `1,204.5` 只有美式读法成立 → 1204.5；`1.204,5` 只有欧式读法成立 → 1204.5。
- `1,204` / `1.204` 两种读法都成立且数值不同 → 写法未定（notation_unknown），
  不给值、不进任何评分的分子分母，只留原值与行号供复核。
- 一种读法合格、另一种不合格时期值仍取决于未证实的写法，同样判写法未定；
  只有两种读法都不合格时，结论与写法无关，才记格式不合格。
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ... import config

MISSING = "missing"
FORMAT_BAD = "format_bad"
NOTATION_UNKNOWN = "notation_unknown"
VALUE = "value"

# 空格类只作分组分隔，不可能是小数点；NFKC 之后仍可能残留 NBSP / 窄空格
SPACE_RE = re.compile(r"[\s    ]+")
PLAIN_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$")
# 美式：逗号只作 3 位分组，点号只作小数点
US_RE = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)$")
# 欧式：点号只作 3 位分组，逗号只作小数点
EU_RE = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:\.\d{3})+)(?:,\d+)?|\d+(?:,\d+)?|,\d+)$")

NOTATION_US = "us"
NOTATION_EU = "eu"
NOTATION_NEUTRAL = "neutral"
NOTATION_LABELS = {
    NOTATION_US: "美式（逗号分组、点号小数）",
    NOTATION_EU: "欧式（点号分组、逗号小数）",
    NOTATION_NEUTRAL: "无分隔小数",
}

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


@dataclass(frozen=True)
class Cell:
    state: str
    code: str | None
    raw: str
    value: str | None
    detail: str = ""
    notation: str = ""          # us / eu / neutral；非数值或未定时为空

    @property
    def missing(self) -> bool:
        return self.state == MISSING

    @property
    def bad(self) -> bool:
        return self.state == FORMAT_BAD

    @property
    def unknown_notation(self) -> bool:
        return self.state == NOTATION_UNKNOWN

    @property
    def ok(self) -> bool:
        return self.state == VALUE

    def as_dict(self) -> dict:
        return {"state": self.state, "code": self.code, "raw": self.raw,
                "value": self.value, "detail": self.detail,
                "notation": self.notation}


def normalize(raw: str | None) -> str:
    if raw is None:
        return ""
    return unicodedata.normalize("NFKC", raw).strip()


def is_sentinel(raw: str | None) -> bool:
    return normalize(raw).lower() in config.SENTINELS


def missing_code(raw: str | None) -> str | None:
    """空值三态：显式空 / 空白串 / 哨兵，分别给原因码。"""
    if raw is None:
        return "K_NULL"
    if raw == "":
        return "K_NULL"
    if raw.strip() == "":
        return "K_BLANK"
    if is_sentinel(raw):
        return "K_SENTINEL_NULL"
    return None


def _collapse(text: str) -> str:
    return SPACE_RE.sub("", text)


def number_readings(text: str) -> list[tuple[str, Decimal]]:
    """按语法列出所有成立的读法；同一数值只保留一次。

    只有一种读法成立 → 可直接取值；两种都成立且数值不同 → 调用方判写法未定。
    「无分组的分隔小数」（如 1204.5）两种语法读出来是同一个数，标 neutral。
    """
    readings: list[tuple[str, Decimal]] = []
    if not text:
        return readings
    if "," not in text and "." not in text:
        if PLAIN_RE.fullmatch(text):
            readings.append((NOTATION_NEUTRAL, _decimal(text)))
        return readings
    if US_RE.fullmatch(text):
        readings.append((NOTATION_US, _decimal(text.replace(",", ""))))
        if PLAIN_RE.fullmatch(text):
            readings[-1] = (NOTATION_NEUTRAL, readings[-1][1])
    if EU_RE.fullmatch(text):
        readings.append((NOTATION_EU, _decimal(text.replace(".", "").replace(",", "."))))
    deduped: list[tuple[str, Decimal]] = []
    seen: set[Decimal] = set()
    for notation, number in readings:
        if number in seen:
            continue
        seen.add(number)
        deduped.append((notation, number))
    return deduped


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def to_decimal(raw: str | None) -> Decimal | None:
    """唯一读法才返回 Decimal；写法未定返回 None（不替调用方猜）。"""
    readings = number_readings(_collapse(normalize(raw)))
    if len(readings) != 1:
        return None
    return readings[0][1]


def quantize(value: Decimal, quant: str) -> Decimal:
    if not quant:
        return value
    return value.quantize(Decimal(quant), rounding=ROUND_HALF_UP)


def _validate(column: config.Column, number: Decimal) -> tuple[str | None, str]:
    """返回 (原因码, 说明)；原因码为 None 表示通过。"""
    if column.kind == "int":
        if number != number.to_integral_value():
            return "K_BAD_FORMAT", "整数列出现小数"
        number = number.to_integral_value()
    minimum, maximum = column.min_dec, column.max_dec
    if (minimum is not None and number < minimum) or \
            (maximum is not None and number > maximum):
        return "K_OUT_OF_RANGE", f"取值需在 [{column.minimum}, {column.maximum}]"
    return None, ""


def to_datetime(raw: str | None) -> dt.datetime | None:
    text = normalize(raw)
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for fmt in DATETIME_FORMATS:
            try:
                parsed = dt.datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        # 无偏移按单据本地时区（+08:00）处理，再统一入库 UTC
        parsed = parsed.replace(tzinfo=config.CHINA_TZ)
    return parsed.astimezone(config.UTC)


def iso_utc(moment: dt.datetime) -> str:
    return moment.astimezone(config.UTC).isoformat(timespec="seconds")


def _numeric_cell(column: config.Column, text: str) -> Cell:
    readings = number_readings(_collapse(text))
    if not readings:
        return Cell(state=FORMAT_BAD, code="K_BAD_FORMAT", raw=text, value=None,
                    detail="数值无法解析")
    if len(readings) == 1:
        notation, number = readings[0]
        code, detail = _validate(column, number)
        if code is not None:
            stored = str(quantize(number, column.quant)) \
                if code == "K_OUT_OF_RANGE" else None
            return Cell(state=FORMAT_BAD, code=code, raw=text, value=stored,
                        detail=detail, notation=notation)
        if column.kind == "int":
            number = number.to_integral_value()
        return Cell(state=VALUE, code=None, raw=text,
                    value=str(quantize(number, column.quant)),
                    detail="", notation=notation)

    graded = [(notation, number, *_validate(column, number))
              for notation, number in readings]
    shown = "；".join(f"{NOTATION_LABELS[notation]}读作 {number}"
                     for notation, number, *_ in graded)
    if all(item[2] is not None for item in graded):
        # 两种读法都判不合格：结论与写法无关，可以安全地记格式不合格
        codes = {item[2] for item in graded}
        code = codes.pop() if len(codes) == 1 else "K_BAD_FORMAT"
        return Cell(state=FORMAT_BAD, code=code, raw=text, value=None,
                    detail=f"两种写法都判格式不合格：{shown}")
    # 只要存在「一种读法合格、另一种不合格」，取值就依赖未证实的写法，
    # 不能按列契约挑一个 —— 一律判写法未定，交给人工确认。
    return Cell(state=NOTATION_UNKNOWN, code="K_NOTATION_UNKNOWN", raw=text,
                value=None,
                detail=f"写法未定，不参与评分：{shown}")


def parse_cell(column: config.Column, raw: str | None) -> Cell:
    text = "" if raw is None else str(raw)
    code = missing_code(text)
    if code is not None:
        return Cell(state=MISSING, code=code, raw=text, value=None,
                    detail="空值不参与格式与离群判定")

    normalized = normalize(text)
    kind = column.kind
    if kind in ("decimal", "int"):
        return _numeric_cell(column, normalized)
    if kind == "datetime":
        moment = to_datetime(normalized)
        if moment is None:
            return Cell(state=FORMAT_BAD, code="K_BAD_FORMAT", raw=text, value=None,
                        detail="时间无法解析")
        return Cell(state=VALUE, code=None, raw=text, value=iso_utc(moment))
    if kind == "enum":
        if normalized in column.enums:
            return Cell(state=VALUE, code=None, raw=text, value=normalized)
        return Cell(state=FORMAT_BAD, code="K_BAD_FORMAT", raw=text, value=None,
                    detail="取值不在枚举内")
    regex = column.regex
    if regex is not None and not regex.match(normalized):
        return Cell(state=FORMAT_BAD, code="K_BAD_FORMAT", raw=text, value=None,
                    detail="编号格式不符")
    if column.kind == "text" and not normalized:
        return Cell(state=MISSING, code="K_BLANK", raw=text, value=None)
    return Cell(state=VALUE, code=None, raw=text, value=normalized)


def numeric_value(cell: dict) -> Decimal | None:
    if cell["state"] != VALUE or cell.get("value") in (None, ""):
        return None
    try:
        return Decimal(cell["value"])
    except (InvalidOperation, ValueError):
        return None
