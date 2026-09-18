"""自动可视化推荐：根据结果表形状 + 数据源 schema 推断最合适的图表类型。

纯规则、不调 LLM、不访问数据库，方便单元测试。规则（按特异性排序，命中即返）：
1. 时间列 + 数值列   → 折线图 ``line``
2. 分类列 + 数值列   → 柱状图 ``bar``
3. 两列数值          → 散点图 ``scatter``
其余情况（单值结果、无指标列等）返回 None。

列类型优先取数据源 schema 的 dtype（datetime/numeric/categorical），
schema 里没有的结果列（如重命名后的聚合列）再按单元格值推断。
"""
from __future__ import annotations

import math
import re
from typing import Any

# 类日期字符串：2024-01 / 2024-01-05 / 2024-01-05T10:00[:00][.123]
_DATE_LIKE_RE = re.compile(
    r"^\d{4}-\d{1,2}(?:-\d{1,2})?"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?$"
)

_KNOWN_KINDS = ("datetime", "numeric", "categorical")
_SAMPLE_ROWS = 50


def _is_finite_number(value: Any) -> bool:
    """是否为可绘图的有限数值（排除 bool / None / NaN / Inf）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _infer_kind(values: list[Any]) -> str:
    """schema 没覆盖到的结果列：按采样值推断 datetime/numeric/categorical。"""
    sample = [v for v in values if v is not None and v != ""][:_SAMPLE_ROWS]
    if not sample:
        return "categorical"
    if all(_is_finite_number(v) for v in sample):
        return "numeric"
    if all(
        isinstance(v, str) and _DATE_LIKE_RE.match(v.strip()) is not None
        for v in sample
    ):
        return "datetime"
    return "categorical"


def suggest_chart(
    result_data: Any, schema: dict[str, Any] | None = None
) -> dict[str, str] | None:
    """对 table 型结果给出 ``{"type", "x", "y"}`` 推荐，无法推荐时返回 None。"""
    if not isinstance(result_data, dict):
        return None
    columns = result_data.get("columns")
    rows = result_data.get("data")
    if (
        not isinstance(columns, list)
        or not isinstance(rows, list)
        or not columns
        or not rows
    ):
        return None

    dtype_map = {
        str(col.get("name")): col.get("dtype")
        for col in (schema or {}).get("columns", [])
        if isinstance(col, dict) and col.get("name") is not None
    }

    kinds: dict[str, str] = {}
    for idx, raw_name in enumerate(columns):
        name = str(raw_name)
        kind = dtype_map.get(name)
        if kind not in _KNOWN_KINDS:
            column_values = [
                row[idx]
                for row in rows
                if isinstance(row, (list, tuple)) and idx < len(row)
            ]
            kind = _infer_kind(column_values)
        kinds[name] = str(kind)

    time_cols = [str(c) for c in columns if kinds[str(c)] == "datetime"]
    cat_cols = [str(c) for c in columns if kinds[str(c)] == "categorical"]
    num_cols = [str(c) for c in columns if kinds[str(c)] == "numeric"]

    if time_cols and num_cols:
        return {"type": "line", "x": time_cols[0], "y": num_cols[0]}
    if cat_cols and num_cols:
        return {"type": "bar", "x": cat_cols[0], "y": num_cols[0]}
    if len(num_cols) >= 2:
        return {"type": "scatter", "x": num_cols[0], "y": num_cols[1]}
    return None


# ============================== 自测入口 ==============================

if __name__ == "__main__":
    SCHEMA = {
        "columns": [
            {"name": "date", "dtype": "datetime"},
            {"name": "region", "dtype": "categorical"},
            {"name": "sales_amount", "dtype": "numeric"},
            {"name": "quantity", "dtype": "numeric"},
        ]
    }

    assert suggest_chart(
        {"columns": ["region", "sales_amount"], "data": [["华东", 150.0]]}, SCHEMA
    ) == {"type": "bar", "x": "region", "y": "sales_amount"}
    assert suggest_chart(
        {"columns": ["date", "sales_amount"],
         "data": [["2024-01-05", 100.0]]}, SCHEMA
    ) == {"type": "line", "x": "date", "y": "sales_amount"}
    assert suggest_chart(
        {"columns": ["quantity", "sales_amount"], "data": [[2, 100.0]]}, SCHEMA
    ) == {"type": "scatter", "x": "quantity", "y": "sales_amount"}
    # schema 没有的重命名聚合列：按值推断仍能推荐
    assert suggest_chart(
        {"columns": ["month", "gmv"], "data": [["2024-01", 100.0]]}, {}
    ) == {"type": "line", "x": "month", "y": "gmv"}
    # 非表格 / 空表 / 单分类列 → 不推荐
    assert suggest_chart({"type": "single_value", "value": 1}, SCHEMA) is None
    assert suggest_chart({"columns": ["region"], "data": [["华东"]]}, SCHEMA) is None
    print("chart_advisor 自测通过")
