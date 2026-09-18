"""数字回溯（反幻觉）校验器测试。

覆盖：
1. 百分比幻觉：230% 在真实数据中找不到 → 抓出
2. 普通数字匹配：100 / 80 回溯到表格单元格
3. 中文数字识别：三个 / 第一 / 两千
以及金额、千分位、序数词行数匹配、single_value / record 证据、
"幻觉 → 重新生成 → 再校验通过" 的完整编排。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.analysis.verifier import (
    NumericBacktracker,
    verify_and_regenerate,
    verify_result,
)


# 验收数据：真实结果只有 华东=100、华南=80
REGION_RESULT = {
    "type": "table",
    "columns": ["region", "sales_amount"],
    "data": [["华东", 100], ["华南", 80]],
    "total_rows": 2,
    "truncated": False,
}


# ---------------- 用例 1：百分比幻觉 ----------------

def test_percent_hallucination_is_caught():
    bt = NumericBacktracker()
    answer = "华南增长了 230%，表现最好"

    numbers = bt.extract_numbers(answer)
    assert len(numbers) == 1
    assert numbers[0]["raw"] == "230%"
    assert numbers[0]["value"] == 230.0
    assert numbers[0]["kind"] == "percent"

    verdicts = bt.backtrack(numbers, REGION_RESULT)
    assert verdicts[0]["found"] is False
    assert verdicts[0]["evidence"] is None
    # 230 / 2.3 / 130 三个候选与证据 {100, 80} 都对不上（5% 容差）

    report = bt.verify_answer(answer, REGION_RESULT)
    assert report["passed"] is False
    assert report["total_numbers"] == 1
    assert report["hallucination_count"] == 1
    assert report["hallucinations"][0]["raw"] == "230%"


@pytest.mark.parametrize("answer", [
    "华南增长了 130%",       # 候选 130 与 100 相差 30%，超出 5% 容差
    "华南增长了 50%",        # 50 / 0.5 / -50 都不在 {100, 80}
    "这是 2.3 倍的增长",     # 2.3 / 230 / 130 同样无证据
])
def test_more_percent_hallucinations(answer):
    report = NumericBacktracker().verify_answer(answer, REGION_RESULT)
    assert report["passed"] is False


def test_percent_that_matches_evidence_passes():
    # 80% 分不清是"80%"还是"80 这个数"，按宽松规则应能对上华南的 80
    report = NumericBacktracker().verify_answer("华南完成了 80%", REGION_RESULT)
    assert report["passed"] is True
    assert report["verified_numbers"][0]["found"] is True


# ---------------- 用例 2：普通数字匹配 ----------------

def test_plain_numbers_match_cells_with_location():
    bt = NumericBacktracker()
    answer = "华东销售额 100，华南 80"

    report = bt.verify_answer(answer, REGION_RESULT)
    assert report["passed"] is True
    assert report["hallucination_count"] == 0
    assert report["total_numbers"] == 2

    by_raw = {item["raw"]: item for item in report["verified_numbers"]}
    # 证据必须给出在 result_data 里的行 / 列位置
    assert by_raw["100"]["evidence"]["row"] == 0
    assert by_raw["100"]["evidence"]["column"] == "sales_amount"
    assert by_raw["100"]["evidence"]["evidence_value"] == 100.0
    assert by_raw["80"]["evidence"]["row"] == 1
    assert by_raw["80"]["evidence"]["column"] == "sales_amount"


def test_tolerance_and_rounding_match():
    result = {"type": "table", "columns": ["x"], "data": [[99.6], [116.67]]}
    bt = NumericBacktracker()
    # 100 对 99.6：整数四舍五入匹配；117 对 116.67：1% 容差内
    report = bt.verify_answer("数值分别是 100 和 117", result)
    assert report["passed"] is True


def test_unrelated_plain_number_is_hallucination():
    report = NumericBacktracker().verify_answer("销售额高达 999", REGION_RESULT)
    assert report["passed"] is False
    assert report["hallucinations"][0]["raw"] == "999"


def test_single_value_and_record_evidence():
    bt = NumericBacktracker()
    single = {"type": "single_value", "value": 42}
    report = bt.verify_answer("答案是 42", single)
    assert report["passed"] is True
    assert report["verified_numbers"][0]["evidence"] == {
        "type": "single_value", "evidence_value": 42.0,
    }

    record = {"rows": 3, "mean": 116.67}
    report = bt.verify_answer("共 3 行，均值 116.67", record)
    assert report["passed"] is True
    locations = {item["evidence"]["key"] for item in report["verified_numbers"]}
    assert locations == {"rows", "mean"}
# ---------------- 用例 3：中文数字识别 ----------------

def test_chinese_numbers_recognized():
    text = "共有三个地区，华东排名第一，去年销售额约两千"
    numbers = NumericBacktracker().extract_numbers(text)

    by_raw = {n["raw"]: n for n in numbers}
    assert by_raw["三个"]["value"] == 3.0
    assert by_raw["三个"]["kind"] == "count"
    assert by_raw["第一"]["value"] == 1.0
    assert by_raw["第一"]["kind"] == "ordinal"
    assert by_raw["两千"]["value"] == 2000.0
    assert by_raw["两千"]["kind"] == "chinese_number"
    # position 是该数字表达在原文中的字符下标
    assert by_raw["第一"]["position"] == text.index("第一")


def test_ordinal_and_count_against_row_count():
    bt = NumericBacktracker()
    # 表里只有 2 行：第一（名次）成立，三个（计数）不成立
    verdicts = {
        item["raw"]: item
        for item in bt.backtrack(bt.extract_numbers("三个地区中华东第一"), REGION_RESULT)
    }
    assert verdicts["第一"]["found"] is True
    assert verdicts["第一"]["evidence"]["matched"] == "row_rank"
    assert verdicts["三个"]["found"] is False

    # 3 行表：三个地区就有证据了
    three_rows = {
        "type": "table",
        "columns": ["region", "sales_amount"],
        "data": [["华东", 100], ["华南", 80], ["华北", 60]],
        "total_rows": 3,
    }
    verdicts = {
        item["raw"]: item
        for item in bt.backtrack(bt.extract_numbers("三个地区"), three_rows)
    }
    assert verdicts["三个"]["found"] is True
    assert verdicts["三个"]["evidence"]["matched"] == "row_count"


@pytest.mark.parametrize("text,expected", [
    ("第十一", 11),
    ("二十三", 23),
    ("一万二千", 12000),
    ("两千三百万", 23_000_000),
])
def test_complex_chinese_numerals(text, expected):
    numbers = NumericBacktracker().extract_numbers(text + "，")
    assert numbers and numbers[0]["value"] == float(expected)


def test_currency_thousands_negative_and_dates():
    bt = NumericBacktracker()
    numbers = bt.extract_numbers(
        "客单价 ¥123（$45），销量 1,234，环比 -5%，日期 2024-01-01，2024年"
    )
    by_raw = {n["raw"]: n for n in numbers}
    assert by_raw["¥123"]["kind"] == "currency" and by_raw["¥123"]["value"] == 123.0
    assert by_raw["$45"]["kind"] == "currency" and by_raw["$45"]["value"] == 45.0
    assert by_raw["1,234"]["kind"] == "number" and by_raw["1,234"]["value"] == 1234.0
    assert by_raw["-5%"]["kind"] == "percent" and by_raw["-5%"]["value"] == -5.0
    # 日期 / 年份不是结论性数字，整体跳过
    assert all("2024" not in n["raw"] for n in numbers)


def test_wan_yi_magnitude_unit_is_not_a_zero_claim():
    # "1000万，"里孤立的量级字"万"不得被解析成数字 0
    numbers = NumericBacktracker().extract_numbers("销售额约1000万，利润翻倍")
    raws = [n["raw"] for n in numbers]
    assert "1000" in raws
    assert not any(n["value"] == 0 for n in numbers)


def test_realistic_wanyuan_answer_passes():
    # LLM 常见口径："100万元 / 80万元 / 第一" 不应产生假幻觉
    answer = "华东销售额100万元，华南80万元，华东第一"
    report = NumericBacktracker().verify_answer(answer, REGION_RESULT)
    assert report["passed"] is True
    assert report["total_numbers"] == 3


# ---------------- verify_result 集成 ----------------

def test_verify_result_runs_backtrack_when_answer_given():
    # executor 信封：success + result_data（table 形态与 REGION_RESULT 一致）
    result = {
        "success": True,
        "result_type": "table",
        "result_data": dict(REGION_RESULT),
    }
    report = verify_result(result, "各地区销售额", answer_text="华南增长了 230%")
    consistency = [c for c in report["checks"] if c["layer"] == "consistency"][0]
    assert consistency["status"] == "fail"
    assert "230%" in consistency["detail"]

    report = verify_result(result, "各地区销售额", answer_text="华东 100，华南 80")
    consistency = [c for c in report["checks"] if c["layer"] == "consistency"][0]
    assert consistency["status"] == "pass"


# ---------------- verify_and_regenerate 编排 ----------------

class ScriptedLLM:
    """记录调用次数、按顺序返回脚本化回复的假 LLM。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen_prompts = []

    async def chat(self, messages, *, temperature=None, **kwargs):
        self.calls += 1
        self.seen_prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


