# -*- coding: utf-8 -*-
"""Streamlit 可视化大屏：审核演示 / 数据大屏 / 审核工作台 / 效果评估 / 系统说明。

启动：  streamlit run ui_streamlit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st
import altair as alt

import config
from core import database as db
from core.lexicon import BANNED_RULES, CATEGORIES, CATEGORY_MAP
from core.llm import engine_name, engine_status, switch_engine
from core.pipeline import audit, submit
from core.samples import SAMPLES

st.set_page_config(page_title="校园二手交易智能审核系统",
                   page_icon="🛡️", layout="wide")

CAT_NAME = {c["key"]: c["name"] for c in CATEGORIES}
STATUS_NAME = {"pass": "自动通过", "review": "转人工复核", "reject": "拒绝发布", "pending": "待审核"}

# experimental_rerun 自 Streamlit 1.27 起废弃、新版已移除，这里做兼容
_rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)


# ------------------------------------------------------------------ #
# 数据缓存：Streamlit 每次交互都会重跑整个脚本，没有缓存就等于每次都全表取数
# ------------------------------------------------------------------ #
@st.cache_data(ttl=20, show_spinner=False)
def cached_stats():
    return db.stats()


@st.cache_data(ttl=20, show_spinner=False)
def cached_items(status: str = "", category: str = "", keyword: str = "",
                 seller_id: str = "", limit: int = 500):
    return db.list_items(status=status, category=category, keyword=keyword,
                         seller_id=seller_id, limit=limit)


@st.cache_data(ttl=60, show_spinner=False)
def cached_trend(days: int = 14):
    return db.daily_trend(days)


def invalidate_cache() -> None:
    """写操作后主动失效，避免大屏显示旧数据。"""
    cached_stats.clear()
    cached_items.clear()
    cached_trend.clear()

# ------------------------------------------------------------------ #
# 侧边栏
# ------------------------------------------------------------------ #
def sidebar():
    st.sidebar.title("🛡️ 智能审核控制台")
    st.sidebar.caption("规则引擎 + 大模型双引擎审核")
    st.sidebar.markdown("**审核引擎（可即时切换）**")
    status = engine_status()
    options = [p["key"] for p in status["available"]]
    labels = {p["key"]: (p["label"] + ("" if p["enabled"] else "（未配置密钥）"))
              for p in status["available"]}
    current = status["provider"] if status["provider"] in options else options[0]
    picked = st.sidebar.selectbox("语义层引擎", options,
                                  index=options.index(current),
                                  format_func=lambda k: labels[k])
    if picked != status["provider"]:
        try:
            switch_engine(picked)
            st.sidebar.success(f"已切换为「{labels[picked]}」")
        except ValueError as exc:
            st.sidebar.error(str(exc))
        status = engine_status()
    if status["running"] == "mock":
        st.sidebar.info("本地语义分析器（离线）\n\n毫秒级、零成本，无网络也可完整演示")
    else:
        st.sidebar.success(f"远程大模型 `{status['model']}`\n\n{status['base_url']}")
    st.sidebar.caption(f"实际生效：`{status['running']}`")

    st.sidebar.markdown("---")
    st.sidebar.subheader("决策阈值")
    config.REJECT_SCORE = st.sidebar.slider("拒绝阈值（风险分 ≥）", 50, 100, config.REJECT_SCORE, 1)
    config.REVIEW_SCORE = st.sidebar.slider("人工复核阈值（风险分 ≥）", 0, 90, config.REVIEW_SCORE, 1)
    # 复核阈值高于拒绝阈值时，决策分支会先命中 reject，"转人工复核"永远不可能出现
    if config.REVIEW_SCORE >= config.REJECT_SCORE:
        st.sidebar.error("人工复核阈值需低于拒绝阈值，否则不会出现「转人工复核」结论")
    config.RULE_WEIGHT = st.sidebar.slider("规则引擎权重", 0.0, 1.0, config.RULE_WEIGHT, 0.05)
    config.LLM_WEIGHT = round(1 - config.RULE_WEIGHT, 2)
    st.sidebar.caption(f"大模型权重 = {config.LLM_WEIGHT}")

    st.sidebar.markdown("---")
    st.sidebar.subheader("数据管理")
    c1, c2 = st.sidebar.columns(2)
    if c1.button("生成模拟数据"):
        from data.seed import SAMPLES as SEED_SAMPLES, build_item
        for i, s in enumerate(SEED_SAMPLES):
            submit(build_item(s, i), source="seed")
        invalidate_cache()
        st.sidebar.success(f"已写入 {len(SEED_SAMPLES)} 条")
    if c2.button("清空数据库"):
        db.clear_items()
        invalidate_cache()
        st.sidebar.warning("已清空")

    st.sidebar.markdown("---")
    st.sidebar.caption(f"数据库：{config.DB_PATH.name} ｜ 共 {db.count_items()} 条")


# ------------------------------------------------------------------ #
# Tab 1：智能审核演示
# ------------------------------------------------------------------ #
def tab_demo():
    st.subheader("🔍 单条信息智能审核演示")
    left, right = st.columns([1, 1.15])

    with left:
        with st.form("audit_form", clear_on_submit=False):
            pick = st.selectbox("快捷示例", list(SAMPLES.keys()))
            default = SAMPLES[pick]
            title = st.text_input("标题 *", value=default[0])
            desc = st.text_area("详细描述", value=default[1], height=110)
            c1, c2 = st.columns(2)
            price = c1.number_input("售价（元）", min_value=0.0, value=float(default[2]), step=1.0)
            cond = c2.selectbox("成色", ["", "全新", "九成新", "八成新", "七成新", "有瑕疵"])
            c3, c4 = st.columns(2)
            sid = c3.text_input("卖家学号", value="2021010101")
            sname = c4.text_input("姓名", value="张明远")
            contact = st.text_input("联系方式", value="手机 13800138000")
            has_img = st.checkbox("已上传实物照片", value=True)
            submitted = st.form_submit_button("🤖 开始智能审核")

    with right:
        if not submitted:
            st.info("填写左侧信息后点击「开始智能审核」，系统将展示规则引擎与大模型的双路分析结论。")
            return

        item = {"title": title, "description": desc, "price": price, "condition": cond,
                "seller_id": sid, "seller_name": sname, "contact": contact, "has_image": has_img}
        res = audit(item)

        color = {"pass": "#16a34a", "review": "#f59e0b", "reject": "#ef4444"}[res.decision]
        st.markdown(
            f"""<div style="background:{color};color:#fff;border-radius:14px;padding:16px 20px;margin-bottom:14px">
            <div style="font-size:22px;font-weight:700">{res.decision_label}</div>
            <div style="font-size:13px;opacity:.95">综合风险分 {res.risk_score}/100 ｜
            分类：{res.category_name}{'/' + res.sub_category if res.sub_category else ''} ｜ 耗时 {res.latency_ms}ms</div></div>""",
            unsafe_allow_html=True)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("规则引擎分", res.rule.score)
        m2.metric("大模型分", res.llm.risk_score)
        m3.metric("模型置信度", f"{res.llm.confidence:.0%}")
        m4.metric("命中规则数", len(res.rule.hits))

        st.progress(res.risk_score / 100)

        st.markdown("**大模型审核结论**")
        st.write(res.llm.reason or "—")
        if res.llm.violations:
            for v in res.llm.violations:
                st.markdown(f"- ⚠️ {v}")
        st.success(f"整改建议：{res.suggestions[0] if res.suggestions else '无'}")

        with st.expander("🔬 规则引擎命中明细", expanded=True):
            if res.rule.hits:
                df = pd.DataFrame([{"规则": h.rule, "类型": h.type, "级别": h.severity,
                                    "证据": h.evidence, "建议": h.advice, "分值": h.score}
                                   for h in res.rule.hits])
                st.dataframe(df)
            else:
                st.caption("未命中任何风险规则")
        with st.expander("🧠 大模型推理过程"):
            st.code(res.llm.reasoning, language="text")
        if res.conflicts:
            with st.expander("⚖️ 双引擎分歧"):
                for c in res.conflicts:
                    st.warning(c)

        if st.button("💾 保存该条信息到数据库"):
            rec = submit(item, source="streamlit")
            invalidate_cache()
            st.success(f"已保存，ID={rec['id']}，状态：{rec['audit']['decision_label']}")


# ------------------------------------------------------------------ #
# Tab 2：数据大屏
# ------------------------------------------------------------------ #
def tab_dashboard():
    stats = cached_stats()
    items = cached_items(limit=1000)
    if not items:
        st.warning("暂无数据，请在侧边栏点击「生成模拟数据」")
        return

    df = pd.DataFrame(items)
    df["品类"] = df["category"].map(lambda k: CAT_NAME.get(k, k))
    df["状态"] = df["status"].map(lambda k: STATUS_NAME.get(k, k))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("累计信息", stats["total"])
    m2.metric("自动通过", stats["by_status"].get("pass", 0))
    m3.metric("转人工复核", stats["by_status"].get("review", 0))
    m4.metric("已拦截", stats["by_status"].get("reject", 0))
    m5.metric("平均风险分", stats["avg_risk"])

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**品类分布**")
        cat_df = df["品类"].value_counts().reset_index()
        cat_df.columns = ["品类", "数量"]
        st.altair_chart(alt.Chart(cat_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
            x=alt.X("品类:N", sort="-y", title=None),
            y=alt.Y("数量:Q"), color=alt.Color("品类:N", legend=None),
            tooltip=["品类", "数量"]).properties(height=280), use_container_width=True)
    with c2:
        st.markdown("**审核决策分布**")
        st_df = df["状态"].value_counts().reset_index()
        st_df.columns = ["状态", "数量"]
        st.altair_chart(alt.Chart(st_df).mark_arc(innerRadius=55).encode(
            theta=alt.Theta("数量:Q"), color=alt.Color("状态:N"), tooltip=["状态", "数量"]
        ).properties(height=280), use_container_width=True)

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**风险分分布**")
        st.altair_chart(alt.Chart(df).mark_bar().encode(
            alt.X("risk_score:Q", bin=alt.Bin(step=10), title="风险分"),
            y="count():Q", color=alt.Color("risk_level:N", title="等级"),
            tooltip=["count()"]).properties(height=240), use_container_width=True)
    with c4:
        st.markdown("**每日发布趋势**")
        trend = pd.DataFrame(db.daily_trend(14))
        if not trend.empty:
            trend["日期"] = trend["d"].astype(str)
            t = trend.melt(id_vars="日期", value_vars=["passed", "reviewed", "rejected"],
                           var_name="类型", value_name="数量")
            t["类型"] = t["类型"].map({"passed": "通过", "reviewed": "复核", "rejected": "拒绝"})
            st.altair_chart(alt.Chart(t).mark_line(point=True).encode(
                x="日期:N", y="数量:Q", color="类型:N", tooltip=["日期", "类型", "数量"]
            ).properties(height=240), use_container_width=True)

    st.markdown("**高风险信息 Top10**")
    top = df.sort_values("risk_score", ascending=False).head(10)
    st.dataframe(top[["id", "title", "price", "品类", "risk_score", "状态", "reason"]])


# ------------------------------------------------------------------ #
# Tab 3：审核工作台
# ------------------------------------------------------------------ #
def tab_workbench():
    st.subheader("🧾 审核工作台")
    f1, f2, f3 = st.columns([1, 1, 2])
    status = f1.selectbox("状态", ["", "pass", "review", "reject"],
                          format_func=lambda x: {"": "全部", "pass": "自动通过",
                                                 "review": "转人工复核", "reject": "拒绝发布"}[x])
    category = f2.selectbox("品类", [""] + [c["key"] for c in CATEGORIES],
                            format_func=lambda x: "全部" if not x else CAT_NAME[x])
    keyword = f3.text_input("关键词（标题/描述/学号）")

    items = cached_items(status=status, category=category, keyword=keyword, limit=500)
    if not items:
        st.info("暂无数据")
        return
    df = pd.DataFrame(items)
    df["品类"] = df["category"].map(lambda k: CAT_NAME.get(k, k))
    df["状态"] = df["status"].map(lambda k: STATUS_NAME.get(k, k))
    st.dataframe(df[["id", "title", "price", "品类", "sub_category", "risk_score",
                     "rule_score", "llm_score", "状态", "seller_id", "created_at"]])

    st.markdown("---")
    ids = [i["id"] for i in items]
    col1, col2, col3 = st.columns([1, 1, 2])
    target = col1.selectbox("选择记录 ID", ids)
    action = col2.selectbox("操作", ["pass", "review", "reject"],
                            format_func=lambda x: STATUS_NAME[x])
    note = col3.text_input("备注")
    if st.button("提交人工判定"):
        db.update_review(int(target), action, reviewer="admin", note=note)
        db.add_log(int(target), f"manual_{action}", f"人工判定{note}", "admin")
        invalidate_cache()
        st.success(f"记录 {target} 已更新为「{STATUS_NAME[action]}」")
        if _rerun:
            _rerun()


# ------------------------------------------------------------------ #
# Tab 4：效果评估
# ------------------------------------------------------------------ #
def tab_eval():
    st.subheader("📊 系统效果评估（dev / holdout 双轨）")
    st.caption("在开发集与留出集上分别回放审核流水线：留出集含难负例与对抗样本，未参与规则设计，更能反映真实泛化能力")
    if st.button("运行评估"):
        with st.spinner("评估中…"):
            from data.seed import evaluate
            res = evaluate(verbose=False)
            dev, hold = res["dev"], res["holdout"]

        st.markdown("**指标总览**")
        overview = pd.DataFrame([
            {"数据集": "开发集 dev", "样本": dev["total"], "三分类准确率": f"{dev['strict']:.2%}",
             "拦截精确率": f"{dev['precision']:.2%}", "拦截召回率": f"{dev['recall']:.2%}",
             "F1": f"{dev['f1']:.2%}", "分类准确率": f"{dev['cat_acc']:.2%}"},
            {"数据集": "留出集 holdout", "样本": hold["total"], "三分类准确率": f"{hold['strict']:.2%}",
             "拦截精确率": f"{hold['precision']:.2%}", "拦截召回率": f"{hold['recall']:.2%}",
             "F1": f"{hold['f1']:.2%}", "分类准确率": f"{hold['cat_acc']:.2%}"},
        ])
        st.dataframe(overview.set_index("数据集"))

        name = st.selectbox("查看数据集", ["留出集 holdout", "开发集 dev"])
        ev = hold if name.startswith("留出集") else dev

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("样本数", ev["total"])
        c2.metric("三分类准确率", f"{ev['strict']:.2%}")
        c3.metric("拦截精确率", f"{ev['precision']:.2%}")
        c4.metric("拦截召回率", f"{ev['recall']:.2%}")
        c5.metric("F1", f"{ev['f1']:.2%}")

        st.markdown("**混淆矩阵**（行=人工标注，列=系统判定）")
        cm = ev["cm"]
        st.dataframe(pd.DataFrame(
            [[cm[a][b] for b in ("pass", "review", "reject")] for a in ("pass", "review", "reject")],
            index=["pass", "review", "reject"], columns=["pass", "review", "reject"]))

        st.markdown("**逐条明细**")
        st.dataframe(pd.DataFrame(ev["rows"]))
    else:
        st.info("点击「运行评估」开始。评估不落库，仅做流水线回放。")


# ------------------------------------------------------------------ #
# Tab 5：系统说明
# ------------------------------------------------------------------ #
def tab_about():
    st.subheader("📘 系统设计说明")
    st.markdown("""
