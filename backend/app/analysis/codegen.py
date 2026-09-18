"""LLM 驱动的 pandas 分析代码生成（本模块只"生成"，不执行代码）。

输入 extractor 产出的 schema 骨架 + 用户自然语言问题，
输出 {plan, code, result_format} JSON。代码约定在沙箱中执行：
数据由预注入的 load_dataframe() 提供，最终结果赋给 RESULT。
"""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

from app.llm.client import LLMClient

logger = logging.getLogger("app.analysis.codegen")

# result_format 允许值
RESULT_FORMATS = ("table", "single_value", "chart")

# 喂给 LLM 的列字段白名单（其余字段如 file_type/meta 不喂，省 token）
_SCHEMA_COLUMN_KEYS = (
    "name",
    "dtype",
    "null_count",
    "null_ratio",
    "unique_count",
    "sample_values",
    "numeric_stats",
    "date_range",
)

SYSTEM_PROMPT = """你是 DataSage「可信数据分析引擎」的 pandas 代码生成器。

【你会收到什么】
- 一份数据集的 schema 骨架（不是全量数据！）：file_name、row_count，以及每列的
  name / dtype（datetime | numeric | categorical | empty）/ null_ratio /
  unique_count / sample_values / numeric_stats / date_range。
- 一个用户用自然语言提出的分析问题。
全量数据太大、不会也不能提供给你；你只需依据骨架生成正确的分析代码。

【沙箱运行环境约定 —— 必须严格遵守】
1. 数据通过沙箱预注入的函数 load_dataframe() 获取，它返回 pandas.DataFrame。
   代码里必须调用 `df = load_dataframe()`；不要写任何文件路径，不要尝试自己读文件。
2. 代码必须把最终结果赋值给名为 RESULT 的变量。RESULT 可以是
   pandas.DataFrame、dict，或数字/字符串等标量；不要 print 结果，不要画图显示。
3. 只允许使用 pandas（pd）、numpy（np）以及纯计算标准库
   （math / statistics / datetime / collections / re / json）。
4. 严禁 import 以下模块：os、sys、subprocess、socket、shutil、requests、
   urllib、http、pickle、ctypes、multiprocessing、threading；
   严禁调用 open()、eval()、exec()、compile()、__import__()、input()。
5. 严禁任何修改/删除源文件、写磁盘、访问网络或启动子进程的操作。
6. 只能使用 schema 中真实存在的列名（原样抄写，注意中英文与大小写）；
   注意空值：聚合前按需要 dropna 或 fillna；日期列先用 pd.to_datetime 转换
   再做时间分组/比较；金额类列确认是 numeric 后再做算术。

【分析方法要求】
7. 先想清楚分析步骤，再写代码：把简洁有条理的中文思路（1~5 句）写进 plan 字段，
   plan 要说明按什么维度、做什么聚合/排序/筛选。
8. 如果问题含糊（例如"分析一下销售情况""看看数据"），必须在 plan 中主动做合理
   拆分：至少给出"按分类维度（如地区/品类）分组聚合"和"按时间维度（如日期/月份）
   分组聚合"两个视角，并在代码中分别计算，把多份结果合并进一个 DataFrame
   （用 result 列区分视角）或 dict，统一赋给 RESULT。
9. 依据答案形态选择 result_format：
   - "table"：多行结构化结果（分组汇总、排行榜、明细）
   - "single_value"：答案就是单个数字或一句话
   - "chart"：结果是适合直接画图的规整表（含明确的 x/y 字段）

【输出格式】
只输出一个合法 JSON 对象，禁止 Markdown 代码块、禁止任何解释文字：
{
  "plan": "中文分析思路",
  "code": "完整可执行的 Python 代码（\\n 表示换行），必须含 load_dataframe() 且给 RESULT 赋值",
  "result_format": "table"
}
"""

# 用户消息带【多轮对话上下文】时，追加到 system 的追问规则
FOLLOWUP_SYSTEM_APPEND = """
【多轮追问】
如果用户消息中带有【多轮对话上下文】，说明这是同一数据源上一轮分析之后的追问：
- 结合上一轮的问题、分析计划与结论理解本轮问题里的指代（如"那华东呢""还是按月份
  呢"），沿用上一轮的指标口径（相同的列、相同的聚合方式）；
- 针对追问做筛选 / 对比 / 细化，不要把上一轮已经做过的全量分析原样重复一遍；
- 仍然只输出约定的 JSON 对象。
"""


def _compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """只保留 LLM 需要的 schema 字段，压缩 token。"""
    compact_columns = []
    for col in schema.get("columns", []):
        compact_columns.append(
            {key: col[key] for key in _SCHEMA_COLUMN_KEYS if key in col}
        )
    return {
        "file_name": schema.get("file_name", ""),
        "row_count": schema.get("row_count", 0),
        "columns": compact_columns,
    }


