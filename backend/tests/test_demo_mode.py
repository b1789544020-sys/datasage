"""反幻觉演示模式端到端测试。

打开 DATASAGE_DEMO_HALLUCINATION 后，mock 的答案初稿必含无法回溯的数字，
完整链路必须完成：初稿翻车 -> 数字回溯拦截 -> 自动重生成 -> 最终全绿。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import get_session
from app.llm import client as client_module
from app.main import app
from app.models import Base
from app.services import datasource_service

CSV_BYTES = (
    "date,region,product,sales_amount,quantity\n"
    "2024-01-08,华东,保温杯,1280,32\n"
    "2024-02-14,华东,背包,760,9\n"
    "2024-02-20,华南,保温杯,860,21\n"
    "2024-03-21,华北,背包,2450,14\n"
    "2024-04-05,西南,保温杯,640,16\n"
    "2024-04-18,华南,背包,530,6\n"
).encode("utf-8")


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def ctx(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}",
        poolclass=NullPool,
    )
    asyncio.run(_create_tables(engine))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    old_upload_dir = datasource_service.UPLOAD_DIR
    datasource_service.UPLOAD_DIR = upload_dir
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        datasource_service.UPLOAD_DIR = old_upload_dir
        asyncio.run(engine.dispose())


def test_demo_mode_draft_fails_then_regeneration_passes(ctx, monkeypatch):
    # 仅对 mock provider 有意义；真实 provider 环境下跳过
    if client_module.settings.llm_provider != "mock":
        pytest.skip("演示模式只在 mock provider 下可复现")
    monkeypatch.setattr(client_module.settings, "demo_hallucination", True)

    up = ctx.post(
        "/api/v1/datasources",
        files={"file": ("demo_sales.csv", CSV_BYTES, "text/csv")},
    )
    assert up.status_code == 201, up.text
    datasource = up.json()

    resp = ctx.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "各地区销售额汇总，总销售额是多少？哪个地区最高？",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    answer_meta = next(
        step["meta"] for step in body["steps"] if step["step_type"] == "answer"
    )

    # 1) 初稿确实产生了被数字回溯拦截的幻觉数字
    assert answer_meta["initial_answer"]
    removed = [
        raw
        for item in answer_meta.get("regeneration_history", [])
        for raw in item.get("removed_hallucinations", [])
    ]
    assert removed, "至少应记录一个被移除的幻觉数字"

    # 2) 触发了自动重生成
    assert answer_meta["regeneration_rounds"] >= 1

    # 3) 最终答案完全干净：全部数字可回溯
    assert body["verified"] is True
    assert body["hallucination_count"] == 0
    assert body["answer_numbers"]
    assert all(v["found"] for v in body["answer_numbers"])

    # 4) 初稿与终稿不是同一句话（确实发生了重写）
    assert body["answer"] != answer_meta["initial_answer"]
