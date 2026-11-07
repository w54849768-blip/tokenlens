# syntax=docker/dockerfile:1
# TokenLens 容器镜像 -- 纯标准库，无需 pip install
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    TOKENLENS_NO_BROWSER=1 \
    TOKENLENS_SOURCE=demo \
    TOKENLENS_HOST=0.0.0.0 \
    TOKENLENS_PORT=8787

WORKDIR /app
COPY . /app

EXPOSE 8787

# --source 由 TOKENLENS_SOURCE 决定：demo（默认，示例数据）或 local（挂载宿主目录）
CMD ["python", "entrypoint.py"]