def test_verify_and_regenerate_fixes_hallucination():
    llm = ScriptedLLM(["华东销售额 100，华南 80，华东表现最好。"])

    final = asyncio.run(verify_and_regenerate(
        "华南增长了 230%，表现最好",
        REGION_RESULT,
        llm,
        context={"question": "各地区销售额对比"},
    ))

    assert final["passed"] is True
    assert final["rounds"] == 1
    assert final["hallucination_count"] == 0
    assert llm.calls == 1
    # 重生成 prompt 必须包含幻觉数字 + 真实数据摘要 + 指定话术
    prompt = llm.seen_prompts[0]
    assert "230%" in prompt
    assert "华东" in prompt and "100" in prompt
    assert "不要编造任何数据里没有的数字" in prompt


def test_verify_and_regenerate_passes_through_clean_answer():
    # 第一版就没有幻觉：不调 LLM，原样返回
    llm = ScriptedLLM([])
    final = asyncio.run(verify_and_regenerate("华东 100，华南 80", REGION_RESULT, llm))
    assert final["passed"] is True
    assert final["rounds"] == 0
    assert llm.calls == 0


def test_verify_and_regenerate_marks_remaining_hallucinations():
    # LLM 两轮都坚持幻觉：达到 max_rounds 后标记未通过并保留剩余幻觉
    llm = ScriptedLLM(["还是华南增长了 230%", "依然是 230% 的增长"])
    final = asyncio.run(verify_and_regenerate(
        "华南增长了 230%", REGION_RESULT, llm, max_rounds=2,
    ))
    assert final["passed"] is False
    assert final["rounds"] == 2
    assert llm.calls == 2
    assert final["hallucination_count"] >= 1
    assert all(item["raw"] for item in final["hallucinations"])


