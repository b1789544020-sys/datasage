"""schema 提取器测试：类型识别、空值率/空值数、数值统计、日期范围、GBK 回退。

全部使用 tmp_path 下的小 CSV / xlsx，不依赖网络与既有数据文件。
"""
from __future__ import annotations

import math

import pytest

from app.schema.extractor import detect_encoding, extract_schema


def _write_csv(tmp_path, name: str, text: str, encoding: str = "utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


# 5 行数据：region 缺 1 行、amount 缺 1 行、memo 只有 1 个值、remark 全空
CSV = (
    "date,region,amount,memo,remark,order_id\n"
    "2024-01-01,华东,100,,,A001\n"
    "2024-01-02,华南,200,x,,A002\n"
    "2024-01-03,华东,,,,A003\n"
    "2024-01-04,,300,,,A004\n"
    "2024-01-05,西北,50,,,A005\n"
)


@pytest.fixture
def schema(tmp_path):
    path = _write_csv(tmp_path, "sales.csv", CSV)
    return extract_schema(str(path), "csv")


def _by_name(schema: dict) -> dict:
    return {col["name"]: col for col in schema["columns"]}


# ------------------------------ 基础结构 ------------------------------

def test_row_count_and_columns(schema):
    assert schema["row_count"] == 5
    assert set(_by_name(schema)) == {
        "date", "region", "amount", "memo", "remark", "order_id"
    }
    assert schema.get("file_type") == "csv"
    # UTF-8 文件能被 utf-8-sig 增量解码器正常解码
    assert schema["meta"]["encoding"] in ("utf-8-sig", "utf-8")


# ------------------------------ 类型识别 ------------------------------

def test_dtype_recognition(schema):
    cols = _by_name(schema)
    assert cols["date"]["dtype"] == "datetime", cols["date"]
    assert cols["region"]["dtype"] == "categorical"
    assert cols["amount"]["dtype"] == "numeric"
    assert cols["order_id"]["dtype"] == "categorical"


def test_datetime_range(schema):
    date_col = _by_name(schema)["date"]
    assert date_col["date_range"] == {"min": "2024-01-01", "max": "2024-01-05"}
    assert date_col["unique_count"] == 5


def test_categorical_samples_and_uniques(schema):
    region = _by_name(schema)["region"]
    assert region["unique_count"] == 3  # 华东 / 华南 / 西北
    samples = region["sample_values"]
    assert isinstance(samples, list)
    assert set(samples) >= {"华东", "华南"}


# ------------------------------ 空值率 ------------------------------

def test_null_count_and_ratio(schema):
    cols = _by_name(schema)
    # region 第 4 行为空：1/5
    assert cols["region"]["null_count"] == 1
    assert math.isclose(cols["region"]["null_ratio"], 0.2, rel_tol=1e-9)
    # amount 第 3 行为空：1/5
    assert cols["amount"]["null_count"] == 1
    assert math.isclose(cols["amount"]["null_ratio"], 0.2, rel_tol=1e-9)


def test_sparse_and_all_null_columns(schema):
    cols = _by_name(schema)
    # memo 5 行里只有 1 个值：仍然识别为 categorical，空值率 80%
    assert cols["memo"]["dtype"] == "categorical"
    assert cols["memo"]["null_count"] == 4
    assert math.isclose(cols["memo"]["null_ratio"], 0.8, rel_tol=1e-9)
    # remark 整列全空：empty
    assert cols["remark"]["dtype"] == "empty"
    assert cols["remark"]["null_count"] == 5
    assert math.isclose(cols["remark"]["null_ratio"], 1.0, rel_tol=1e-9)


# ------------------------------ 数值统计 ------------------------------

def test_numeric_stats(schema):
    stats = _by_name(schema)["amount"]["numeric_stats"]
    assert stats is not None
    assert stats["min"] == 50
    assert stats["max"] == 300
    # 非空 4 个值：100 / 200 / 300 / 50
    assert math.isclose(stats["mean"], 162.5, rel_tol=1e-9)
    assert math.isclose(stats["median"], 150.0, rel_tol=1e-9)


# ------------------------------ 编码探测 ------------------------------

def test_detect_encoding_gbk(tmp_path):
    text = "日期,大区,金额\n2024-01-01,华东,100.5\n2024-02-01,华北,200\n"
    path = _write_csv(tmp_path, "gbk.csv", text, encoding="gbk")
    assert detect_encoding(str(path)) == "gbk"

    schema_gbk = extract_schema(str(path), "csv")
    cols = _by_name(schema_gbk)
    assert schema_gbk["meta"]["encoding"] == "gbk"
    assert cols["大区"]["dtype"] == "categorical"
    assert cols["金额"]["dtype"] == "numeric"
    assert cols["日期"]["dtype"] == "datetime"
    assert cols["大区"]["sample_values"] == ["华东", "华北"]


def test_utf8_bom_file(tmp_path):
    path = _write_csv(tmp_path, "bom.csv", CSV, encoding="utf-8-sig")
    schema_bom = extract_schema(str(path), "csv")
    assert _by_name(schema_bom)["amount"]["dtype"] == "numeric"
    assert schema_bom["row_count"] == 5


# ------------------------------ xlsx ------------------------------

def test_xlsx_extraction(tmp_path):
    pd = pytest.importorskip("pandas")
    openpyxl = pytest.importorskip("openpyxl")  # noqa: F841
    path = tmp_path / "sales.xlsx"
    pd.DataFrame(
        {
            "order_date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "region": ["华东", "华南", "华东"],
            "sales_amount": [123.5, 89.0, 300.0],
        }
    ).to_excel(path, index=False, engine="openpyxl")

    sx = extract_schema(str(path), "xlsx")
    cols = _by_name(sx)
    assert sx["row_count"] == 3
    assert cols["order_date"]["dtype"] == "datetime"
    assert cols["region"]["dtype"] == "categorical"
    assert cols["sales_amount"]["dtype"] == "numeric"
    assert math.isclose(cols["sales_amount"]["numeric_stats"]["max"], 300.0)
