"""数据源 schema 提取器。

只提取喂给大模型所需的"数据骨架"（列名 / 推断类型 / 空值率 / 示例值 /
数值统计 / 日期范围），**不把全量数据载入内存**：

1. 编码探测：二进制分块流式解码，utf-8-sig 失败再试 gbk（中文 CSV 常见）；
2. 采样遍：只读前 ``SAMPLE_NROWS`` 行做类型推断与示例值提取；
3. 行数遍：``get_row_count`` 用 chunksize 流式数行（不加载数据）；
4. 统计遍：按块流式聚合空值数 / 去重数 / 数值统计，逐块丢弃。

后续步骤中，本模块的输出会序列化进 DataSource.schema_json 并提供给 LLM。
"""
from __future__ import annotations

import codecs
import os
import re
import warnings
from typing import Any

import numpy as np
import pandas as pd

# ================================ 常量 ================================

SAMPLE_NROWS = 200              # 类型推断 / 示例值采样行数
COUNT_CHUNKSIZE = 100_000       # 行数统计的块大小
STATS_CHUNKSIZE = 50_000        # 统计聚合的块大小
DISTINCT_CAP = 200_000          # 单列去重集合元素上限（内存保护）
MEDIAN_VALUES_CAP = 200_000     # 数值列中位数候选值数量上限
TYPE_MATCH_THRESHOLD = 0.8      # datetime / numeric 判定成功率阈值
HIGH_CARDINALITY_THRESHOLD = 10_000  # 文本列高基数阈值

# 匹配整数 / 小数 / 科学计数法（前后允许空白）
_NUMERIC_RE = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")


# ============================== 通用工具 ==============================

def _normalize_file_type(file_type: str | None, path: str) -> str:
    """把外部传入的文件类型归一化为 'csv' / 'xlsx'。"""
    ft = (file_type or "").strip().lower().lstrip(".")
    if ft in {"csv", "txt"}:
        return "csv"
    if ft in {"xlsx", "xlsm", "excel"}:
        return "xlsx"
    if ft == "xls":
        raise ValueError("暂不支持旧版 .xls，请另存为 .xlsx 后上传")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "csv":
        return "csv"
    if ext in {"xlsx", "xlsm"}:
        return "xlsx"
    raise ValueError(f"不支持的文件类型: {file_type!r}（仅支持 csv / xlsx）")


def detect_encoding(path: str) -> str:
    """流式探测文本文件编码：先 utf-8-sig（兼容 BOM），失败再 gbk。

    以 1MB 二进制块喂给增量解码器，任何一块解码失败即切换候选编码，
    全程不把整个文件读进内存。
    """
    for encoding in ("utf-8-sig", "gbk"):
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"  # 理论上 gbk 几乎能解码任意字节，走兜底


def _format_timestamp(ts: pd.Timestamp) -> str:
    """时间戳转字符串：整点日期只保留日期部分，否则保留完整时间。"""
    ts = pd.Timestamp(ts)
    if (ts.hour, ts.minute, ts.second, ts.microsecond) == (0, 0, 0, 0):
        return ts.strftime("%Y-%m-%d")
    return ts.isoformat()


def _to_python_number(value: Any) -> int | float:
    """numpy 数值转 Python 原生数值（保证 JSON 可序列化）。"""
    value = float(value)
    return int(value) if value.is_integer() else value


def _round_stat(value: float) -> float:
    return round(float(value), 4)


def _coerce_datetime(values: pd.Series) -> pd.Series:
    """尝试把一列样本转日期；mixed 格式需要在 pandas 2.x+ 显式指定。

    抑制 dateutil 逐元素回退解析的 UserWarning（探测文本列时必然出现，
    属于预期噪声）。
    """
    for kwargs in ({}, {"format": "mixed"}):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(values, errors="coerce", **kwargs)
        except (ValueError, TypeError):
            continue
    return pd.Series([pd.NaT] * len(values), index=values.index)


# ============================== 类型推断 ==============================

