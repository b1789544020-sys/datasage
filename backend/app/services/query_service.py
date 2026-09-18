"""提问 / 分析编排 service：把前面各模块串成完整可信分析链路。

流程（对应需求中的 9 步）：
  1. 读取 DataSource（schema + 文件路径）
  2. 全量加载到 pandas DataFrame（分析需要全量数据）
  3. codegen：LLM 生成 plan + pandas code
  4. executor：沙箱执行 + LLM 自动修复
  5. verifier：结果三层验证（syntax / semantic / consistency 占位）
  6. answer：LLM 基于真实 result_data 写第一版自然语言总结
  7. NumericBacktracker 数字回溯，发现幻觉自动喂回重生成（≤2 轮）
  8. 写 QuerySession / AnalysisStep / QueryResult
  9. 返回前端渲染所需的结构化结果与溯源步骤

plan / code / execute / verify / answer 各写一条 AnalysisStep，
关键信息放 meta_json，供前端"溯源"展示。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.answer import draft_answer
from app.analysis.chart_advisor import suggest_chart
from app.analysis.codegen import generate_analysis_plan_and_code
from app.analysis.executor import execute_with_autofix
from app.analysis.verifier import (
    NumericBacktracker,
    verify_and_regenerate,
    verify_result,
)
from app.llm.client import LLMClient
from app.models import AnalysisStep, DataSource, QueryResult, QuerySession
from app.services.errors import ServiceError

logger = logging.getLogger("app.services.query")

_STDOUT_TAIL_LIMIT = 1000      # execute 步骤 meta 中保留的沙箱输出尾部长度
_STEP_TYPES = ("plan", "code", "execute", "verify", "answer")


# ------------------------------ 数据加载 --------------------------------

def load_full_dataframe(file_path: str, schema: dict[str, Any]) -> pd.DataFrame:
    """把上传文件全量读入 DataFrame（分析执行需要全量数据）。"""
    file_type = str(
        schema.get("file_type") or Path(file_path).suffix.lstrip(".")
    ).lower()
    if file_type in ("xlsx", "xls"):
        return pd.read_excel(file_path)

    # CSV：优先用 extractor 已探测出的编码，再用常见编码兜底
    meta = schema.get("meta") or {}
    detected = str(meta.get("encoding") or "").lower().replace("-", "")
    candidates: list[str] = []
    if detected in ("utf8", "ascii"):
        candidates.append("utf-8-sig")
    elif detected:
        candidates.append(meta["encoding"])
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        if enc not in candidates:
            candidates.append(enc)

    last_error: Exception | None = None
    for encoding in candidates:
        try:
            return pd.read_csv(file_path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
        except LookupError as exc:  # 运行环境缺该编码
            last_error = exc
    raise ServiceError(400, f"无法识别文件编码，全量加载失败：{last_error}")


# ------------------------------ 步骤序列化 ------------------------------

def _step_to_dict(step: AnalysisStep) -> dict[str, Any]:
    meta = None
    if step.meta_json:
        try:
            meta = json.loads(step.meta_json)
        except json.JSONDecodeError:
            meta = None
    return {
        "id": step.id,
        "step_type": step.step_type,
        "content": step.content,
        "meta": meta,
        "created_at": step.created_at.isoformat() if step.created_at else None,
    }


def _checks_summary(checks: list[dict[str, str]]) -> str:
    return "；".join(
        f"[{item['layer']} {item['status']}] {item['detail']}" for item in checks
    )


# ----------------------------- 多轮追问上下文 -----------------------------

_CONTEXT_PREV_RESULT_ROWS = 5


def _schema_summary(schema: dict[str, Any]) -> str:
    """把数据源 schema 压成一行摘要（行数列数 + 每列名/类型），供追问 prompt 使用。"""
    columns = schema.get("columns") or []
    col_text = "、".join(
        f"{col.get('name')}（{col.get('dtype')}）" for col in columns
    )
    return f"共 {schema.get('row_count', '?')} 行；列：{col_text}"


def _compact_prev_result(result_data: Any) -> Any:
    """上一轮 table 结果只带前几行进 prompt，其余结构（单值 / record）原样返回。"""
    if (
        isinstance(result_data, dict)
        and result_data.get("type") == "table"
        and isinstance(result_data.get("data"), list)
    ):
        return {
            "type": "table",
            "columns": result_data.get("columns"),
            "total_rows": result_data.get("total_rows"),
            "data": result_data["data"][:_CONTEXT_PREV_RESULT_ROWS],
        }
    return result_data


async def _load_followup_context(
    session: AsyncSession, prev_session_id: int, datasource_id: int
) -> dict[str, Any]:
    """加载上一轮会话摘要，供 codegen / answer 注入"追问"上下文。

    约束：上一轮必须存在、属于同一数据源、且已分析完成（status=done）。
    """
    prev = await session.get(QuerySession, prev_session_id)
    if prev is None:
        raise ServiceError(404, f"上一轮会话 {prev_session_id} 不存在")
    if prev.datasource_id != datasource_id:
        raise ServiceError(400, "追问必须基于与上一轮相同的数据源")
    if prev.status != "done":
        raise ServiceError(
            409, f"上一轮会话 {prev_session_id} 尚未完成（status={prev.status}）"
        )

    prev_result = (
        await session.execute(
            select(QueryResult).where(QueryResult.session_id == prev.id)
        )
    ).scalar_one_or_none()
    prev_steps = (
        await session.execute(
            select(AnalysisStep)
            .where(AnalysisStep.session_id == prev.id)
            .order_by(AnalysisStep.id)
        )
    ).scalars().all()

    prev_plan = next(
        (step.content for step in prev_steps if step.step_type == "plan"), ""
    )
    prev_table: Any = None
    for step in prev_steps:
        if step.step_type == "execute" and step.meta_json:
            try:
                meta = json.loads(step.meta_json)
            except json.JSONDecodeError:
                continue
            if isinstance(meta.get("result_data"), dict):
                prev_table = meta["result_data"]
                break

    return {
        "prev_session_id": prev.id,
        "prev_question": prev.question,
        "prev_plan": prev_plan,
        "prev_answer": prev_result.answer_text if prev_result else "",
        "prev_result": _compact_prev_result(prev_table),
    }


# -------------------------------- 主流程 --------------------------------

async def run_query(
    session: AsyncSession,
    datasource_id: int,
    question: str,
    llm: LLMClient,
    prev_session_id: int | None = None,
) -> dict[str, Any]:
    """执行完整分析链路并持久化，返回前端响应 payload。

    prev_session_id 非空时视为多轮追问，会把上一轮会话的 schema 摘要与
    问答 / 结果摘要注入 codegen 与 answer 的 prompt。
    """
    question = (question or "").strip()
    if not question:
        raise ServiceError(400, "问题不能为空")

    # ---- 1. 读取数据源 ----
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise ServiceError(404, f"数据源 {datasource_id} 不存在")
    if datasource.status != "ready" or not datasource.schema_json:
        raise ServiceError(
            409, f"数据源 {datasource_id} 当前不可用（status={datasource.status}）"
        )
    schema = json.loads(datasource.schema_json)

    # ---- 1.5 多轮追问：加载上一轮上下文摘要 ----
    followup_context: dict[str, Any] | None = None
    if prev_session_id is not None:
        followup_context = await _load_followup_context(
            session, prev_session_id, datasource.id
        )
        followup_context["schema_summary"] = _schema_summary(schema)
        logger.info(
            "多轮追问 datasource=%s prev_session=%s question=%r",
            datasource.id, prev_session_id, question[:50],
        )

    query_session = QuerySession(
        datasource_id=datasource.id,
        question=question,
        prev_session_id=prev_session_id,
        status="planning",
    )
    session.add(query_session)
    await session.flush()  # 拿到 session id

    async def add_step(
        step_type: str, content: str, meta: dict[str, Any] | None = None
    ) -> None:
        if step_type not in _STEP_TYPES:
            raise ValueError(f"未知 step_type: {step_type}")
        session.add(
            AnalysisStep(
                session_id=query_session.id,
                step_type=step_type,
                content=content or "（无内容）",
                meta_json=(
                    json.dumps(meta, ensure_ascii=False, default=str)
                    if meta is not None
                    else None
                ),
            )
        )
        await session.flush()

    plan = ""
    executed: dict[str, Any] = {}
    try:
        # ---- 2. 全量加载数据（IO/CPU 密集，放线程池）----
        t0 = perf_counter()
        df = await asyncio.to_thread(
            load_full_dataframe, datasource.file_path, schema
        )
        logger.info(
            "session=%s 全量加载完成：%d 行 × %d 列，耗时 %.0fms",
            query_session.id, len(df), len(df.columns),
            (perf_counter() - t0) * 1000,
        )

        # ---- 3. codegen：plan + code ----
        query_session.status = "planning"
        t0 = perf_counter()
        generated = await generate_analysis_plan_and_code(
            schema, question, llm, followup_context=followup_context
        )
        plan = generated["plan"]
        code = generated["code"]
        result_format = generated["result_format"]
        codegen_ms = (perf_counter() - t0) * 1000
        plan_meta: dict[str, Any] = {
            "result_format": result_format,
            "duration_ms": round(codegen_ms),
        }
        if followup_context:
            # 记录追问链路，便于前端标识与溯源回看
            plan_meta["followup"] = {
                "prev_session_id": followup_context["prev_session_id"],
                "prev_question": followup_context["prev_question"],
            }
        await add_step("plan", plan, plan_meta)
        await add_step("code", code, {
            "char_count": len(code),
            "static_validation": "passed",
            "result_format": result_format,
        })

        # ---- 4. 沙箱执行 + 自动修复 ----
        query_session.status = "executing"
        t0 = perf_counter()
        executed = await execute_with_autofix(
            code, df, llm, schema=schema, question=question
        )
        execute_ms = (perf_counter() - t0) * 1000
        result_data = executed.get("result_data")
        exec_meta: dict[str, Any] = {
            "success": bool(executed.get("success")),
            "result_type": executed.get("result_type"),
            "attempts": executed.get("attempts", 1),
            "autofix_history": executed.get("autofix_history", []),
            "autofix_error": executed.get("autofix_error"),
            "duration_ms": round(execute_ms),
            "stdout_tail": (executed.get("stdout") or "")[-_STDOUT_TAIL_LIMIT:],
        }
        if isinstance(result_data, dict):
            exec_meta["result_data"] = result_data  # 供会话详情回看
            if result_data.get("type") == "table":
                exec_meta["total_rows"] = result_data.get("total_rows")
                exec_meta["returned_rows"] = result_data.get("returned_rows")
        # 自动可视化推荐：按结果表形状 + 数据源 schema 推断图表（仅 table 型）
        chart_suggestion = None
        if executed.get("result_type") == "table":
            chart_suggestion = suggest_chart(result_data, schema)
        exec_meta["chart_suggestion"] = chart_suggestion
        if executed.get("success"):
            exec_content = (
                f"沙箱执行成功（第 {exec_meta['attempts']} 次尝试），"
                f"结果类型 {executed.get('result_type')}"
                + (
                    f"，共 {exec_meta['total_rows']} 行"
                    if exec_meta.get("total_rows") is not None
                    else ""
                )
            )
        else:
            exec_content = f"沙箱执行失败：{str(executed.get('error', ''))[:500]}"
            exec_meta["error"] = executed.get("error", "")
        await add_step("execute", exec_content, exec_meta)
        if not executed.get("success"):
            raise ServiceError(
                500,
                "生成的代码多次执行仍失败："
                + str(executed.get("error", ""))[-500:],
            )

        # ---- 5. 三层基础验证（此时还没有答案，consistency 为占位 pass）----
        query_session.status = "verifying"
        base_verification = verify_result(executed, question)
        await add_step(
            "verify",
            "结果三层验证：" + _checks_summary(base_verification["checks"]),
            {"stage": "result", "passed": base_verification["passed"],
             "checks": base_verification["checks"]},
        )
    except ServiceError:
        query_session.status = "failed"
        await session.commit()
        raise
    except Exception as exc:
        logger.exception("session=%s 分析流程在执行阶段异常", query_session.id)
        query_session.status = "failed"
        await session.commit()
        raise ServiceError(
            500, f"分析流程失败：{type(exc).__name__}: {exc}"
        ) from exc

    return await _finish_query(
        session, query_session, plan, executed, base_verification,
        question, llm, add_step,
        followup_context=followup_context,
        chart_suggestion=chart_suggestion,
    )


async def _finish_query(
    session: AsyncSession,
    query_session: QuerySession,
    plan: str,
    executed: dict[str, Any],
    base_verification: dict[str, Any],
    question: str,
    llm: LLMClient,
    add_step,  # run_query 内的闭包，保证步骤挂在同一会话上
    followup_context: dict[str, Any] | None = None,
    chart_suggestion: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行阶段之后的步骤：写答案 → 数字回溯重生成 → 写 QueryResult → 组装响应。"""
    result_data = executed.get("result_data")
    try:
        # ---- 6. LLM 基于真实 result_data 写第一版答案 ----
        initial_answer = await draft_answer(
            question, plan, result_data, llm,
            followup_context=followup_context,
        )
        # ---- 7. 数字回溯 → 发现幻觉自动重生成（≤2 轮）----
        regeneration = await verify_and_regenerate(
            initial_answer,
            result_data,
            llm,
            context={"question": question},
        )
        answer_text = regeneration["answer"]
        # 初稿的数字级回溯判定：前端据此把被丢弃初稿里的幻觉数字标红
        initial_report = NumericBacktracker().verify_answer(
            initial_answer, result_data
        )
        # 带上答案再跑一次三层验证（consistency 这次是真正的数字回溯）
        final_verification = verify_result(executed, question, answer_text=answer_text)
        await add_step("answer", answer_text, {
            "initial_answer": initial_answer,
            "initial_verdicts": initial_report["verified_numbers"],
            "regeneration_rounds": regeneration["rounds"],
            "total_numbers": regeneration["total_numbers"],
            "hallucination_count": regeneration["hallucination_count"],
            "hallucinations": [
                item["raw"] for item in regeneration["hallucinations"]
            ],
            # 每个数字的回溯判定（含 evidence 定位），会话详情回看时复用
            "number_verdicts": regeneration["verified_numbers"],
            "regeneration_error": regeneration["regeneration_error"],
            "regeneration_history": regeneration["history"],
            "final_checks": final_verification["checks"],
        })

        # 三层结果验证与数字回溯都通过才算 verified
        verified = bool(
            base_verification["passed"]
            and final_verification["passed"]
            and regeneration["passed"]
        )

        # ---- 8. 写 QueryResult，会话收尾 ----
        session.add(QueryResult(
            session_id=query_session.id,
            answer_text=answer_text,
            verified=verified,
            hallucination_count=regeneration["hallucination_count"],
        ))
        query_session.status = "done"
        await session.commit()
    except ServiceError:
        query_session.status = "failed"
        await session.commit()
        raise
    except Exception as exc:
        logger.exception("session=%s 分析流程在答案阶段异常", query_session.id)
        query_session.status = "failed"
        await session.commit()
        raise ServiceError(
            500, f"答案生成 / 校验失败：{type(exc).__name__}: {exc}"
        ) from exc

    # ---- 9. 从库里读步骤 / 结果，组装前端响应 ----
    result = (
        await session.execute(
            select(QueryResult).where(QueryResult.session_id == query_session.id)
        )
    ).scalar_one()
    steps = (
        await session.execute(
            select(AnalysisStep)
            .where(AnalysisStep.session_id == query_session.id)
            .order_by(AnalysisStep.id)
        )
    ).scalars().all()
    return {
        "session_id": query_session.id,
        "question": query_session.question,
        "status": query_session.status,
        "plan": plan,
        "answer": result.answer_text,
        "verified": result.verified,
        "hallucination_count": result.hallucination_count,
        "result_data": result_data,
        # 多轮追问：上一轮会话 id（首轮为 null）
        "prev_session_id": query_session.prev_session_id,
        # 自动可视化推荐：{type, x, y} 或 null
        "chart_suggestion": chart_suggestion,
        # 答案数字级回溯结果：[{raw,value,kind,position,found,evidence}]
        "answer_numbers": regeneration["verified_numbers"],
        "steps": [_step_to_dict(step) for step in steps],
    }


