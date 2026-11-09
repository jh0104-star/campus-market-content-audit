# -*- coding: utf-8 -*-
"""大模型审核与分类模块。

两种引擎，接口完全一致，可随时切换（无需重启进程，见 switch_engine）：
* MockLLMAnalyzer  : 本地语义分析器，离线可跑，模拟大模型的泛化与推理过程
* OpenAICompatLLM  : DeepSeek（OpenAI 兼容协议）

真实调用失败时自动降级为本地分析器，保证系统可用性（答辩演示不翻车）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

import config
from .lexicon import CATEGORIES, CATEGORY_MAP
from .rules import (content_text, keyword_classify, normalize, raw_lower,
                    resolve_price_reference)
from .logutils import get_logger, log_swallow

_logger = get_logger("llm")

# ------------------------------------------------------------------ #
# 语义风险模式：模拟大模型"看得懂话外之音"的能力
# 这些表述规则词表命中不了，靠语义泛化识别（规则引擎的盲区）
# ------------------------------------------------------------------ #
SEMANTIC_RISK = [
    {"name": "管制刀具(语义)", "type": "weapon", "score": 70,
     "patterns": ["防身", "自卫", "削铁如泥", "开过刃", "开刃了", "能捅", "随身带着防",
                  "打架用", "砍人", "杀伤力"],
     "advice": "描述指向防身/攻击用途，属于管制器具范畴，禁止发布。"},
    {"name": "违规学术服务(语义)", "type": "cheating", "score": 75,
     "patterns": ["稳过", "包你过", "不用复习", "代上", "替我上", "保分数", "包发表",
                  "代做", "代肝", "代跑", "包毕业", "不用自己去"],
     "advice": "涉及代考代写等学术不端服务，平台禁止并提供举报渠道。"},
    {"name": "烟草酒精(语义)", "type": "tobacco", "score": 65,
     "patterns": ["抽了上头", "提神烟", "口感醇厚", "度数高", "喝了就睡", "微醺",
                  "小酒", "助眠酒", "劲大", "上头的"],
     "advice": "描述暗示烟草/酒精饮品，校园平台不予展示。"},
    {"name": "账号交易(语义)", "type": "account", "score": 70,
     "patterns": ["满级号", "初始号", "白号", "实名过的号", "绑卡号", "闲置号出",
                  "接码", "号出", "出号", "小号出售"],
     "advice": "虚拟账号与实名凭证交易涉嫌违法，禁止发布。"},
    {"name": "偷拍隐私(语义)", "type": "spy", "score": 75,
     "patterns": ["拍隔壁", "看不见的摄像头", "远程监听", "查定位", "跟踪人",
                  "偷录", "悄悄录", "装在宿舍"],
     "advice": "涉及侵犯他人隐私的器材或用途，严禁交易。"},
    {"name": "假冒侵权(语义)", "type": "piracy", "score": 40,
     "patterns": ["和正品一样", "看不出来", "专柜同款", "原厂尾货", "内部渠道",
                  "无盒无票", "工厂直供原单"],
     "advice": "疑似假冒或渠道不明商品，需提供购买凭证。"},
    {"name": "诈骗倾向(语义)", "type": "fraud", "score": 55,
     "patterns": ["懂的都懂", "不议价急出", "先到先得先付", "要的直接拍", "非诚勿扰先款",
                  "低价出给有缘人", "手慢无先打款", "没时间解释"],
     "advice": "话术具有催促私下付款特征，存在诈骗风险，建议走平台担保交易。"},
    {"name": "引流灰产(语义)", "type": "drain", "score": 60,
     "patterns": ["日入过千", "兼职月入", "带你赚", "躺赚", "拉人进群", "扫码进群",
                  "名额有限速来", "招聘兼职", "非二手物品"],
     "advice": "疑似站外引流或兼职灰产广告，非二手交易信息。"},
]

# 语义正面信号（用于降低风险分）
SEMANTIC_POSITIVE = [
    ("描述详实", ["购入时间", "购买时间", "发票", "保修", "成色", "实拍", "实物图",
                  "当面交易", "自提", "可小刀", "可议价"]),
    ("转手原因合理", ["毕业", "换校区", "升学", "闲置", "用不上", "换了新", "搬迁", "出国"]),
]

RISK_LEVEL_MAP = [(70, "high"), (35, "medium"), (0, "low")]


def score_to_level(score: int) -> str:
    for threshold, level in RISK_LEVEL_MAP:
        if score >= threshold:
            return level
    return "low"


@dataclass
class LLMResult:
    """大模型输出的结构化审核结论（两种引擎输出结构完全一致）。"""
    category: str = "other"
    sub_category: str = ""
    confidence: float = 0.0
    risk_level: str = "low"
    risk_score: int = 0
    violations: List[str] = field(default_factory=list)
    violation_types: List[str] = field(default_factory=list)
    is_fake: bool = False
    price_verdict: str = "unknown"     # normal / suspicious_low / suspicious_high / unknown
    reason: str = ""
    suggestion: str = ""
    keywords: List[str] = field(default_factory=list)
    reasoning: str = ""                # 推理过程（演示用）
    engine: str = "mock"
    model: str = "local-semantic-v1"
    latency_ms: int = 0
    fallback: bool = False
    raw: Optional[dict] = None
    # 结构化信号（信号名 -> 分值）：与规则引擎同源的信号在融合阶段会被去重，
    # 避免同一个事实（如"描述过短"）被规则层与语义层各计一次分。
    signals: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        # 浅拷贝即可（raw 不参与序列化），避免 asdict 的深拷贝开销
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d.pop("raw", None)
        return d


# ------------------------------------------------------------------ #
# 本地语义分析器（离线模式）
# ------------------------------------------------------------------ #
class MockLLMAnalyzer:
    """离线语义分析器。

    不是简单关键词匹配：叠加了
      1) 语义风险模式（理解话外之音）
      2) 标题-描述语义一致性校验
      3) 成色/价格/用途的交叉推理
    输出与真实大模型同构的 JSON，便于无缝替换。
    """

    engine = "mock"

    def analyze(self, item: dict) -> LLMResult:
        t0 = time.time()
        title = str(item.get("title") or "")
        desc = str(item.get("description") or "")
        price = item.get("price")
        text = content_text(title, desc)
        norm = normalize(text)
        raw = raw_lower(text)

        # -------- 1. 分类 --------
        cat_key, sub_name, conf, scores = keyword_classify(text)
        cat = CATEGORY_MAP.get(cat_key, CATEGORY_MAP["other"])
        if not sub_name:
            sub_name = list(cat["sub"].keys())[0] if cat["sub"] else ""
        kws = sorted([w for w in cat["keywords"] if w in norm],
                     key=lambda w: -cat["keywords"][w])[:6]

        # -------- 2. 语义风险识别 --------
        violations: List[str] = []
        violation_types: List[str] = []
        reasons: List[str] = []
        risk = 0
        for rule in SEMANTIC_RISK:
            hit = [p for p in rule["patterns"] if p in raw]
            if hit:
                # 同一风险类型命中多条表述，说明信号更强，适度累加
                risk += rule["score"] + (len(hit) - 1) * 12
                violations.append(f"{rule['name']}：命中“{'、'.join(hit[:3])}”")
                violation_types.append(rule["type"])
                reasons.append(rule["advice"])

        # -------- 3. 标题/描述语义一致性 --------
        if len(desc) >= 6:
            c1, _, _, sc1 = keyword_classify(title)
            c2, _, _, sc2 = keyword_classify(desc)
            s1, s2 = sc1.get(c1, 0.0), sc2.get(c2, 0.0)
            if c1 != c2 and s1 >= 4 and s2 >= 4 and c2 != "other":
                risk += 45
                violations.append(
                    f"图文语义不一致：标题偏向「{CATEGORY_MAP[c1]['name']}」，描述偏向「{CATEGORY_MAP[c2]['name']}」")
                reasons.append("标题与正文描述的品类不一致，疑似模板套用或虚假信息。")

        # -------- 4. 价格语义判断 --------
        price_verdict = "unknown"
        signals: Dict[str, int] = {}
        try:
            p = float(price)
        except (TypeError, ValueError):
            p = None
        # 与规则引擎共用同一套参考价（具体商品优先、其次类目），避免两路口径不一致
        # 把"高配旗舰的正常高价"误判成异常（如 MacBook Pro 16寸 M3 Max 卖 9000）
        ref, ref_name = resolve_price_reference(norm, cat_key)
        if p is not None and ref is not None:
            ref_new = ref["new"]
            ratio = p / ref_new
            if ratio < 0.05 and p < ref["min"] * 0.5:
                price_verdict = "suspicious_low"
                risk += 28
                signals["price"] = 28
                violations.append(
                    f"价格异常偏低：售价仅为「{ref_name}」参考价({ref_new}元)的 {ratio*100:.1f}%")
                reasons.append("价格显著偏离市场行情，常见于低价引流或虚假信息。")
            elif p > ref["max"] * 1.3:
                price_verdict = "suspicious_high"
                risk += 18
                signals["price"] = 18
                violations.append(
                    f"价格异常偏高：售价 {p:g} 元高于「{ref_name}」二手合理上限 {ref['max']} 元")
                reasons.append("二手商品价格高于该类目二手合理区间，定价不合理。")
            else:
                price_verdict = "normal"

        # -------- 5. 虚假描述语义 --------
        is_fake = False
        if len(desc.strip()) < 10:
            is_fake = True
            risk += 20
            signals["short_desc"] = 20
            violations.append("描述信息量不足：正文少于 10 字，无法判断商品真实性")
        if re.search(r"(.)\1{6,}", raw):
            is_fake = True
            risk += 12
            signals["repeat"] = 12
            violations.append("描述存在刷屏式重复字符")
        if re.search(r"(全新|未拆封|99新)", raw) and p is not None and p < 20 and cat_key in ("electronics", "sports"):
            is_fake = True
            risk += 25
            violations.append("成色与价格严重矛盾：宣称近乎全新却标价极低")
            reasons.append("高价品类 + 全新声明 + 极低价，是典型虚假信息特征。")

        # -------- 6. 正面信号降权 --------
        positives = []
        for name, words in SEMANTIC_POSITIVE:
            hit = [w for w in words if w in raw]
            if hit:
                positives.append(f"{name}（{'、'.join(hit[:3])}）")
                risk -= 6

        risk = max(0, min(100, risk))
        level = score_to_level(risk)

        # -------- 7. 生成自然语言结论 --------
        if violations:
            reason = "经语义分析，该信息存在以下问题：" + "；".join(violations[:3]) + "。"
        else:
            reason = (f"该信息语义完整，内容指向「{cat['name']}"
                      f"{('/' + sub_name) if sub_name else ''}」品类，"
                      f"未发现违禁、虚假或价格异常特征。")
        if positives:
            reason += " 同时识别到正向信号：" + "、".join(positives[:2]) + "，可信度较高。"

        if reasons:
            suggestion = " ".join(reasons[:2])
        elif level == "medium":
            suggestion = "建议补充实物照片与购买凭证后再发布。"
        else:
            suggestion = "内容合规，可直接发布；建议走平台担保交易。"

        reasoning = self._build_reasoning(title, desc, cat, sub_name, conf, violations, positives)

        return LLMResult(
            category=cat_key, sub_category=sub_name, confidence=round(conf, 3),
            risk_level=level, risk_score=risk, violations=violations,
            violation_types=sorted(set(violation_types)), is_fake=is_fake,
            price_verdict=price_verdict, reason=reason, suggestion=suggestion,
            keywords=kws, reasoning=reasoning, engine="mock",
            model="local-semantic-v1", latency_ms=int((time.time() - t0) * 1000),
            signals=signals,
        )

    @staticmethod
    def _build_reasoning(title, desc, cat, sub, conf, violations, positives) -> str:
        lines = [
            f"[输入解析] 标题「{title}」；正文 {len(desc)} 字。",
            f"[品类推断] 语义与关键词加权匹配到「{cat['name']}"
            f"{('/' + sub) if sub else ''}」，置信度 {conf:.0%}。",
        ]
        if violations:
            lines.append("[风险识别] " + "；".join(violations))
        else:
            lines.append("[风险识别] 未命中违禁语义、价格异常与虚假描述模式。")
        if positives:
            lines.append("[可信度增强] 识别到 " + "、".join(positives))
        lines.append(f"[结论] 风险等级 {score_to_level(0)} 之外综合判定完成。")
        return "\n".join(lines).replace("[结论] 风险等级 low 之外综合判定完成。", "[结论] 综合判定完成。")


# ------------------------------------------------------------------ #
# 真实大模型（OpenAI 兼容协议）
# ------------------------------------------------------------------ #
SYSTEM_PROMPT = """你是高校二手交易平台的内容审核与分类专家。
任务：对一条学生发布的二手交易信息进行审核，输出严格 JSON，不要输出任何解释文字以外的内容。

