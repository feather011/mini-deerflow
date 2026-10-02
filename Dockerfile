# ============================================================
# 后端镜像（Phase 23）
#
# 关键约束：
# - 非 root 用户运行
# - 镜像里**不含** .env / API Key / 数据库 / 上传文件 / 测试结果
# - 依赖先装、源码后拷，充分利用层缓存
# - 凭据只在容器运行时由 compose 注入
# ============================================================

# ---- 构建阶段：用 uv 解析并同步锁定的依赖 ----
FROM python:3.12-slim AS builder

# 与本机 uv 版本一致的官方镜像，避免"能跑但锁文件版本漂移"
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只拷依赖清单：只要 pyproject/uv.lock 不变，这一层就命中缓存
COPY pyproject.toml uv.lock ./
# setuptools 的 src 布局需要源码才能构建项目本身
COPY src ./src
RUN uv sync --frozen --no-dev

# ---- 运行阶段：精简、非 root ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    # 生产数据一律落在挂载卷里，容器重建后仍然存在
    MINI_DEERFLOW_DB_PATH=/data/mini-deerflow.db \
    MINI_DEERFLOW_WORKSPACE_ROOT=/workspace

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src

# 固定卷挂载点；所有权交给非 root 用户
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data /workspace \
    && chown -R appuser:appuser /data /workspace

USER appuser

EXPOSE 8000

# create_app 是工厂函数，必须用 --factory；它在 lifespan 内才读配置、建客户端
CMD ["uvicorn", "mini_deerflow.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
