# -*- coding: utf-8 -*-
"""用户权限管理（RBAC）。

设计要点：
* 角色四级：admin / auditor / seller / viewer，权限以「资源.动作」颗粒度声明；
* 口令只存 `pbkdf2_hmac('sha256', 口令, 随机盐, 100000 次)`，不存明文，也不用可逆加密；
* 登录态两种：浏览器走 Flask session，脚本 / curl 走 HMAC 签名令牌（`X-Auth-Token`）；
* 与既有 `app.admin_allowed()`（ADMIN_TOKEN / 本机回环）兼容：未开启 RBAC_ENFORCE 时，
  旧调用方式仍然可用，不会让已有脚本和演示流程失效。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config
from . import database as db
from .logutils import get_logger

logger = get_logger("rbac")

# 口令哈希迭代次数：10 万次在普通笔记本上约 60ms，兼顾安全与演示时的响应速度
PBKDF2_ROUNDS = 100_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LEN = 6

ROLES = {
    "admin": "管理员",
    "auditor": "审核员",
    "seller": "卖家",
    "viewer": "访客",
}
# 角色由高到低，用于「是否至少是某角色」的判断
ROLE_ORDER = ("viewer", "seller", "auditor", "admin")

PERMISSIONS = {
    "item.read": "查看商品与审核结论",
    "item.submit": "提交发布审核",
    "item.review": "人工复核改判",
    "item.delete": "删除信息",
    "appeal.create": "发起申诉",
    "appeal.handle": "处理申诉",
    "blacklist.write": "维护卖家黑名单",
    "rule.replay": "规则重放回归",
    "engine.switch": "切换审核引擎",
    "data.export": "导出审核数据",
    "data.import": "导入批量数据",
    "batch.run": "执行批量任务",
    "batch.read": "查看批量任务",
    "oplog.read": "查看操作日志",
    "oplog.purge": "清理操作日志",
    "notify.read": "查看站内通知",
    "notify.broadcast": "发布系统通知",
    "user.manage": "用户与角色管理",
}

_ALL = frozenset(PERMISSIONS)
ROLE_PERMISSIONS = {
    "admin": _ALL,
    "auditor": _ALL - {"user.manage", "oplog.purge", "item.delete", "engine.switch"},
    "seller": frozenset({"item.read", "item.submit", "appeal.create",
                         "notify.read", "batch.read"}),
    "viewer": frozenset({"item.read", "notify.read"}),
}


# ------------------------------------------------------------------ #
# 口令
# ------------------------------------------------------------------ #
def hash_password(password: str, salt: str = "") -> Tuple[str, str]:
    """返回 (口令哈希, 盐)；盐为空时随机生成。"""
    salt = salt or hashlib.sha256(
        f"{time.time_ns()}:{id(password)}".encode("utf-8")).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), PBKDF2_ROUNDS)
    return digest.hex(), salt


def verify_password(password: str, pwd_hash: str, salt: str) -> bool:
    if not pwd_hash or not salt:
        return False
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, pwd_hash)


def _validate(username: str, password: str, role: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线、点或连字符")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"口令长度不能少于 {MIN_PASSWORD_LEN} 位")
    if role not in ROLES:
        raise ValueError(f"非法角色：{role}（可选 {list(ROLES)}）")


# ------------------------------------------------------------------ #
# 用户管理
# ------------------------------------------------------------------ #
def public_user(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """对外返回的用户信息：剔除口令哈希与盐。"""
    if not user:
        return None
    return {
        "id": user.get("id"),
        "username": user.get("username", ""),
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "viewer"),
        "role_name": ROLES.get(user.get("role", ""), user.get("role", "")),
        "seller_id": user.get("seller_id", ""),
        "status": user.get("status", "active"),
        "created_at": user.get("created_at", ""),
        "last_login_at": user.get("last_login_at", ""),
        "permissions": sorted(perms_of(user.get("role", "viewer"))),
    }


def create_user(username: str, password: str, role: str = "seller",
                display_name: str = "", seller_id: str = "") -> int:
    username = (username or "").strip()
    _validate(username, password, role)
    if db.get_user_by_username(username):
        raise ValueError(f"用户名 {username} 已存在")
    pwd_hash, salt = hash_password(password)
    return db.insert_user({
        "username": username,
        "display_name": (display_name or "").strip()[:50] or username,
        "role": role,
        "pwd_hash": pwd_hash,
        "pwd_salt": salt,
        "seller_id": (seller_id or "").strip()[:50],
        "status": "active",
    })


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验账号口令；成功返回公开用户信息并刷新最近登录时间。"""
    user = db.get_user_by_username((username or "").strip())
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password or "", user.get("pwd_hash", ""), user.get("pwd_salt", "")):
        return None
    db.touch_login(int(user["id"]))
    user["last_login_at"] = db.now()
    return public_user(user)