def format_followup_context(context: dict[str, Any]) -> str:
    """把上一轮会话摘要渲染成注入 user 消息的【多轮对话上下文】文本块。"""
    lines = ["【多轮对话上下文】（本轮是同一数据源上的追问）"]
    schema_summary = str(context.get("schema_summary") or "").strip()
    if schema_summary:
        lines.append(f"- 数据概览：{schema_summary}")
    lines.append(
        f"- 上一轮问题：{str(context.get('prev_question') or '').strip()}"
    )
    prev_plan = str(context.get("prev_plan") or "").strip()
    if prev_plan:
        lines.append(f"- 上一轮分析计划：{prev_plan[:300]}")
    prev_answer = str(context.get("prev_answer") or "").strip()
    if prev_answer:
        lines.append(f"- 上一轮结论：{prev_answer[:500]}")
    prev_result = context.get("prev_result")
    if prev_result is not None:
        result_text = json.dumps(prev_result, ensure_ascii=False, default=str)
        lines.append(f"- 上一轮结果摘要：{result_text[:800]}")
    return "\n".join(lines)


def build_messages(
    schema: dict[str, Any],
    question: str,
    followup_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """组装 system + user 两轮消息（temperature 在调用处固定为 0.0）。

    followup_context 非空时注入多轮追问上下文，system 追加追问规则。
    """
    schema_text = json.dumps(_compact_schema(schema), ensure_ascii=False, indent=2)
    sections = [f"【数据 schema 骨架】\n{schema_text}"]
    if followup_context:
        sections.append(format_followup_context(followup_context))
    sections.append(
        f"【用户问题】\n{question.strip()}\n\n"
        "请严格按 system 中的约定，只输出 JSON 对象。"
    )
    system_content = SYSTEM_PROMPT + (
        FOLLOWUP_SYSTEM_APPEND if followup_context else ""
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


# ----------------------------- 生成代码静态校验 -----------------------------

# 可访问文件系统 / 网络 / 进程的模块，一律禁止
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "os", "sys", "subprocess", "socket", "shutil", "pathlib",
        "requests", "urllib", "http", "ftplib", "smtplib",
        "pickle", "marshal", "ctypes", "multiprocessing", "threading",
    }
)

# 可读写文件 / 动态执行 / 逃逸沙箱的内建调用，一律禁止
FORBIDDEN_CALLS = frozenset(
    {
        "open", "eval", "exec", "compile", "__import__",
        "input", "breakpoint", "exit", "quit",
    }
)

_RESULT_ASSIGN_RE = re.compile(r"^\s*RESULT\s*=", re.MULTILINE)


