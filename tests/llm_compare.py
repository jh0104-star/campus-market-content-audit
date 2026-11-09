# -*- coding: utf-8 -*-
"""真实大模型 vs 本地分析器 对比实验。

在完全相同的样本上，分别用「真实大模型」与「本地语义分析器」跑完整融合链路，
比较三分类准确率与风险拦截能力。

运行：
    python tests/llm_compare.py                # dev + holdout 全量
    python tests/llm_compare.py --only dev     # 只跑开发集
    python tests/llm_compare.py --workers 8    # 提高并发（注意限流）
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import MockLLMAnalyzer, OpenAICompatLLM, LLMResult   # noqa: E402
from core.pipeline import PIPELINE                                 # noqa: E402
from core.rules import ENGINE                                      # noqa: E402
from data.seed import SAMPLES, build_item                          # noqa: E402
from data.holdout import HOLDOUT                                   # noqa: E402


def _mock_analyze(item: dict) -> LLMResult:
    return MockLLMAnalyzer().analyze(item)


def _api_analyze(item: dict, analyzer) -> LLMResult:
    for attempt in (0, 1):                     # 限流/抖动时重试一次
        try:
            return analyzer.analyze(item)
        except Exception:                      # noqa: BLE001
            if attempt:
                raise
            time.sleep(1.5)


def _row(sample: Dict, item: dict, rule, llm_res, name: str, latency: int) -> Dict:
    merged = PIPELINE._fuse(dict(item), rule, llm_res)
    return {
        "title": sample["title"],
        "expect": sample["expect"],
        "pred": merged.decision,
        "expect_cat": sample.get("expect_cat", ""),
        "pred_cat": merged.category,
        "score": merged.risk_score,
        "llm_score": llm_res.risk_score,
        "fallback": bool(llm_res.fallback),
        "latency": latency,
        "_engine": name,
    }


def metrics(rows: List[Dict]) -> Dict:
    labels = ("pass", "review", "reject")
    total = len(rows)
    blocked = lambda x: x in ("review", "reject")
    tp = sum(1 for r in rows if r["expect"] != "pass" and blocked(r["pred"]))
    fp = sum(1 for r in rows if r["expect"] == "pass" and blocked(r["pred"]))
    fn = sum(1 for r in rows if r["expect"] != "pass" and not blocked(r["pred"]))
    tn = sum(1 for r in rows if r["expect"] == "pass" and not blocked(r["pred"]))
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {
        "total": total,
        "strict": sum(1 for r in rows if r["expect"] == r["pred"]) / total,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "cat_acc": sum(1 for r in rows if r["expect_cat"] == r["pred_cat"]) / total,
        "fallback": sum(1 for r in rows if r["fallback"]) / total,
        "latency": sum(r["latency"] for r in rows) / total,
    }


def run_all(datasets: Dict[str, List[Dict]], workers: int) -> Dict[str, Dict[str, Dict]]:
    out: Dict[str, Dict[str, Dict]] = {}
    for name, samples in datasets.items():
        api_analyzer = OpenAICompatLLM()
        mock = MockLLMAnalyzer()

        items, rules = [], []
        for i, s in enumerate(samples):
            it = build_item(s, i)
            it.pop("seller_id", None)
            items.append(it)
            rules.append(ENGINE.run(it))

        def call_api(item: dict, a=api_analyzer):
            t0 = time.time()
            try:
                res = _api_analyze(item, a)
            except Exception as exc:                                # noqa: BLE001
                res = mock.analyze(item)
                res.fallback = True
                res.engine = "mock(fallback)"
                res.reason += f"（调用失败：{type(exc).__name__}）"
            return res, int((time.time() - t0) * 1000)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            api_pairs = list(pool.map(call_api, items))
        local_pairs = []
        for it in items:
            t0 = time.time()
            local_pairs.append((mock.analyze(it), int((time.time() - t0) * 1000)))

        rows_api = [_row(s, it, r, res, "api", lat)
                    for s, it, r, (res, lat) in zip(samples, items, rules, api_pairs)]
        rows_local = [_row(s, it, r, res, "mock", lat)
                      for s, it, r, (res, lat) in zip(samples, items, rules, local_pairs)]

        print(f"\n[{name}] 完成：真实大模型 {sum(1 for r in rows_api if not r['fallback'])}/{len(rows_api)} 未降级")
        out[name] = {"真实大模型": rows_api, "本地分析器": rows_local}
    return out


def report(results: Dict[str, Dict[str, Dict]]) -> None:
    print("\n" + "=" * 78)
    print("对比实验：真实大模型(DeepSeek) vs 本地分析器（相同规则引擎，仅替换语义层）")
    print("=" * 78)
    header = f"{'数据集':<10}{'语义层':<14}{'严格准确':<10}{'拦截精确':<10}{'拦截召回':<10}{'F1':<10}{'分类准确':<10}{'均耗时':<10}"
    print(header)
    print("-" * 78)
    for ds, engines in results.items():
        for eng, rows in engines.items():
            m = metrics(rows)
            print(f"{ds:<10}{eng:<14}{m['strict']:.2%}{'':<4}{m['precision']:.2%}{'':<4}"
                  f"{m['recall']:.2%}{'':<4}{m['f1']:.2%}{'':<4}{m['cat_acc']:.2%}{'':<4}"
                  f"{m['latency']:.0f}ms")
        print("-" * 78)

    # 逐条差异
    for ds, engines in results.items():
        api_rows = engines["真实大模型"]
        local_rows = engines["本地分析器"]
        diffs = [(a, b) for a, b in zip(api_rows, local_rows)
                 if a["pred"] != b["pred"] or a["pred_cat"] != b["pred_cat"]]
        print(f"\n【{ds}】两者结论不一致 {len(diffs)}/{len(api_rows)} 条：")
        for a, b in diffs[:12]:
            print(f"  · {a['title'][:26]}")
            print(f"      标注={a['expect']:<7} DeepSeek={a['pred']:<7}(lm{a['llm_score']:>3}) "
                  f"本地={b['pred']:<7}(lm{b['llm_score']:>3})")
        if len(diffs) > 12:
            print(f"  ... 另有 {len(diffs) - 12} 条")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("dev", "holdout"), help="只跑指定数据集")
    ap.add_argument("--workers", type=int, default=4, help="并发数")
    args = ap.parse_args()

    datasets = {"dev": SAMPLES, "holdout": HOLDOUT}
    if args.only:
        datasets = {args.only: datasets[args.only]}

    t0 = time.time()
    results = run_all(datasets, args.workers)
    report(results)
    print(f"\n总耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
