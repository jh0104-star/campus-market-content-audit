# -*- coding: utf-8 -*-
"""Streamlit 页面逻辑回归测试。

用 stub 替换 streamlit 的交互组件，驱动 ui_streamlit 的各 Tab 函数，
验证数据处理与渲染链路不抛异常（无需启动浏览器）。

运行： python tests/ui_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Magic:
    """通用的"随便调用都行"对象。"""
    def __init__(self, v=None):
        self.v = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, k):
        return Magic()

    def __getitem__(self, k):
        return Magic()

    def __call__(self, *a, **k):
        return Magic()

    def __iter__(self):
        return iter([Magic()])

    def __bool__(self):
        return True


class StubStreamlit(Magic):
    """模拟 streamlit API：输入类组件返回合理默认值，输出类组件吞掉。"""

    def __init__(self, with_sidebar=True):
        super().__init__()
        object.__setattr__(self, "sidebar",
                           StubStreamlit(with_sidebar=False) if with_sidebar else Magic())

    @staticmethod
    def columns(n, **k):
        # 列容器同样需要支持输入类组件
        return [StubStreamlit(False) for _ in (range(n) if isinstance(n, int) else n)]

    @staticmethod
    def tabs(seq):
        return [Magic() for _ in seq]

    @staticmethod
    def selectbox(label, options=(), *a, **k):
        opts = list(options or [])
        return opts[0] if opts else ""

    @staticmethod
    def number_input(label, *a, **k):
        return k.get("value", a[1] if len(a) > 1 else 0.0)

    @staticmethod
    def text_input(label, *a, **k):
        return k.get("value", a[0] if a else "")

    @staticmethod
    def text_area(label, *a, **k):
        return k.get("value", a[0] if a else "")

    @staticmethod
    def checkbox(label, *a, **k):
        return k.get("value", False)

    @staticmethod
    def slider(label, *a, **k):
        return a[2] if len(a) >= 3 else k.get("value", 0)

    @staticmethod
    def form_submit_button(*a, **k):
        return True

    @staticmethod
    def button(label="", *a, **k):
        # 只触发"运行评估"，避免其他按钮写入数据库
        return "评估" in str(label)


def main():
    import ui_streamlit as ui
    ui.st = StubStreamlit()

    ok = fail = 0
    for name in ("tab_demo", "tab_dashboard", "tab_workbench",
                 "tab_seller", "tab_eval", "tab_about"):
        fn = getattr(ui, name)
        try:
            fn()
            ok += 1
            print(f"  [PASS] {name}")
        except Exception as exc:
            fail += 1
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")

    print(f"\n结果：通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
