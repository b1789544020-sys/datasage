"""DataSage 结果验证：对沙箱执行结果做三层基础验证。

三层分别是：
1. syntax（语法/结构层）：执行是否成功、result_data 是否非空；
2. semantic（语义 / sanity 层）：表行数是否合理、是否存在全空列、标量是否
   为 NaN/None/Inf，并对数值列做 IQR 离群点检测（只标记不拦截）；
3. consistency（一致层）：本步仅占位 pass；"LLM 解释 vs 真实数据"的数字
   回溯（反幻觉）校验在下一步实现，见 validate_answer_vs_data。

verify_result 是纯函数：不调 LLM、不读文件、不访问数据库，只对传入的
result dict 做判断，方便单元测试。
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

logger = logging.getLogger("app.analysis.verifier")

# 表格行数 sanity 上限：100 万行以内视为合理
MAX_REASONABLE_ROWS = 1_000_000


def _check(layer: str, status: str, detail: str) -> dict[str, str]:
    """构造一条检查记录。status 只取 'pass' / 'fail'。"""
    return {"layer": layer, "status": status, "detail": detail}


def _is_missing_or_nonfinite(value: Any) -> bool:
    """判断值是否为 None / NaN / ±Inf。

    沙箱序列化后 NaN 通常已变成 None，这里对裸 float('nan') 等保持防御。
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not math.isfinite(float(value))
    return False


