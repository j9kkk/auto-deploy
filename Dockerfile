# AutoDeploy —— 自托管部署服务镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUTODEPLOY_HOST=0.0.0.0 \
    AUTODEPLOY_PORT=8770 \
    AUTODEPLOY_DATA_DIR=/app/data

# git / ssh / rsync 供各任务部署方式使用；
# docker CLI 与 compose 插件供「Docker / Docker Compose」部署方式连接宿主机
# 守护进程（需在 compose 中挂载 /var/run/docker.sock，见 README）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client rsync \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo ${VERSION_CODENAME}) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# 以独立低权限账号运行；/app/data 为数据目录（compose 中映射为命名数据卷）
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin autodeploy \
    && mkdir -p /app/data \
    && chown -R autodeploy:autodeploy /app

USER autodeploy
VOLUME /app/data

EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${AUTODEPLOY_PORT:-8770}/api/health" || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["--no-browser"]
