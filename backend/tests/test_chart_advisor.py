"""chart_advisor 纯规则推荐 + 多轮追问 prompt 注入的单元测试（不启服务、不联网）。"""
from __future__ import annotations

from app.analysis.answer import build_messages as answer_messages
from app.analysis.chart_advisor import suggest_chart
from app.analysis.codegen import build_messages as codegen_messages

SCHEMA = {
    "row_count": 6,
    "columns": [
        {"name": "date", "dtype": "datetime"},
        {"name": "region", "dtype": "categorical"},
        {"name": "sales_amount", "dtype": "numeric"},
        {"name": "quantity", "dtype": "numeric"},
    ],
}

FOLLOWUP_CONTEXT = {
    "prev_session_id": 5,
    "prev_question": "各地区销售额总和？",
    "prev_plan": "按地区分组对销售额求和并降序排列",
    "prev_answer": "西南最高，销售额 300。",
    "prev_result": {
        "type": "table",
        "columns": ["region", "sales_amount"],
        "total_rows": 4,
        "data": [["西南", 300.0]],
    },
    "schema_summary": "共 6 行；列：date（datetime）、region（categorical）",
}


# ------------------------------ 图表推荐规则 ------------------------------

def test_bar_for_one_category_plus_one_metric():
    suggestion = suggest_chart(
        {"columns": ["region", "sales_amount"],
         "data": [["华东", 150.0], ["西南", 300.0]]},
        SCHEMA,
    )
    assert suggestion == {"type": "bar", "x": "region", "y": "sales_amount"}


def test_line_for_time_plus_metric():
    suggestion = suggest_chart(
        {"columns": ["date", "sales_amount"],
         "data": [["2024-01-05", 100.0]]},
        SCHEMA,
    )
    assert suggestion == {"type": "line", "x": "date", "y": "sales_amount"}


def test_scatter_for_two_numeric_columns():
    suggestion = suggest_chart(
        {"columns": ["quantity", "sales_amount"], "data": [[2, 100.0]]},
        SCHEMA,
    )
    assert suggestion == {"type": "scatter", "x": "quantity", "y": "sales_amount"}


def test_renamed_columns_are_inferred_from_values():
    """schema 没有的重命名聚合列（month/gmv）按值推断，仍能推荐折线图。"""
    suggestion = suggest_chart(
        {"columns": ["month", "gmv"], "data": [["2024-01", 100.0]]},
        {},
    )
    assert suggestion == {"type": "line", "x": "month", "y": "gmv"}


def test_no_suggestion_for_non_table_or_missing_metric():
    assert suggest_chart({"type": "single_value", "value": 1}, SCHEMA) is None
    assert suggest_chart({"columns": ["region"], "data": [["华东"]]}, SCHEMA) is None
    assert suggest_chart({"columns": [], "data": []}, SCHEMA) is None
    assert suggest_chart("not-a-dict", SCHEMA) is None


# ------------------------------ 追问 prompt ------------------------------

def test_codegen_messages_inject_followup_context():
    messages = codegen_messages(SCHEMA, "那华东呢", FOLLOWUP_CONTEXT)
    system_text, user_text = messages[0]["content"], messages[1]["content"]

    assert "【多轮对话上下文】" in user_text
    assert "各地区销售额总和？" in user_text  # 上一轮问题
    assert "西南最高，销售额 300。" in user_text  # 上一轮结论
    assert "上一轮结果摘要" in user_text
    assert "那华东呢" in user_text  # 本轮问题仍在
    assert "多轮追问" in system_text

    # 首轮提问不带上下文：system 不追加规则，user 不含上下文块
    plain = codegen_messages(SCHEMA, "各地区销售额")
    assert "【多轮对话上下文】" not in plain[1]["content"]
    assert "多轮追问" not in plain[0]["content"]


def test_answer_messages_inject_followup_context():
    messages = answer_messages(
        "那华东呢", "筛选华东后聚合", {"type": "single_value", "value": 150},
        FOLLOWUP_CONTEXT,
    )
    user_text = messages[1]["content"]
    assert "上一轮问题" in user_text
    assert "各地区销售额总和？" in user_text
    assert "本轮真实结果" in user_text
    assert "150" in user_text  # 本轮真实结果照常注入
