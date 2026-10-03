#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 冒烟测试：构建界面、切换全部标签页、跑一遍主题切换，然后截图退出。

存在的意义：tkinter 的错误常常只在真正渲染时才暴露
（字体缺失、Canvas 参数非法、布局循环）。跑一遍这个脚本
能在几秒内确认界面没被改坏，比手动点开快得多。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_PROG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROG_DIR))

from pharma_crawler import Library, load_config  # noqa: E402
from pharma_gui import PharmaGUI  # noqa: E402


def main() -> int:
    cfg, _path = load_config(_PROG_DIR, None)
    cfg["output_dir"] = str(_PROG_DIR / "library")
    lib = Library(Path(cfg["output_dir"]))
    lib.ensure()

    errors: list[str] = []
    try:
        app = PharmaGUI(cfg, lib, _PROG_DIR)
    except Exception:
        print("界面构建失败：")
        traceback.print_exc()
        return 1

    app.root.update_idletasks()
    app.root.update()

    # 逐页切换，确认每页都能渲染
    for key, _icon, label, _hint in __import__("pharma_gui").TABS:
        try:
            app._switch_tab(key)
            app.root.update_idletasks()
            app.root.update()
            print(f"  [ok] 标签页 {label} 渲染正常")
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            print(f"  [FAIL] 标签页 {label}: {exc}")
            traceback.print_exc()

    # 主题切换（浅色 <-> 深色）
    for _ in range(2):
        try:
            app._toggle_theme()
            app.root.update_idletasks()
            app.root.update()
        except Exception as exc:
            errors.append(f"主题切换: {exc}")
            print(f"  [FAIL] 主题切换: {exc}")
            traceback.print_exc()
    print(f"  [ok] 主题切换正常（当前 {'深色' if app.palette.dark else '浅色'}）")

    # 检索页：跑一次真实检索
    try:
        app._switch_tab("library")
        app._do_search()
        app.root.update_idletasks()
        app.root.update()
        print(f"  [ok] 检索执行正常（{len(app.table.rows)} 条结果）")
    except Exception as exc:
        errors.append(f"检索: {exc}")
        print(f"  [FAIL] 检索: {exc}")
        traceback.print_exc()

    # 统计页
    try:
        app._switch_tab("stats")
        app.root.update_idletasks()
        app.root.update()
        txt = app.stats_text.get("1.0", "end").strip()
        print(f"  [ok] 统计页渲染正常（{len(txt)} 字符）")
    except Exception as exc:
        errors.append(f"统计: {exc}")
        print(f"  [FAIL] 统计: {exc}")
        traceback.print_exc()

    # 截图（需要 Pillow，没有就跳过 —— 不因此判失败）
    shot = _PROG_DIR / "docs" / "gui_screenshot.png"
    try:
        app._switch_tab("crawl")
        app.root.update_idletasks()
        app.root.update()
        _screenshot(app.root, shot)
        print(f"  [ok] 已截图 {shot}")
    except Exception as exc:
        print(f"  [skip] 截图跳过：{exc}")

    app.root.destroy()

    if errors:
        print(f"\n冒烟测试失败：{len(errors)} 项")
        return 1
    print("\n冒烟测试通过：界面全部正常。")
    return 0


def _screenshot(root, path: Path) -> None:
    """抓取窗口截图。

    用 Windows 自带的 PowerShell + .NET 截屏，不引入 Pillow 依赖
    （保持"纯标准库"承诺）。非 Windows 直接跳过。
    """
    import subprocess

    if sys.platform != "win32":
        raise RuntimeError("截图仅支持 Windows")
    path.parent.mkdir(parents=True, exist_ok=True)
    root.update_idletasks()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    ps = (
        "Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
        f"$bmp = New-Object System.Drawing.Bitmap({w}, {h});"
        "$g = [System.Drawing.Graphics]::FromImage($bmp);"
        f"$g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size);"
        f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $bmp.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, capture_output=True, timeout=60)


if __name__ == "__main__":
    sys.exit(main())