def _is_number(value: Any) -> bool:
    """是否为可参与统计的数值（排除 bool 与非有限值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


# ============================== 第 1 层：syntax ==============================

def _check_syntax(result: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    success = result.get("success")
    if success is True:
        checks.append(_check("syntax", "pass", "沙箱执行成功（success=True）"))
    else:
        detail = f"沙箱执行未成功（success={success!r}）"
        if result.get("error"):
            detail += f"：{result['error']}"
        checks.append(_check("syntax", "fail", detail))

    data = result.get("result_data")
    if data is None:
        checks.append(_check("syntax", "fail", "result_data 缺失（为 None）"))
    elif isinstance(data, (dict, list, str)) and len(data) == 0:
        checks.append(_check("syntax", "fail", "result_data 为空（空容器 / 空字符串）"))
    else:
        checks.append(_check("syntax", "pass", "result_data 非空"))

    return checks


# ============================== 第 2 层：semantic ==============================

def _percentile(sorted_values: list[float], pct: float) -> float:
    """线性插值法百分位数（pct ∈ [0, 100]），入参需已排序且非空。"""
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    pos = (n - 1) * pct / 100.0
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(sorted_values[low])
    frac = pos - low
    return float(sorted_values[low]) * (1 - frac) + float(sorted_values[high]) * frac


def _check_table(data: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    columns = data.get("columns")
    rows = data.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        checks.append(
            _check("semantic", "fail", "table 结构异常：缺少 columns/data 列表")
        )
        return checks

    # 行数：优先用执行器给出的 total_rows，否则用回传行数
    total = data.get("total_rows", len(rows))
    if not isinstance(total, int) or total <= 0:
        checks.append(_check("semantic", "fail", f"结果表为空（行数={total!r}）"))
    elif total >= MAX_REASONABLE_ROWS:
        checks.append(
            _check(
                "semantic",
                "fail",
                f"结果表行数 {total} 超过合理上限 {MAX_REASONABLE_ROWS}",
            )
        )
    else:
        checks.append(_check("semantic", "pass", f"结果表行数合理（{total} 行）"))

    # 全 NaN / 全空列：该列每一行都是 None 或非有限数值
    empty_columns: list[str] = []
    for idx, col_name in enumerate(columns):
        cells = [
            row[idx] if isinstance(row, list) and idx < len(row) else None
            for row in rows
        ]
        if cells and all(_is_missing_or_nonfinite(v) for v in cells):
            empty_columns.append(str(col_name))
    if empty_columns:
        checks.append(
            _check(
                "semantic",
                "fail",
                f"存在全为空（NaN/None）的列：{', '.join(empty_columns)}",
            )
        )
    else:
        checks.append(_check("semantic", "pass", "不存在全为空的列"))

    # IQR 离群点检测（可选规则）：只在 detail 中标记，状态始终 pass
    for idx, col_name in enumerate(columns):
        values = sorted(
            v
            for row in rows
            if isinstance(row, list)
            and idx < len(row)
            and _is_number(row[idx])
            for v in [float(row[idx])]
        )
        if len(values) < 4:
            continue
        q1, q3 = _percentile(values, 25), _percentile(values, 75)
        iqr = q3 - q1
        if iqr <= 0:
            continue
        low_bound, high_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        outliers = [v for v in values if v < low_bound or v > high_bound]
        if outliers:
            checks.append(
                _check(
                    "semantic",
                    "pass",
                    f"数值列「{col_name}」有 {len(outliers)} 个 IQR 离群值"
                    f"（正常区间约 [{low_bound:.2f}, {high_bound:.2f}]，仅标记不拦截）",
                )
            )

    return checks


def _check_single_value(data: dict[str, Any]) -> list[dict[str, str]]:
    value = data.get("value")
    if _is_missing_or_nonfinite(value):
        return [
            _check(
                "semantic",
                "fail",
                f"单值结果无效：value={value!r}（None / NaN / Inf 不允许作为答案）",
            )
        ]
    return [_check("semantic", "pass", f"单值结果有效（{type(value).__name__}: {value!r}）")]


def _check_semantic(result: dict[str, Any]) -> list[dict[str, str]]:
    data = result.get("result_data")
    result_type = result.get("result_type")

    if data is None or (isinstance(data, (dict, list, str)) and len(data) == 0):
        return [_check("semantic", "fail", "result_data 为空，无法做语义检查")]

    if result_type == "table":
        if not isinstance(data, dict):
            return [_check("semantic", "fail", "table 结果的 result_data 不是对象")]
        return _check_table(data)

    if result_type == "single_value":
        if not isinstance(data, dict):
            return [_check("semantic", "fail", "single_value 结果的 result_data 不是对象")]
        return _check_single_value(data)

    if result_type == "record":
        return [_check("semantic", "pass", "record（dict）结果无额外语义规则，视为合理")]

    return [
        _check("semantic", "fail", f"未知的 result_type={result_type!r}，无法做语义检查")
    ]


# ============================== 第 3 层：consistency（占位） ==============================

def validate_answer_vs_data(
    answer_text: str,
    result_data: Any,
) -> dict[str, Any] | None:
    """数字回溯（反幻觉）校验：答案里的每个数字都必须能在真实结果中找到证据。

    薄封装 :meth:`NumericBacktracker.verify_answer`，找不到证据的数字记为幻觉。
    ``answer_text`` 为空时返回 None（没有可供校验的结论）。
    """
    if not answer_text or not str(answer_text).strip():
        return None
    return NumericBacktracker().verify_answer(str(answer_text), result_data)


def _check_consistency(
    result: dict[str, Any], answer_text: str | None = None
) -> list[dict[str, str]]:
    """一致层。

    - 没有提供答案文本（本步之前的调用方式）：占位 pass；
    - 提供了 ``answer_text``：执行真正的数字回溯，答案中存在无法在
      ``result_data`` 里回溯的数字时判 fail。
    """
    if not answer_text or not str(answer_text).strip():
        return [
            _check(
                "consistency",
                "pass",
                "一致性层占位：未提供答案文本，数字回溯（反幻觉）校验跳过",
            )
        ]

    report = validate_answer_vs_data(answer_text, result.get("result_data"))
    if report is None:
        return [_check("consistency", "pass", "答案为空，数字回溯跳过")]

    if report["passed"]:
        return [
            _check(
                "consistency",
                "pass",
                f"数字回溯通过：答案中的 {report['total_numbers']} 个数字"
                "全部能在真实结果中找到证据",
            )
        ]

    fake = "、".join(item["raw"] for item in report["hallucinations"])
    return [
        _check(
            "consistency",
            "fail",
            f"数字回溯发现 {report['hallucination_count']} 个无法溯源的数字"
            f"（疑似幻觉）：{fake}",
        )
    ]


# ============================== 对外入口 ==============================

def verify_result(
    result: dict[str, Any],
    question: str,
    answer_text: str | None = None,
) -> dict[str, Any]:
    """对执行结果做三层基础验证（纯函数，不读文件 / 不访问数据库；consistency
    层的数字回溯同样是纯规则计算，不调 LLM）。

    返回：
        {
            "passed": bool,                 # 三层所有检查均 pass 才为 True
            "checks": [
                {"layer": "syntax"|"semantic"|"consistency",
                 "status": "pass"|"fail", "detail": "..."},
                ...
            ],
        }

    ``question`` 预留给"问题 ↔ 结果"匹配；传入可选 ``answer_text``（LLM 写的
    最终总结）时，consistency 层会执行数字回溯（反幻觉）校验。
    """
    if not isinstance(result, dict):
        return {
            "passed": False,
            "checks": [
                _check("syntax", "fail", f"result 不是 dict（实际 {type(result).__name__}）"),
                _check("semantic", "fail", "输入非法，跳过语义检查"),
                _check("consistency", "pass", "输入非法，数字回溯跳过"),
            ],
        }

    checks = (
        _check_syntax(result)
        + _check_semantic(result)
        + _check_consistency(result, answer_text)
    )
    return {
        "passed": all(item["status"] == "pass" for item in checks),
        "checks": checks,
    }


# =====================================================================
# 数字回溯（反幻觉）校验
#
# 思路：从 LLM 总结文本中抽取所有"数字表达" → 把执行结果 result_data 里的
# 全部数值收集成带位置的"证据集合" → 逐个数字做近似匹配，找不到证据的数字
# 判定为幻觉。整个过程是纯规则 / 纯函数，不调 LLM、不读数据库。
# =====================================================================

# 阿拉伯数字核心：千分位整数或普通整数，可带小数
_CORE_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
# 汉字量词：X 个 / 三种 / 五台 …，计数词与表行数做匹配
_MEASURE_CHARS = "个件只条位种品类家天周日月年份次名台套双颗张本份届期轮"

# 日期 / 年份 / 时钟：这些数字不是"结论性主张"，整体跳过不抽取，
# 否则 "2024-01-01" 会被误抽成 2024/1/1 造成假幻觉
_DATE_PATTERNS = (
    re.compile(r"[0-9]{4}\s*[年/\-]\s*[0-9]{1,2}\s*[月/\-]\s*[0-9]{1,2}\s*日?"),
    re.compile(r"[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日"),
    re.compile(r"[0-9]{4}\s*年"),
    re.compile(r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?"),
)

# 230% / -5% / 全角百分号；12 个百分点；百分之二十三
_PERCENT_RE = re.compile(rf"(?<![0-9.])([-−]?{_CORE_NUMBER})\s*(?:%|％)")
_PCTPOINT_RE = re.compile(rf"(?<![0-9.])([-−]?{_CORE_NUMBER})\s*个百分点")
_CN_PERCENT_RE = re.compile(
    r"百分之([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]{1,10})"
)
_MULTIPLIER_RE = re.compile(rf"(?<![0-9.])([-−]?{_CORE_NUMBER})\s*倍")  # 2.3 倍
_CURRENCY_PREFIX_RE = re.compile(rf"([¥￥$€£])\s*([-−]?{_CORE_NUMBER})")  # ¥123 / $45
_CURRENCY_SUFFIX_RE = re.compile(rf"(?<![0-9.])([-−]?{_CORE_NUMBER})\s*(?:美元|人民币|元(?!宵))")
_ORDINAL_RE = re.compile(
    r"第\s*([0-9]{1,3}(?:,[0-9]{3})*|[零〇一二两三四五六七八九十百千万亿]{1,10})"
)
# 含十/百/千/万/亿的中文数字（两千、二十三、一万二千），需后接量词/边界，
# 避免"千万要注意"这类口语被误读
_CN_UNIT_RE = re.compile(
    r"[零〇一二两三四五六七八九]?[十百千万亿][零〇一二两三四五六七八九十百千万亿]{0,8}"
    rf"(?=[{_MEASURE_CHARS}多余来，。、；：！？\s）\)】」]|$)"
)
_CN_COUNT_RE = re.compile(rf"([零〇一二两三四五六七八九][{_MEASURE_CHARS}])")  # 三个、五次
_ARAB_COUNT_RE = re.compile(  # 3 个、12 种（量词吃进匹配片段，raw 形如 "3个"）
    rf"(?<![0-9A-Za-z_.])([0-9]{{1,4}}[{_MEASURE_CHARS}])"
)
_CN_NEGATIVE_RE = re.compile(
    r"负([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]{1,10})"
)
# 普通阿拉伯数字（含小数 / 千分位 / 负号），边界保护防止从 id123、230% 重复抽取
_PLAIN_NUMBER_RE = re.compile(
    rf"(?<![0-9A-Za-z_.%¥￥$])[-−]?(?:{_CORE_NUMBER})(?![0-9A-Za-z_%％])"
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(text: str) -> int | None:
    """中文数字转 int：两千→2000、二十三→23、一万二千三百四十五→12345、十万→100000。"""
    if not text or any(
        ch not in _CN_DIGITS and ch not in _CN_SMALL_UNITS and ch not in ("万", "亿")
        for ch in text
    ):
        return None
    total = section = current = 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_SMALL_UNITS:  # "十二"的十位没有前置数字，按 1 个十处理
            section += (current or 1) * _CN_SMALL_UNITS[ch]
            current = 0
        elif ch == "万":
            section = (section + current) * 10_000
            total += section
            section, current = 0, 0
        elif ch == "亿":
            total = (total + section + current) * 100_000_000
            section, current = 0, 0
    value = total + section + current
    # 孤立的"万 / 亿"（如"1000万，"里被切出的单字"万"）解析为 0，
    # 不构成数字主张，丢弃；"十/百/千"单独出现仍合法（10/100/1000）
    return value if value > 0 else None


def _parse_arabic_number(raw: str) -> float:
    """千分位字符串 → float，如 '1,234.5' → 1234.5。"""
    return float(raw.replace(",", "").replace("−", "-").strip())
class NumericBacktracker:
    """数字回溯校验器：抽取答案中的数字主张，并在真实执行结果里找证据。

    Args:
        number_tol: 普通数字相对容差（默认 1%）。
        percent_tol: 百分比 / 倍数候选值的相对容差（默认 5%，单独放宽）。
        abs_tol: 绝对容差下限（数值接近 0 时相对容差会失效）。
        max_summary_rows: 重生成 prompt 中 result_data 摘要的最大行数。
    """

    def __init__(
        self,
        number_tol: float = 0.01,
        percent_tol: float = 0.05,
        abs_tol: float = 0.05,
        max_summary_rows: int = 20,
    ) -> None:
        self.number_tol = number_tol
        self.percent_tol = percent_tol
        self.abs_tol = abs_tol
        self.max_summary_rows = max_summary_rows

    # ----------------------------- 1. 数字抽取 -----------------------------

    def extract_numbers(self, text: str) -> list[dict[str, Any]]:
        """从文本中抽取全部数字表达，按出现位置排序。

        每项：{"raw": "230%", "value": 230.0, "kind": "percent", "position": 12}
        kind：percent / multiplier / currency / number / ordinal / count /
        chinese_number。
        """
        if not text:
            return []

        skip_spans = [
            (m.start(), m.end()) for pat in _DATE_PATTERNS for m in pat.finditer(text)
        ]

        candidates: list[tuple[int, int, int, str, float]] = []

        def add(start: int, end: int, priority: int, kind: str, value: Any) -> None:
            if value is None:
                return
            try:
                num = float(value)
            except (TypeError, ValueError):
                return
            if math.isfinite(num):
                candidates.append((start, end, priority, kind, num))

        for m in _PERCENT_RE.finditer(text):
            add(m.start(), m.end(), 10, "percent", _parse_arabic_number(m.group(1)))
        for m in _PCTPOINT_RE.finditer(text):
            add(m.start(), m.end(), 10, "percent", _parse_arabic_number(m.group(1)))
        for m in _CN_PERCENT_RE.finditer(text):
            token = m.group(1)
            add(m.start(), m.end(), 10, "percent",
                _cn_to_int(token) if not token[0].isdigit() else float(token))
        for m in _MULTIPLIER_RE.finditer(text):
            add(m.start(), m.end(), 20, "multiplier", _parse_arabic_number(m.group(1)))
        for m in _CURRENCY_PREFIX_RE.finditer(text):
            add(m.start(), m.end(), 20, "currency", _parse_arabic_number(m.group(2)))
        for m in _CURRENCY_SUFFIX_RE.finditer(text):
            add(m.start(1), m.end(), 20, "currency", _parse_arabic_number(m.group(1)))
        for m in _ORDINAL_RE.finditer(text):
            token = m.group(1)
            value = _cn_to_int(token) if not token[0].isdigit() else int(token.replace(",", ""))
            add(m.start(), m.end(), 20, "ordinal", value)
        for m in _CN_UNIT_RE.finditer(text):
            add(m.start(), m.end(), 40, "chinese_number", _cn_to_int(m.group(0)))
        for m in _CN_COUNT_RE.finditer(text):
            token = m.group(1)
            add(m.start(1), m.end(1), 30, "count", _CN_DIGITS[token[0]])
        for m in _ARAB_COUNT_RE.finditer(text):
            token = m.group(1)
            add(m.start(1), m.end(1), 30, "count", int(token[:-1]))
        for m in _CN_NEGATIVE_RE.finditer(text):
            token = m.group(1)
            value = _cn_to_int(token) if not token[0].isdigit() else float(token)
            if value is not None:
                add(m.start(), m.end(), 20, "number", -value)
        for m in _PLAIN_NUMBER_RE.finditer(text):
            add(m.start(), m.end(), 50, "number", _parse_arabic_number(m.group(0)))

        # 跨度去重：同起点长片段优先（230% 胜出内部的 230；¥123 胜出 123），
        # 与已选片段重叠 / 落在日期跨度内的候选丢弃
        candidates.sort(key=lambda c: (c[0], -(c[1] - c[0]), c[2]))
        numbers: list[dict[str, Any]] = []
        cursor = -1
        for start, end, _pri, kind, value in candidates:
            if start < cursor:
                continue
            if any(s < end and e > start for s, e in skip_spans):
                continue
            cursor = end
            numbers.append({
                "raw": text[start:end],
                "value": value,
                "kind": kind,
                "position": start,
            })
        numbers.sort(key=lambda item: item["position"])
        return numbers
    # ----------------------------- 2. 证据收集 -----------------------------

    @staticmethod
    def _cell_numbers(cell: Any) -> list[float]:
        """把一个单元格转成可作为证据的数值列表（字符串尽量解析）。"""
        if isinstance(cell, bool) or cell is None:
            return []
        if isinstance(cell, (int, float)):
            return [float(cell)] if math.isfinite(float(cell)) else []
        if isinstance(cell, str):
            s = cell.strip().replace(",", "")
            if not s:
                return []
            is_percent = s.endswith(("%", "％"))
            s = s.strip("%％").strip()
            for prefix in ("¥", "￥", "$", "€", "£"):
                s = s.replace(prefix, "")
            for suffix in ("元", "美元", "人民币"):
                if s.endswith(suffix):
                    s = s[: -len(suffix)]
            try:
                value = float(s)
            except ValueError:
                return []
            return [value, value / 100.0] if is_percent else [value]
        return []

    def _collect_evidence(
        self, result_data: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """收集证据数字集合。

        返回 ``(evidence, meta)``：
        - evidence: [{"value": float, "location": {"row": 0, "column": "x"}}]
        - meta:     {"total_rows": int, "columns": [...]}（计数词 / 序数词用）
        """
        evidence: list[dict[str, Any]] = []
        meta: dict[str, Any] = {"total_rows": 0, "columns": []}
        if not isinstance(result_data, dict):
            return evidence, meta

        rtype = result_data.get("type")
        data = result_data.get("data")

        # 标准信封 {type:"table"} 或裸 {columns:[...], data:[...]} 都按表格处理
        is_table = rtype == "table" or (
            isinstance(data, list) and isinstance(result_data.get("columns"), list)
        )
        if is_table and isinstance(data, list):
            columns = result_data.get("columns") or []
            meta["columns"] = list(columns)
            meta["total_rows"] = int(result_data.get("total_rows") or len(data))
            for row_idx, row in enumerate(data):
                if not isinstance(row, (list, tuple)):
                    continue
                for col_idx, cell in enumerate(row):
                    location = {"row": row_idx, "column_index": col_idx}
                    if col_idx < len(columns):
                        location["column"] = columns[col_idx]
                    for value in self._cell_numbers(cell):
                        evidence.append({"value": value, "location": location})
            return evidence, meta

        if rtype == "single_value":
            for num in self._cell_numbers(result_data.get("value")):
                evidence.append({"value": num, "location": {"type": "single_value"}})
            return evidence, meta

        # record（executor 对 dict 结果不加 type 包装）：递归收集所有数值
        if rtype is None:
            def walk(obj: Any, path: str) -> None:
                if isinstance(obj, dict):
                    for key, val in obj.items():
                        walk(val, f"{path}.{key}" if path else str(key))
                elif isinstance(obj, (list, tuple)):
                    for idx, val in enumerate(obj):
                        walk(val, f"{path}[{idx}]")
                else:
                    for num in self._cell_numbers(obj):
                        evidence.append(
                            {"value": num, "location": {"type": "record", "key": path}}
                        )

            walk(result_data, "")
        return evidence, meta

    # ----------------------------- 3. 近似匹配 -----------------------------

    @staticmethod
    def _approx_equal(a: float, b: float, rel_tol: float, abs_tol: float) -> bool:
        """值近似相等：相对容差 + 绝对容差下限 + 整数四舍五入匹配。"""
        if a == b:
            return True
        diff = abs(a - b)
        if diff <= abs_tol:
            return True
        if diff <= rel_tol * max(abs(a), abs(b)):
            return True
        if abs(a) >= 1 and abs(b) >= 1 and round(a) == round(b):
            return True
        return False

    def _search_evidence(
        self, value: float, evidence: list[dict[str, Any]], rel_tol: float
    ) -> dict[str, Any] | None:
        for item in evidence:
            if self._approx_equal(value, item["value"], rel_tol, self.abs_tol):
                location = dict(item["location"])
                location["evidence_value"] = item["value"]
                return location
        return None
    def _match_one(
        self,
        number: dict[str, Any],
        evidence: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        """为单个数字找证据，返回 location；找不到返回 None。"""
        value = float(number["value"])
        kind = number["kind"]
        total_rows = int(meta.get("total_rows") or 0)

        # 序数词（第一 / 第三）：与表行数 / 名次做宽松匹配 —— 第一名存在 ⇔ 至少 1 行
        if kind == "ordinal":
            if 1 <= value <= max(total_rows, 0):
                return {"type": "table_meta", "matched": "row_rank",
                        "total_rows": total_rows, "rank": int(value)}
            return None  # 序数名次不应去数值单元格里凑证据

        # 计数词（三个 / 3 种）：优先与表行数匹配，匹配不上再退回数值证据
        if kind == "count":
            if 1 <= value <= max(total_rows, 0):
                return {"type": "table_meta", "matched": "row_count",
                        "total_rows": total_rows, "count": int(value)}

        # 百分比 / 倍数：模型可能说成 230%、2.3 倍，也可能把基数说错（增长 230%
        # 对应 330/100，退化为"增量"口径则接近 130），三个候选都找一遍，容差 5%
        if kind in ("percent", "multiplier"):
            for candidate in (value, value / 100.0, value - 100.0):
                hit = self._search_evidence(candidate, evidence, self.percent_tol)
                if hit is not None:
                    hit["matched_value"] = candidate
                    return hit
            return None

        # 普通数字 / 金额 / 中文数量词（两千）：在证据数值集合里按 1% 容差匹配
        return self._search_evidence(value, evidence, self.number_tol)

    # ----------------------------- 4. 回溯 / 总入口 -----------------------------

    def backtrack(
        self, numbers: list[dict[str, Any]], result_data: Any
    ) -> list[dict[str, Any]]:
        """逐个数字回溯到 ``result_data``，给出 found 与证据位置。"""
        evidence, meta = self._collect_evidence(result_data)
        verdicts: list[dict[str, Any]] = []
        for number in numbers:
            location = self._match_one(number, evidence, meta)
            verdicts.append({
                "raw": number["raw"],
                "value": number["value"],
                "kind": number["kind"],
                "position": number["position"],
                "found": location is not None,
                "evidence": location,
            })
        return verdicts

    def verify_answer(self, answer_text: str, result_data: Any) -> dict[str, Any]:
        """总入口：抽取 → 回溯 → 汇总。

        返回 {"passed": bool, "total_numbers": int, "hallucination_count": int,
              "hallucinations": [找不到证据的数字判定],
              "verified_numbers": [全部数字判定]}。
        """
        numbers = self.extract_numbers(answer_text)
        verdicts = self.backtrack(numbers, result_data)
        hallucinations = [item for item in verdicts if not item["found"]]
        report = {
            "passed": not hallucinations,
            "total_numbers": len(verdicts),
            "hallucination_count": len(hallucinations),
            "hallucinations": hallucinations,
            "verified_numbers": verdicts,
        }
        if hallucinations:
            raws = "、".join(item["raw"] for item in hallucinations)
            logger.warning(
                "数字回溯发现 %d 个幻觉数字：%s（答案共 %d 个数字）",
                len(hallucinations), raws, len(verdicts),
            )
        else:
            logger.info("数字回溯通过：%d 个数字全部找到证据", len(verdicts))
        return report
# =====================================================================
# 触发重生成的编排
# =====================================================================

def _summarize_result_data(result_data: Any, max_rows: int = 20) -> str:
    """把 result_data 压成给 LLM 看的纯文本摘要（表格只取前 max_rows 行）。"""
    if not isinstance(result_data, dict):
        return str(result_data)
    rtype = result_data.get("type")
    if rtype == "table":
        columns = result_data.get("columns") or []
        rows = result_data.get("data") or []
        lines = [" | ".join(str(c) for c in columns)]
        lines.extend(
            " | ".join("" if c is None else str(c) for c in row)
            for row in rows[:max_rows]
            if isinstance(row, (list, tuple))
        )
        if len(rows) > max_rows:
            lines.append(f"（共 {len(rows)} 行，仅展示前 {max_rows} 行）")
        return "\n".join(lines)
    if rtype == "single_value":
        return f"value = {result_data.get('value')}"
    return json.dumps(result_data, ensure_ascii=False, default=str)


def _strip_answer_text(reply: str) -> str:
    """清洗 LLM 回复：去代码围栏 / 引号包裹；若是 JSON 且含总结字段则取字段。"""
    text = (reply or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            for key in ("answer", "summary", "text", "conclusion", "结论", "总结"):
                if isinstance(obj.get(key), str) and obj[key].strip():
                    return obj[key].strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    return text.strip("「」\"'“”").strip()


async def verify_and_regenerate(
    answer_text: str,
    result_data: Any,
    llm: Any,
    context: dict[str, Any] | None = None,
    max_rounds: int = 2,
) -> dict[str, Any]:
    """数字回溯 → 喂回幻觉数字让 LLM 重写 → 再回溯，最多 ``max_rounds`` 轮。

    Args:
        answer_text: LLM 第一版总结。
        result_data: executor 产出的真实结果（table / single_value / record）。
        llm: 已配置的 :class:`~app.llm.client.LLMClient`，需有 ``chat(messages)``。
        context: 可选 {"question": 用户问题, ...}，会带进重生成 prompt。
        max_rounds: 最多重新生成次数（初版校验不计轮次）。

    返回：
        {"passed", "answer", "rounds", "hallucination_count",
         "hallucinations", "history", "regeneration_error"}。
        最后一轮仍有幻觉时 passed=False，由上层决定是否放行并标记未通过。
    """
    context = context or {}
    backtracker = NumericBacktracker(max_summary_rows=20)
    current = answer_text
    history: list[dict[str, Any]] = []
    regeneration_error: str | None = None

    report = backtracker.verify_answer(current, result_data)
    rounds = 0
    while not report["passed"] and rounds < max_rounds:
        hallucinated = [
            {"raw": item["raw"], "value": item["value"], "kind": item["kind"]}
            for item in report["hallucinations"]
        ]
        question = context.get("question")
        question_line = f"用户的原始问题是：{question}\n" if question else ""
        prompt = (
            f"{question_line}你刚才的总结里有这些数字在真实数据里找不到："
            f"{json.dumps(hallucinated, ensure_ascii=False)}。\n"
            f"真实数据如下（前若干行）：\n"
            f"{_summarize_result_data(result_data, backtracker.max_summary_rows)}\n"
            "请重新写总结，这一次只基于真实数据里的数，不要编造任何数据里没有的数字。"
        )
        messages = [
            {"role": "system",
             "content": "你是 DataSage 的数据分析师。重写总结时只能引用给定真实数据中"
                        "确实存在的数字，禁止臆造任何数值、百分比或排名。直接输出总结"
                        "正文，不要输出代码。"},
            {"role": "user", "content": prompt},
        ]
        try:
            reply = await llm.chat(messages, temperature=0.0)
        except Exception as exc:  # LLM 不可用时不再重试，交给上层
            regeneration_error = f"重新生成失败：{exc}"
            logger.exception("verify_and_regenerate: LLM 重新生成失败")
            break

        current = _strip_answer_text(reply)
        rounds += 1
        history.append({
            "round": rounds,
            "removed_hallucinations": [item["raw"] for item in report["hallucinations"]],
            "answer": current,
        })
        logger.info("数字回溯：第 %d 轮重新生成完成", rounds)
        report = backtracker.verify_answer(current, result_data)

    return {
        "passed": report["passed"],
        "answer": current,
        "rounds": rounds,
        "hallucination_count": report["hallucination_count"],
        "hallucinations": report["hallucinations"],
        # 最终答案里每个数字的回溯判定（raw/value/kind/position/found/evidence），
        # 供前端做数字级溯源高亮与证据面板
        "verified_numbers": report["verified_numbers"],
        "total_numbers": report["total_numbers"],
        "history": history,
        "regeneration_error": regeneration_error,
    }
if __name__ == "__main__":
    # python -m app.analysis.verifier
    import asyncio
    import sys

    try:  # Windows GBK 控制台也能打印 ¥ / 中文
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = {
        "type": "table",
        "columns": ["region", "sales_amount"],
        "data": [["华东", 100], ["华南", 80]],
        "total_rows": 2,
    }

    print("=" * 70)
    print("场景 1：幻觉答案（真实数据只有 100 / 80，模型编造 230%）")
    hallucinated = "华南增长了 230%，表现最好"
    report = NumericBacktracker().verify_answer(hallucinated, result)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert not report["passed"]
    assert report["hallucinations"][0]["raw"] == "230%"

    print("=" * 70)
    print("场景 2：正确答案（每个数字都能回溯到表格）")
    correct = "华东销售额 100，华南 80，华东最高"
    report = NumericBacktracker().verify_answer(correct, result)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["passed"]

    print("=" * 70)
    print("场景 3：中文数字 / 序数词 / 金额 / 千分位 / 负数识别")
    text = "共有三个地区，华东排名第一，总预算约两千，客单价 ¥123（$45），" \
           "销量 1,234 件，环比 -5%，数据日期 2024-01-01"
    numbers = NumericBacktracker().extract_numbers(text)
    for item in numbers:
        print(f"  {item['raw']!r:12} -> value={item['value']:>10} "
              f"kind={item['kind']:<14} position={item['position']}")
    kinds = {(n["raw"], n["kind"], n["value"]) for n in numbers}
    assert ("三个", "count", 3.0) in kinds
    assert ("第一", "ordinal", 1.0) in kinds
    assert ("两千", "chinese_number", 2000.0) in kinds
    assert ("¥123", "currency", 123.0) in kinds
    assert ("$45", "currency", 45.0) in kinds
    assert ("1,234", "number", 1234.0) in kinds
    assert ("-5%", "percent", -5.0) in kinds
    # 日期里的 2024/1/1 不应被抽取
    assert all(not n["raw"].startswith("2024") for n in numbers)

    print("=" * 70)
    print("场景 4：发现幻觉 → 喂回 LLM 重新生成 → 再回溯通过")

    class ScriptedLLM:
        """测试用假 LLM：不管被问什么，都吐出只含真实数字的正确总结。"""

        async def chat(self, messages: list[dict], *, temperature: float | None = None):
            return "华东销售额 100，华南 80，华东表现最好。"

    final = asyncio.run(
        verify_and_regenerate(
            "华南增长了 230%，表现最好",
            result,
            ScriptedLLM(),
            context={"question": "各地区销售额对比"},
        )
    )
    print(json.dumps({k: v for k, v in final.items() if k != "history"},
                     ensure_ascii=False, indent=2))
    assert final["passed"] and final["rounds"] == 1
    print("\n全部数字回溯自测通过 ✅")

