"""提问 / 分析 / 会话追溯路由。

- POST /query           针对某数据源发起自然语言提问，跑完整可信分析链路
- GET  /sessions/{id}   查询会话状态、结构化结果、分析步骤与最终答案

路由只做参数校验与依赖注入，编排在 app.services.query_service。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session as get_db_session
from app.llm.client import LLMClient, get_llm_client
from app.services import query_service
from app.services.errors import ServiceError

router = APIRouter(prefix="/query", tags=["query"])
sessions_router = APIRouter(prefix="/sessions", tags=["query"])


class QueryRequest(BaseModel):
    """提问请求体。"""

    datasource_id: int = Field(..., description="已上传数据源的 id")
    question: str = Field(..., min_length=1, max_length=2000, description="自然语言问题")
    # 多轮追问：传入同一数据源上一轮会话 id，后端把上轮摘要注入 prompt
    prev_session_id: int | None = Field(
        default=None, ge=1, description="多轮追问时上一轮会话的 id"
    )

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


def llm_dependency() -> LLMClient:
    """按全局 settings 构造 LLM 客户端（mock / openai / anthropic）。"""
    return get_llm_client()


@router.post("", summary="发起自然语言分析（plan→执行→验证→答案→反幻觉回溯）")
async def create_query(
    body: QueryRequest,
    session: AsyncSession = Depends(get_db_session),
    llm: LLMClient = Depends(llm_dependency),
) -> dict:
    return await query_service.run_query(
        session,
        body.datasource_id,
        body.question,
        llm,
        prev_session_id=body.prev_session_id,
    )


@sessions_router.get("/{session_id}", summary="会话详情（含溯源步骤）")
async def get_session(
    session_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    detail = await query_service.get_session_detail(session, session_id)
    if detail is None:
        raise ServiceError(404, f"会话 {session_id} 不存在")
    return detail


async def export_session_report(
    session_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    """导出一次会话的 Markdown 分析报告（问题/计划/图表建议/答案/数字证据）。"""
    markdown = await query_service.build_session_report(session, session_id)
    if markdown is None:
        raise ServiceError(404, f"会话 {session_id} 不存在")
    filename = f"datasage-report-{session_id}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# 同时挂在 /query 与 /sessions 两个前缀下，前端走 sessions，文档契约保留 query
report_summary = "导出 Markdown 分析报告"
sessions_router.add_api_route(
    "/{session_id}/report",
    export_session_report,
    methods=["GET"],
    summary=report_summary,
)
router.add_api_route(
    "/{session_id}/report",
    export_session_report,
    methods=["GET"],
    summary=report_summary,
)