async def get_session_detail(
    session: AsyncSession, session_id: int
) -> dict[str, Any] | None:
    """会话详情：问题 + 最终答案 + 结构化结果 + 全部溯源步骤。"""
    query_session = await session.get(QuerySession, session_id)
    if query_session is None:
        return None

    result = (
        await session.execute(
            select(QueryResult).where(QueryResult.session_id == session_id)
        )
    ).scalar_one_or_none()
    steps = (
        await session.execute(
            select(AnalysisStep)
            .where(AnalysisStep.session_id == session_id)
            .order_by(AnalysisStep.id)
        )
    ).scalars().all()
    step_dicts = [_step_to_dict(step) for step in steps]

    plan = next(
        (s["content"] for s in step_dicts if s["step_type"] == "plan"), ""
    )
    result_data = None
    chart_suggestion = None
    answer_numbers: list[dict[str, Any]] = []
    for step in step_dicts:
        meta = step["meta"]
        if step["step_type"] == "execute" and meta:
            result_data = meta.get("result_data")
            chart_suggestion = meta.get("chart_suggestion")
        elif step["step_type"] == "answer" and meta:
            # 历史会话可能没有 number_verdicts，缺省回退为空列表
            answer_numbers = meta.get("number_verdicts") or []

    return {
        "session_id": query_session.id,
        "datasource_id": query_session.datasource_id,
        "question": query_session.question,
        "status": query_session.status,
        "created_at": (
            query_session.created_at.isoformat()
            if query_session.created_at else None
        ),
        "plan": plan,
        "answer": result.answer_text if result else None,
        "verified": result.verified if result else False,
        "hallucination_count": (
            result.hallucination_count if result else 0
        ),
        "result_data": result_data,
        "prev_session_id": query_session.prev_session_id,
        "chart_suggestion": chart_suggestion,
        "answer_numbers": answer_numbers,
        "steps": step_dicts,
    }


