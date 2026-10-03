#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Material Design 3 主题层（tkinter 实现）。

取值依据见 ``docs/MATERIAL3_NOTES.md``（源自 AOSP/androidx 设计令牌文件，
而非 JS 渲染的 m3.material.io）。

本模块解决 tkinter 落地 M3 的四个硬伤：

1. **没有圆角**。ttk 的 clam/alt 主题无法产出圆角形状。这里提供 ``RoundedFrame``
   —— 基于 Canvas 平滑样条自绘的圆角矩形容器，可承载任意子控件。
2. **没有阴影**。M3 当前规范本就用"色调表面容器"表达层级而非投影，
   所以这里用 surface-container 色阶 + 1px outline-variant 描边模拟，不伪造阴影。
3. **没有状态层**。ttk 的 active 状态是换色而非叠加。这里提供 ``mix()``
   按 M3 精确比例（悬停 8%、聚焦/按下 10%、拖拽 16%）混合出叠加色。
4. **没有 Material Symbols 字形**。用 Unicode 几何符号近似，
   保证零外部资源依赖（不引入图标字体，纯标准库承诺不破）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# 颜色令牌
# --------------------------------------------------------------------------------------

#: 亮色方案 —— M3 基线 37 令牌，逐一对应 docs/MATERIAL3_NOTES.md 的表格
LIGHT: Dict[str, str] = {
    "primary": "#6750A4",
    "on_primary": "#FFFFFF",
    "primary_container": "#EADDFF",
    "on_primary_container": "#21005D",
    "secondary": "#625B71",
    "on_secondary": "#FFFFFF",
    "secondary_container": "#E8DEF8",
    "on_secondary_container": "#1D192B",
    "tertiary": "#7D5260",
    "on_tertiary": "#FFFFFF",
    "tertiary_container": "#FFD8E4",
    "on_tertiary_container": "#31111D",
    "error": "#B3261E",
    "on_error": "#FFFFFF",
    "error_container": "#F9DEDC",
    "on_error_container": "#410E0B",
    "background": "#FEF7FF",
    "on_background": "#1D1B20",
    "surface": "#FEF7FF",
    "on_surface": "#1D1B20",
    "surface_variant": "#E7E0EC",
    "on_surface_variant": "#49454F",
    "surface_dim": "#DED8E1",
    "surface_bright": "#FEF7FF",
    "surface_container_lowest": "#FFFFFF",
    "surface_container_low": "#F7F2FA",
    "surface_container": "#F3EDF7",
    "surface_container_high": "#ECE6F0",
    "surface_container_highest": "#E6E0E9",
    "outline": "#79747E",
    "outline_variant": "#CAC4D0",
    "shadow": "#000000",
    "scrim": "#000000",
    "inverse_surface": "#322F35",
    "inverse_on_surface": "#F5EFF7",
    "inverse_primary": "#D0BCFF",
    "surface_tint": "#6750A4",
}

#: 暗色方案
DARK: Dict[str, str] = {
    "primary": "#D0BCFF",
    "on_primary": "#381E72",
    "primary_container": "#4F378B",
    "on_primary_container": "#EADDFF",
    "secondary": "#CCC2DC",
    "on_secondary": "#332D41",
    "secondary_container": "#4A4458",
    "on_secondary_container": "#E8DEF8",
    "tertiary": "#EFB8C8",
    "on_tertiary": "#492532",
    "tertiary_container": "#633B48",
    "on_tertiary_container": "#FFD8E4",
    "error": "#F2B8B5",
    "on_error": "#601410",
    "error_container": "#8C1D18",
    "on_error_container": "#F9DEDC",
    "background": "#141218",
    "on_background": "#E6E0E9",
    "surface": "#141218",
    "on_surface": "#E6E0E9",
    "surface_variant": "#49454F",
    "on_surface_variant": "#CAC4D0",
    "surface_dim": "#141218",
    "surface_bright": "#3B383E",
    "surface_container_lowest": "#0F0D13",
    "surface_container_low": "#1D1B20",
    "surface_container": "#211F26",
    "surface_container_high": "#2B2930",
    "surface_container_highest": "#36343B",
    "outline": "#938F99",
    "outline_variant": "#49454F",
    "shadow": "#000000",
    "scrim": "#000000",
    "inverse_surface": "#E6E0E9",
    "inverse_on_surface": "#322F35",
    "inverse_primary": "#6750A4",
    "surface_tint": "#D0BCFF",
}

