"""应用配置。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
全程类型安全，启动时即完成校验。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["mock", "openai", "anthropic"]


class Settings(BaseSettings):
    """全局配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---- 应用 ----
    app_name: str = Field(default="DataSage", alias="APP_NAME")
    debug: bool = Field(default=True, alias="DEBUG")

    # ---- LLM ----
    llm_provider: LLMProvider = Field(
        default="mock", alias="DATASAGE_LLM_PROVIDER"
    )
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="LLM_BASE_URL"
    )
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")

    # ---- 数据库 ----
    database_url: str = Field(
        default="sqlite:///./datasage.db", alias="DATABASE_URL"
    )

    # ---- 反幻觉演示模式 ----
    # 仅对 mock provider 生效：打开后，"最终答案初稿"会刻意编造 1~2 个数据中
    # 不存在的数字（如虚构的总销售额 / 同比百分比），由数字回溯器抓出并自动
    # 重生成，用于面试 / 录屏演示"红色幻觉 -> 自动修复 -> 绿色全通过"。
    # 默认关闭，任何测试与正式使用都不受影响。
    demo_hallucination: bool = Field(
        default=False, alias="DATASAGE_DEMO_HALLUCINATION"
    )


@lru_cache
def get_settings() -> Settings:
    """返回单例 Settings（lru_cache 保证 .env 只解析一次）。"""
    return Settings()


# 便捷导入：from app.config import settings
settings = get_settings()