可选品类（key: 名称）：
textbook: 教材教辅；electronics: 电子产品；daily: 生活用品；clothing: 服饰鞋包；
sports: 运动户外；beauty: 美妆个护；ticket: 票务卡券；other: 其他杂项

审核维度：
1. 违禁品：管制器具、烟草酒精、药品、活体动物、易燃易爆、代考代写、账号/银行卡交易、偷拍窃听设备、假冒侵权、赌博灰产。
2. 虚假描述：描述与标题不符、极简灌水、夸大宣传、站外引流、先款后货等诈骗话术、成色与价格矛盾。
3. 价格异常：与同类商品市场价严重偏离（过低引流 / 过高虚标）。

输出 JSON（字段必须齐全）：
{
  "category": "品类key",
  "sub_category": "子类名称，可为空字符串",
  "confidence": 0.0-1.0,
  "risk_score": 0-100 的整数,
  "violations": ["问题1", "问题2"],
  "is_fake": true/false,
  "price_verdict": "normal" | "suspicious_low" | "suspicious_high" | "unknown",
  "reason": "一段中文审核理由",
  "suggestion": "给学生的整改建议",
  "keywords": ["关键词1", "关键词2"]
}

请以上述 json 对象的形式直接返回，便于程序解析。
"""

FEW_SHOT = [
    {
        "role": "user",
        "content": "标题：出一把防身用的折叠刀，开过刃\n正文：不多说了，懂的来，加微信 xxxx，先款后货\n价格：88",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "category": "other", "sub_category": "", "confidence": 0.95, "risk_score": 95,
            "violations": ["管制器具：折叠刀且已开刃", "描述含防身用途，属管制刀具范畴",
                           "先款后货+站外引流，具备诈骗特征"],
            "is_fake": True, "price_verdict": "unknown",
            "reason": "商品为已开刃折叠刀并宣称防身用途，属于校园平台严禁交易的管制器具；同时存在站外引流与先款后货话术，风险极高。",
            "suggestion": "立即下架并拒绝发布，情节严重者上报学生处。",
            "keywords": ["折叠刀", "防身", "开刃"]
        }, ensure_ascii=False),
    },
]


class OpenAICompatLLM:
    """DeepSeek 官方 OpenAI 兼容接口。"""

    engine = "api"

    def __init__(self):
        import requests  # 局部导入，离线模式不强依赖
        self._requests = requests
        self.model = config.LLM_MODEL

    def analyze(self, item: dict) -> LLMResult:
        t0 = time.time()
        user_prompt = (
            f"标题：{item.get('title') or ''}\n"
            f"正文：{item.get('description') or ''}\n"
            f"价格：{item.get('price')}\n"
            f"成色：{item.get('condition') or '未填写'}\n"
            f"是否上传实物图：{'是' if item.get('has_image') else '否'}"
        )
        payload = {
            "model": self.model,
            "temperature": config.LLM_TEMPERATURE,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *FEW_SHOT,
                {"role": "user", "content": user_prompt},
            ],
        }
        # 可选：要求服务端返回严格 JSON（DeepSeek / OpenAI 支持，部分网关不支持）
        if config.LLM_JSON_MODE:
            payload["response_format"] = {"type": "json_object"}
        resp = self._requests.post(
            f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload, timeout=config.LLM_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = self._parse_json(content)
        return self._to_result(data, int((time.time() - t0) * 1000), self.model)

    # ---------------- 工具 ----------------
    @staticmethod
    def _parse_json(content: str) -> dict:
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ValueError("大模型返回内容中未找到 JSON")
        return json.loads(m.group(0))

    @staticmethod
    def _to_result(data: dict, latency: int, model: str) -> LLMResult:
        cat = str(data.get("category", "other")).strip()
        if cat not in CATEGORY_MAP:
            cat = "other"
        risk = int(data.get("risk_score", 0) or 0)
        risk = max(0, min(100, risk))
        violations = [str(v) for v in data.get("violations", [])][:8]
        return LLMResult(
            category=cat, sub_category=str(data.get("sub_category", "") or ""),
            confidence=float(data.get("confidence", 0.5) or 0),
            risk_level=score_to_level(risk), risk_score=risk,
            violations=violations, is_fake=bool(data.get("is_fake", False)),
            price_verdict=str(data.get("price_verdict", "unknown")),
            reason=str(data.get("reason", "")), suggestion=str(data.get("suggestion", "")),
            keywords=[str(k) for k in data.get("keywords", [])][:8],
            reasoning="由远程大模型生成，详见 reason 字段。",
            engine="api", model=model, latency_ms=latency, raw=data,
        )


# ------------------------------------------------------------------ #
# 统一入口（带缓存与降级）
# ------------------------------------------------------------------ #
# 有界 LRU：避免长跑时缓存无限膨胀占用内存
_CACHE_MAX = 512
_CACHE: "OrderedDict[str, LLMResult]" = OrderedDict()
_analyzer = None
# 降级次数等运行期计数：便于 /api/metrics 暴露"远程引擎到底有没有在真跑"
_stats = {"fallback": 0, "remote": 0}


def _cache_put(key: str, result: LLMResult) -> None:
    _CACHE[key] = result
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _cache_get(key: str) -> Optional[LLMResult]:
    result = _CACHE.get(key)
    if result is not None:
        _CACHE.move_to_end(key)
    return result


# ------------------------------------------------------------------ #
# 运行时引擎切换（Web / 大屏均可一键切换，无需改 .env 也不用重启）
# ------------------------------------------------------------------ #
# 状态落在 data/runtime.json：Flask 与 Streamlit 是两个独立进程，
# 用文件做介质可以同时生效，且服务重启后保持上一次的选择。
RUNTIME_FILE = config.DATA_DIR / "runtime.json"

# 各 provider 的默认模型（用户若显式配过模型则不覆盖）
PROVIDER_DEFAULT_MODEL = {"deepseek": "deepseek-chat"}
PROVIDER_LABEL = {
    "mock": "本地分析器（离线）",
    "deepseek": "DeepSeek",
}

_ENGINE_LOCK = threading.RLock()
_analyzer_provider: Optional[str] = None
# 带 TTL 的文件状态缓存：避免每次审核都去读磁盘
_runtime_state: Dict[str, Any] = {"ts": 0.0, "mtime": -1.0, "provider": None}
_RUNTIME_TTL = 0.5


def _read_runtime() -> Dict[str, Any]:
    try:
        if RUNTIME_FILE.exists():
            return json.loads(RUNTIME_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        pass
    return {}


def current_provider() -> str:
    """当前生效的 provider：运行时切换 > 环境变量/.env > 默认 mock。"""
    now = time.time()
    if now - _runtime_state["ts"] > _RUNTIME_TTL:
        _runtime_state["ts"] = now
        try:
            mtime = RUNTIME_FILE.stat().st_mtime
        except OSError:
            mtime = -1.0
        if mtime != _runtime_state["mtime"]:
            _runtime_state["mtime"] = mtime
            _runtime_state["provider"] = (
                (_read_runtime().get("provider") or "").strip().lower() or None)
    return _runtime_state["provider"] or config.LLM_PROVIDER


def available_providers() -> List[Dict[str, Any]]:
    """可切换的引擎清单（是否可用取决于有没有配置 API Key）。"""
    has_key = bool(config.LLM_API_KEY)
    items = [{"key": "mock", "label": PROVIDER_LABEL["mock"],
              "enabled": True, "needs_key": False}]
    for p, url in config._PROVIDER_PRESET.items():
        items.append({"key": p, "label": PROVIDER_LABEL.get(p, p), "url": url,
                      "enabled": has_key, "needs_key": True})
    return items


def use_provider(provider: str) -> None:
    """仅在当前进程内指定引擎，不写 runtime.json、不影响 Web/大屏的选择。

    用于离线评估与回归测试：保证指标可复现、不消耗远程额度、不受网络波动影响。
    """
    _runtime_state["provider"] = (provider or "").strip().lower() or None
    # 必须同时把 ts/mtime 置为当前值：否则 current_provider() 会判定缓存过期，
    # 重新去读 runtime.json，把这里强制的 provider 又覆盖成界面上的选择
    _runtime_state["ts"] = time.time()
    try:
        _runtime_state["mtime"] = RUNTIME_FILE.stat().st_mtime
    except OSError:
        _runtime_state["mtime"] = -1.0
    global _analyzer, _analyzer_provider
    _analyzer, _analyzer_provider = None, None


def switch_engine(provider: str) -> Dict[str, Any]:
    """切换语义层引擎，立刻对后续审核生效（线程安全）。"""
    global _analyzer, _analyzer_provider
    provider = (provider or "").strip().lower()
    known = {p["key"] for p in available_providers()}
    if provider not in known:
        raise ValueError(f"不支持的引擎「{provider}」，可选：{sorted(known)}")

    with _ENGINE_LOCK:
        # 用户未显式指定模型时，跟随 provider 切换到对应默认模型
        explicit_model = config.LLM_MODEL not in PROVIDER_DEFAULT_MODEL.values()
        config.LLM_PROVIDER = provider
        preset = config._PROVIDER_PRESET.get(provider)
        if preset:
            config.LLM_BASE_URL = preset
        if not explicit_model and provider in PROVIDER_DEFAULT_MODEL:
            config.LLM_MODEL = PROVIDER_DEFAULT_MODEL[provider]
        try:
            RUNTIME_FILE.write_text(json.dumps({"provider": provider},
                                               ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        _runtime_state["provider"] = provider
        _runtime_state["ts"] = 0.0          # 下次读取前强制刷新（同步磁盘状态）
        _analyzer, _analyzer_provider = None, None   # 丢弃旧实例，下次调用重建
    return engine_status()


def engine_status() -> Dict[str, Any]:
    """当前引擎状态，供 API 与前端展示。"""
    provider = current_provider()
    has_key = bool(config.LLM_API_KEY)
    # 没有密钥时远程引擎无法启用，实际仍会回落到本地分析器
    running = "mock" if (provider == "mock" or not has_key) else provider
    return {
        "provider": provider,            # 用户选择的引擎
        "running": running,              # 实际在跑的引擎
        "model": config.LLM_MODEL if running != "mock" else "local-semantic-v1",
        "has_key": has_key,
        "base_url": "" if running == "mock" else config.LLM_BASE_URL,
        "engine": engine_name(),
        "available": available_providers(),
    }


def get_analyzer():
    global _analyzer, _analyzer_provider
    provider = current_provider()
    if _analyzer is None or _analyzer_provider != provider:
        with _ENGINE_LOCK:
            if _analyzer is None or _analyzer_provider != provider:
                _analyzer = _build_analyzer(provider)
                _analyzer_provider = provider
    return _analyzer


def _build_analyzer(provider: str):
    if provider == "mock" or not config.LLM_API_KEY:
        return MockLLMAnalyzer()
    return OpenAICompatLLM()


def analyze(item: dict, use_cache: bool = True, force_local: bool = False) -> LLMResult:
    """调用大模型审核；失败自动降级为本地分析器。

    force_local=True 表示"本次强制走本地分析器"——由流水线的分级调用策略触发：
    规则引擎已把结论钉死时，远程模型无法改变结论，但会付出 ~1.2s 延迟与调用额度。
    """
    # 引擎必须进入缓存键：否则切换后又读回上一个引擎的结论
    provider = current_provider()
    eff = "mock" if force_local else provider
    key = "|".join([eff, *(str(item.get(k, "")) for k in
                           ("title", "description", "price", "condition", "has_image"))])
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    analyzer = MockLLMAnalyzer() if (force_local or provider == "mock") else get_analyzer()
    degraded = False
    if provider != "mock" and not force_local:
        _stats["remote"] += 1
    try:
        result = analyzer.analyze(item)
    except Exception as exc:                       # 网络/鉴权/超时 -> 降级
        degraded = True
        result = MockLLMAnalyzer().analyze(item)
        result.fallback = True
        result.engine = "mock(fallback)"
        result.reason += f"（远程大模型调用失败已降级：{type(exc).__name__}）"
        log_swallow(_logger, f"远程大模型审核（{provider}）", exc)
        _stats["fallback"] += 1

    # 降级结果不入缓存：缓存无 TTL，一次网络抖动会把降级结论固化到进程结束
    if use_cache and not degraded:
        _cache_put(key, result)
    return result


def clear_cache() -> None:
    """清空审核结果缓存（测试/基准用）。"""
    _CACHE.clear()


def fallback_stats() -> Dict[str, int]:
    """远程调用降级次数（>0 说明界面上的结论可能来自本地兜底）。"""
    return dict(_stats)


def engine_name() -> str:
    a = get_analyzer()
    if isinstance(a, OpenAICompatLLM):
        return f"远程大模型 {a.model} @ {config.LLM_BASE_URL}"
    return "本地语义分析器（离线模式）"
