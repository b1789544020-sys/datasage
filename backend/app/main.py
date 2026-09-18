"""FastAPI 应用入口。

启动方式（在 backend/ 目录下）：
    uvicorn app.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import datasource, query
from app.config import settings
from app.database import init_db
from app.services.errors import ServiceError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时建表，关闭时清理连接池。"""
    await init_db()
    yield
    from app.database import engine  # noqa: PLC0415

    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    description="可信数据分析引擎：自然语言提问 → pandas 代码 → 沙箱执行 → 反幻觉校验",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

# 前端 Vite dev server 跨域支持（生产环境通过反向代理同源部署）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


@app.exception_handler(ServiceError)
async def service_error_handler(
    request: Request, exc: ServiceError
) -> JSONResponse:
    """service 层业务异常统一翻译成 ``{"detail": ...}`` 的 HTTP 响应。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# 业务路由：统一挂在 /api/v1 前缀下
# POST   /api/v1/datasources           上传
# GET    /api/v1/datasources           列表
# GET    /api/v1/datasources/{id}      详情（完整 schema）
# POST   /api/v1/query                 提问 / 分析（可带 prev_session_id 追问）
# GET    /api/v1/sessions/{id}         会话溯源详情
# GET    /api/v1/sessions/{id}/report  导出 Markdown 分析报告（/query/{id}/report 同义）
app.include_router(datasource.router, prefix="/api/v1")
app.include_router(query.router, prefix="/api/v1")
app.include_router(query.sessions_router, prefix="/api/v1")
