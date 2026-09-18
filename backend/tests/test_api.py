"""端到端 API 测试：数据上传 → 提问全链路（mock provider）→ 四张表落库校验。

不依赖网络：LLMClient 走 mock provider，沙箱真实执行 pandas 代码，
数据库用 tmp_path 下的临时 SQLite（NullPool 保证跨事件循环安全）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import get_session
from app.main import app
from app.models import AnalysisStep, Base, DataSource, QueryResult, QuerySession
from app.services import datasource_service

# 内置示例数据的同款结构（mock codegen 能自动识别 region 维度 + sales_amount 指标）
CSV_BYTES = (
    "date,region,category,sales_amount,quantity\n"
    "2024-01-05,华东,家电,100,2\n"
    "2024-01-12,华东,数码,50,1\n"
    "2024-02-03,华南,家电,80,3\n"
    "2024-02-15,华北,食品,30,5\n"
    "2024-03-01,西南,家电,200,4\n"
    "2024-03-09,西南,数码,100,2\n"
).encode("utf-8")


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def ctx(tmp_path):
    """返回 (TestClient, 异步 session 工厂)，DB 与上传目录都隔离在 tmp_path。"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test_api.db').as_posix()}",
        poolclass=NullPool,
    )
    asyncio.run(_create_tables(engine))
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    old_upload_dir = datasource_service.UPLOAD_DIR
    datasource_service.UPLOAD_DIR = upload_dir

    # 不使用 with：避免触发 lifespan 去建默认 datasage.db
    client = TestClient(app)
    try:
        yield client, session_factory
    finally:
        app.dependency_overrides.clear()
        datasource_service.UPLOAD_DIR = old_upload_dir
        asyncio.run(engine.dispose())


