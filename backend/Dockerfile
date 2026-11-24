# TradeRAG API / Celery worker 共用镜像（10-4）
# 说明：模型（models/，约 3.3GB）与运行数据不打进镜像，运行时挂载——
# 镜像保持可重建、可缓存；换模型不换镜像。
FROM docker.m.daocloud.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq：psycopg binary 自带则不需要，装上以防万一小版本回落源码包
# libgl1/libglib2.0/libxcb1：rapidocr(opencv) 运行时依赖，缺了 OCR 引擎起不来
# fonts-dejavu-core：验证码渲染需要真字体（slim 镜像零字体，truetype 全部失败
# 会静默回退到内置点阵小字，字号参数失效——真机排查过）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl libgl1 libglib2.0-0 libxcb1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# torch 必须先单独装 CPU 轮子：默认源在 Linux 会拉 CUDA 版（多 2GB+ 的 nvidia 依赖）
# 索引源默认走清华镜像：容器内直连 pypi.org 会偶发 TLS 中断（实测 33 分钟后整层失败），
# 需要官方源时：docker build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --retries 5 --timeout 120 torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --retries 5 --timeout 120 -r requirements.txt -i ${PIP_INDEX}

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