def test_regeneration_json_reply_is_unwrapped():
    from app.analysis.verifier import _strip_answer_text

    reply = json.dumps({"summary": "华东 100，华南 80"}, ensure_ascii=False)
    assert _strip_answer_text(reply) == "华东 100，华南 80"
    assert _strip_answer_text("```\n华东 100\n```") == "华东 100"


# =====================================================================
# 重点补充：百分比幻觉 / 普通数字 / 中文数字 / 金额 / 千分位 / 派生证据
# =====================================================================


@pytest.mark.parametrize("text,raw", [
    ("销售额同比增长 35%", "35%"),
    ("环比下降了 12.5%", "12.5%"),
    ("同比增长百分之三十五", "百分之三十五"),
    ("提升了 73 个百分点", "73 个百分点"),
    ("利润率为 -5%", "-5%"),
])
def test_percent_hallucination_family(text, raw):
    report = NumericBacktracker().verify_answer(text, REGION_RESULT)
    assert report["passed"] is False
    fake = next(item for item in report["hallucinations"] if item["raw"] == raw)
    assert fake["kind"] == "percent"
    assert fake["evidence"] is None


def test_negative_percent_value_is_minus_five():
    numbers = NumericBacktracker().extract_numbers("利润率为 -5%")
    assert numbers[0]["raw"] == "-5%"
    assert numbers[0]["value"] == -5.0


def test_chinese_percent_extracts_number_and_fails():
    numbers = NumericBacktracker().extract_numbers("同比增长百分之三十五")
    assert numbers[0]["kind"] == "percent"
    assert numbers[0]["value"] == 35.0


