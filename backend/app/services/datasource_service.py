"""数据源 service：文件落盘、schema 提取、DataSource 表读写与响应序列化。

路由层只负责接收 ``UploadFile``，所有业务规则集中在本模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DataSource
from app.schema.extractor import extract_schema
from app.services.errors import ServiceError

logger = logging.getLogger("app.services.datasource")

# 上传文件统一落在 backend/uploads/（已在 .gitignore 忽略）
BACKEND_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BACKEND_ROOT / "uploads"

ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xls"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB 上限

# 文件名中不允许保留的字符（路径分隔 / Windows 非法字符 / 空白）
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\s]+')


# ------------------------------- 文件落盘 -------------------------------

def _safe_filename(original_name: str) -> tuple[str, str]:
    """清洗上传文件名，返回 (安全文件名, 小写扩展名)。"""
    base = Path(original_name or "").name.strip()
    suffix = Path(base).suffix.lower()
    stem = Path(base).stem or "data"
    stem = _UNSAFE_NAME_RE.sub("_", stem).strip("._")[:120] or "data"
    return f"{stem}{suffix}", suffix


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ------------------------------- 序列化 --------------------------------

def parse_schema(datasource: DataSource) -> dict[str, Any]:
    """DataSource.schema_json 文本 → dict（历史数据可能为空）。"""
    if not datasource.schema_json:
        return {}
    try:
        return json.loads(datasource.schema_json)
    except json.JSONDecodeError:
        logger.warning("数据源 %s 的 schema_json 无法解析", datasource.id)
        return {}


def _simplified_columns(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """上传响应里的精简 schema：只回传前端列表预览需要的字段。"""
    simple = []
    for col in schema.get("columns", []):
        simple.append(
            {
                "name": col.get("name"),
                "dtype": col.get("dtype"),
                "sample_values": (col.get("sample_values") or [])[:3],
            }
        )
    return simple


def serialize_summary(
    datasource: DataSource, *, include_columns: bool = False
) -> dict[str, Any]:
    """列表 / 创建响应：数据源概要，默认不含 schema。"""
    item: dict[str, Any] = {
        "id": datasource.id,
        "name": datasource.name,
        "status": datasource.status,
        "row_count": datasource.row_count,
        "created_at": (
            datasource.created_at.isoformat() if datasource.created_at else None
        ),
    }
    if include_columns:
        item["columns"] = _simplified_columns(parse_schema(datasource))
    return item


def serialize_detail(datasource: DataSource) -> dict[str, Any]:
    """详情响应：概要 + extractor 产出的完整 schema。"""
    return {
        **serialize_summary(datasource),
        "file_path": datasource.file_path,
        "schema": parse_schema(datasource),
    }


# -------------------------------- 操作 ---------------------------------

async def create_datasource(
    session: AsyncSession, filename: str, content: bytes
) -> dict[str, Any]:
    """保存上传文件 → 提取 schema → 写入 DataSource 表。"""
    if not filename:
        raise ServiceError(400, "缺少文件名")
    safe_name, suffix = _safe_filename(filename)
    if suffix not in ALLOWED_SUFFIXES:
        raise ServiceError(
            400,
            f"不支持的文件类型 {suffix or '（无扩展名）'}，"
            f"仅支持 {sorted(ALLOWED_SUFFIXES)}",
        )
    if not content:
        raise ServiceError(400, "上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ServiceError(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB）")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_path = UPLOAD_DIR / (
        f"{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}_{safe_name}"
    )
    await asyncio.to_thread(saved_path.write_bytes, content)
    logger.info("上传文件已保存: %s（%d 字节）", saved_path, len(content))

    # schema 提取是 CPU / IO 密集型，放线程池避免阻塞事件循环
    try:
        schema = await asyncio.to_thread(
            extract_schema, str(saved_path), suffix.lstrip(".")
        )
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都转成 400 并清理文件
        logger.exception("schema 提取失败: %s", saved_path)
        await asyncio.to_thread(_unlink_quiet, saved_path)
        raise ServiceError(
            400, f"文件解析失败，请确认是合法的 CSV / Excel 文件：{exc}"
        ) from exc

    datasource = DataSource(
        name=Path(filename).name,
        file_path=str(saved_path),
        schema_json=json.dumps(schema, ensure_ascii=False),
        row_count=schema.get("row_count"),
        status="ready",
    )
    session.add(datasource)
    await session.flush()  # 拿到 id
    await session.commit()
    await session.refresh(datasource)
    logger.info(
        "数据源已入库 id=%s name=%s rows=%s cols=%d",
        datasource.id, datasource.name, datasource.row_count,
        len(schema.get("columns", [])),
    )
    return serialize_summary(datasource, include_columns=True)


# 内置示例数据（随仓库分发在 backend/data/ 下）
SAMPLE_DATA_PATH = BACKEND_ROOT / "data" / "sample_sales.csv"


async def create_sample_datasource(session: AsyncSession) -> dict[str, Any]:
    """加载内置示例 CSV：读取样例文件字节后复用普通上传管线。

    省去用户在试用时自己找文件；样例同样会落盘 uploads/ 并提取 schema。
    """
    if not SAMPLE_DATA_PATH.exists():
        raise ServiceError(404, f"内置示例数据不存在：{SAMPLE_DATA_PATH.name}")
    content = await asyncio.to_thread(SAMPLE_DATA_PATH.read_bytes)
    logger.info("加载内置示例数据: %s（%d 字节）", SAMPLE_DATA_PATH, len(content))
    return await create_datasource(session, SAMPLE_DATA_PATH.name, content)


async def list_datasources(session: AsyncSession) -> list[dict[str, Any]]:
    """全部数据源概要（不含 schema），新的在前。"""
    rows = (
        await session.execute(select(DataSource).order_by(DataSource.id.desc()))
    ).scalars().all()
    return [serialize_summary(row) for row in rows]


async def get_datasource(
    session: AsyncSession, datasource_id: int
) -> DataSource | None:
    return await session.get(DataSource, datasource_id)