**审核流程**

```
用户发布信息 → 文本规范化 → 规则引擎(违禁/价格/虚假/账号)
                              ↓
                         大模型语义审核(分类/风险/理由)
                              ↓
                    决策融合(加权 + 强信号保底) → 通过 / 复核 / 拒绝
                              ↓
                        SQLite 落库 + 审核日志 + 人工复核
```
""")
    st.markdown("**双引擎分工**")
    st.table(pd.DataFrame([
        {"引擎": "规则引擎", "职责": "硬红线：违禁词、价格区间、诈骗话术、账号风险",
         "优点": "毫秒级、可解释、零成本", "短板": "召回有限、难识别话外之音"},
        {"引擎": "大模型", "职责": "软语义：品类判定、隐性违规、标题与正文矛盾、成色与价格矛盾",
         "优点": "泛化强、输出自然语言理由", "短板": "有延迟与调用成本"},
    ]))

    st.markdown("**分类体系**")
    st.table(pd.DataFrame([{"门类": f"{c['icon']} {c['name']}", "key": c["key"],
                            "说明": c["desc"], "子类": "、".join(c["sub"].keys())}
                           for c in CATEGORIES]))

    st.markdown("**违禁品规则**")
    st.table(pd.DataFrame([{"类型": r["name"], "级别": r["severity"],
                            "关键词（部分）": "、".join(r["words"][:8]),
                            "处理建议": r["advice"]} for r in BANNED_RULES]))

    st.markdown("**接入真实大模型**")
    st.code("""# 方式一：环境变量（Windows PowerShell）