def test_percentage_point_extracts_as_percent():
    numbers = NumericBacktracker().extract_numbers("提升了 12 个百分点")
    assert numbers[0]["kind"] == "percent"
    assert numbers[0]["value"] == 12.0


def test_percent_candidates_cover_ratio_and_growth_calibers():
    # 证据只有 130：增长 230% 的候选口径 230 / 2.3 / (230-100)=130 中 130 命中
    result = {"type": "table", "columns": ["v"], "data": [[130]]}
    assert NumericBacktracker().verify_answer("同比增长 230%", result)["passed"] is True
    # 200% 的三种口径 200 / 2.0 / 100 都离 130 超过 5% 百分比容差 -> 幻觉
    assert NumericBacktracker().verify_answer("同比增长 200%", result)["passed"] is False


def test_multiplier_hallucination_and_value():
    numbers = NumericBacktracker().extract_numbers("这是 1.5 倍的增长")
    assert numbers[0]["raw"] == "1.5 倍"
    assert numbers[0]["value"] == 1.5
    report = NumericBacktracker().verify_answer("这是 1.5 倍的增长", REGION_RESULT)
    assert report["passed"] is False


def test_multiplier_two_matches_plain_evidence():
    # "2 倍"的原始口径候选 2 与证据 2 精确匹配 -> 放过
    result = {"type": "table", "columns": ["v"], "data": [[2]]}
    report = NumericBacktracker().verify_answer("实现了 2 倍增长", result)
    assert report["passed"] is True


def test_currency_variants_match_cells():
    result = {"type": "table", "columns": ["sales"], "data": [[123], [45]]}
    report = NumericBacktracker().verify_answer("销售额 ¥123，成本 $45", result)
    assert report["passed"] is True
    raws = {n["raw"]: n for n in report["verified_numbers"]}
    assert raws["¥123"]["kind"] == "currency"
    assert raws["$45"]["kind"] == "currency"


def test_currency_hallucination_caught():
    report = NumericBacktracker().verify_answer("销售额高达 ¥999", REGION_RESULT)
    assert report["passed"] is False
    assert report["hallucinations"][0]["raw"] == "¥999"


def test_thousands_separator_number_matches():
    result = {"type": "table", "columns": ["qty"], "data": [[1234]]}
    numbers = NumericBacktracker().extract_numbers("销量 1,234 件")
    assert numbers[0]["value"] == 1234.0
    report = NumericBacktracker().verify_answer("销量 1,234 件", result)
    assert report["passed"] is True


def test_decimal_number_matches_precisely():
    result = {"type": "table", "columns": ["pi"], "data": [[3.14]]}
    assert NumericBacktracker().verify_answer("结果是 3.14", result)["passed"] is True
    # 1% 容差约为 [3.11, 3.17]，3.9 明显超出 -> 幻觉
    assert NumericBacktracker().verify_answer("结果是 3.9", result)["passed"] is False


# ---- 中文数字（计数词 / 序数词 / 数词 / 万级单位）----

def test_chinese_count_matches_row_count():
    report = NumericBacktracker().verify_answer("共有两个地区参与排名", REGION_RESULT)
    assert report["passed"] is True
    verdict = report["verified_numbers"][0]
    assert verdict["raw"] == "两个"
    assert verdict["value"] == 2.0
    assert verdict["evidence"]["matched"] == "row_count"


def test_chinese_count_exceeding_rows_is_hallucination():
    report = NumericBacktracker().verify_answer("共有三个地区", REGION_RESULT)
    assert report["passed"] is False
    assert report["hallucinations"][0]["raw"] == "三个"


def test_chinese_ordinal_second_matches_rank():
    numbers = NumericBacktracker().extract_numbers("华南排名第二")
    assert numbers[0]["raw"] == "第二"
    assert numbers[0]["value"] == 2.0
    report = NumericBacktracker().verify_answer("华南排名第二", REGION_RESULT)
    assert report["passed"] is True
    assert report["verified_numbers"][0]["evidence"]["matched"] == "row_rank"


def test_ordinal_beyond_row_count_fails():
    report = NumericBacktracker().verify_answer("华南排名第九", REGION_RESULT)
    assert report["passed"] is False


