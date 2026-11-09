# -*- coding: utf-8 -*-
"""模拟数据集 + 一键入库 + 效果评估。

运行：
    python data/seed.py            # 写入模拟数据
    python data/seed.py --reset    # 清库后重新写入
    python data/seed.py --eval     # 只跑评估（不落库）
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.holdout import HOLDOUT

random.seed(20240520)

# ------------------------------------------------------------------ #
# 模拟样本（覆盖：正常 / 违禁 / 价格异常 / 虚假描述）
# expect 为人工标注的期望结论，用于系统效果评估
# ------------------------------------------------------------------ #
SAMPLES: List[Dict] = [
    # ---------- 正常合规 ----------
    dict(title="高等数学同济第七版上册", description="大一上学期教材，只写过几页笔记，无缺页无涂画，八成新，毕业离校出。",
         price=15, condition="八成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="iPad 9代 64G WiFi版", description="2022年京东购入，有电子发票，屏幕无划痕，电池健康92%，送保护壳和钢化膜。",
         price=1600, condition="九成新", has_image=True, expect="pass", expect_cat="electronics"),
    dict(title="小米移动电源 20000mAh", description="用过半年，支持快充，可以带上飞机，接口正常无鼓包。",
         price=45, condition="九成新", has_image=True, expect="pass", expect_cat="electronics"),
    dict(title="宿舍护眼台灯 三档调光", description="USB供电，白光暖光可切换，灯臂可折叠，毕业甩卖可小刀。",
         price=25, condition="八成新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="考研英语一真题 2010-2024", description="全新没做过，附带详细答案解析，换了新版资料所以出。",
         price=30, condition="全新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="Yonex羽毛球拍 入门款", description="用了两个学期，拍线完好，手胶新换，送三个训练球，当面交易。",
         price=120, condition="八成新", has_image=True, expect="pass", expect_cat="sports"),
    dict(title="优衣库羽绒服 男款M码", description="去年冬天买的，洗过两次，无破损无跑绒，实物如图，可自提。",
         price=180, condition="九成新", has_image=True, expect="pass", expect_cat="clothing"),
    dict(title="兰蔻菁纯口红 196色号", description="专柜购入有包装盒，用过三次，保质期到2026年，介意者勿拍。",
         price=120, condition="九成新", has_image=True, expect="pass", expect_cat="beauty"),
    dict(title="校园歌手大赛门票两张", description="5月20号的场次，临时有事去不了，原价转让，可当面验票。",
         price=300, condition="全新", has_image=True, expect="pass", expect_cat="ticket"),
    dict(title="捷安特ATX山地自行车", description="骑了两年，变速正常，刹车片刚换，链条上过油，毕业出。",
         price=400, condition="七成新", has_image=True, expect="pass", expect_cat="sports"),
    dict(title="飞利浦电动牙刷 HX3216", description="用了三个月，配件齐全，刷头另送两个，换新机所以出。",
         price=90, condition="九成新", has_image=True, expect="pass", expect_cat="beauty"),
    dict(title="塑料收纳箱 三个一套", description="宿舍用，尺寸40*30*25，干净无异味，打包出。",
         price=20, condition="八成新", has_image=True, expect="pass", expect_cat="daily"),
    dict(title="罗技K380蓝牙键盘", description="九成新，支持三设备切换，电池仓无腐蚀，实物拍摄。",
         price=110, condition="九成新", has_image=True, expect="pass", expect_cat="electronics"),
    dict(title="数据结构 严蔚敏 教材", description="有少量笔记和荧光笔标注，不影响阅读，低价出。",
         price=12, condition="七成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="双肩电脑包 15.6寸", description="容量大，内胆完好，背带无断裂，通勤用了半年。",
         price=60, condition="九成新", has_image=True, expect="pass", expect_cat="clothing"),
    dict(title="四级词汇红宝书", description="背了一半，前面有笔记，后面全新，送一本配套练习册。",
         price=18, condition="八成新", has_image=True, expect="pass", expect_cat="textbook"),
    dict(title="宿舍电风扇 落地扇", description="去年买的，摇头正常，三档风速，毕业离校便宜出。",
         price=55, condition="八成新", has_image=True, expect="pass", expect_cat="daily"),

    # ---------- 违禁品 ----------
    dict(title="出一把防身用的折叠刀，开过刃", description="不多说了，懂的来，加微信 xxxx，先款后货。",
         price=88, condition="全新", has_image=False, expect="reject", expect_cat="other"),
    dict(title="闲置电子烟套装 含5个烟弹", description="烟弹口味任选，抽了上头，校内当面交易，非诚勿扰。",
         price=100, condition="九成新", has_image=True, expect="reject", expect_cat="other"),
    dict(title="四六级代考包过", description="专业团队，稳过不用复习，不过全额退款，加vx详谈。",
         price=800, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="转让布洛芬和感冒药", description="之前囤多了，退烧药感冒药都有，便宜出给需要的同学。",
         price=30, condition="全新", has_image=True, expect="reject", expect_cat="other"),
    dict(title="出王者荣耀满级号", description="v10满级号，实名过的号，绑卡号，便宜出，先款。",
         price=500, condition="", has_image=False, expect="reject", expect_cat="other"),
    # 偷拍设备属违禁品，命中硬红线后强制归入"其他杂项"，故标注为 other
    dict(title="迷你针孔摄像头", description="可以装在宿舍，看不见的摄像头，支持远程监听和偷录。",
         price=150, condition="全新", has_image=True, expect="reject", expect_cat="other"),
    dict(title="出借校园卡饭卡", description="校园卡出租，饭卡出租，可以刷食堂和图书馆，按月租。",
         price=50, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="高仿AJ1 专柜同款", description="a货高仿，看不出来，和正品一样，内部渠道拿货。",
         price=200, condition="全新", has_image=True, expect="reject", expect_cat="other"),
    dict(title="过年烟花鞭炮便宜出", description="库存烟花爆竹，威力大，晚上放很漂亮，自提。",
         price=60, condition="全新", has_image=False, expect="reject", expect_cat="other"),
    dict(title="接码平台 出号 白号", description="初始号白号出售，支持接码注册，量大优惠，懂的来。",
         price=5, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="代写毕业论文 包发表", description="专业老师代写，保分数包发表，不用来学校，全程托管。",
         price=2000, condition="", has_image=False, expect="reject", expect_cat="other"),

    # ---------- 价格异常 ----------
    dict(title="iPhone 15 Pro Max 256G 全新未拆封", description="全新未拆，官方正品，急出只要999元，先款后货。",
         price=99, condition="全新", has_image=False, expect="reject", expect_cat="electronics"),
    dict(title="MacBook Pro M3 16寸", description="顶配，成色如图，诚意出。",
         price=99999, condition="九成新", has_image=True, expect="reject", expect_cat="electronics"),
    dict(title="高等数学教材", description="大一教材，随便出。",
         price=999, condition="八成新", has_image=True, expect="review", expect_cat="textbook"),
    dict(title="二手山地自行车", description="骑了一年，能骑。",
         price=11111, condition="七成新", has_image=True, expect="reject", expect_cat="sports"),
    dict(title="iPad Air 5 全新", description="全新未激活，低价出，只要1元，先到先得。",
         price=1, condition="全新", has_image=False, expect="reject", expect_cat="electronics"),
    dict(title="演唱会门票 内场前排", description="原价出，位置很好。",
         price=8888, condition="全新", has_image=True, expect="review", expect_cat="ticket"),

    # ---------- 虚假描述 / 诈骗 ----------
    dict(title="出", description="私聊。",
         price=100, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="iPhone14 128G", description="高数教材全新，考研资料专业课笔记，随便出，懂的都懂。",
         price=3000, condition="全新", has_image=False, expect="reject", expect_cat="textbook"),
    dict(title="转让闲置物品", description="asdfghjkl，随便填的，想要的私聊。",
         price=50, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="全新AirPods Pro 2代", description="全新未拆99新，只要150元，加微信 xiaoming_520，先款后货不走平台。",
         price=150, condition="全新", has_image=False, expect="reject", expect_cat="electronics"),
    dict(title="九成新机械键盘", description="先款后货，不走平台，不退不换，不要问太多，急出。",
         price=200, condition="九成新", has_image=True, expect="reject", expect_cat="electronics"),
    dict(title="aaaaaaaaaaa 出书", description="aaaaaaaaaaaaaaaaaaa，想要的私，扫码进群。",
         price=20, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="测试数据测试", description="test 测试测试 占位 待定，随便看看。",
         price=111, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="日入过千校园兼职", description="带你赚，躺赚项目，扫码进群，名额有限速来，非二手物品。",
         price=0, condition="", has_image=False, expect="reject", expect_cat="other"),
    dict(title="二手教材 专业课", description="真题课件讲义都有，想要的加我微信看主页，先付款再发货。",
         price=25, condition="八成新", has_image=False, expect="review", expect_cat="textbook"),
    dict(title="MacBook Pro 16寸 M3 Max", description="用了半年，性能没问题，实物拍摄。",
         price=9000, condition="九成新", has_image=False, expect="pass", expect_cat="electronics"),
]

SELLERS = [(f"20{random.randint(19,23)}{random.randint(100000,999999)}", n)
           for n in ["张明远", "李思琪", "王雨桐", "陈子豪", "刘佳怡", "赵天宇",
                     "孙梦洁", "周泽楷", "吴欣妍", "郑浩然", "冯晓晴", "何俊熙"]]


def build_item(sample: Dict, idx: int) -> Dict:
    """把标注样本转成发布请求；发布时间在过去 14 天内随机分布，便于展示趋势图。"""
    from datetime import datetime, timedelta
    sid, sname = SELLERS[idx % len(SELLERS)]
    created = datetime.now() - timedelta(days=random.randint(0, 13),
                                         hours=random.randint(0, 23))
    return {
        "title": sample["title"],
        "description": sample["description"],
        "price": sample["price"],
        "condition": sample.get("condition", ""),
        "has_image": bool(sample.get("has_image")),
        "seller_id": sid,
        "seller_name": sname,
        "contact": f"手机 13{random.randint(100000000, 999999999)}",
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ------------------------------------------------------------------ #
# 评估
# ------------------------------------------------------------------ #
def _run(samples: List[Dict], name: str, verbose: bool = True) -> Dict:
    """在给定样本集上回放流水线并统计指标。"""
    from core.pipeline import audit

    rows = []
    for i, s in enumerate(samples):
        item = build_item(s, i)
        # 评估只针对内容本身，剥离卖家维度（黑名单/频次与库中状态相关，非内容质量）
        item.pop("seller_id", None)
        res = audit(item)
        rows.append({
            "title": s["title"],
            "expect": s["expect"],
            "pred": res.decision,
            "expect_cat": s.get("expect_cat", ""),
            "pred_cat": res.category,
            "score": res.risk_score,
            "rule_score": res.rule.score if res.rule else 0,
            "blocked": bool(res.rule and res.rule.blocked),
        })

    labels = ["pass", "review", "reject"]
    cm = {a: {b: 0 for b in labels} for a in labels}
    for r in rows:
        cm[r["expect"]][r["pred"]] += 1

    total = len(rows)
    strict = sum(cm[l][l] for l in labels) / total
    # 拦截能力：把 review+reject 视为"已拦截风险"
    def blocked(x):
        return x in ("review", "reject")
    tp = sum(1 for r in rows if r["expect"] != "pass" and blocked(r["pred"]))
    fp = sum(1 for r in rows if r["expect"] == "pass" and blocked(r["pred"]))
    fn = sum(1 for r in rows if r["expect"] != "pass" and not blocked(r["pred"]))
    tn = sum(1 for r in rows if r["expect"] == "pass" and not blocked(r["pred"]))
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    cat_acc = sum(1 for r in rows if r["expect_cat"] == r["pred_cat"]) / total
    # 分级调用收益：规则引擎已钉死结论（硬红线 / 风险分达拒绝档）的样本无需再问远程大模型
    from core.pipeline import SKIP_REMOTE_RULE_SCORE
    skippable = sum(1 for r in rows
                    if r["blocked"] or r["rule_score"] >= SKIP_REMOTE_RULE_SCORE)

    if verbose:
        print(f"\n=========== {name}（{total} 条） ===========")
        print(f"{'标题':<28}{'期望':<8}{'预测':<8}{'风险分':<8}{'分类':<12}")
        print("-" * 70)
        for r in rows:
            flag = "√" if r["expect"] == r["pred"] else "×"
            print(f"{flag} {r['title'][:26]:<26}{r['expect']:<8}{r['pred']:<8}"
                  f"{r['score']:<8}{r['pred_cat']:<12}")
        print("-" * 70)
        print("混淆矩阵(行=期望, 列=预测):")
        print(f"{'':<10}" + "".join(f"{l:<10}" for l in labels))
        for a in labels:
            print(f"{a:<10}" + "".join(f"{cm[a][b]:<10}" for b in labels))
        print(f"\n三分类严格准确率 : {strict:.2%}")
        print(f"风险拦截 精确率  : {precision:.2%}")
        print(f"风险拦截 召回率  : {recall:.2%}")
        print(f"风险拦截 F1      : {f1:.2%}")
        print(f"分类准确率       : {cat_acc:.2%}")
        print("=" * 60)

    return {"total": total, "strict": strict, "precision": precision,
            "recall": recall, "f1": f1, "cat_acc": cat_acc, "cm": cm, "rows": rows,
            "skippable": skippable, "skip_rate": skippable / total if total else 0}


def evaluate(verbose: bool = True, provider: str = "mock", md: bool = False) -> Dict:
    """双轨评估（dev + holdout）。

    provider 默认强制为本地分析器：评估必须可复现、零成本、不受网络波动影响；
    需要验证真实大模型效果时显式传 provider="deepseek"。
    md=True 时输出 Markdown 表格，便于直接贴进 README，杜绝手工录入导致的指标漂移。
    """
    from core.llm import use_provider

    use_provider(provider)
    dev = _run(SAMPLES, "开发集 dev（参与规则设计）", verbose and not md)
    hold = _run(HOLDOUT, "留出集 holdout（未参与规则设计）", verbose and not md)

    if md:
        print(f"<!-- 由 `python data/seed.py --eval --md` 生成，引擎：{provider} -->")
        print("| 数据集 | 样本 | 严格准确 | 拦截精确 | 拦截召回 | F1 | 分类准确 |")
        print("|---|---|---|---|---|---|---|")
        for label, m in (("开发集 dev", dev), ("留出集 holdout", hold)):
            print(f"| {label} | {m['total']} | {m['strict']:.1%} | {m['precision']:.1%} | "
                  f"{m['recall']:.1%} | {m['f1']:.1%} | {m['cat_acc']:.1%} |")
        return {"dev": dev, "holdout": hold}

    if verbose:
        print("\n【双轨对比】")
        print(f"{'':<14}{'样本':<8}{'严格准确':<10}{'拦截精确':<10}{'拦截召回':<10}{'F1':<10}{'分类准确':<10}")
        for label, m in (("开发集 dev", dev), ("留出集 holdout", hold)):
            print(f"{label:<14}{m['total']:<8}{m['strict']:.2%}{'':<4}"
                  f"{m['precision']:.2%}{'':<4}{m['recall']:.2%}{'':<4}"
                  f"{m['f1']:.2%}{'':<4}{m['cat_acc']:.2%}")
        print("\n说明：dev 集的文案直接参与规则词表与阈值制定，属「开卷考试」，")
        print("      holdout 为独立留出集，更能反映真实泛化能力与误杀情况。")
        sp = dev["skippable"] + hold["skippable"]
        tot = dev["total"] + hold["total"]
        print(f"\n【分级调用收益】规则已钉死结论 {sp}/{tot} 条 "
              f"({sp / tot:.1%})，这些样本无需调用远程大模型，"
              f"可直接省下对应的延迟与额度。")
    return {"dev": dev, "holdout": hold}


def main():
    args = sys.argv[1:]
    import config
    from core import database as db
    from core.pipeline import submit

    def _opt(flag: str, default: str = "") -> str:
        """取 `--flag value` 形式的参数值。"""
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    db.init_db()
    if "--eval" in args:
        # --provider 缺省为 mock，保证指标可复现；--md 直接产出 README 用的表格
        evaluate(provider=_opt("--provider") or "mock", md="--md" in args)
        return
    if "--reset" in args:
        db.clear_items()
        print("[seed] 已清空历史数据")

    ok = 0
    for i, s in enumerate(SAMPLES):
        submit(build_item(s, i), source="seed")
        ok += 1
    print(f"[seed] 已写入 {ok} 条模拟交易信息 -> {config.DB_PATH}")
    print(f"[seed] 当前总量 {db.count_items()} 条")
    evaluate()


if __name__ == "__main__":
    main()
