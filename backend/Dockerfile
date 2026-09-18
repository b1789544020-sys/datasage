# DataSage 后端镜像：FastAPI + pandas，默认 mock LLM，开箱即用
FROM python:3.11-slim

# Python 运行期优化：不生成 .pyc、stdout/stderr 直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码
COPY . .

# SQLite 数据库与上传目录（compose 中以命名卷持久化）
RUN mkdir -p /app/data /app/uploads

EXPOSE 8000

# 容器内直接用异步驱动地址，避免相对路径歧义
ENV DATABASE_URL=sqlite+aiosqlite:///./data/datasage.db

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
