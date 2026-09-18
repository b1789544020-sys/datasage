"""基于真实执行结果撰写自然语言答案（本模块只"写总结"，不做校验）。

输入：用户问题、codegen 的 plan、executor 产出的 result_data（table / single_value /
record）。输出：一段中文总结正文。数字回溯校验在 verifier 中完成，发现幻觉后由
:func:`app.analysis.verifier.verify_and_regenerate` 带着真实数据重新生成。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.analysis.verifier import _strip_answer_text  # 复用回复清洗（同包内部复用）
from app.llm.client import LLMClient

logger = logging.getLogger("app.analysis.answer")

# system 文案中的「最终答案撰写」标记也被 mock provider 用于识别本场景
SYSTEM_PROMPT = """你是 DataSage「最终答案撰写」助手。

系统已经用沙箱执行 pandas 代码，得到了真实分析结果（JSON）。请据此回答用户的问题：
1. 只引用 JSON 中真实存在的数字、名称和排名，严禁编造任何 JSON 里没有的数值、
   百分比、倍数、环比/同比或排名；
2. 数字照实引用（保留 JSON 中的数值与单位），不要自行做四舍五入或换算；
3. 直接输出 2~5 句中文总结正文，先给结论再给关键数字；
4. 不要输出代码、Markdown、JSON，不要复述分析计划，不要提及"JSON""数据集"等字样。
"""

MAX_EMBED_ROWS = 20


def _compact_result(result_data: Any, max_rows: int = MAX_EMBED_ROWS) -> Any:
    """表格结果只嵌前若干行进 prompt（total_rows 等元信息原样保留）。"""
    if (
        isinstance(result_data, dict)
        and result_data.get("type") == "table"
        and isinstance(result_data.get("data"), list)
        and len(result_data["data"]) > max_rows
    ):
        compact = dict(result_data)
        compact["data"] = result_data["data"][:max_rows]
        compact["note"] = f"结果共 {result_data.get('total_rows')} 行，仅给出前 {max_rows} 行"
        return compact
    return result_data


def build_messages(
    question: str,
    plan: str,
    result_data: Any,
    followup_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """组装 system + user 两轮消息（temperature 在调用处固定 0.0）。

    followup_context 非空时追加一小段上一轮问答摘要，帮助模型理解追问指代；
    但系统约束不变：答案只能引用本轮真实结果里的数字。
    """
    result_text = json.dumps(
        _compact_result(result_data), ensure_ascii=False, indent=2, default=str
    )
    sections = [
        f"【用户问题】\n{question.strip()}",
        f"【分析计划】\n{(plan or '').strip()}",
    ]
    if followup_context:
        prev_question = str(
            followup_context.get("prev_question") or ""
        ).strip()
        prev_answer = str(followup_context.get("prev_answer") or "").strip()
        sections.append(
            "【多轮对话上下文】（本轮是追问，行文需承接上一轮的口径与结论，"
            "但数字只能取自下面的本轮真实结果）\n"
            f"- 上一轮问题：{prev_question}\n"
            f"- 上一轮结论：{prev_answer[:500]}"
        )
    sections.append(f"【真实执行结果 JSON】\n{result_text}")
    sections.append("请严格按 system 规则直接输出中文总结。")
    user_content = "\n\n".join(sections)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def draft_answer(
    question: str,
    plan: str,
    result_data: Any,
    llm: LLMClient,
    followup_context: dict[str, Any] | None = None,
) -> str:
    """让 LLM 基于真实结果写第一版答案，返回清洗后的纯文本。"""
    messages = build_messages(question, plan, result_data, followup_context)
    raw = await llm.chat(messages, temperature=0.0)
    answer = _strip_answer_text(raw)
    logger.info("最终答案初稿生成完成（%d 字）", len(answer))
    return answer
