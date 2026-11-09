# -*- coding: utf-8 -*-
"""留出集（Hold-out Set）：用于客观评估系统的真实泛化能力。

与 data/seed.py 中的 SAMPLES 的区别：
* SAMPLES 是"开发集"，其文案直接参与了规则词表与阈值的制定，指标天然偏高（开卷考试）；
* 本留出的样本**不参与任何规则设计**，包含三类难点：
    A. 难负例 —— 含敏感词但属于正常商品（考验误杀率）
    B. 对抗样本 —— 用规避手法改写过的违规信息（考验召回率）
    C. 常见异常 —— 价格异常、占位灌水（对照基线）

运行：python data/seed.py --eval   （同时报告 dev / holdout 双轨指标）
"""
from __future__ import annotations

from typing import List, Dict

HOLDOUT: List[Dict] = [
    # ---------------- A. 难负例：含敏感词但应当放行 ----------------
    dict(title="《这本书能让你戒烟》正版书闲转",
         description="今年三月购入，看完就戒了，书脊有轻微折痕，内容完整无缺页。",
         price=18, condition="九成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="酒红色真丝连衣裙 M码",
         description="毕业典礼穿过一次，无勾丝，送防尘袋，可校内当面交易。",
         price=160, condition="九成新", has_image=True, expect="pass", expect_cat="clothing"),
    dict(title="酒精湿巾三大包 宿舍用",
         description="超市囤多了，保质期到明年底，独立包装方便携带，全新未拆。",
         price=20, condition="全新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="医药箱收纳盒 空箱出",
         description="结实的大容量药箱，内部隔板可调，用来装文具或数据线都合适，不含任何药品。",
         price=25, condition="九成新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="得力裁纸刀 + 笔刀 手工模型工具套装",
         description="建筑模型课买的，用途是裁纸板，非管制器具，整套便宜出。",
         price=30, condition="八成新", has_image=True, expect="pass", expect_cat="other"),
    # 宠物用品 / 香薰属生活用品，标注为 daily（原标 other 属标注偏严，会系统性低估分类准确率）
    dict(title="猫粮狗粮 宠物用品一批",
         description="家里宠物换了新粮，剩余两袋原封未拆，保质期充足，同城自提。",
         price=60, condition="全新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="考研数学真题 1998-2024 含详细解析",
         description="正版购买的试卷合集，附带手写错题本，本人上岸已上岸不需要了。",
         price=40, condition="八成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="雅思词汇红宝书 + 听力机经",
         description="词汇书做过一半笔记，机经全新，出给有需要的同学。",
         price=35, condition="八成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="动漫手办 正版带原盒",
         description="正版购入的动漫手办一个，展示柜摆放过，带原盒，非盗版，毕业出。",
         price=220, condition="九成新", has_image=True, expect="pass", expect_cat="other"),
    dict(title="香薰蜡烛 无花果香型 房间除味",
         description="无花果香型，点燃过两次，余量八成，搬家出，同城自提。",
         price=45, condition="九成新", has_image=True, expect="pass", expect_cat="daily"),

    # ---------------- B. 对抗样本：规避手法改写的违规信息 ----------------
    dict(title="出一把防身用的折-叠-刀，已开刃",
         description="不多说了，懂的来，加微微详谈，先款后货。",
         price=88, condition="八成新", has_image=False, expect="reject", expect_cat="other"),
    dict(title="论文 daixie，价格美丽",
         description="专业团队，稳过不用修改，先付一半即可开始写，加vx详谈。",
         price=800, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="闲置煙彈口味任选",
         description="水果味和烟草味都有，校内当面交付，非诚勿扰。",
         price=120, condition="全新", has_image=False, expect="reject", expect_cat="other"),
    dict(title="4六级代*考包*过",
         description="专业团队操作，不过全额退款，详情加卫星号私聊。",
         price=900, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="闲置烟蛋 5 个装",
         description="口味任选，抽了上头，校内当面交易，懂的都懂。",
         price=100, condition="全新", has_image=False, expect="reject", expect_cat="other"),
    dict(title="9.9元出 iPhone 15 Pro Max 全新未拆",
         description="先-款-后-货，不-走-平-台，不退不换，加卫星详谈。",
         price=99, condition="全新", has_image=False, expect="reject", expect_cat="electronics"),
    dict(title="出考试答案，保guo包过",
         description="历年真题库加考前答案，安全稳过，具体方式私信我。",
         price=600, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="二手高仿A货LV neverfull",
         description="原单复刻，做工一模一样，看不出来区别，便宜出，懂的私。",
         price=380, condition="九成新", has_image=False, expect="reject", expect_cat="other"),

    # ---------------- C. 常见异常：价格 / 灌水（对照基线）----------------
    dict(title="iPhone 15 Pro Max 256G 全新未拆封",
         description="京东自营全新未拆，官方正品，急出，价格好说，当面验货。",
         price=99, condition="全新", has_image=False, expect="reject", expect_cat="electronics"),
    dict(title="MacBook Pro M3 16寸顶配",
         description="成色如图，配件齐全，诚意出，价格可谈。",
         price=99999, condition="九成新", has_image=True, expect="reject", expect_cat="electronics"),
    dict(title="aaaa",
         description="test test 随便填的，占位勿拍，qq看主页。",
         price=1, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="转让闲置",
         description="想要的私聊，加微信，价格私谈。",
         price=50, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="二手山地自行车",
         description="骑了两年，变速刹车都正常，链条上过油，毕业出，校内自提。",
         price=350, condition="七成新", has_image=True, expect="pass", expect_cat="sports"),
    dict(title="小米充电宝 10000mAh",
         description="用了一年，支持快充，接口无损坏，外观有轻微磨损，可小刀。",
         price=35, condition="八成新", has_image=True, expect="pass", expect_cat="electronics"),
    dict(title="台灯 三档调光",
         description="USB供电，护眼无频闪，灯臂可折叠，毕业甩卖。",
         price=25, condition="八成新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="校园歌手大赛门票两张",
         description="临时有事去不了，原价转让，可当面验票，支持平台付款。",
         price=300, condition="全新", has_image=True, expect="pass", expect_cat="ticket"),
    dict(title="二手医用一次性口罩 100 只",
         description="家里囤的医用外科口罩，独立包装，保质期到明年，校内自提。",
         price=30, condition="全新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="篮球一个 + 打气筒",
         description="斯伯丁室外球，用了半年，弹性还行，送打气筒，毕业出。",
         price=60, condition="八成新", has_image=True, expect="pass", expect_cat="sports"),
]