def infer_column_dtype(values: pd.Series) -> tuple[str, pd.Series | None]:
    """根据**非空样本值**推断列类型，返回 (dtype, 转换后的序列)。

    优先级：
      0. 非空值为 0                                     -> empty
      1. 非"纯数字样貌"且 pd.to_datetime 成功率 > 80%  -> datetime
         （纯数字直接跳过，避免整数被当成 ns 级时间戳误判）
      2. pd.to_numeric 成功率 > 80%                     -> numeric
      3. 其余                                           -> categorical

    返回的 converted：datetime 时为 Timestamp 序列，numeric 时为数值序列，
    categorical 时为 None。
    """
    non_null = values.dropna().astype("object")
    if len(non_null) == 0:
        return "empty", None

    numeric_like_ratio = non_null.map(
        lambda v: bool(_NUMERIC_RE.match(str(v)))
    ).mean()

    # 规则 1：datetime（纯数字样貌的列不参与，防止 12345 被解释为 1970 年时间戳）
    if numeric_like_ratio <= TYPE_MATCH_THRESHOLD:
        converted_dt = _coerce_datetime(non_null)
        if converted_dt.notna().mean() > TYPE_MATCH_THRESHOLD:
            return "datetime", converted_dt

    # 规则 2：numeric
    converted_num = pd.to_numeric(non_null, errors="coerce")
    if converted_num.notna().mean() > TYPE_MATCH_THRESHOLD:
        return "numeric", converted_num

    # 规则 3：categorical
    return "categorical", None


def _extract_sample_values(raw_values: pd.Series, dtype: str) -> list[Any]:
    """去重后取前 3 个非空样本值，保持类型语义（数值->数值，日期->字符串）。"""
    unique_raw = pd.unique(raw_values.dropna().astype("object"))[:3]
    if len(unique_raw) == 0:
        return []
    if dtype == "datetime":
        ts = pd.to_datetime(pd.Series(unique_raw), errors="coerce")
        return [_format_timestamp(t) for t in ts if pd.notna(t)]
    if dtype == "numeric":
        nums = pd.to_numeric(pd.Series(unique_raw), errors="coerce")
        return [_to_python_number(x) for x in nums if pd.notna(x)]
    return [str(v) for v in unique_raw]


# ============================== 流式聚合 ==============================

class _ColumnAggregator:
    """对单列逐块聚合全量统计量，任何时刻只持有当前块。"""

    def __init__(self, name: str, dtype: str) -> None:
        self.name = name
        self.dtype = dtype
        self.null_count = 0
        self.distinct: set[Any] = set()
        self.distinct_capped = False

        # numeric 累加量（mean 精确；中位数保留候选值，超限降级采样）
        self.num_sum: float = 0.0
        self.num_count = 0
        self.num_min: float | None = None
        self.num_max: float | None = None
        self.median_values: list[float] = []
        self.median_capped = False

        # datetime 范围
        self.dt_min: pd.Timestamp | None = None
        self.dt_max: pd.Timestamp | None = None

        # 样本中全空、但后续块出现值的列：保留前 200 个非空值用于二次推断
        self.fallback_values: list[Any] = []

    def update(self, series: pd.Series) -> None:
        series = series.astype("object")
        self.null_count += int(series.isna().sum())
        non_null = series.dropna()
        if len(non_null) == 0:
            return

        if self.dtype == "empty":
            # 样本里全空：先留证据，统计遍结束后再二次推断类型
            if len(self.fallback_values) < SAMPLE_NROWS:
                room = SAMPLE_NROWS - len(self.fallback_values)
                self.fallback_values.extend(non_null.head(room).tolist())
            self._add_distinct({str(v) for v in non_null})
            return

        if self.dtype == "numeric":
            nums = pd.to_numeric(non_null, errors="coerce")
            good = nums.dropna()
            if len(good) > 0:
                values = [float(v) for v in good.tolist()]
                self.num_sum += sum(values)
                self.num_count += len(values)
                # 注意：必须对整个 chunk 取极值，不能用 values[0]，
                # 否则单 chunk 文件（< STATS_CHUNKSIZE 行）的 min/max 会错成首行值
                self.num_min = min(values) if self.num_min is None else min(self.num_min, min(values))
                self.num_max = max(values) if self.num_max is None else max(self.num_max, max(values))
                if len(self.median_values) < MEDIAN_VALUES_CAP:
                    room = MEDIAN_VALUES_CAP - len(self.median_values)
                    self.median_values.extend(values[:room])
                    if len(values) > room:
                        self.median_capped = True
                self._add_distinct(set(values))
            return

        if self.dtype == "datetime":
            parsed = _coerce_datetime(non_null)
            good = parsed.dropna()
            if len(good) > 0:
                col_min, col_max = good.min(), good.max()
                self.dt_min = col_min if self.dt_min is None else min(self.dt_min, col_min)
                self.dt_max = col_max if self.dt_max is None else max(self.dt_max, col_max)
                self._add_distinct({_format_timestamp(t) for t in good})
            return

        # categorical
        self._add_distinct({str(v) for v in non_null})

    def _add_distinct(self, values: set[Any]) -> None:
        if self.distinct_capped:
            return
        self.distinct.update(values)
        if len(self.distinct) >= DISTINCT_CAP:
            self.distinct_capped = True


