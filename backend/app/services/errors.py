"""service 层统一异常，由 API 层（全局 exception handler）翻译成 HTTP 响应。

service 不直接依赖 fastapi，保持编排逻辑可独立测试。
"""
from __future__ import annotations


class ServiceError(Exception):
    """业务流程失败（资源不存在 / 参数非法 / 分析流程失败等）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