def validate_generated_code(code: str) -> list[str]:
    """对 LLM 生成的代码做静态安全/约定校验，返回错误信息列表（空列表=通过）。

    检查项：语法正确、必须调用 load_dataframe()、必须给 RESULT 赋值、
    无危险模块导入、无 open/eval/exec 等内建调用。这是 prompt 约束之外的
    第二道防线（fail-closed），真正的强隔离在沙箱执行环节。
    """
    errors: list[str] = []
    if not code or not code.strip():
        return ["生成的 code 为空"]
    if "load_dataframe()" not in code:
        errors.append("代码必须通过 load_dataframe() 获取数据")

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        errors.append(f"代码存在语法错误: {exc}")
        return errors

    assigns_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    errors.append(f"禁止导入模块: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                errors.append(f"禁止从模块导入: {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                errors.append(f"禁止调用内建函数: {node.func.id}()")
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "RESULT" for t in node.targets):
                assigns_result = True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "RESULT":
                assigns_result = True

    if not assigns_result and not _RESULT_ASSIGN_RE.search(code):
        errors.append("代码必须将最终结果赋值给 RESULT 变量")
    return errors


# ----------------------------- 主入口 -----------------------------

async def generate_analysis_plan_and_code(
    schema: dict[str, Any],
    question: str,
    llm: LLMClient,
    followup_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """根据 schema 骨架与用户问题，让 LLM 生成分析计划 + pandas 代码。

    Args:
        followup_context: 多轮追问时上一轮会话的摘要（问题 / 计划 / 结论 /
            结果摘要），非空则注入 prompt，让模型理解追问中的指代。

    Returns:
        ``{"plan": str, "code": str, "result_format": "table|single_value|chart"}``

    Raises:
        ValueError: 入参为空，或生成内容未通过静态安全/约定校验。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("用户问题不能为空")
    if not schema.get("columns"):
        raise ValueError("schema 中没有 columns，无法生成分析代码")

    # 规则 8 的稳定性保障：temperature 固定 0.0
    messages = build_messages(schema, question, followup_context)
    result = await llm.chat_json(messages)

    plan = str(result.get("plan", "")).strip()
    code = str(result.get("code", "")).strip()
    result_format = str(result.get("result_format", "")).strip() or "table"
    if result_format not in RESULT_FORMATS:
        logger.warning("模型返回未知 result_format=%r，回退为 table", result_format)
        result_format = "table"

    errors: list[str] = []
    if not plan:
        errors.append("缺少 plan 字段")
    errors.extend(validate_generated_code(code))
    if errors:
        logger.error("LLM 生成内容未通过静态校验: %s", errors)
        raise ValueError(
            f"LLM 生成内容未通过校验: {'; '.join(errors)}"
        )

    logger.info(
        "分析代码生成成功 question=%r result_format=%s 代码行数=%d",
        question[:50], result_format, code.count("\n") + 1,
    )
    return {"plan": plan, "code": code, "result_format": result_format}


# ============================== 自测入口 ==============================

if __name__ == "__main__":
    import asyncio
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )

    SAMPLE_SCHEMA: dict[str, Any] = {
        "file_name": "sales.csv",
        "row_count": 120_000,
        "columns": [
            {
                "name": "order_date",
                "dtype": "datetime",
                "null_count": 0,
                "null_ratio": 0.0,
                "unique_count": 365,
                "sample_values": ["2024-02-02", "2024-10-09", "2024-08-26"],
                "numeric_stats": None,
                "date_range": {"min": "2024-01-01", "max": "2024-12-31"},
            },
            {
                "name": "region",
                "dtype": "categorical",
                "null_count": 0,
                "null_ratio": 0.0,
                "unique_count": 7,
                "sample_values": ["华东", "西北", "东北"],
                "numeric_stats": None,
                "date_range": None,
            },
            {
                "name": "sales_amount",
                "dtype": "numeric",
                "null_count": 128,
                "null_ratio": 0.001067,
                "unique_count": 119167,
                "sample_values": [60544.5, 73214.67, 90499.54],
                "numeric_stats": {
                    "min": 1.75, "max": 99996.96,
                    "mean": 50006.7753, "median": 49924.09,
                },
                "date_range": None,
            },
        ],
    }

    class FlakyClient(LLMClient):
        """第一次返回非法 JSON，第二次返回合法 JSON，用于验证 chat_json 修复重试。"""

        def __init__(self) -> None:
            super().__init__("mock", "", "", "mock-flaky")
            self.calls = 0

        async def chat(self, messages, temperature: float = 0.0) -> str:
            self.calls += 1
            if self.calls == 1:
                return '好的，这是结果：\n```json\n{plan: "先分组", code: 不合法}\n```'
            return json.dumps(
                {
                    "plan": "按地区分组求销售额总和",
                    "code": (
                        "import pandas as pd\n"
                        "df = load_dataframe()\n"
                        "RESULT = df.groupby('region', as_index=False)['sales_amount'].sum()"
                    ),
                    "result_format": "table",
                },
                ensure_ascii=False,
            )

    async def main() -> None:
        # ---- 场景 1：mock provider 全流程 ----
        llm = LLMClient("mock", "", "", "mock-1")
        result = await generate_analysis_plan_and_code(
            SAMPLE_SCHEMA, "各地区销售额总和", llm
        )
        print("===== generate_analysis_plan_and_code(mock) =====")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        assert result["plan"], "plan 不应为空"
        assert result["result_format"] in RESULT_FORMATS
        assert "load_dataframe()" in result["code"]
        assert "RESULT" in result["code"]
        compile(result["code"], "<llm-generated>", "exec")  # 语法必须正确
        assert validate_generated_code(result["code"]) == []
        print("\n[1/3] mock 生成 plan+code，且代码通过静态校验 OK")

        # ---- 场景 2：非法 JSON 自动修复重试 ----
        flaky = FlakyClient()
        obj = await flaky.chat_json([{"role": "user", "content": "各地区销售额总和"}])
        assert obj["result_format"] == "table"
        assert "load_dataframe()" in obj["code"]
        assert flaky.calls == 2, f"应恰好重试 1 次，实际调用 {flaky.calls} 次"
        print(f"[2/3] chat_json 非法输出修复重试 OK（共调用 {flaky.calls} 次）")

        # ---- 场景 3：危险代码必须被静态校验拦截 ----
        dangerous_cases = {
            "import os": "import os\ndf = load_dataframe()\nRESULT = os.listdir('.')",
            "subprocess": (
                "import subprocess\ndf = load_dataframe()\n"
                "RESULT = subprocess.run(['ls'], capture_output=True)"
            ),
            "open()": "df = load_dataframe()\nopen('x.txt', 'w').write('x')\nRESULT = 1",
            "缺 load_dataframe": "RESULT = 1",
            "缺 RESULT": "df = load_dataframe()\nprint(df)",
            "语法错误": "df = load_dataframe()\nRESULT = ((("
        }
        for label, bad_code in dangerous_cases.items():
            errs = validate_generated_code(bad_code)
            assert errs, f"危险/违规代码未被拦截：{label}"
            print(f"  - 拦截 [{label}]: {errs[0]}")
        print("[3/3] 静态安全校验拦截全部违规代码 OK")

        print("\n全部自测通过")

    asyncio.run(main())