#: 语义别名 —— 药学领域用色。用 tertiary 系标注"文献"，secondary 系标注"药品"，
#: 让两个来源在界面上一眼可分。
SEMANTIC_COLORS = {
    "drug": "secondary",        # 药品类数据
    "literature": "tertiary",   # 文献类数据
    "success": "primary",
    "warning": "tertiary",
}


# --------------------------------------------------------------------------------------
# 颜色运算
# --------------------------------------------------------------------------------------


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    c = str(color).strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (0, 0, 0)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb: Sequence[int]) -> str:
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def mix(bg: str, fg: str, alpha: float) -> str:
    """把 fg 以 alpha 比例叠加到 bg 上，返回混合色。

    这就是 M3 状态层的实现方式：**在容器色上叠加内容色的半透明层**，
    而不是替换成另一个实色。ttk 默认的 active 换色行为不符合 M3，
    所以自绘控件时统一走这个函数。

    M3 精确比例：悬停 8%、聚焦 10%、按下 10%、拖拽 16%。
    """
    a = max(0.0, min(1.0, float(alpha)))
    br, bgc, bb = hex_to_rgb(bg)
    fr, fgc, fb = hex_to_rgb(fg)
    return rgb_to_hex((
        round(br + (fr - br) * a),
        round(bgc + (fgc - bgc) * a),
        round(bb + (fb - bb) * a),
    ))


def on_color(bg: str, light: str = "#FFFFFF", dark: str = "#1D1B20") -> str:
    """按背景亮度自动选前景色（保底可读性）。"""
    r, g, b = hex_to_rgb(bg)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return dark if lum > 0.55 else light


# --------------------------------------------------------------------------------------
# 形状 / 间距 / 字型刻度
# --------------------------------------------------------------------------------------

#: 圆角刻度（dp）
SHAPE = {
    "none": 0, "extra_small": 4, "small": 8, "medium": 12,
    "large": 16, "extra_large": 28, "full": 9999,
}

#: 4dp 基准网格
SPACE = {
    "xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24,
    "2xl": 32, "3xl": 48, "4xl": 64,
}

#: M3 字形刻度：(字号, 行高, 字重, 字距)
TYPE_SCALE: Dict[str, Tuple[int, int, int, float]] = {
    "display_large": (57, 64, 400, -0.25),
    "display_medium": (45, 52, 400, 0),
    "display_small": (36, 44, 400, 0),
    "headline_large": (32, 40, 400, 0),
    "headline_medium": (28, 36, 400, 0),
    "headline_small": (24, 32, 400, 0),
    "title_large": (22, 28, 400, 0),
    "title_medium": (16, 24, 500, 0.15),
    "title_small": (14, 20, 500, 0.1),
    "body_large": (16, 24, 400, 0.5),
    "body_medium": (14, 20, 400, 0.25),
    "body_small": (12, 16, 400, 0.4),
    "label_large": (14, 20, 500, 0.1),
    "label_medium": (12, 16, 500, 0.5),
    "label_small": (11, 16, 500, 0.5),
}

#: 组件规格（本 GUI 实际用到的部分）
COMPONENT = {
    "button_height": 40,
    "button_radius": 20,
    "button_pad_x": 24,
    "field_height": 56,
    "field_radius": 4,
    "card_radius": 12,
    "card_pad": 16,
    "chip_height": 32,
    "chip_radius": 8,
    "tab_height": 48,
    "tab_indicator": 3,
    "dialog_radius": 28,
    "dialog_min_w": 280,
    "dialog_max_w": 560,
    "top_app_bar": 64,
    "nav_rail": 80,
    "nav_rail_wide": 96,
    "list_one_line": 56,
    "list_two_line": 72,
    "progress_height": 4,
    "divider": 1,
}

#: 窗口尺寸类断点（实测自 WindowSizeClass.kt）
BREAKPOINTS = [
    ("compact", 0, 599),
    ("medium", 600, 839),
    ("expanded", 840, 1199),
    ("large", 1200, 1599),
    ("extra_large", 1600, 10 ** 6),
]

#: 状态层不透明度（精确值）
STATE = {"hover": 0.08, "focus": 0.10, "pressed": 0.10, "dragged": 0.16}

#: 字体族回退栈。Windows 上 Roboto 常缺席，退到 Segoe UI。
FONT_STACK = ["Roboto", "Roboto Medium", "Segoe UI", "Microsoft YaHei UI", "Arial"]
MONO_STACK = ["Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New"]


