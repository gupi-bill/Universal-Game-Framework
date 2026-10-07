# Universal-Game-Framework 单文件 Agent 镜像
# ==========================================
# 构建: docker build -t ugf-agent .
# 试跑: docker run --rm ugf-agent run --dry-run --rounds 20
# 自检: docker run --rm ugf-agent selftest
#
# 说明：离线 dry-run 链路只用标准库，镜像不需要任何第三方包。
# 想让容器接真实感知 / LLM，把 .env 挂进来即可（-v "$PWD/.env:/app/.env"）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 可选依赖（pyyaml / requests）体积很小，一起装上省得运行时才发现降级
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py config.yaml game_profiles/ ./

# 默认跑一次自检，确认链路完好
CMD ["python", "agent.py", "selftest"]
