#!/bin/sh
# 容器启动入口：首次运行自动灌演示数据，之后直接起服务
set -e

if [ "${SEED_ON_START:-1}" = "1" ] && [ ! -f data/campus_market.db ]; then
    echo "[entrypoint] 未发现数据库，写入演示数据（约 40 条样本）"
    python data/seed.py
fi

echo "[entrypoint] 启动 Flask -> ${FLASK_HOST}:${FLASK_PORT}（引擎：${LLM_PROVIDER:-mock}）"
exec python app.py