# ------------------------------ Markdown 报告 ------------------------------

_REPORT_MAX_ROWS = 100
_CHART_TYPE_LABELS = {"bar": "柱状图", "line": "折线图", "scatter": "散点图"}
_NUMBER_KIND_LABELS = {
    "number": "数字",
    "percent": "百分比",
    "multiplier": "倍数",
    "currency": "金额",
    "count": "计数",
    "ordinal": "序数",
    "chinese_number": "中文数量词",
}


def _md_cell(value: Any) -> str:
    """转义 Markdown 表格单元格：去换行、转义竖线。"""
    if value is None:
        return ""
    return (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", "\\|")
        .strip()
    )


def _result_markdown_table(result_data: Any) -> str | None:
    """把 table 型 result_data 渲染成 Markdown 表格（非表格返回 None）。"""
    if not isinstance(result_data, dict):
        return None
    columns = result_data.get("columns")
    rows = result_data.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    header = "| # | " + " | ".join(_md_cell(c) for c in columns) + " |"
    separator = "| " + " | ".join(["---"] * (len(columns) + 1)) + " |"
    lines = [header, separator]
    shown = rows[:_REPORT_MAX_ROWS]
    for idx, row in enumerate(shown):
        if not isinstance(row, (list, tuple)):
            continue
        cells = " | ".join(
            _md_cell(row[i]) if i < len(row) else "" for i in range(len(columns))
        )
        lines.append(f"| {idx + 1} | {cells} |")
    if len(rows) > len(shown):
        lines.append(
            f"\n> 结果共 {len(rows)} 行，仅展示前 {_REPORT_MAX_ROWS} 行。"
        )
    return "\n".join(lines)


