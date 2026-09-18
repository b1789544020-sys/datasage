"""LLM 生成代码的隔离执行器（v1：进程内受限命名空间 + 线程超时）。

为什么不直接 exec：LLM 生成的代码不可信，可能死循环、删文件、占满内存。
v1 的防线：
1. 受限内建：__builtins__ 只保留安全函数，__import__ 替换为白名单版本
   （pandas/numpy 预注入，另放行 math/statistics/datetime 等纯计算库），
   open/eval/exec/compile/__import__ 等一律不可用；
2. 只注入 load_dataframe() / pd / np，代码拿不到本模块的真实全局；
3. 在独立守护线程中执行，join(timeout) 超时后用 PyThreadState_SetAsyncExc
   向线程异步注入异常终止纯 Python 死循环；
4. 结果只取命名空间里的 RESULT 并做 JSON 化清洗、表格截断 100 行。

注意：这是"轻量沙箱"，无法防御 C 扩展层卡死或解释器级漏洞；
后续可平滑升级为 Docker 子进程沙箱（本模块不含任何 subprocess/docker 调用）。
"""
from __future__ import annotations

import asyncio
import builtins as _builtins
import contextlib
import ctypes
import io
import json
import logging
import math
import re
import threading
import traceback
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.codegen import SYSTEM_PROMPT, _compact_schema
from app.llm.client import LLMClient

logger = logging.getLogger("app.analysis.executor")

MAX_RESULT_ROWS = 100          # 表格结果最多回传前 100 行
DEFAULT_TIMEOUT = 30           # 秒
HISTORY_ERROR_LIMIT = 2000     # autofix 历史里每条错误信息的截断长度

# 允许生成代码自行 import 的纯计算模块（pandas/numpy 已预注入，也允许显式导入）
_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "pandas", "numpy", "math", "statistics", "datetime",
        "collections", "functools", "itertools", "re", "json",
    }
)


class CodeExecutionTimeout(Exception):
    """超时后异步注入工作线程的异常（纯 Python 死循环会被它打断）。"""


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    """__builtins__.__import__ 的白名单替身：拒绝危险模块与相对导入。"""
    root = str(name).split(".")[0]
    if level != 0 or root not in _ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"沙箱禁止导入模块: {name}")
    return _builtins.__import__(name, globals, locals, fromlist, level)


# 生成代码可见的"安全内建"。刻意不含 open/exec/eval/compile/input/globals 等。
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "chr", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "getattr", "hasattr", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    # 常用异常类（生成代码里 try/except 可能引用）
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "ZeroDivisionError", "RuntimeError", "StopIteration",
    "NameError", "NotImplementedError", "OverflowError", "ArithmeticError",
    "FileNotFoundError", "Warning",
    "True", "False", "None",
)

_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(_builtins, name)
    for name in _SAFE_BUILTIN_NAMES
    if hasattr(_builtins, name)
}
_SAFE_BUILTINS["__import__"] = _safe_import


def _build_namespace(df: pd.DataFrame) -> dict[str, Any]:
    """构造沙箱全局命名空间：只有 load_dataframe / pd / np 与受限内建。"""
    return {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "__sandbox__",
        "load_dataframe": lambda: df,  # 按约定返回传入的 DataFrame
        "pd": pd,
        "np": np,
    }

def _async_raise_in_thread(thread: threading.Thread, exc_type: type[BaseException]) -> int:
    """向运行中的线程异步注入异常（CPython API），返回受影响的线程数。

    只能打断执行 Python 字节码的线程；卡在 C 扩展里时返回 0 / 不生效。
    """
    tid = ctypes.c_long(thread.ident or 0)
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        tid, ctypes.py_object(exc_type)
    )
    if affected > 1:  # 异常状态：撤销，避免误伤其他线程
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, None)
        raise RuntimeError("PyThreadState_SetAsyncExc 状态异常，已撤销")
    return affected


