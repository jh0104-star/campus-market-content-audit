# -*- coding: utf-8 -*-
"""审核链路性能基准。

对 44 条标注样本做多轮全量审核，输出平均耗时与吞吐，
用于验证规则引擎 / 大模型 / 数据访问层的优化效果（答辩可量化展示）。

运行： python tests/bench.py [轮数]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.llm as llm
from core.pipeline import audit
from data.seed import SAMPLES, build_item


def main(rounds: int = 20) -> int:
    items = [build_item(s, i) for i, s in enumerate(SAMPLES)]
    n = len(items)

    for it in items[:3]:                       # 预热（编译正则 / 建索引）
        audit(it)

    total_calls = 0
    t0 = time.perf_counter()
    for _ in range(rounds):
        llm.clear_cache()                      # 每轮清空结果缓存，保证是真算
        for it in items:
            audit(it)
        total_calls += n
    elapsed = time.perf_counter() - t0

    avg_ms = elapsed / total_calls * 1000
    print(f"样本数 {n} ｜ 轮数 {rounds} ｜ 总调用 {total_calls}")
    print(f"总耗时   {elapsed:.3f} s")
    print(f"平均单次 {avg_ms:.3f} ms")
    print(f"吞吐     {total_calls / elapsed:.1f} 条/秒（单线程）")
    return 0


if __name__ == "__main__":
    r = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    sys.exit(main(r))