# ============================== 文件读取 ==============================

def _read_sample(
    path: str, file_type: str
) -> tuple[pd.DataFrame, str | None]:
    """只读前 SAMPLE_NROWS 行，统一按字符串读以便统一执行类型推断。"""
    if file_type == "csv":
        encoding = detect_encoding(path)
        df = pd.read_csv(
            path, nrows=SAMPLE_NROWS, dtype=str, encoding=encoding
        )
        return df, encoding
    df = pd.read_excel(path, nrows=SAMPLE_NROWS, dtype=str, engine="openpyxl")
    return df, None


def _iter_chunks(path: str, file_type: str, encoding: str | None):
    """逐块产出 DataFrame；CSV 流式分块，xlsx 单次读入（Excel 本身有行数上限）。"""
    if file_type == "csv":
        yield from pd.read_csv(
            path,
            chunksize=STATS_CHUNKSIZE,
            dtype=str,
            encoding=encoding,
            low_memory=False,
        )
    else:
        yield pd.read_excel(path, dtype=str, engine="openpyxl")


def get_row_count(
    path: str, file_type: str = "csv", encoding: str | None = None
) -> int:
    """单独统计数据行数（不含表头），不加载全量数据。

    - CSV：chunksize 分块且只读第 1 列（usecols=[0]），内存开销极小，
      且由 pandas 解析，引号内含换行的字段不会被多数；
    - xlsx：openpyxl read_only 模式读 worksheet 的 max_row。
    """
    file_type = _normalize_file_type(file_type, path)
    if file_type == "csv":
        encoding = encoding or detect_encoding(path)
        count = 0
        for chunk in pd.read_csv(
            path,
            chunksize=COUNT_CHUNKSIZE,
            usecols=[0],
            dtype=str,
            encoding=encoding,
        ):
            count += len(chunk)
        return count

    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True)
    try:
        ws = wb.active
        return max((ws.max_row or 1) - 1, 0)
    finally:
        wb.close()


# ============================== 结果组装 ==============================

