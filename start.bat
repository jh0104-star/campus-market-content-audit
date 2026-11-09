@echo off
chcp 65001 >nul
title 校园二手交易智能审核系统 - 一键启动
cd /d "%~dp0"

echo ==========================================
echo   校园二手交易信息智能审核与分类系统
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

echo [1/3] 检查依赖...
pip show flask >nul 2>&1
if errorlevel 1 (
    echo       首次运行，正在安装依赖（使用清华源）...
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
) else (
    echo       依赖已就绪
)

if not exist "data\campus_market.db" (
    echo [2/3] 初始化数据库并写入模拟数据...
    python data/seed.py --reset
) else (
    echo [2/3] 数据库已存在，跳过初始化（如需重置： python data/seed.py --reset）
)

echo [3/3] 启动服务...
start "Flask Web (5000)" cmd /k "python app.py"
timeout /t 3 >nul
start "Streamlit (8501)" cmd /k "python -m streamlit run ui_streamlit.py --server.headless=true"

echo.
echo ==========================================
echo   Web 端        http://127.0.0.1:5000
echo   卖家中心      http://127.0.0.1:5000/seller
echo   管理后台      http://127.0.0.1:5000/admin
echo   可视化大屏    http://127.0.0.1:8501
echo ==========================================
echo 关闭方式：直接关闭两个服务窗口，或按 Ctrl+C
pause
