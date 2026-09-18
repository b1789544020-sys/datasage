"""数据库连接与会话管理。

提供：
- 全局异步 SQLAlchemy engine
- AsyncSessionLocal 异步会话工厂
- get_session：FastAPI 依赖注入用的会话依赖
- init_db：建表（骨架阶段直接 create_all，后续可换成 Alembic）
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def normalize_database_url(url: str) -> str:
    """把同步 sqlite 驱动前缀自动改写为异步 aiosqlite 驱动。

    例：sqlite:///./datasage.db -> sqlite+aiosqlite:///./datasage.db
    已经带驱动名（如 postgresql+asyncpg://）的地址原样返回。
    """
    if url.startswith("sqlite:///") and "+aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


engine = create_async_engine(
    normalize_database_url(settings.database_url),
    echo=settings.debug,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求一个会话，请求结束自动关闭。"""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """创建所有数据表（仅骨架 / 开发环境使用）。"""
    # 局部导入，避免循环导入
    from app.models import Base  # noqa: PLC0415

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Alembic 之前的过渡：给老库补新增的可空列
        await conn.run_sync(_apply_lightweight_migrations)


def _apply_lightweight_migrations(conn) -> None:  # noqa: ANN001
    """开发期轻量迁移：老库缺少新增列时自动 ``ALTER TABLE ADD COLUMN``。

    只做"追加可空列"这类向后兼容的安全操作，新库 create_all 已含这些列，
    此函数自动跳过；后续正式迁移应交给 Alembic。
    """
    from sqlalchemy import inspect, text  # noqa: PLC0415

    inspector = inspect(conn)
    if "query_sessions" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("query_sessions")}
    if "prev_session_id" not in existing:
        conn.execute(
            text("ALTER TABLE query_sessions ADD COLUMN prev_session_id INTEGER")
        )