def _evidence_markdown(verdict: dict[str, Any]) -> str:
    """把单个数字回溯判定渲染成一行报告证据。"""
    raw = _md_cell(verdict.get("raw")) or "(空)"
    kind = str(verdict.get("kind") or "number")
    kind_label = _NUMBER_KIND_LABELS.get(kind, kind)
    head = f"- `{raw}`（{kind_label}）"
    if not verdict.get("found"):
        return head + " — ❌ **未在结果数据中找到证据（疑似幻觉数字）**"

    ev = verdict.get("evidence") or {}
    matched = ev.get("matched")
    if ev.get("type") == "table_meta" and matched == "row_count":
        detail = (
            f"✅ 计数与结果表行数一致（表共 {ev.get('total_rows')} 行，"
            f"答案称 {ev.get('count')}）"
        )
    elif ev.get("type") == "table_meta" and matched == "row_rank":
        detail = (
            f"✅ 序数与结果表排名一致（第 {ev.get('rank')} 名，"
            f"表共 {ev.get('total_rows')} 行）"
        )
    elif ev.get("column") is not None:
        row_idx = ev.get("row")
        if isinstance(row_idx, (int, float)):
            row_label = f"第 {int(row_idx) + 1} 行（row={int(row_idx)}）"
        else:
            row_label = "某一行"
        detail = (
            f"✅ 命中结果表{row_label} `{_md_cell(ev.get('column'))}` 列，"
            f"实际值 `{_md_cell(ev.get('evidence_value'))}`"
        )
    elif ev.get("type") == "single_value":
        detail = f"✅ 命中单值结果，值 `{_md_cell(ev.get('evidence_value'))}`"
    elif ev.get("type") == "record":
        detail = (
            f"✅ 命中结果字段 `{_md_cell(ev.get('key'))}`，"
            f"值 `{_md_cell(ev.get('evidence_value'))}`"
        )
    else:
        detail = (
            "✅ 已验证：`"
            + _md_cell(json.dumps(ev, ensure_ascii=False, default=str))
            + "`"
        )
    return head + " " + detail