def _build_column_schema(
    name: str,
    dtype: str,
    agg: _ColumnAggregator,
    row_count: int,
    sample_values: list[Any],
    fallback_sample: list[Any] | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """把聚合结果组装成单列 schema dict。"""
    null_ratio = round(agg.null_count / row_count, 6) if row_count else 0.0
    column: dict[str, Any] = {
        "name": name,
        "dtype": dtype,
        "null_count": int(agg.null_count),
        "null_ratio": null_ratio,
        "unique_count": int(len(agg.distinct)),
        "sample_values": sample_values,
        "numeric_stats": None,
        "date_range": None,
    }

    if dtype == "numeric":
        median = float(np.median(agg.median_values)) if agg.median_values else None
        if agg.median_capped and fallback_sample:
            # 候选值超上限：中位数改用样本估算，min/max/mean 仍为全量精确值
            median = float(
                pd.to_numeric(pd.Series(fallback_sample), errors="coerce").median()
            )
            meta.setdefault("stats_from_sample", {})[name] = "median"
        if agg.num_count > 0:
            column["numeric_stats"] = {
                "min": _round_stat(agg.num_min),
                "max": _round_stat(agg.num_max),
                "mean": _round_stat(agg.num_sum / agg.num_count),
                "median": _round_stat(median),
            }

    if dtype == "datetime" and agg.dt_min is not None:
        column["date_range"] = {
            "min": _format_timestamp(agg.dt_min),
            "max": _format_timestamp(agg.dt_max),
        }

    # 规则 3：文本高基数备注（直接追加到 sample_values）
    if (
        dtype == "categorical"
        and len(agg.distinct) > HIGH_CARDINALITY_THRESHOLD
        and "high_cardinality: true" not in column["sample_values"]
    ):
        column["sample_values"].append("high_cardinality: true")

    if agg.distinct_capped:
        meta.setdefault("unique_counts_approximate", {})[name] = DISTINCT_CAP

    return column


def extract_schema(file_path: str, file_type: str = "csv") -> dict[str, Any]:
    """提取 CSV / Excel 文件的数据骨架。

    Args:
        file_path: 上传文件的绝对路径。
        file_type: ``csv`` 或 ``xlsx``；留空时按扩展名推断。

    Returns:
        包含 file_name / row_count / columns / meta 的 dict，可直接 JSON 序列化。
    """
    file_type = _normalize_file_type(file_type, file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"数据文件不存在: {file_path}")

    # ---- 第 1 遍：采样（仅前 200 行），推断类型 + 提取样本值 ----
    sample_df, encoding = _read_sample(file_path, file_type)

    dtypes: dict[str, str] = {}
    sample_values: dict[str, list[Any]] = {}
    for col in sample_df.columns:
        col_name = str(col)
        dtype, converted = infer_column_dtype(sample_df[col])
        dtypes[col_name] = dtype
        sample_values[col_name] = (
            [] if dtype == "empty"
            else _extract_sample_values(sample_df[col], dtype)
        )

    # ---- 行数独立统计（不加载全量） ----
    row_count = get_row_count(file_path, file_type, encoding=encoding)

    # ---- 第 2 遍：分块流式聚合空值 / 去重 / 数值统计 / 日期范围 ----
    aggs = {
        str(col): _ColumnAggregator(str(col), dtypes[str(col)])
        for col in sample_df.columns
    }
    for chunk in _iter_chunks(file_path, file_type, encoding):
        for col in sample_df.columns:
            aggs[str(col)].update(chunk[col])

    # ---- 边界：前 200 行全空、后续有值 -> 用 fallback 二次推断 ----
    meta: dict[str, Any] = {
        "sampled_rows": min(SAMPLE_NROWS, row_count),
        "sample_size": SAMPLE_NROWS,
    }
    if encoding is not None:
        meta["encoding"] = encoding

    final_columns: list[dict[str, Any]] = []
    for col in sample_df.columns:
        name = str(col)
        agg, dtype, values = aggs[name], dtypes[name], sample_values[name]

        if dtype == "empty" and agg.fallback_values:
            fallback = pd.Series(agg.fallback_values)
            dtype, converted = infer_column_dtype(fallback)
            values = _extract_sample_values(fallback, dtype)
            meta.setdefault("reidentified_after_sample", []).append(name)
            if dtype == "numeric":
                nums = pd.to_numeric(fallback, errors="coerce").dropna()
                agg.num_sum, agg.num_count = float(nums.sum()), int(len(nums))
                agg.num_min, agg.num_max = float(nums.min()), float(nums.max())
                agg.median_values = [float(v) for v in nums]
                meta.setdefault("stats_from_sample", {})[name] = "all"
            elif dtype == "datetime" and converted is not None:
                good = converted.dropna()
                agg.dt_min, agg.dt_max = good.min(), good.max()

        final_columns.append(
            _build_column_schema(name, dtype, agg, row_count, values, values, meta)
        )

    return {
        "file_name": os.path.basename(file_path),
        "file_type": file_type,
        "row_count": int(row_count),
        "columns": final_columns,
        "meta": meta,
    }


# ============================== 自测入口 ==============================

def _generate_sales_csv(path: str, rows: int = 120_000) -> None:
    """造一个 12 万行的 sales.csv：覆盖日期/文本/数值/空值/全空/高基数列。"""
    rng = np.random.default_rng(42)
    # 2024 是闰年共 366 天；构造恰好 365 个日期，min=01-01、max=12-31
    dates = (
        pd.date_range("2024-01-01", periods=365)  # 01-01 .. 12-30
        .union(pd.DatetimeIndex(["2024-12-31"]))
        .drop("2024-12-30")
        .strftime("%Y-%m-%d")
    )
    regions = np.array(["华东", "华南", "华北", "华中", "西南", "西北", "东北"])

    df = pd.DataFrame(
        {
            "order_id": [f"ORD{i:06d}" for i in range(rows)],        # 高基数文本
            "order_date": rng.choice(dates, rows),                   # datetime
            "region": rng.choice(regions, rows),                     # categorical
            "sales_amount": rng.uniform(0, 99_999, rows).round(2),   # numeric
            # 约 95% 为空的备注列（含空值但非全空）
            "remark": np.where(
                rng.random(rows) < 0.05,
                rng.choice(["团购", "促销", "退货"], rows),
                None,
            ),
            "memo": [None] * rows,                                   # 全空 -> empty
        }
    )
    # 数值列精确造 128 个空值（与任务示例一致）
    null_idx = rng.choice(rows, size=128, replace=False)
    df.loc[null_idx, "sales_amount"] = np.nan
    df.to_csv(path, index=False, encoding="utf-8")


if __name__ == "__main__":
    import json
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="datasage_schema_")

    # ---- 场景 1：12 万行 UTF-8 CSV ----
    csv_path = os.path.join(tmp_dir, "sales.csv")
    print(f"[1/3] 生成测试文件: {csv_path}")
    _generate_sales_csv(csv_path)

    schema = extract_schema(csv_path, "csv")
    print("\n===== extract_schema(sales.csv) 结果 =====")
    print(json.dumps(schema, ensure_ascii=False, indent=2))

    by_name = {col["name"]: col for col in schema["columns"]}

    assert schema["row_count"] == 120_000, schema["row_count"]
    assert by_name["order_date"]["dtype"] == "datetime"
    assert by_name["order_date"]["date_range"] == {
        "min": "2024-01-01",
        "max": "2024-12-31",
    }
    assert by_name["order_date"]["unique_count"] == 365
    assert by_name["region"]["dtype"] == "categorical"
    assert by_name["region"]["unique_count"] == 7
    assert by_name["sales_amount"]["dtype"] == "numeric"
    assert by_name["sales_amount"]["null_count"] == 128
    assert abs(by_name["sales_amount"]["null_ratio"] - 128 / 120_000) < 1e-6
    assert by_name["sales_amount"]["numeric_stats"] is not None
    assert by_name["memo"]["dtype"] == "empty"
    assert by_name["memo"]["null_ratio"] == 1.0
    assert "high_cardinality: true" in by_name["order_id"]["sample_values"]
    assert by_name["order_id"]["unique_count"] == 120_000
    print("\n[1/3] UTF-8 大文件断言全部通过 OK")

    # ---- 场景 2：GBK 编码 CSV（验证编码回退） ----
    gbk_path = os.path.join(tmp_dir, "sales_gbk.csv")
    pd.DataFrame(
        {
            "订单日期": ["2024-01-01", "2024-02-01", "2024-03-01"],
            "大区": ["华东", "华北", "华南"],
            "金额": [100.5, 200, 300.25],
        }
    ).to_csv(gbk_path, index=False, encoding="gbk")
    gbk_schema = extract_schema(gbk_path, "csv")
    gbk_by_name = {c["name"]: c for c in gbk_schema["columns"]}
    assert gbk_schema["meta"]["encoding"] == "gbk"
    assert gbk_by_name["订单日期"]["dtype"] == "datetime"
    assert gbk_by_name["大区"]["dtype"] == "categorical"
    assert gbk_by_name["金额"]["dtype"] == "numeric"
    print("[2/3] GBK 编码回退断言通过 OK")

    # ---- 场景 3：xlsx ----
    xlsx_path = os.path.join(tmp_dir, "sales.xlsx")
    pd.DataFrame(
        {
            "order_date": ["2024-01-01", "2024-01-02"],
            "region": ["华东", "华南"],
            "sales_amount": [123.5, 89.0],
        }
    ).to_excel(xlsx_path, index=False, engine="openpyxl")
    xlsx_schema = extract_schema(xlsx_path, "xlsx")
    xlsx_by_name = {c["name"]: c for c in xlsx_schema["columns"]}
    assert xlsx_schema["row_count"] == 2
    assert xlsx_by_name["order_date"]["dtype"] == "datetime"
    assert xlsx_by_name["sales_amount"]["dtype"] == "numeric"
    print("[3/3] xlsx 解析断言通过 OK")

    print(f"\n全部自测通过，临时文件目录：{tmp_dir}")
