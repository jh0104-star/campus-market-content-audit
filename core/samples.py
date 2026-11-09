# -*- coding: utf-8 -*-
"""演示用快捷示例。

Flask Web 端与 Streamlit 大屏共用同一份数据源，避免两处重复维护、
示例口径不一致的问题。覆盖正常 / 违禁 / 价格异常 / 虚假描述四类典型场景。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

QUICK_SAMPLES: List[Dict[str, Any]] = [
    {"short": "✅ 正常-教材", "title": "高等数学同济第七版上册",
     "description": "大一上学期教材，只写过几页笔记，无缺页无涂画，八成新，毕业离校出。", "price": 15},
    {"short": "✅ 正常-数码", "title": "iPad 9代 64G WiFi版",
     "description": "2022年京东购入，有电子发票，屏幕无划痕，电池健康92%，送保护壳和钢化膜。", "price": 1600},
    {"short": "🚫 违禁-管制刀具", "title": "出一把防身用的折叠刀，开过刃",
     "description": "不多说了，懂的来，加微信 xxxx，先款后货。", "price": 88},
    {"short": "🚫 违禁-代考代写", "title": "四六级代考包过",
     "description": "专业团队，稳过不用复习，不过全额退款，加vx详谈。", "price": 800},
    {"short": "🚫 违禁-烟草制品", "title": "闲置电子烟套装 含5个烟弹",
     "description": "烟弹口味任选，抽了上头，校内当面交易，非诚勿扰。", "price": 100},
    {"short": "🚫 违禁-账号交易", "title": "出王者荣耀满级号",
     "description": "v10满级号，实名过的号，绑卡号，便宜出，先款。", "price": 500},
    {"short": "💰 异常-极低价", "title": "iPhone 15 Pro Max 256G 全新未拆封",
     "description": "全新未拆，官方正品，急出只要99元，先款后货。", "price": 99},
    {"short": "💰 异常-超高价", "title": "MacBook Pro M3 16寸",
     "description": "顶配，成色如图，诚意出。", "price": 99999},
    {"short": "⚠️ 虚假-诈骗话术", "title": "九成新机械键盘",
     "description": "先款后货，不走平台，不退不换，不要问太多，急出。", "price": 200},
    {"short": "⚠️ 虚假-灌水", "title": "转让闲置物品",
     "description": "asdfghjkl，随便填的，想要的私聊。", "price": 50},
    {"short": "⚠️ 虚假-图文不符", "title": "iPhone14 128G",
     "description": "高数教材全新，考研资料专业课笔记，随便出，懂的都懂。", "price": 3000},

    # --- 反规避：刻意改写的违规信息 ---
    {"short": "🛡️ 对抗-干扰符", "title": "出一把防身用的折-叠-刀，已开刃",
     "description": "不多说了，懂的来，加微微详谈，先款后货。", "price": 88},
    {"short": "🛡️ 对抗-繁体字", "title": "闲置摺疊刀一把",
     "description": "已開刃，懂的私，不退不換。", "price": 88},
    {"short": "🛡️ 对抗-拼音替换", "title": "论文 daixie，价格美丽",
     "description": "专业团队，稳过不用修改，加vx详谈。", "price": 800},
    {"short": "🛡️ 对抗-谐音字", "title": "闲置烟蛋 5 个装",
     "description": "口味任选，抽了上头，校内当面交易。", "price": 100},
    {"short": "🛡️ 难负例-易误杀", "title": "《这本书能让你戒烟》正版书闲转",
     "description": "三月购入，看完就戒了，书脊轻微折痕，内容完整无缺页。", "price": 18},
]

# Streamlit selectbox 用：short -> (标题, 描述, 价格)
SAMPLES: Dict[str, Tuple[str, str, float]] = {
    s["short"]: (s["title"], s["description"], float(s["price"]))
    for s in QUICK_SAMPLES
}