def size_class(width_px: int) -> str:
    for name, lo, hi in BREAKPOINTS:
        if lo <= width_px <= hi:
            return name
    return "compact"


# --------------------------------------------------------------------------------------
# Unicode 图标（替代 Material Symbols）
#
# tkinter 没有矢量字形渲染器，引入图标字体又会破坏"纯标准库"承诺
# （得随包分发 .ttf）。这些几何符号在所有 Windows 字体里都存在，
# 零依赖且不会出现豆腐块。
# --------------------------------------------------------------------------------------

ICONS = {
    "search": "🔍",
    "download": "⤓",
    "database": "▤",
    "settings": "⚙",
    "history": "◷",
    "folder": "▣",
    "check": "✓",
    "close": "✕",
    "warning": "⚠",
    "error": "✕",
    "info": "ⓘ",
    "play": "▶",
    "stop": "■",
    "refresh": "⟳",
    "export": "⤴",
    "chart": "▦",
    "book": "▤",
    "pill": "◍",
    "lab": "⚗",
    "clock": "◷",
    "plus": "＋",
    "minus": "－",
    "chevron_right": "›",
    "chevron_down": "⌄",
    "external": "↗",
    "file": "▢",
    "trash": "🗑",
    "light": "☀",
    "dark": "☾",
}


class Palette:
    """当前生效的配色方案。

    ``Colors`` 是本模块对外的主要对象：所有界面代码只从它取色，
    切换明暗时只要改它的 ``dark`` 标志并重绘，不必到处写死色值。
    """

    def __init__(self, dark: bool = False) -> None:
        self.dark = bool(dark)
        self._tokens: Dict[str, str] = dict(DARK if dark else LIGHT)

    # ---- 令牌访问 ----

    def token(self, name: str) -> str:
        return self._tokens.get(name, "#FF00FF")  # 洋红 = 缺令牌的显式警告

    def __getitem__(self, name: str) -> str:
        return self.token(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tokens

    def keys(self) -> Sequence[str]:
        return list(self._tokens.keys())

    def as_dict(self) -> Dict[str, str]:
        return dict(self._tokens)

    # ---- 语义色 ----

    def semantic(self, kind: str) -> Tuple[str, str]:
        """返回 (容器色, 前景色)。用于给不同数据源上色。"""
        role = SEMANTIC_COLORS.get(kind, "primary")
        return self.token(f"{role}_container"), self.token(f"on_{role}_container")

    # ---- 层级（用色调表面容器替代阴影）----

    def elevation(self, level: int) -> str:
        """按 M3 当前规范用色调表面容器表达层级，而非投影。

        level: 0=页面底  1=低  2=容器  3/4=高  5=最高
        """
        mapping = {
            0: "surface",
            1: "surface_container_low",
            2: "surface_container",
            3: "surface_container_high",
            4: "surface_container_high",
            5: "surface_container_highest",
        }
        return self.token(mapping.get(max(0, min(5, level)), "surface_container"))

    # ---- 状态层 ----

    def state(self, base: str, content: str, state: str = "hover") -> str:
        """按 M3 精确比例算出叠加后的颜色。"""
        return mix(base, content, STATE.get(state, 0.08))

    def hover(self, base: str, content: str) -> str:
        return self.state(base, content, "hover")

    def pressed(self, base: str, content: str) -> str:
        return self.state(base, content, "pressed")

    # ---- 按角色快捷取色 ----

    def button_colors(self, variant: str = "filled") -> Tuple[str, str]:
        """返回 (容器, 前景)。variant: filled / tonal / outlined / text / elevated。"""
        if variant == "filled":
            return self.token("primary"), self.token("on_primary")
        if variant == "tonal":
            return self.token("secondary_container"), self.token("on_secondary_container")
        if variant == "elevated":
            return self.token("surface_container_low"), self.token("primary")
        if variant == "outlined":
            return self.token("surface"), self.token("primary")
        return self.token("surface"), self.token("primary")

    def disabled_colors(self) -> Tuple[str, str]:
        """禁用态：容器 10% 不透明，内容 38%（M3 规定）。"""
        return (
            mix(self.token("surface"), self.token("on_surface"), 0.12),
            mix(self.token("surface"), self.token("on_surface"), 0.38),
        )

    def toggle(self) -> "Palette":
        """返回相反明暗的新配色方案。"""
        return Palette(not self.dark)
