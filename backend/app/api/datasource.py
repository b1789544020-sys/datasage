"""数据源相关路由：上传 CSV / Excel、数据源列表、数据源详情（完整 schema）。

路由只做参数接收与依赖注入，业务逻辑在 app.services.datasource_service。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.services import datasource_service
from app.services.errors import ServiceError

router = APIRouter(prefix="/datasources", tags=["datasources"])


@router.post("", status_code=201, summary="上传 CSV / Excel 并解析 schema")
async def create_datasource(
    file: UploadFile = File(..., description="数据文件（.csv / .xlsx / .xls）"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """multipart/form-data 上传：保存到 uploads/ → 提取 schema → 写 DataSource 表。"""
    content = await file.read()
    return await datasource_service.create_datasource(
        session, file.filename or "", content
    )


@router.post("/sample", status_code=201, summary="加载内置示例数据")
async def create_sample_datasource(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """一键加载 backend/data/sample_sales.csv，响应结构与上传接口一致。"""
    return await datasource_service.create_sample_datasource(session)


@router.get("", summary="数据源列表（仅概要，不含 schema）")
async def list_datasources(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await datasource_service.list_datasources(session)


@router.get("/{datasource_id}", summary="数据源详情（含完整 schema）")
async def get_datasource(
    datasource_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    datasource = await datasource_service.get_datasource(session, datasource_id)
    if datasource is None:
        raise ServiceError(404, f"数据源 {datasource_id} 不存在")
    return datasource_service.serialize_detail(datasource)