def _failure(error: str, stdout: str) -> dict[str, Any]:
    return {
        "success": False,
        "result_data": None,
        "result_type": "",
        "error": error,
        "stdout": stdout,
    }


def _execute_sync(code: str, df: pd.DataFrame, timeout: int) -> dict[str, Any]:
    """受限命名空间 + 守护线程 + 超时注入的同步执行实现。"""
    stdout_buf = io.StringIO()
    namespace = _build_namespace(df)
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            # 同时重定向 stdout/stderr，捕获 print 与 pandas 告警
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(
                stdout_buf
            ):
                exec(compile(code, "<datasage-analysis>", "exec"), namespace, namespace)
            box["ok"] = True
        except BaseException:  # noqa: BLE001 - 沙箱必须兜住一切，包括 SystemExit
            box["traceback"] = traceback.format_exc()

    worker = threading.Thread(target=_target, name="datasage-sandbox", daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        # 超时：尝试注入异常打断纯 Python 死循环
        try:
            affected = _async_raise_in_thread(worker, CodeExecutionTimeout)
        except Exception as exc:  # noqa: BLE001
            affected = 0
            logger.warning("向沙箱线程注入超时异常失败: %s", exc)
        worker.join(2.0)
        if worker.is_alive():
            # 卡在 C 扩展中无法打断；守护线程不会阻止进程退出，记录日志
            logger.error(
                "沙箱线程在超时后仍存活（可能卡在 C 扩展），已放弃等待: code=%.80r",
                code,
            )
        return _failure(
            f"CodeExecutionTimeout: 代码执行超过 {timeout} 秒被强制终止 (timeout)",
            stdout_buf.getvalue(),
        )

    stdout_text = stdout_buf.getvalue()
    if "traceback" in box:
        return _failure(box["traceback"].strip(), stdout_text)

    if "RESULT" not in namespace:
        return _failure(
            "NameError: 代码执行结束后命名空间中不存在 RESULT 变量"
            "（必须把最终结果赋值给 RESULT）",
            stdout_text,
        )

    try:
        result_data, result_type = _serialize_result(namespace["RESULT"])
    except Exception as exc:  # noqa: BLE001 - 结果序列化失败也算执行失败
        logger.exception("沙箱结果序列化失败")
        return _failure(
            f"{type(exc).__name__}: 结果序列化失败: {exc}", stdout_text
        )

    return {
        "success": True,
        "result_data": result_data,
        "result_type": result_type,
        "error": "",
        "stdout": stdout_text,
    }


async def execute_code(
    code: str, df: pd.DataFrame, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """在受限沙箱中执行生成的 pandas 代码。

    Returns:
        ``{"success", "result_data", "result_type", "error", "stdout"}``。
        阻塞执行放在默认线程池里（内部再派一个守护线程做超时打断），
        不阻塞事件循环。
    """
    return await asyncio.to_thread(_execute_sync, code, df, timeout)


# ============================== 结果序列化 ==============================

def _json_sanitize(obj: Any) -> Any:
    """递归把 numpy / pandas / datetime 等类型转成严格可 JSON 化的原生类型。

    NaN / ±Inf 统一转成 None（标准 JSON 不支持 NaN，避免前端解析失败）。
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {str(key): _json_sanitize(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_json_sanitize(item) for item in obj]
    if isinstance(obj, np.ndarray):
        return [_json_sanitize(item) for item in obj.tolist()]
    if isinstance(obj, pd.Series):
        return _table_from_dataframe(obj.to_frame())
    if isinstance(obj, pd.DataFrame):
        return _table_from_dataframe(obj)
    # 兜底：未知类型转字符串，保证结果一定能 JSON 序列化
    return str(obj)


def _table_from_dataframe(df: pd.DataFrame) -> dict[str, Any]:
    """DataFrame → 列式表格结构，最多保留前 MAX_RESULT_ROWS 行。"""
    total_rows = int(len(df))
    head = df.head(MAX_RESULT_ROWS)
    columns = [str(column) for column in head.columns]
    # object 视图 + where 掩码，把 NaN/NaT 统一替换成 None
    head = head.astype(object).where(head.notna(), None)
    rows = _json_sanitize(head.values.tolist())
    return {
        "type": "table",
        "columns": columns,
        "data": rows,
        "total_rows": total_rows,
        "returned_rows": len(rows),
        "truncated": total_rows > MAX_RESULT_ROWS,
    }


def _table_from_list(items: list[Any]) -> dict[str, Any]:
    """list 结果统一转表格：list[dict]/list[tuple] 直接建表，标量列表包成 value 列。"""
    frame = pd.DataFrame(items)
    # pd.DataFrame([1,2]) 的列名是 0，归一化为 value
    frame.columns = ["value" if str(column) == "0" else str(column) for column in frame.columns]
    return _table_from_dataframe(frame)


def _serialize_result(result: Any) -> tuple[Any, str]:
    """把 RESULT 转成 (result_data, result_type)。

    - DataFrame / list → {"type":"table", ...}
    - dict → 原样（经 JSON 清洗），type="record"
    - 标量 → {"type":"single_value","value":...}
    """
    if isinstance(result, pd.DataFrame):
        return _table_from_dataframe(result), "table"
    if isinstance(result, dict):
        return _json_sanitize(result), "record"
    if isinstance(result, (list, tuple, set, frozenset, np.ndarray)):
        return _table_from_list(list(result)), "table"
    if isinstance(result, pd.Series):
        return _table_from_dataframe(result.to_frame()), "table"
    if result is None or isinstance(
        result,
        (bool, int, float, str, bytes, np.generic, pd.Timestamp, datetime, date, Decimal),
    ):
        return {"type": "single_value", "value": _json_sanitize(result)}, "single_value"
    raise TypeError(f"不支持的 RESULT 类型: {type(result).__name__}")


# ============================== 自动修复 ==============================

# 独立成行的 ``` / ```python 围栏标记
_FENCE_LINE_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_-]*[ \t]*$", re.MULTILINE)


def _extract_code_from_reply(text: str) -> str:
    """从模型回复中取出代码。

    1) 若模型仍按 codegen 约定返回 JSON（mock provider / 偶发不遵从指令），取 code 字段；
    2) 否则剥掉 markdown 围栏行；若回复前后夹带解释文字，取最长的围栏代码块。
    """
    stripped = text.strip()
    try:
        obj = json.loads(_FENCE_LINE_RE.sub("", stripped).strip())
    except (json.JSONDecodeError, TypeError):
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("code"), str):
        return obj["code"].strip()

    if "```" in stripped:
        blocks = [
            block.strip()
            for block in re.split(r"```[a-zA-Z0-9_-]*", stripped)
            if block.strip() and block.strip() != "```"
        ]
        if blocks:
            return max(blocks, key=len).strip()
    return _FENCE_LINE_RE.sub("", stripped).strip()


def _build_fix_messages(
    schema: dict[str, Any] | None,
    question: str,
    code: str,
    error: str,
    prior_errors: list[str],
    attempt: int,
) -> list[dict[str, str]]:
    sections = []
    if question:
        sections.append(f"【原始用户问题】\n{question.strip()}")
    if schema and schema.get("columns"):
        schema_text = json.dumps(_compact_schema(schema), ensure_ascii=False, indent=2)
        sections.append(f"【数据 schema 骨架】\n{schema_text}")
    sections.append(f"【你之前写的代码】\n```python\n{code}\n```")
    sections.append(f"【执行报错完整 traceback】\n{error}")
    if prior_errors:
        sections.append(
            "【此前的报错（这些改法都没成功，请换一种写法）】\n"
            + "\n".join(f"{idx}. {err[:500]}" for idx, err in enumerate(prior_errors, 1))
        )
    sections.append(
        f"你写的 pandas 代码报错了，错误信息是：{_one_line_error(error)}。"
        "请修复代码，只输出修复后的完整代码，不要解释，不要 markdown 代码块标记，只要代码本身。"
        f"这是第 {attempt} 次修复，请确保代码仍然调用 load_dataframe() 获取数据，"
        "并把最终结果赋值给 RESULT。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def _one_line_error(error: str) -> str:
    """traceback 最后一行通常是关键信息，供 prompt 里引用。"""
    lines = [line.strip() for line in error.strip().splitlines() if line.strip()]
    return lines[-1] if lines else error.strip()


async def execute_with_autofix(
    code: str,
    df: pd.DataFrame,
    llm: LLMClient,
    max_attempts: int = 3,
    *,
    schema: dict[str, Any] | None = None,
    question: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """执行代码，失败时把错误 + 原代码 + schema 回喂 LLM 自动修复后重跑。

    Returns:
        最后一次 execute_code 的结果，额外带：
        - attempts: 实际执行次数（1 表示首次就成功）
        - autofix_history: 每次执行的 {attempt, success, error(截断)}
        - autofix_error: 修复环节本身调用 LLM 失败时的说明（可选）
    """
    history: list[dict[str, Any]] = []
    prior_errors: list[str] = []
    current_code = code
    last_result: dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        logger.info("沙箱执行第 %d/%d 次，代码 %d 字符", attempt, max_attempts, len(current_code))
        last_result = await execute_code(current_code, df, timeout=timeout)
        history.append(
            {
                "attempt": attempt,
                "success": last_result["success"],
                "error": (last_result["error"] or "")[:HISTORY_ERROR_LIMIT],
            }
        )

        if last_result["success"]:
            logger.info("沙箱执行成功（第 %d 次尝试）", attempt)
            break
        prior_errors.append(last_result["error"])
        logger.warning(
            "沙箱执行失败（第 %d/%d 次）: %s",
            attempt, max_attempts, _one_line_error(last_result["error"]),
        )

        if attempt == max_attempts:
            break

        # 回喂 LLM 请求修复
        messages = _build_fix_messages(
            schema, question, current_code, last_result["error"], prior_errors, attempt
        )
        try:
            reply = await llm.chat(messages, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - LLM 服务异常不应击穿整个接口
            logger.exception("自动修复调用 LLM 失败")
            last_result = {
                **last_result,
                "attempts": attempt,
                "autofix_history": history,
                "autofix_error": f"调用 LLM 修复代码失败: {type(exc).__name__}: {exc}",
            }
            return last_result

        current_code = _extract_code_from_reply(reply)

    last_result["attempts"] = len(history)
    last_result["autofix_history"] = history
    return last_result


# ============================== 自测入口 ==============================

if __name__ == "__main__":
    import asyncio
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )

    SAMPLE_SCHEMA = {
        "file_name": "sales.csv",
        "row_count": 3,
        "columns": [
            {"name": "region", "dtype": "categorical", "sample_values": ["华东", "西北"]},
            {"name": "sales_amount", "dtype": "numeric", "sample_values": [100.0, 200.0]},
        ],
    }

    DF = pd.DataFrame(
        {
            "region": ["华东", "华东", "西北"],
            "sales_amount": [100.0, 200.0, 50.0],
        }
    )

    GOOD_CODE = (
        "import pandas as pd\n"
        "df = load_dataframe()\n"
        "print('hello from sandbox')\n"
        "RESULT = (\n"
        "    df.groupby('region', as_index=False)['sales_amount']\n"
        "    .sum().sort_values('sales_amount', ascending=False)\n"
        "    .reset_index(drop=True)\n"
        ")\n"
    )

    # 故意拼错列名 regoin（应为 region），首次必失败，等 autofix 修复
    BUGGY_CODE = (
        "df = load_dataframe()\n"
        "RESULT = df.groupby('regoin', as_index=False)['sales_amount'].sum()\n"
    )

    async def main() -> None:
        # ---- 场景 1：正常代码 → table，结果数值正确、严格可 JSON 化 ----
        res = await execute_code(GOOD_CODE, DF, timeout=10)
        assert res["success"], res["error"]
        assert res["result_type"] == "table"
        table = res["result_data"]
        assert table["columns"] == ["region", "sales_amount"], table["columns"]
        assert table["data"][0] == ["华东", 300.0], table["data"]
        assert table["data"][1] == ["西北", 50.0], table["data"]
        assert "hello from sandbox" in res["stdout"]
        json.dumps(res["result_data"], ensure_ascii=False, allow_nan=False)  # 不抛异常即可
        print(f"[1/5] 正常 groupby 执行 OK: {table['data']}，stdout 已捕获")

        # ---- 场景 2：拼错列名 → mock LLM autofix 后第 2 次成功 ----
        llm = LLMClient("mock", "", "", "mock-1")
        fixed = await execute_with_autofix(
            BUGGY_CODE, DF, llm, max_attempts=3,
            schema=SAMPLE_SCHEMA, question="各地区销售额总和", timeout=10,
        )
        assert fixed["success"], fixed["error"]
        assert fixed["attempts"] == 2, f"应在第 2 次成功，实际 {fixed['attempts']}"
        assert fixed["autofix_history"][0]["success"] is False
        assert "regoin" in fixed["autofix_history"][0]["error"]
        assert fixed["result_data"]["data"][0] == ["华东", 300.0]
        print(f"[2/5] autofix 第 1 次报错（{_one_line_error(fixed['autofix_history'][0]['error'])}）→ 第 2 次成功 OK")

        # ---- 场景 3：死循环必须被 timeout 拦住 ----
        t0 = asyncio.get_running_loop().time()
        timed_out = await execute_code("while True:\n    pass\n", DF, timeout=1)
        elapsed = asyncio.get_running_loop().time() - t0
        assert timed_out["success"] is False
        assert "timeout" in timed_out["error"].lower(), timed_out["error"]
        print(f"[3/5] 死循环被超时拦截 OK（耗时 {elapsed:.1f}s）：{_one_line_error(timed_out['error'])}")

        # ---- 场景 4：250 行结果截断为 100 行，记录 total_rows ----
        big_df = pd.DataFrame({"idx": range(250), "value": range(250)})
        truncated = await execute_code("df = load_dataframe()\nRESULT = df\n", big_df, timeout=10)
        assert truncated["success"], truncated["error"]
        t = truncated["result_data"]
        assert t["total_rows"] == 250 and t["returned_rows"] == 100 and t["truncated"] is True
        assert len(t["data"]) == 100
        print(f"[4/5] 大结果截断 OK：total_rows={t['total_rows']} → {t['returned_rows']} 行")

        # ---- 场景 5：标量/dict 序列化 + 危险模块与 open() 被拦 ----
        scalar = await execute_code("df = load_dataframe()\nRESULT = int(df['sales_amount'].sum())\n", DF)
        assert scalar["result_type"] == "single_value" and scalar["result_data"]["value"] == 350
        record = await execute_code(
            "df = load_dataframe()\nRESULT = {'rows': len(df), 'mean': float(df['sales_amount'].mean())}\n",
            DF,
        )
        assert record["result_type"] == "record"
        assert record["result_data"] == {"rows": 3, "mean": 116.66666666666667}
        json.dumps(record["result_data"], allow_nan=False)

        blocked_os = await execute_code("import os\ndf = load_dataframe()\nRESULT = 1\n", DF)
        assert not blocked_os["success"] and "沙箱禁止导入模块: os" in blocked_os["error"]
        blocked_open = await execute_code("df = load_dataframe()\nopen('x.txt', 'w')\nRESULT = 1\n", DF)
        assert not blocked_open["success"] and "NameError" in blocked_open["error"]
        no_result = await execute_code("df = load_dataframe()\nprint(len(df))\n", DF)
        assert not no_result["success"] and "RESULT" in no_result["error"]
        print("[5/5] single_value/record 序列化 + os/open/缺 RESULT 拦截 OK")

        print("\n全部自测通过")

    asyncio.run(main())