async def build_session_report(
    session: AsyncSession, session_id: int
) -> str | None:
    """组装一次会话的 Markdown 报告。

    版块：基本信息 → 问题 → 分析计划 → 图表建议 + 结果数据表 →
    最终答案 → 每个数字的溯源证据。会话不存在返回 None；尚未完成抛 409。
    """
    query_session = await session.get(QuerySession, session_id)
    if query_session is None:
        return None
    result = (
        await session.execute(
            select(QueryResult).where(QueryResult.session_id == session_id)
        )
    ).scalar_one_or_none()
    if result is None:
        raise ServiceError(409, f"会话 {session_id} 尚未分析完成，暂无报告")
    datasource = await session.get(DataSource, query_session.datasource_id)

    steps = (
        await session.execute(
            select(AnalysisStep)
            .where(AnalysisStep.session_id == session_id)
            .order_by(AnalysisStep.id)
        )
    ).scalars().all()

    plan = ""
    result_data: Any = None
    chart_suggestion: dict[str, Any] | None = None
    verdicts: list[dict[str, Any]] = []
    for step in steps:
        meta = None
        if step.meta_json:
            try:
                meta = json.loads(step.meta_json)
            except json.JSONDecodeError:
                meta = None
        if step.step_type == "plan":
            plan = step.content
        elif step.step_type == "execute" and meta:
            result_data = meta.get("result_data")
            chart_suggestion = meta.get("chart_suggestion")
        elif step.step_type == "answer" and meta:
            verdicts = meta.get("number_verdicts") or []

    lines: list[str] = ["# DataSage 分析报告", ""]
    ds_label = datasource.name if datasource else str(query_session.datasource_id)
    ds_line = f"- 数据源：**{_md_cell(ds_label)}**（ID: {query_session.datasource_id}"
    if datasource and datasource.row_count:
        ds_line += f"，{datasource.row_count} 行"
    lines.append(ds_line + "）")
    lines.append(f"- 会话 ID：{session_id}")
    if query_session.prev_session_id:
        lines.append(f"- 多轮追问：上一轮会话 #{query_session.prev_session_id}")
    if query_session.created_at:
        lines.append(
            "- 提问时间："
            + query_session.created_at.strftime("%Y-%m-%d %H:%M:%S")
        )
    found_count = sum(1 for item in verdicts if item.get("found"))
    if result.verified and result.hallucination_count == 0:
        verify_text = (
            f"✅ 已通过反幻觉校验（{found_count}/{len(verdicts)} 个数字均可回溯）"
        )
    else:
        verify_text = (
            f"⚠️ 检出 {result.hallucination_count} 处无法回溯的数字"
            f"（{found_count}/{len(verdicts)} 可回溯）"
        )
    lines.append(f"- 反幻觉校验：{verify_text}")

    lines += ["", "## 问题", "", query_session.question, ""]
    lines += ["## 分析计划", "", plan or "（无）", ""]

    lines.append("## 结果数据")
    lines.append("")
    if chart_suggestion:
        chart_label = _CHART_TYPE_LABELS.get(
            str(chart_suggestion.get("type")),
            str(chart_suggestion.get("type")),
        )
        lines.append(
            f"> 自动可视化建议：**{chart_label}**，"
            f"X 轴 `{_md_cell(chart_suggestion.get('x'))}`，"
            f"Y 轴 `{_md_cell(chart_suggestion.get('y'))}`"
        )
        lines.append("")
    table_md = _result_markdown_table(result_data)
    if table_md:
        if isinstance(result_data, dict) and result_data.get("total_rows"):
            lines.append(f"结果共 {result_data['total_rows']} 行：")
            lines.append("")
        lines.append(table_md)
        lines.append("")
    elif result_data is not None:
        lines.append("```json")
        lines.append(
            json.dumps(result_data, ensure_ascii=False, indent=2, default=str)[:4000]
        )
        lines += ["```", ""]

    lines += ["## 最终答案", "", result.answer_text, ""]
    lines += ["## 数字溯源", ""]
    if verdicts:
        lines.extend(_evidence_markdown(item) for item in verdicts)
    else:
        lines.append("（本答案不包含需要回溯的数字）")
    lines += [
        "",
        "---",
        "",
        "*本报告由 DataSage 自动生成：自然语言提问 → LLM 生成 pandas 代码 → "
        "沙箱执行 → 三层验证 → 数字级反幻觉回溯。*",
        "",
    ]
    return "\n".join(lines)
