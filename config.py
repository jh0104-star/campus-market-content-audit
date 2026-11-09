# -*- coding: utf-8 -*-
"""全局配置。

系统只保留两种引擎，通过环境变量或运行界面切换，缺省为 mock(离线) 模式，
保证没有网络 / 没有 API Key 时系统依然可完整演示。

    LLM_PROVIDER = mock     : 本地语义分析器(离线可跑，模拟大模型输出)
    LLM_PROVIDER = deepseek : DeepSeek(deepseek-chat / deepseek-reasoner)
"""
import os
import secrets
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "campus_market.db"


def _load_dotenv(path: Optional[Path] = None) -> None:
    """极简 .env 加载器，避免额外依赖。"""
    path = path or BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        _ = os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ---------------- LLM ----------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock").lower()
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "25"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
# 是否要求服务端返回严格 JSON（response_format=json_object）。部分网关不支持，故默认关闭；
# 若大模型经常在 JSON 前后附带多余文字，可设为 1 提升解析稳定性。
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "0") in ("1", "true", "True", "yes")

_PROVIDER_PRESET = {
    # DeepSeek 官方 OpenAI 兼容地址本身不带 /v1，代码会自动拼接 /chat/completions
    "deepseek": "https://api.deepseek.com",
}
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or _PROVIDER_PRESET.get(LLM_PROVIDER, "")

# ---------------- 审核阈值 ----------------
REJECT_SCORE = 70      # 风险分 >= 该值 -> 拒绝发布
REVIEW_SCORE = 35      # 风险分 >= 该值 -> 转人工复核
RULE_WEIGHT = 0.55     # 融合时规则引擎权重
LLM_WEIGHT = 0.45      # 融合时大模型权重

# ---------------- 安全 ----------------
# 管理接口口令。未配置时管理接口仅允许本机回环地址访问（演示默认安全），
# 一旦要暴露到局域网/公网必须配置，否则非本机请求一律 403。
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# 卖家登录口令（统一口令，演示用）。未配置时退化为"学号直接登录"（保持演示体验），
# 部署到真实场景前必须设置，否则任何人可冒充学号登录。
SELLER_LOGIN_CODE = os.getenv("SELLER_LOGIN_CODE", "")

# ---------------- 用户权限（RBAC） ----------------
# 是否强制登录：0（默认）时未登录请求沿用旧行为（本机 / ADMIN_TOKEN 视为内置管理员），
# 便于老脚本与演示直接调用；设为 1 后所有受保护接口必须携带有效登录态。
RBAC_ENFORCE = os.getenv("RBAC_ENFORCE", "0") in ("1", "true", "True", "yes")
# 内置管理员账号（首次启动自动创建，已存在则跳过）
DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
# 登录令牌有效期（秒）：供 curl / 脚本等无 cookie 场景使用
AUTH_TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", "43200"))

# ---------------- 运维 ----------------
OPLOG_RETENTION_DAYS = int(os.getenv("OPLOG_RETENTION_DAYS", "180"))     # 操作日志保留天数
NOTIFY_RETENTION_DAYS = int(os.getenv("NOTIFY_RETENTION_DAYS", "90"))    # 站内通知保留天数
BATCH_MAX_ITEMS = int(os.getenv("BATCH_MAX_ITEMS", "200"))               # 单个批量任务条目上限
IMPORT_MAX_ROWS = int(os.getenv("IMPORT_MAX_ROWS", "1000"))              # 单次导入行数上限

# ---------------- 服务 ----------------
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# 未显式配置时随机生成：避免源码里的硬编码密钥被直接拿来伪造 session cookie。
# 代价是进程重启后登录态失效（多进程部署时请显式设置 SECRET_KEY 环境变量）。
_SECRET_KEY_ENV = os.getenv("SECRET_KEY")
SECRET_KEY = _SECRET_KEY_ENV or secrets.token_hex(32)
SECRET_KEY_IS_EPHEMERAL = not _SECRET_KEY_ENV