def set_role(user_id: int, role: str) -> Dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"非法角色：{role}")
    user = db.get_user(user_id)
    if not user:
        raise ValueError("用户不存在")
    # 不允许把最后一个管理员降级，否则系统会失去自我管理能力
    if user.get("role") == "admin" and role != "admin" and db.count_users("admin") <= 1:
        raise ValueError("至少保留一个管理员，不能降级最后一个管理员")
    db.update_user_fields(user_id, role=role)
    return public_user(db.get_user(user_id))


def set_status(user_id: int, status: str) -> Dict[str, Any]:
    if status not in ("active", "disabled"):
        raise ValueError("状态只能是 active / disabled")
    user = db.get_user(user_id)
    if not user:
        raise ValueError("用户不存在")
    if user.get("role") == "admin" and status != "active" and db.count_users("admin") <= 1:
        raise ValueError("至少保留一个启用状态的管理员")
    db.update_user_fields(user_id, status=status)
    return public_user(db.get_user(user_id))


def reset_password(user_id: int, new_password: str) -> None:
    """管理员重置口令（不需要旧口令）。"""
    if len(new_password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"口令长度不能少于 {MIN_PASSWORD_LEN} 位")
    if not db.get_user(user_id):
        raise ValueError("用户不存在")
    pwd_hash, salt = hash_password(new_password)
    db.update_user_fields(user_id, pwd_hash=pwd_hash, pwd_salt=salt)


def change_password(username: str, old_password: str, new_password: str) -> None:
    """本人修改口令：必须校验旧口令。"""
    user = db.get_user_by_username((username or "").strip())
    if not user or not verify_password(old_password or "", user.get("pwd_hash", ""),
                                       user.get("pwd_salt", "")):
        raise ValueError("原口令不正确")
    reset_password(int(user["id"]), new_password)


def delete_user(user_id: int) -> None:
    user = db.get_user(user_id)
    if not user:
        raise ValueError("用户不存在")
    if user.get("role") == "admin" and db.count_users("admin") <= 1:
        raise ValueError("不能删除最后一个管理员")
    db.delete_user(user_id)


def list_users(role: str = "", keyword: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    return [public_user(u) for u in db.list_users(role=role, keyword=keyword, limit=limit)]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return public_user(db.get_user(user_id))


def ensure_default_admin() -> bool:
    """首次启动自动创建内置管理员；已存在管理员时跳过。返回是否新建。"""
    if db.count_users("admin") > 0:
        return False
    try:
        create_user(config.DEFAULT_ADMIN_USER, config.DEFAULT_ADMIN_PASSWORD,
                    role="admin", display_name="系统管理员")
    except ValueError as exc:                        # 用户名重复等，交由调用方决定是否告警
        logger.warning("内置管理员创建失败：%s", exc)
        return False
    logger.warning("已创建内置管理员 %s（默认口令来自 .env 的 DEFAULT_ADMIN_PASSWORD，"
                   "部署前请修改）", config.DEFAULT_ADMIN_USER)
    return True


# ------------------------------------------------------------------ #
# 权限判定
# ------------------------------------------------------------------ #
def perms_of(role: str) -> Iterable[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def can(role: str, perm: str) -> bool:
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def can_any(role: str, perms: Iterable[str]) -> bool:
    return any(can(role, p) for p in perms)


def at_least(role: str, target: str) -> bool:
    """角色高低比较：admin >= auditor >= seller >= viewer。"""
    return ROLE_ORDER.index(role) >= ROLE_ORDER.index(target) if role in ROLE_ORDER else False


def role_matrix() -> Dict[str, Any]:
    """角色 / 权限矩阵，供接口与前端展示。"""
    return {
        "roles": [{"key": k, "name": v} for k, v in ROLES.items()],
        "permissions": [{"key": k, "name": v} for k, v in PERMISSIONS.items()],
        "matrix": {r: sorted(p) for r, p in ROLE_PERMISSIONS.items()},
        "enforce": config.RBAC_ENFORCE,
    }


# ------------------------------------------------------------------ #
# 登录令牌（供无 cookie 的脚本 / curl 调用）
# ------------------------------------------------------------------ #
def _sign(payload: str) -> str:
    return hmac.new(str(config.SECRET_KEY).encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def issue_token(user: Dict[str, Any], ttl: Optional[int] = None) -> str:
    """签发 `用户ID.过期时间戳.签名` 形式的令牌。"""
    exp = int(time.time()) + int(ttl if ttl is not None else config.AUTH_TOKEN_TTL)
    payload = f"{int(user['id'])}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """校验令牌：签名有效 + 未过期 + 账号仍启用；否则返回 None。"""
    if not token or token.count(".") != 2:
        return None
    uid, exp, sign = token.split(".")
    if not hmac.compare_digest(sign, _sign(f"{uid}.{exp}")):
        return None
    try:
        if int(exp) < int(time.time()):
            return None
        user = db.get_user(int(uid))
    except (TypeError, ValueError):
        return None
    if not user or user.get("status") != "active":
        return None
    return public_user(user)
