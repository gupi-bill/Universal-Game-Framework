# syntax=docker/dockerfile:1
# Universal-Game-Framework 单文件 Agent 镜像（ROADMAP #23）
# ==========================================================
# 构建: docker build -t ugf-agent .
# 试跑: docker run --rm ugf-agent run --dry-run --rounds 20
# 自检: docker run --rm ugf-agent selftest
# 编排: docker compose up agent   （见 docker-compose.yml）
#
# 改进点（相对旧版单阶段）：
#   - 多阶段构建：依赖在 builder 阶段装进独立 prefix，运行层只拷产物
#   - 非 root 运行（uid 10001），/app 与数据卷最小权限
#   - HEALTHCHECK 用 `agent.py mode`（快速、只读、零副作用）
#   - UGF_HOME=/data 声明为 VOLUME：知识库/日志/状态跨容器重建保留
#   - 想接真实感知 / LLM：把 .env 挂进来（-v "$PWD/.env:/app/.env"）

# ──────────── 构建阶段 ────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

# ──────────── 运行阶段 ────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UGF_HOME=/data \
    UGF_DRY_RUN=1 \
    UGF_PERCEPTION_BACKEND=mock

WORKDIR /app

COPY --from=builder /install /usr/local
COPY agent.py config.yaml ./
COPY game_profiles/ ./game_profiles/
COPY tools/ ./tools/

RUN useradd -m -u 10001 ugf \
    && mkdir -p /data \
    && chown -R ugf:ugf /app /data

USER ugf
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=30s --start-period=15s --retries=3 \
    CMD ["python", "agent.py", "mode"]

# 默认跑一次自检，确认链路完好；生产用 command 覆盖为 run
CMD ["python", "agent.py", "selftest"]