def test_chinese_number_word_matches_cell():
    result = {"type": "table", "columns": ["v"], "data": [[2000]]}
    report = NumericBacktracker().verify_answer("去年销售额约两千", result)
    assert report["passed"] is True
    assert report["verified_numbers"][0]["value"] == 2000.0


def test_complex_chinese_number_parsing():
    numbers = NumericBacktracker().extract_numbers("销售额一万二千三百四十五")
    assert any(n["value"] == 12345.0 for n in numbers)


def test_wan_unit_matches_large_cell():
    result = {"type": "table", "columns": ["gmv"], "data": [[1_000_000]]}
    report = NumericBacktracker().verify_answer("GMV 达一百万", result)
    assert report["passed"] is True


# ---- 普通数字：容差边界；保守策略：派生数（求和/均值）也要出现在结果里 ----

def test_tolerance_boundaries():
    result = {"type": "table", "columns": ["v"], "data": [[100]]}
    assert NumericBacktracker().verify_answer("约 99", result)["passed"] is True
    assert NumericBacktracker().verify_answer("约 98", result)["passed"] is False


def test_derived_aggregates_are_held_to_the_same_bar():
    # 当前回溯策略很保守：答案里的每个数字都必须在沙箱结果中出现。
    # 结果只有 100 / 80 时，"合计 180 / 平均 90 / 差 20" 这类模型自行
    # 推导的数字同样判为无法回溯（避免模型用错误算式"洗"出数字）。
    report = NumericBacktracker().verify_answer(
        "合计 180，平均 90，差距 20", REGION_RESULT
    )
    assert report["passed"] is False
    assert {item["raw"] for item in report["hallucinations"]} == {"180", "90", "20"}


# ---- 混合答案：真话与幻觉并存，逐条判定 ----

def test_mixed_answer_marks_only_fake_numbers():
    report = NumericBacktracker().verify_answer(
        "华东销售额 100，但华南同比增长 50%", REGION_RESULT
    )
    assert report["passed"] is False
    by_raw = {v["raw"]: v for v in report["verified_numbers"]}
    assert by_raw["100"]["found"] is True
    assert by_raw["100"]["evidence"]["column"] == "sales_amount"
    assert by_raw["50%"]["found"] is False
    assert report["hallucination_count"] == 1


def test_multiple_hallucinations_all_listed():
    report = NumericBacktracker().verify_answer(
        "总额 999，增长 230%，达到 7.5 倍", REGION_RESULT
    )
    raws = {item["raw"] for item in report["hallucinations"]}
    assert {"999", "230%", "7.5 倍"} <= raws


# ---- 日期数字不参与回溯；无数字答案天然通过 ----

def test_date_like_numbers_are_ignored_completely():
    numbers = NumericBacktracker().extract_numbers(
        "截至 2024-03-01，华东销售额 100；14:30 更新"
    )
    values = {n["value"] for n in numbers}
    assert 100.0 in values
    assert not any(v in values for v in (2024.0, 3.0, 1.0, 14.0, 30.0))


def test_answer_without_numbers_passes():
    report = NumericBacktracker().verify_answer(
        "华东销售额最高，表现最好", REGION_RESULT
    )
    assert report["passed"] is True
    assert report["total_numbers"] == 0
    assert report["hallucination_count"] == 0


# ---- single_value / record 证据位置 ----

def test_single_value_scalar_backtrack():
    result = {"type": "single_value", "value": 5230}
    report = NumericBacktracker().verify_answer("总销售额 5230", result)
    assert report["passed"] is True
    evidence = report["verified_numbers"][0]["evidence"]
    assert evidence["type"] == "single_value"
    assert evidence["evidence_value"] == 5230.0


def test_record_nested_field_backtrack():
    # executor 的 record 结果就是裸 dict（不加 type 包装）
    result = {"total": 6520, "top": {"sales": 2450}}
    report = NumericBacktracker().verify_answer("合计 6520，最高 2450", result)
    assert report["passed"] is True
    paths = {v["evidence"]["key"] for v in report["verified_numbers"]}
    assert "total" in paths and "top.sales" in paths
    assert all(v["evidence"]["type"] == "record" for v in report["verified_numbers"])