def _count(session_factory, model) -> int:
    async def _query() -> int:
        async with session_factory() as session:
            return (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()

    return asyncio.run(_query())


def _upload(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/datasources",
        files={"file": ("sample_sales.csv", CSV_BYTES, "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------- 任务 A：上传接口 ---------------------------

def test_upload_csv_returns_schema_and_id(ctx):
    client, _ = ctx
    body = _upload(client)

    assert set(body) >= {"id", "name", "row_count", "columns", "created_at"}
    assert body["name"] == "sample_sales.csv"
    assert body["row_count"] == 6
    assert body["id"] > 0

    columns = {col["name"]: col for col in body["columns"]}
    assert set(columns) == {"date", "region", "category", "sales_amount", "quantity"}
    assert columns["region"]["dtype"] == "categorical"
    assert columns["sales_amount"]["dtype"] == "numeric"
    assert isinstance(columns["region"]["sample_values"], list)

    # 文件确实落盘到了 uploads 目录
    assert datasource_service.UPLOAD_DIR.exists()
    assert any(
        datasource_file.name.endswith(".csv")
        for datasource_file in datasource_service.UPLOAD_DIR.iterdir()
    )


def test_list_summary_and_detail_full_schema(ctx):
    client, _ = ctx
    created = _upload(client)

    # 列表：只有概要，不含 schema / columns
    resp = client.get("/api/v1/datasources")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    summary = items[0]
    assert summary["id"] == created["id"]
    assert "columns" not in summary and "schema" not in summary
    assert summary["status"] == "ready"

    # 详情：完整 schema（含 extractor 的统计字段）
    resp = client.get(f"/api/v1/datasources/{created['id']}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["schema"]["file_name"].endswith("sample_sales.csv")
    assert detail["schema"]["row_count"] == 6
    schema_cols = {c["name"]: c for c in detail["schema"]["columns"]}
    assert "numeric_stats" in schema_cols["sales_amount"]
    assert schema_cols["sales_amount"]["numeric_stats"]["max"] == 200.0


def test_sample_datasource_endpoint(ctx):
    """POST /datasources/sample 一键加载内置示例，响应结构与上传一致。"""
    client, _ = ctx
    resp = client.post("/api/v1/datasources/sample")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["name"] == "sample_sales.csv"
    assert body["status"] == "ready"
    assert body["row_count"] == 6
    assert {col["name"] for col in body["columns"]} == {
        "date", "region", "category", "sales_amount", "quantity"
    }
    sample_cols = {col["name"]: col for col in body["columns"]}
    assert sample_cols["sales_amount"]["dtype"] == "numeric"


def test_upload_rejects_unsupported_type(ctx):
    client, _ = ctx
    resp = client.post(
        "/api/v1/datasources",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


# --------------------------- 任务 B：提问接口 ---------------------------

def test_query_end_to_end_mock_provider(ctx):
    client, session_factory = ctx
    datasource = _upload(client)

    resp = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "各地区销售额总和是多少？哪个地区最高？",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # ---- 响应契约 ----
    assert set(body) >= {
        "session_id", "question", "plan", "answer", "verified",
        "hallucination_count", "result_data", "steps",
    }
    assert body["question"] == "各地区销售额总和是多少？哪个地区最高？"
    assert body["plan"]
    assert body["status"] == "done"

    # mock 答案只引用真实数字（300/150），数字回溯零幻觉
    assert body["verified"] is True
    assert body["hallucination_count"] == 0
    assert "300" in body["answer"]
    assert "西南" in body["answer"]

    # 结构化结果：按地区聚合后的 4 行表
    result_data = body["result_data"]
    assert result_data["type"] == "table"
    assert result_data["total_rows"] == 4
    assert result_data["columns"] == ["region", "sales_amount"]
    first_row = result_data["data"][0]
    assert first_row[0] == "西南" and float(first_row[1]) == 300.0

    # 溯源步骤：plan / code / execute / verify / answer 各一条且顺序正确
    step_types = [s["step_type"] for s in body["steps"]]
    assert step_types == ["plan", "code", "execute", "verify", "answer"]
    plan_step, code_step, exec_step, verify_step, answer_step = body["steps"]
    assert "load_dataframe()" in code_step["content"]
    assert exec_step["meta"]["success"] is True
    assert exec_step["meta"]["attempts"] >= 1
    assert verify_step["meta"]["passed"] is True
    assert answer_step["meta"]["hallucination_count"] == 0
    assert answer_step["meta"]["regeneration_rounds"] == 0
    # meta_json 里保留了结构化结果，供会话详情回看
    assert exec_step["meta"]["result_data"]["type"] == "table"

    # ---- 四张表都有正确写入 ----
    assert _count(session_factory, DataSource) == 1
    assert _count(session_factory, QuerySession) == 1
    assert _count(session_factory, AnalysisStep) == 5
    assert _count(session_factory, QueryResult) == 1

    # ---- 会话详情接口 ----
    detail = client.get(f"/api/v1/sessions/{body['session_id']}").json()
    assert detail["status"] == "done"
    assert detail["answer"] == body["answer"]
    assert detail["verified"] is True
    assert len(detail["steps"]) == 5
    assert detail["result_data"]["total_rows"] == 4


def test_query_response_contains_number_verdicts(ctx):
    """响应（及会话详情）带每个数字的 found/evidence 判定，供前端溯源渲染。"""
    client, _ = ctx
    datasource = _upload(client)

    resp = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "各地区销售额总和是多少？哪个地区最高？",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    verdicts = body["answer_numbers"]
    assert isinstance(verdicts, list) and verdicts
    for item in verdicts:
        assert set(item) >= {
            "raw", "value", "kind", "position", "found", "evidence",
        }
    # mock 答案数字全部可回溯
    assert all(item["found"] for item in verdicts)

    # 300 应回溯到结果表 sales_amount 列的某个单元格
    hit_300 = next(item for item in verdicts if float(item["value"]) == 300.0)
    evidence = hit_300["evidence"]
    assert evidence["column"] == "sales_amount"
    assert isinstance(evidence["row"], int)
    assert float(evidence["evidence_value"]) == 300.0

    # answer 步骤 meta 持久化了同一份判定，会话详情可回放
    answer_meta = next(
        s["meta"] for s in body["steps"] if s["step_type"] == "answer"
    )
    assert len(answer_meta["number_verdicts"]) == len(verdicts)
    detail = client.get(f"/api/v1/sessions/{body['session_id']}").json()
    assert len(detail["answer_numbers"]) == len(verdicts)


def test_query_returns_chart_suggestion_for_table(ctx):
    """table 型结果自动带 chart_suggestion（region 分类 + sales_amount 数值 → 柱状图）。"""
    client, _ = ctx
    datasource = _upload(client)

    body = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "各地区销售额总和是多少？",
        },
    ).json()

    assert body["chart_suggestion"] == {
        "type": "bar", "x": "region", "y": "sales_amount",
    }
    # 推荐持久化在 execute 步骤 meta 中，会话详情回看仍可拿到
    detail = client.get(f"/api/v1/sessions/{body['session_id']}").json()
    assert detail["chart_suggestion"] == body["chart_suggestion"]
    assert detail["prev_session_id"] is None


def test_followup_query_carries_previous_context(ctx):
    """连续两轮：第二轮带 prev_session_id，mock 追问消解后只筛出"华东"。"""
    client, _ = ctx
    datasource = _upload(client)

    q1 = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "各地区销售额总和是多少？哪个地区最高？",
        },
    ).json()
    assert q1["prev_session_id"] is None
    assert q1["result_data"]["total_rows"] == 4

    q2 = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "那华东呢？",
            "prev_session_id": q1["session_id"],
        },
    ).json()

    # 追问链路回传并落库
    assert q2["prev_session_id"] == q1["session_id"]
    plan_meta = next(
        step["meta"] for step in q2["steps"] if step["step_type"] == "plan"
    )
    assert plan_meta["followup"] == {
        "prev_session_id": q1["session_id"],
        "prev_question": "各地区销售额总和是多少？哪个地区最高？",
    }

    # mock 沿用上轮口径 + 消解"华东"指代：只聚合华东（100 + 50 = 150）
    table = q2["result_data"]
    assert table["columns"] == ["region", "sales_amount"]
    assert table["total_rows"] == 1
    assert table["data"][0][0] == "华东"
    assert float(table["data"][0][1]) == 150.0

    # 第二轮表格同样自动出图表
    assert q2["chart_suggestion"]["type"] == "bar"