$env:LLM_PROVIDER="deepseek"; $env:LLM_API_KEY="sk-xxx"; $env:LLM_MODEL="deepseek-chat"
streamlit run ui_streamlit.py

# 方式二：项目根目录创建 .env
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat

# 未配置 Key 或调用失败时，自动降级为本地语义分析器，系统仍可完整演示""",
            language="bash")


# ------------------------------------------------------------------ #
# Tab 4：卖家中心
# ------------------------------------------------------------------ #
def tab_seller():
    st.subheader("🙋 卖家中心")
    st.caption("卖家视角：查看本人发布信息的审核反馈，对拦截结论发起申诉")

    sid = st.text_input("卖家学号", value="2021010101")
    if not sid.strip():
        st.info("请输入学号")
        return

    stats = db.seller_stats(sid.strip())
    items = cached_items(seller_id=sid.strip(), limit=200)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("我的发布", stats["total"])
    m2.metric("已通过", stats["by_status"].get("pass", 0))
    m3.metric("待复核", stats["by_status"].get("review", 0))
    m4.metric("已拦截", stats["by_status"].get("reject", 0))
    m5.metric("通过率", f"{stats['pass_rate']:.0%}")

    if not items:
        st.info("该学号下暂无发布记录")
        return

    df = pd.DataFrame(items)
    df["品类"] = df["category"].map(lambda k: CAT_NAME.get(k, k))
    df["状态"] = df["status"].map(lambda k: STATUS_NAME.get(k, k))
    st.dataframe(df[["id", "title", "price", "品类", "risk_score", "状态", "reason"]])

    st.markdown("---")
    st.markdown("**审核反馈详情**")
    ids = [i["id"] for i in items]
    target = st.selectbox("选择记录 ID", ids)
    detail = next(i for i in items if i["id"] == target)
    st.markdown(f"**标题**：{detail['title']}")
    st.markdown(f"**审核结论**：{detail['reason'] or '—'}")
    st.success(f"整改建议：{detail['suggestion'] or '无'}")

    appealable = [i["id"] for i in items if i["status"] in ("reject", "review")]
    st.markdown("**申诉**")
    if not appealable:
        st.caption("当前没有被拦截或待复核的信息，无需申诉。")
        return
    appeal_id = st.selectbox("对哪条信息申诉", appealable)
    reason = st.text_area("申诉理由（不少于 5 字）", height=80)
    if st.button("提交申诉"):
        if len(reason.strip()) < 5:
            st.warning("申诉理由请不少于 5 个字")
        elif db.pending_appeal_of(appeal_id):
            st.warning("该信息已有申诉在处理中")
        else:
            aid = db.insert_appeal(appeal_id, sid.strip(), reason=reason.strip())
            db.add_log(appeal_id, "appeal", f"卖家申诉：{reason.strip()[:60]}", sid.strip())
            st.success(f"申诉已提交（编号 {aid}），管理员将尽快复核")

    my_appeals = db.list_appeals(seller_id=sid.strip(), limit=50)
    if my_appeals:
        st.markdown("**我的申诉记录**")
        st.dataframe(pd.DataFrame(my_appeals)[
            ["id", "item_id", "reason", "status", "reply", "created_at"]])


# ------------------------------------------------------------------ #
@st.cache_resource
def init_db_once():
    """建表语句只需执行一次：此前每次 rerun 都跑一遍 executescript(SCHEMA)。"""
    db.init_db()
    return True


def main():
    init_db_once()
    sidebar()
    st.title("校园二手交易信息智能审核与分类系统")
    tabs = st.tabs(["🔍 智能审核演示", "📊 数据大屏", "🧾 审核工作台",
                    "🙋 卖家中心", "📈 效果评估", "📘 系统说明"])
    with tabs[0]:
        tab_demo()
    with tabs[1]:
        tab_dashboard()
    with tabs[2]:
        tab_workbench()
    with tabs[3]:
        tab_seller()
    with tabs[4]:
        tab_eval()
    with tabs[5]:
        tab_about()


if __name__ == "__main__":
    main()
