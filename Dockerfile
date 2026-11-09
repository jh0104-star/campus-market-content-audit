# syntax=docker/dockerfile:1
# 校园二手交易内容审核系统 —— 运行镜像
# 构建：docker build -t campus-market-content-audit:1.0 .
# 运行：docker run -p 8000:8000 --env-file .env campus-market-content-audit:1.0
FROM python:3.11-slim

# 容器内必须监听 0.0.0.0，否则宿主机映射不到端口（代码默认是 127.0.0.1）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8000

WORKDIR /app

# 依赖单独一层：改动代码时不必重装依赖
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# 非 root 运行；data/ 需要可写（数据库、上传图、runtime.json 都在这里）
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app \
    && chmod +x docker-entrypoint.sh
USER app

EXPOSE 8000

# 打首页探活（无 API Key 时为离线模式，同样返回 200）
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request;\
u='http://127.0.0.1:%s/' % os.environ.get('FLASK_PORT','8000');\
sys.exit(0 if urllib.request.urlopen(u, timeout=2).getcode()==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]