def test_followup_unknown_prev_session_returns_404(ctx):
    client, _ = ctx
    datasource = _upload(client)
    resp = client.post(
        "/api/v1/query",
        json={
            "datasource_id": datasource["id"],
            "question": "那华东呢？",
            "prev_session_id": 9999,
        },
    )
    assert resp.status_code == 404


def test_session_report_endpoint_returns_markdown(ctx):
    """报告导出：问题 / 计划 / 图表建议 / 答案 / 数字证据都在 Markdown 里。"""
    client, _ = ctx
    datasource = _upload(client)
    question = "各地区销售额总和是多少？哪个地区最高？"
    body = client.post(
        "/api/v1/query",
        json={"datasource_id": datasource["id"], "question": question},
    ).json()

    resp = client.get(f"/api/v1/sessions/{body['session_id']}/report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    markdown = resp.text

    assert "# DataSage 分析报告" in markdown
    assert question in markdown                 # 问题
    assert body["plan"] in markdown             # 分析计划
    assert "柱状图" in markdown                 # 图表建议
    assert "sales_amount" in markdown           # 结果数据表
    assert body["answer"] in markdown           # 最终答案
    assert "数字溯源" in markdown
    assert "300" in markdown                     # 可回溯数字
    assert "反幻觉校验" in markdown

    # /query/{id}/report 同义路径
    alias = client.get(f"/api/v1/query/{body['session_id']}/report")
    assert alias.status_code == 200
    assert alias.text == markdown

    # 不存在的会话 → 404
    assert client.get("/api/v1/sessions/9999/report").status_code == 404


def test_query_unknown_datasource_returns_404(ctx):
    client, _ = ctx
    resp = client.post(
        "/api/v1/query",
        json={"datasource_id": 9999, "question": "销售额最高的地区？"},
    )
    assert resp.status_code == 404


def test_query_blank_question_returns_422(ctx):
    client, _ = ctx
    datasource = _upload(client)
    resp = client.post(
        "/api/v1/query",
        json={"datasource_id": datasource["id"], "question": "   "},
    )
    assert resp.status_code == 422
