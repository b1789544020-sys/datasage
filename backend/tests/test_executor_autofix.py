"""沙箱执行 + LLM 自动修复测试。

覆盖：
1. 坏代码（拼错列名）首次执行失败 -> LLM 修复 -> 第二次成功；
2. 内置 mock LLM 的字符串替换修复同样有效；
3. 首次成功时不调用 LLM（attempts=1）；
4. 连续修复失败达到 max_attempts 后放弃，返回最后一次错误；
5. 修复调用本身抛异常时错误信息可解释；
6. 危险 import 被沙箱拦截后也能走修复链路恢复。
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from app.analysis.executor import execute_code, execute_with_autofix
from app.llm.client import LLMClient

DF = pd.DataFrame(
    {
        "region": ["华东", "华东", "华南"],
        "sales_amount": [100, 50, 80],
    }
)

SCHEMA = {
    "file_name": "sales.csv",
    "row_count": 3,
    "columns": [
        {"name": "region", "dtype": "categorical", "null_ratio": 0.0,
         "unique_count": 2, "sample_values": ["华东", "华南"],
         "numeric_stats": None, "date_range": None},
        {"name": "sales_amount", "dtype": "numeric", "null_ratio": 0.0,
         "unique_count": 3, "sample_values": [100, 50, 80],
         "numeric_stats": {"min": 50, "max": 100, "mean": 76.67, "median": 80},
         "date_range": None},
    ],
}

BUGGY_CODE = (
    "df = load_dataframe()\n"
    "result = df.groupby('regoin', as_index=False)['sales_amount'].sum()\n"
    "result = result.sort_values('sales_amount', ascending=False)\n"
    "RESULT = result\n"
)

FIXED_CODE = BUGGY_CODE.replace("'regoin'", "'region'")


class ScriptedLLM:
    """按调用顺序返回修复代码的假 LLM；boom=True 时直接抛异常。"""

    def __init__(self, replies, boom: bool = False):
        self.replies = list(replies)
        self.calls = 0
        self.boom = boom

    async def chat(self, messages, *, temperature=None, **kwargs):
        self.calls += 1
        if self.boom:
            raise RuntimeError("LLM 服务不可用")
        return self.replies.pop(0)


class MustNotCallLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("首次执行就成功时不应调用 LLM 修复")


def _run(coro):
    return asyncio.run(coro)


# ------------------------------ 正常执行 ------------------------------

def test_good_code_executes_without_llm():
    res = _run(
        execute_with_autofix(
            FIXED_CODE, DF, MustNotCallLLM(), max_attempts=3,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is True
    assert res["attempts"] == 1
    table = res["result_data"]
    assert table["columns"] == ["region", "sales_amount"]
    assert table["data"][0] == ["华东", 150.0]
    assert table["data"][1] == ["华南", 80.0]


# ------------------------------ 自动修复主路径 ------------------------------

def test_buggy_code_is_fixed_on_second_attempt():
    llm = ScriptedLLM([FIXED_CODE])
    res = _run(
        execute_with_autofix(
            BUGGY_CODE, DF, llm, max_attempts=3,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )

    assert res["success"] is True, res["error"]
    assert res["attempts"] == 2
    assert llm.calls == 1

    history = res["autofix_history"]
    assert len(history) == 2
    assert history[0]["success"] is False
    assert "regoin" in history[0]["error"]
    assert history[1]["success"] is True

    # 修复后的结果正确
    assert res["result_data"]["data"][0] == ["华东", 150.0]


def test_builtin_mock_client_autofix_works():
    """不注入假 LLM：mock provider 的 _llm_fix_code 走列名替换修复。"""
    mock_llm = LLMClient("mock", "", "", "mock-1")
    res = _run(
        execute_with_autofix(
            BUGGY_CODE, DF, mock_llm, max_attempts=3,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is True, res["error"]
    assert res["attempts"] == 2
    assert res["result_data"]["data"][0] == ["华东", 150.0]


def test_two_failures_then_fixed_on_third_attempt():
    # 第一次"修复"仍返回坏代码，第二次才给对
    llm = ScriptedLLM([BUGGY_CODE, FIXED_CODE])
    res = _run(
        execute_with_autofix(
            BUGGY_CODE, DF, llm, max_attempts=3,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is True
    assert res["attempts"] == 3
    assert llm.calls == 2
    assert [h["success"] for h in res["autofix_history"]] == [False, False, True]


# ------------------------------ 修复失败 / 放弃 ------------------------------

def test_exhausted_attempts_returns_last_error():
    llm = ScriptedLLM([BUGGY_CODE, BUGGY_CODE])  # 两次都修不好
    res = _run(
        execute_with_autofix(
            BUGGY_CODE, DF, llm, max_attempts=2,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is False
    assert res["attempts"] == 2
    assert "regoin" in res["error"]
    assert res["result_data"] is None
    assert len(res["autofix_history"]) == 2
    assert all(not h["success"] for h in res["autofix_history"])


def test_fix_call_exception_is_reported():
    llm = ScriptedLLM([], boom=True)
    res = _run(
        execute_with_autofix(
            BUGGY_CODE, DF, llm, max_attempts=3,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is False
    assert "autofix_error" in res
    assert "RuntimeError" in res["autofix_error"]
    assert "LLM" in res["autofix_error"]
    assert llm.calls == 1  # 修复调用本身炸了，立即返回，不空转重试


# ------------------------------ 危险代码被拦后也能修复 ------------------------------

def test_dangerous_import_blocked_then_autofixed():
    dangerous = (
        "import os\n"
        "df = load_dataframe()\n"
        "result = df.groupby('region', as_index=False)['sales_amount'].sum()\n"
        "RESULT = result\n"
    )
    llm = ScriptedLLM([
        dangerous.replace("import os\n", "")  # 去掉违禁 import 即合法
    ])
    first = _run(execute_code(dangerous, DF, timeout=15))
    assert first["success"] is False
    assert "沙箱禁止导入模块" in first["error"]

    res = _run(
        execute_with_autofix(
            dangerous, DF, llm, max_attempts=2,
            schema=SCHEMA, question="各地区销售额", timeout=15,
        )
    )
    assert res["success"] is True, res["error"]
    assert res["attempts"] == 2
    assert res["result_data"]["data"][0] == ["华东", 150.0]
