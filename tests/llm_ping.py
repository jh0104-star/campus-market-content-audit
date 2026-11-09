# -*- coding: utf-8 -*-
"""大模型连通性自检：配置是否生效 + 能否真实调用 + 返回结果能否被解析。

运行： python tests/llm_ping.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import llm


def check_config() -> bool:
    print("---- 1. 配置检查 ----")
    print(f"LLM_PROVIDER : {config.LLM_PROVIDER}")
    print(f"LLM_MODEL    : {config.LLM_MODEL}")
    print(f"LLM_BASE_URL : {config.LLM_BASE_URL}")
    key_desc = ("已配置 (长度 %d)" % len(config.LLM_API_KEY)) if config.LLM_API_KEY else "未配置"
    print(f"LLM_API_KEY  : {key_desc}")
    print(f"JSON 严格模式 : {config.LLM_JSON_MODE}")

    if config.LLM_PROVIDER == "mock":
        print("× provider 仍是 mock，请设为 deepseek")
        return False
    if not config.LLM_API_KEY:
        print("× 未读取到 API Key，请检查 .env 是否在【项目根目录】、变量名是否为 LLM_API_KEY")
        return False
    if not config.LLM_BASE_URL:
        print("× 未解析到接口地址，请在 .env 中显式设置 LLM_BASE_URL=https://api.deepseek.com")
        return False
    print("√ 配置读取正常\n")
    return True


def check_call() -> bool:
    print("---- 2. 真实调用 DeepSeek ----")
    reqs = __import__("requests")
    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    print(f"POST {url}")
    try:
        resp = reqs.post(
            url,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": config.LLM_MODEL, "temperature": 0,
                  "messages": [{"role": "user", "content": "回复两个字：正常"}],
                  "max_tokens": 10},
            timeout=config.LLM_TIMEOUT,
        )
    except Exception as exc:
        print(f"× 请求异常（网络不通 / 地址写错 / 代理问题）：{type(exc).__name__}: {exc}")
        return False

    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"× 鉴权或参数失败，返回：{resp.text[:300]}")
        hint = {
            401: "密钥无效或已失效，检查 LLM_API_KEY 是否复制完整（sk- 开头）",
            402: "账号余额不足，DeepSeek 需先充值",
            429: "触发限流，稍后重试",
            404: "接口地址错误，检查 LLM_BASE_URL（应为 https://api.deepseek.com，不带 /v1）",
        }.get(resp.status_code)
        if hint:
            print(f"  排错建议：{hint}")
        return False
    content = resp.json()["choices"][0]["message"]["content"]
    print(f"√ 调用成功，返回：{content!r}\n")
    return True


def check_audit() -> bool:
    print("---- 3. 完整审核链路 ----")
    llm.clear_cache()                      # 避免读到之前失败后被缓存的降级结果
    from core.pipeline import audit
    item = {"title": "iPad 9代 64G WiFi版",
            "description": "2022年京东购入，有电子发票，屏幕无划痕，电池健康92%。",
            "price": 1600, "condition": "九成新", "has_image": True}
    res = audit(item)
    engine = res.llm.engine if res.llm else "-"
    fallback = res.llm.fallback if res.llm else None
    print(f"引擎      : {engine}")
    print(f"是否降级  : {fallback}")
    print(f"最终判定  : {res.decision_label}（风险 {res.risk_score} 分，分类 {res.category_name}）")
    print(f"LLM 分    : {res.llm.risk_score if res.llm else '-'}")
    if fallback:
        print("× 已降级为本地分析器，说明调用失败，详见上面 reason：")
        print(f"  {res.llm.reason if res.llm else ''}")
        return False
    print("√ 真实大模型已参与审核\n")
    return True


def main() -> int:
    print(f"接入引擎：{llm.engine_name()}\n")
    steps = [check_config()]
    if steps[0]:
        steps.append(check_call())
        steps.append(check_audit())
    ok = all(steps)
    print("结果：" + ("全部通过，DeepSeek 已成功接入" if ok else "存在失败项，请按提示排错"))
    print("注意：修改 .env 后必须重启 Flask / Streamlit 进程才会重新加载。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
