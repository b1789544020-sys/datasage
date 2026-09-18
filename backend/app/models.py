"""SQLAlchemy 数据模型。

四张核心表：
- DataSource    上传的数据源（CSV / Excel）
- QuerySession  一次自然语言提问会话
- AnalysisStep  分析过程步骤（plan/code/execute/verify/answer），用于溯源
- QueryResult   最终答案及反幻觉校验结果
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


class DataSource(Base):
    """上传的数据源。"""

    __tablename__ = "datasources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 解析出的 schema（列名 / 类型 / 示例值等），JSON 字符串
    schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending | ready | failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    sessions: Mapped[list["QuerySession"]] = relationship(
        back_populates="datasource",
        cascade="all, delete-orphan",
    )


class QuerySession(Base):
    """一次提问会话。"""

    __tablename__ = "query_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(
        ForeignKey("datasources.id"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # 多轮追问：指向同一数据源上一轮会话（首轮提问为空）
    prev_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("query_sessions.id"), nullable=True
    )
    # pending | planning | executing | verifying | done | failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    datasource: Mapped["DataSource"] = relationship(back_populates="sessions")
    steps: Mapped[list["AnalysisStep"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AnalysisStep.id",
    )
    result: Mapped["QueryResult | None"] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        uselist=False,
    )


class AnalysisStep(Base):
    """分析步骤记录，用于答案溯源。

    step_type 取值：plan | code | execute | verify | answer
    """

    __tablename__ = "analysis_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("query_sessions.id"), nullable=False
    )
    step_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 步骤附加元数据（耗时、代码输出、校验明细等），JSON 字符串
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    session: Mapped["QuerySession"] = relationship(back_populates="steps")


class QueryResult(Base):
    """一次提问的最终答案与校验结论。"""

    __tablename__ = "query_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("query_sessions.id"), unique=True, nullable=False
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 是否通过反幻觉校验
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 检出的幻觉（无法回溯的数字 / 结论）数量
    hallucination_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    session: Mapped["QuerySession"] = relationship(back_populates="result")
