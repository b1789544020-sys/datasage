"""verify_result 三层验证的单元测试（纯函数，无需 LLM / 数据库）。"""

from __future__ import annotations

import math

from app.analysis.verifier import verify_result


def _layer_status(report: dict, layer: str) -> str:
    """汇总某一层所有检查：全部 pass 才记 pass，否则 fail。"""
    statuses = {c["status"] for c in report["checks"] if c["layer"] == layer}
    assert statuses, f"缺少 {layer} 层检查"
    return "pass" if statuses == {"pass"} else "fail"


def _table_result(rows: list, columns=("region", "sales_amount")) -> dict:
    """按 executor 的 table 契约构造一个执行成功的结果。"""
    return {
        "success": True,
        "result_type": "table",
        "result_data": {
            "type": "table",
            "columns": list(columns),
            "data": rows,
            "total_rows": len(rows),
            "returned_rows": len(rows),
            "truncated": False,
        },
        "error": "",
        "stdout": "",
    }


# ---------------- 验收用例 1：正常结果三层全 pass ----------------

def test_normal_result_passes_all_three_layers():
    result = _table_result([["华东", 300.0], ["西北", 50.0], ["华北", 120.0]])

    report = verify_result(result, question="各地区销售额分别是多少？")

    assert report["passed"] is True
    assert _layer_status(report, "syntax") == "pass"
    assert _layer_status(report, "semantic") == "pass"
    assert _layer_status(report, "consistency") == "pass"
    assert all(c["status"] == "pass" for c in report["checks"])


# ---------------- 验收用例 2：全 NaN 表在 semantic 层 fail ----------------

def test_all_nan_table_fails_at_semantic_layer():
    # executor 会把 NaN 序列化成 None，这里模拟"每列每行都为空"的表
    result = _table_result(
        [[None, None], [None, None], [None, None]],
        columns=("region", "sales_amount"),
    )

    report = verify_result(result, question="各地区销售额分别是多少？")

    assert report["passed"] is False
    assert _layer_status(report, "syntax") == "pass"          # 结构本身没问题
    assert _layer_status(report, "semantic") == "fail"       # 语义层必须拦下
    assert _layer_status(report, "consistency") == "pass"    # 占位层不受影响
    detail = " ".join(
        c["detail"] for c in report["checks"] if c["layer"] == "semantic"
    )
    assert "全为空" in detail
    assert "region" in detail and "sales_amount" in detail


# ---------------- 验收用例 3：NaN 单值结果 fail ----------------

def test_nan_single_value_fails():
    result = {
        "success": True,
        "result_type": "single_value",
        "result_data": {"type": "single_value", "value": float("nan")},
        "error": "",
        "stdout": "",
    }

    report = verify_result(result, question="平均销售额是多少？")

    assert report["passed"] is False
    assert _layer_status(report, "syntax") == "pass"
    assert _layer_status(report, "semantic") == "fail"
    assert any("单值结果无效" in c["detail"] for c in report["checks"])


# ---------------- 补充边界用例 ----------------

def test_none_and_inf_single_value_fail():
    base = {
        "success": True,
        "result_type": "single_value",
        "error": "",
        "stdout": "",
    }
    for bad in (None, math.inf, -math.inf):
        result = {**base, "result_data": {"type": "single_value", "value": bad}}
        report = verify_result(result, "平均销售额是多少？")
        assert report["passed"] is False, f"坏值 {bad!r} 未被拦截"
        assert _layer_status(report, "semantic") == "fail"


def test_empty_table_fails_semantic():
    result = _table_result([])
    report = verify_result(result, "销售额最高的地区是？")
    assert report["passed"] is False
    assert _layer_status(report, "semantic") == "fail"
    assert any("结果表为空" in c["detail"] for c in report["checks"])


def test_iqr_outliers_are_marked_but_not_failed():
    # 1000 明显是离群值：应被 IQR 规则标记，但整体仍然 pass（不拦截）
    result = _table_result(
        [["a", 10], ["b", 11], ["c", 12], ["d", 13], ["e", 1000]],
        columns=("name", "amount"),
    )
    report = verify_result(result, "列出所有金额")
    assert report["passed"] is True
    outlier_checks = [
        c for c in report["checks"] if c["layer"] == "semantic" and "离群" in c["detail"]
    ]
    assert len(outlier_checks) == 1
    assert outlier_checks[0]["status"] == "pass"
    assert "amount" in outlier_checks[0]["detail"]


def test_failed_execution_fails_at_syntax_layer():
    result = {
        "success": False,
        "result_type": None,
        "result_data": None,
        "error": "KeyError: 'regoin'",
        "stdout": "",
    }
    report = verify_result(result, "各地区销售额")
    assert report["passed"] is False
    assert _layer_status(report, "syntax") == "fail"
    detail = " ".join(c["detail"] for c in report["checks"] if c["layer"] == "syntax")
    assert "regoin" in detail  # 失败原因应透传到 detail


# ------------------------------ 补充边界用例 ------------------------------

def test_missing_result_data_fails_syntax():
    result = {"success": True, "result_type": "table", "result_data": None}
    report = verify_result(result, "各地区销售额")
    assert report["passed"] is False
    assert _layer_status(report, "syntax") == "fail"
    assert any("result_data" in c["detail"] for c in report["checks"]
               if c["layer"] == "syntax")


def test_unknown_result_type_fails_semantic():
    result = {
        "success": True,
        "result_type": "heatmap",
        "result_data": {"anything": [1, 2, 3]},
    }
    report = verify_result(result, "画个热力图")
    assert report["passed"] is False
    assert _layer_status(report, "semantic") == "fail"
    assert any("heatmap" in c["detail"] for c in report["checks"]
               if c["layer"] == "semantic")


def test_record_result_passes_semantic():
    result = {
        "success": True,
        "result_type": "record",
        "result_data": {"rows": 3, "mean": 116.67},
    }
    report = verify_result(result, "数据概况")
    assert report["passed"] is True
    assert _layer_status(report, "semantic") == "pass"


def test_oversized_table_fails_semantic_without_building_rows():
    # total_rows 可由执行器直接给出，无需真的造 100 万行
    result = {
        "success": True,
        "result_type": "table",
        "result_data": {
            "type": "table",
            "columns": ["region", "sales_amount"],
            "data": [["华东", 1]],
            "total_rows": 2_000_000,
            "returned_rows": 1,
            "truncated": True,
        },
    }
    report = verify_result(result, "导出明细")
    assert _layer_status(report, "semantic") == "fail"
    detail = " ".join(c["detail"] for c in report["checks"]
                      if c["layer"] == "semantic")
    assert "上限" in detail or "超过" in detail


def test_non_dict_input_is_rejected():
    report = verify_result(["not", "a", "dict"], "随便问问")  # type: ignore[arg-type]
    assert report["passed"] is False


def test_consistency_layer_runs_backtrack_when_answer_given():
    result = _table_result([["华东", 100], ["华南", 80]])
    bad = verify_result(result, "对比", answer_text="华南增长 230%")
    good = verify_result(result, "对比", answer_text="华东 100，华南 80")
    assert _layer_status(bad, "consistency") == "fail"
    assert _layer_status(good, "consistency") == "pass"


def test_empty_answer_skips_consistency_check():
    result = _table_result([["华东", 100]])
    report = verify_result(result, "对比", answer_text="   ")
    assert _layer_status(report, "consistency") == "pass"
