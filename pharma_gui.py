#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PharmaCrawler 图形界面（Material Design 3）。

设计依据见 ``docs/MATERIAL3_NOTES.md``。

tkinter 落地 M3 的三个硬伤与对策
--------------------------------
1. **无圆角**。ttk 主题产不出圆角形状。本模块用 ``Canvas`` 平滑样条自绘
   圆角矩形（``RoundedFrame``），它是整个主题的地基 ——
   卡片 12、按钮 20、对话框 28 全靠它。
2. **无阴影**。M3 当前规范本就用色调表面容器表达层级，故直接用
   ``surface-container-*`` 色阶 + 1px ``outline-variant`` 描边，不伪造阴影。
3. **无状态层**。ttk 的 active 是换色而非叠加；这里用 ``Palette.state()``
   按 M3 精确比例（悬停 8%、按下 10%）算出叠加色，在 Canvas 上重绘。

界面结构（对应 M3 导航栏 + 顶栏 + 内容区）::

    ┌──────────────────────────────────────────────┐
    │ 顶栏 64dp：标题 + 明暗切换 + 关于            │
    ├────────┬─────────────────────────────────────┤
    │ 导航栏 │  内容区（卡片 + 表单 + 结果表）      │
    │ 80dp   │                                     │
    └────────┴─────────────────────────────────────┘
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_PROG_DIR = Path(__file__).resolve().parent
if str(_PROG_DIR) not in sys.path:
    sys.path.insert(0, str(_PROG_DIR))

import m3_theme as m3  # noqa: E402
from m3_theme import COMPONENT, ICONS, SHAPE, SPACE, Palette  # noqa: E402

from pharma_crawler import (  # noqa: E402
    APP_TITLE, APP_VERSION, EXPORT_FIELDS, OPENFDA_ENDPOINTS, PUBMED_FIELD_TAGS,
    PUBMED_MAX_RETMAX, CrawlError, CrawlSession, DateRange, Library,
    OPENFDA_WINDOW, format_size, magnitude_cn, magnitude_seconds,
    normalize_openfda, normalize_pubmed, now_iso, parse_bool, parse_pubmed_articles,
    parse_user_date, print_records, record_key, search_records, truncate_display,
)

# --------------------------------------------------------------------------------------
# 字体解析
# --------------------------------------------------------------------------------------


class FontBook:
    """把 M3 字形刻度解析成 tkinter 字体。

    M3 指定 Roboto，但 Windows 常缺席，故按回退栈探测一次并缓存 ——
    每次建字体都调 tkinter.font.families() 会明显拖慢启动。
    """

    def __init__(self, root: tk.Misc) -> None:
        import tkinter.font as tkfont

        available = {f.lower() for f in tkfont.families(root)}
        self.family = "Segoe UI"
        for cand in m3.FONT_STACK:
            if cand.lower() in available:
                self.family = cand
                break
        self.mono = "Consolas"
        for cand in m3.MONO_STACK:
            if cand.lower() in available:
                self.mono = cand
                break
        self._cache: Dict[Tuple[str, int, str], Any] = {}
        self._tkfont = tkfont

    def get(self, role: str = "body_medium", *, bold: bool = False,
            size_delta: int = 0, mono: bool = False) -> Any:
        size, _lh, weight, _ls = m3.TYPE_SCALE.get(role, m3.TYPE_SCALE["body_medium"])
        use_bold = bold or weight >= 500
        # tkinter 是点不是像素，粗略按 0.75 折算
        pt = max(7, int(round(size * 0.75)) + size_delta)
        key = ("mono" if mono else "ui", pt, "b" if use_bold else "n")
        if key not in self._cache:
            fam = self.mono if mono else self.family
            self._cache[key] = self._tkfont.Font(
                family=fam, size=pt, weight="bold" if use_bold else "normal"
            )
        return self._cache[key]


# --------------------------------------------------------------------------------------
# 圆角绘制基础件
# --------------------------------------------------------------------------------------


def rounded_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> List[float]:
    """生成圆角矩形的样条控制点。

    用 ``create_polygon(..., smooth=True)`` 配合这些点即可画出圆角。
    平滑样条会把控制点当"引力点"，所以每条边中段要补一个中点，
    否则边会被拉成弧线而不是直线。
    """
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    return [
        x1 + r, y1,
        x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2,
        x1 + r, y2, x1, y2, x1, y2 - r,
        x1, y1 + r, x1, y1, x1 + r, y1,
    ]


class RoundedFrame(tk.Frame):
    """自绘圆角矩形容器，可承载任意子控件。

    实现要点：Canvas 作底，子控件通过 ``body`` 内部 Frame 用 ``create_window``
    嵌入。这样既有圆角背景，又能正常摆放 ttk 控件。
    """

    def __init__(self, master: tk.Misc, palette: Palette, *, radius: int = 12,
                 fill: Optional[str] = None, outline: Optional[str] = None,
                 outline_width: int = 1, padding: int = 0, **kw: Any) -> None:
        self.palette = palette
        self.radius = radius
        self._fill = fill if fill is not None else palette.token("surface_container")
        self._outline = outline if outline is not None else palette.token("outline_variant")
        self._outline_width = outline_width
        super().__init__(master, bg=palette.token("surface"), highlightthickness=0, bd=0, **kw)

        self.canvas = tk.Canvas(
            self, highlightthickness=0, bd=0,
            bg=palette.token("surface"),
        )
        self.canvas.pack(fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg=self._fill)
        self._win = self.canvas.create_window(
            padding + outline_width, padding + outline_width,
            window=self.body, anchor="nw",
        )
        self._shape: Optional[int] = None
        self._padding = padding + outline_width
        self.canvas.bind("<Configure>", self._redraw)

    def set_colors(self, *, fill: Optional[str] = None,
                   outline: Optional[str] = None) -> None:
        if fill is not None:
            self._fill = fill
            self.body.configure(bg=fill)
        if outline is not None:
            self._outline = outline
        self._redraw()

    def _redraw(self, _event: Any = None) -> None:
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 2 or h <= 2:
            return
        # 圆形/胶囊：半径取一半高度
        r = self.radius
        if r >= 9000:
            r = h / 2
        if self._shape is not None:
            self.canvas.delete(self._shape)
        pad = self._outline_width / 2.0
        pts = rounded_rect_points(pad, pad, w - pad, h - pad, r)
        self._shape = self.canvas.create_polygon(
            pts, smooth=True,
            fill=self._fill,
            outline=self._outline if self._outline_width else "",
            width=self._outline_width,
        )
        self.canvas.tag_lower(self._shape)
        self.canvas.itemconfigure(
            self._win,
            width=max(1, w - self._padding * 2),
            height=max(1, h - self._padding * 2),
        )


class M3Button(tk.Canvas):
    """M3 按钮。variant: filled / tonal / outlined / text / elevated。

    自绘而非用 ttk.Button —— ttk 做不出胶囊圆角，
    也做不出 M3 的状态层（悬停 8%、按下 10% 的叠加）。
    """

    def __init__(self, master: tk.Misc, palette: Palette, text: str, *,
                 variant: str = "filled", command: Optional[Callable[[], Any]] = None,
                 icon: str = "", width: Optional[int] = None, height: int = 36,
                 font_role: str = "label_large", fonts: Optional[FontBook] = None) -> None:
        self.palette = palette
        self.variant = variant
        self.text = text
        self.icon = icon
        self.command = command
        self.fonts = fonts
        self._enabled = True
        self._hover = False
        self._pressed = False
        # ⚠️ 绝不能用 self._w / self._h：tkinter 内部用 _w 存控件的路径名，
        # 覆盖它会得到 `bad window path name "72"` 这种诡异错误。
        # 这里用 _cw / _ch（canvas width/height）避开冲突。
        self._cw = 0
        self._ch = height

        label = f"{icon} {text}".strip() if icon else text
        if fonts is not None:
            f = fonts.get(font_role)
            req_w = f.measure(label) + (56 if icon else 48)
        else:
            f = None
            req_w = len(label) * 10 + 48
        w = width or max(72, req_w)

        super().__init__(master, width=w, height=height,
                         highlightthickness=0, bd=0, bg=palette.token("surface"))
        self._cw = w
        self._ch = height
        self._font = f
        self._label = label

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        # 延后到事件循环再画：Canvas 刚构造完时底层窗口还没建好，
        # 此时 delete("all") 会抛 TclError: invalid command name。
        self.after_idle(self._draw)

    # ---- 状态 ----

    def _colors(self) -> Tuple[str, str, str]:
        """返回 (容器色, 前景色, 描边色)。"""
        p = self.palette
        if not self._enabled:
            cont, fore = p.disabled_colors()
            return cont, fore, p.token("outline_variant")
        base, fore = p.button_colors(self.variant)
        outline = ""
        if self.variant in ("outlined",):
            outline = p.token("outline")
        if self._pressed:
            base = p.state(base, fore, "pressed")
        elif self._hover:
            base = p.state(base, fore, "hover")
        return base, fore, outline

    def _draw(self) -> None:
        # 控件销毁后仍可能被排队的 after_idle 回调打到，静默跳过即可
        try:
            if not self.winfo_exists():
                return
            self.delete("all")
        except tk.TclError:
            return
        cont, fore, outline = self._colors()
        h = self._ch
        r = h / 2  # M3 按钮是胶囊形
        pts = rounded_rect_points(1, 1, self._cw - 1, h - 1, r)
        self.create_polygon(
            pts, smooth=True, fill=cont,
            outline=outline if outline else "", width=2 if outline else 0,
        )
        self.create_text(
            self._cw / 2, h / 2, text=self._label, fill=fore,
            font=self._font if self._font else ("Segoe UI", 9),
        )

    # ---- 事件 ----

    def _on_enter(self, _e: Any) -> None:
        if self._enabled:
            self._hover = True
            self.configure(cursor="hand2")
            self._draw()

    def _on_leave(self, _e: Any) -> None:
        self._hover = self._pressed = False
        self.configure(cursor="")
        self._draw()

    def _on_press(self, _e: Any) -> None:
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, _e: Any) -> None:
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._enabled and self.command:
            self.command()

    # ---- 外部接口 ----

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "")
        self._draw()

    def set_text(self, text: str, icon: Optional[str] = None) -> None:
        self.text = text
        if icon is not None:
            self.icon = icon
        self._label = f"{self.icon} {text}".strip() if self.icon else text
        self._draw()

    def refresh(self) -> None:
        self.configure(bg=self.palette.token("surface"))
        self._draw()


class M3Chip(tk.Canvas):
    """M3 筛选纸片（可选中）。用于数据集/来源筛选。"""

    def __init__(self, master: tk.Misc, palette: Palette, text: str, *,
                 selected: bool = False, command: Optional[Callable[[str, bool], Any]] = None,
                 fonts: Optional[FontBook] = None, height: int = 30) -> None:
        self.palette = palette
        self.text = text
        self.selected = selected
        self.command = command
        w = (fonts.get("label_medium").measure(text) if fonts else len(text) * 9) + 34
        super().__init__(master, width=w, height=height, highlightthickness=0, bd=0,
                         bg=palette.token("surface"))
        # 同上：避开 tkinter 内部保留的 _w/_h
        self._cw, self._ch = w, height
        self._font = fonts.get("label_medium") if fonts else ("Segoe UI", 8)
        self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._toggle)
        self.after_idle(self._draw)

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self.configure(cursor="hand2" if on else "")
        self._draw()

    def _toggle(self, _e: Any) -> None:
        self.selected = not self.selected
        self._draw()
        if self.command:
            self.command(self.text, self.selected)

    def _draw(self) -> None:
        try:
            if not self.winfo_exists():
                return
            self.delete("all")
        except tk.TclError:
            return
        p = self.palette
        if self.selected:
            cont = p.token("secondary_container")
            fore = p.token("on_secondary_container")
            outline = ""
        else:
            cont = p.token("surface")
            fore = p.token("on_surface_variant")
            outline = p.token("outline_variant")
            if self._hover:
                cont = p.state(p.token("surface"), fore, "hover")
        pts = rounded_rect_points(1, 1, self._cw - 1, self._ch - 1, self._ch / 2)
        self.create_polygon(pts, smooth=True, fill=cont,
                            outline=outline if outline else "", width=1 if outline else 0)
        label = f"{ICONS['check']} {self.text}" if self.selected else self.text
        self.create_text(self._cw / 2, self._ch / 2, text=label, fill=fore, font=self._font)

    def refresh(self) -> None:
        self.configure(bg=self.palette.token("surface"))
        self._draw()


# --------------------------------------------------------------------------------------
# 简易结果表（树形表）
# --------------------------------------------------------------------------------------


class ResultTable(tk.Frame):
    """结果表格。用 ttk.Treeview 但套上 M3 配色。"""

    def __init__(self, master: tk.Misc, palette: Palette, fonts: FontBook) -> None:
        super().__init__(master, bg=palette.token("surface"))
        self.palette = palette
        self.fonts = fonts
        self.rows: List[Dict[str, Any]] = []

        cols = ("title", "source", "id", "date", "extra")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, label, width, anchor in (
            ("title", "标题", 420, "w"),
            ("source", "来源", 110, "w"),
            ("id", "标识符", 130, "w"),
            ("date", "日期", 100, "w"),
            ("extra", "期刊 / 厂商", 220, "w"),
        ):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=width, anchor=anchor, stretch=(c == "title"))

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double)

    def _on_double(self, _e: Any) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.rows):
            DetailDialog(self.winfo_toplevel(), self.palette, self.fonts, self.rows[idx])

    def set_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.tree.delete(*self.tree.get_children())
        label_of = {"openfda": "FDA", "pubmed": "PubMed"}
        for i, r in enumerate(self.rows):
            src = label_of.get(str(r.get("source")), str(r.get("source") or ""))
            if src == "FDA":
                ds = OPENFDA_ENDPOINTS.get(str(r.get("dataset")), {}).get("label", "")
                src = f"FDA·{ds[:8]}" if ds else "FDA"
            extra = r.get("journal") or r.get("manufacturer") or r.get("pharm_class") or ""
            rid = r.get("pmid") or r.get("doi") or r.get("id") or ""
            self.tree.insert("", "end", iid=str(i), values=(
                truncate_display(r.get("title") or "(无标题)", 120),
                src,
                truncate_display(rid, 40),
                str(r.get("date") or r.get("year") or ""),
                truncate_display(extra, 60),
            ))
        self.apply_style()

    def apply_style(self) -> None:
        p = self.palette
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            background=p.token("surface_container_low"),
            fieldbackground=p.token("surface_container_low"),
            foreground=p.token("on_surface"),
            rowheight=28,
            borderwidth=0,
            font=self.fonts.get("body_medium"),
        )
        style.configure(
            "Treeview.Heading",
            background=p.token("surface_container_high"),
            foreground=p.token("on_surface_variant"),
            relief="flat",
            font=self.fonts.get("label_large"),
            padding=(8, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", p.token("secondary_container"))],
            foreground=[("selected", p.token("on_secondary_container"))],
        )
        style.map("Treeview.Heading", background=[("active", p.token("surface_container_highest"))])


# --------------------------------------------------------------------------------------
# 对话框
# --------------------------------------------------------------------------------------


class DetailDialog(tk.Toplevel):
    """记录详情 —— M3 对话框（圆角 28、surface-container-high）。"""

    def __init__(self, master: tk.Misc, palette: Palette, fonts: FontBook,
                 rec: Dict[str, Any]) -> None:
        super().__init__(master)
        self.palette = palette
        self.fonts = fonts
        self.rec = rec
        self.title("记录详情")
        self.configure(bg=palette.token("surface"))
        self.geometry("860x640")
        self.minsize(560, 400)

        outer = RoundedFrame(self, palette, radius=COMPONENT["dialog_radius"],
                             fill=palette.token("surface_container_high"),
                             outline="", outline_width=0, padding=20)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        head = tk.Frame(outer.body, bg=palette.token("surface_container_high"))
        head.pack(fill="x")
        tk.Label(
            head, text=truncate_display(rec.get("title") or "(无标题)", 110),
            bg=palette.token("surface_container_high"),
            fg=palette.token("on_surface"),
            font=fonts.get("title_medium"), anchor="w", justify="left",
            wraplength=780,
        ).pack(fill="x")

        meta_bits: List[str] = []
        src = str(rec.get("source") or "")
        meta_bits.append("PubMed 文献" if src == "pubmed" else
                         "FDA·" + OPENFDA_ENDPOINTS.get(str(rec.get("dataset")), {}).get("label", ""))
        for k, lbl in (("id", "ID"), ("pmid", "PMID"), ("pmcid", "PMCID"),
                       ("doi", "DOI"), ("date", "日期"), ("journal", "期刊")):
            v = rec.get(k)
            if v:
                meta_bits.append(f"{lbl}={v}")
        tk.Label(
            head, text="　·　".join(meta_bits),
            bg=palette.token("surface_container_high"),
            fg=palette.token("on_surface_variant"),
            font=fonts.get("body_small"), anchor="w", justify="left", wraplength=780,
        ).pack(fill="x", pady=(6, 0))

        # 正文
        box = tk.Frame(outer.body, bg=palette.token("surface_container_high"))
        box.pack(fill="both", expand=True, pady=(12, 0))
        txt = tk.Text(
            box, wrap="word", relief="flat", bd=0,
            bg=palette.token("surface_container_lowest"),
            fg=palette.token("on_surface"),
            font=fonts.get("body_medium"),
            padx=14, pady=12, highlightthickness=1,
            highlightbackground=palette.token("outline_variant"),
        )
        vsb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        txt.insert("1.0", self._render_body())
        txt.configure(state="disabled")

        # 动作区
        actions = tk.Frame(outer.body, bg=palette.token("surface_container_high"))
        actions.pack(fill="x", pady=(14, 0))
        M3Button(actions, palette, "关闭", variant="text", command=self.destroy,
                 fonts=fonts, height=34).pack(side="right")
        if rec.get("url"):
            M3Button(actions, palette, "打开原始页面", variant="tonal",
                     icon=ICONS["external"], command=self._open_url,
                     fonts=fonts, height=34).pack(side="right", padx=(0, 8))
        M3Button(actions, palette, "复制 JSON", variant="outlined",
                 command=self._copy_json, fonts=fonts, height=34).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())

    def _render_body(self) -> str:
        r = self.rec
        lines: List[str] = []
        if r.get("authors"):
            lines.append("作者：" + str(r["authors"]).replace(" | ", "; "))
            lines.append("")
        if r.get("abstract"):
            lines.append(str(r["abstract"]))
            lines.append("")
        for key, lbl in (
            ("substance_name", "物质名"), ("generic_name", "通用名"), ("brand_name", "商品名"),
            ("manufacturer", "厂商"), ("dosage_form", "剂型"), ("route", "给药途径"),
            ("product_ndc", "NDC"), ("application_number", "申请号"),
            ("rxcui", "RxCUI"), ("unii", "UNII"), ("pharm_class", "药理分类"),
            ("classification", "召回分级"), ("reason", "召回原因"), ("status", "状态"),
            ("volume", "卷"), ("issue", "期"), ("pages", "页码"),
        ):
            v = r.get(key)
            if v:
                lines.append(f"{lbl}：{v}")
        for key, lbl in (("mesh", "MeSH 主题词"), ("keywords", "关键词"), ("pubtypes", "出版类型")):
            v = r.get(key)
            if v:
                lines.append(f"{lbl}：{'、'.join(str(x) for x in v)}")
        if not lines:
            lines.append("（该记录没有可显示的正文内容）")
        lines.append("")
        lines.append("─" * 50)
        lines.append(f"入库时间：{r.get('fetched_at') or ''}")
        lines.append(f"检索词　：{r.get('query') or ''}")
        return "\n".join(lines)

    def _open_url(self) -> None:
        url = str(self.rec.get("url") or "")
        if url:
            try:
                if os.name == "nt":
                    os.startfile(url)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", url])
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc), parent=self)

    def _copy_json(self) -> None:
        import json
        try:
            self.clipboard_clear()
            self.clipboard_append(json.dumps(self.rec, ensure_ascii=False, indent=2))
            messagebox.showinfo("已复制", "记录 JSON 已复制到剪贴板。", parent=self)
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc), parent=self)


# --------------------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------------------


TABS = [
    ("crawl", ICONS["download"], "爬取", "配置并抓取 FDA 药品数据与 PubMed 文献"),
    ("library", ICONS["search"], "数据检索", "在已爬取的数据里多维检索"),
    ("stats", ICONS["chart"], "统计", "数据规模与分布概览"),
    ("settings", ICONS["settings"], "设置", "凭据、速率、目录与导出选项"),
]


class PharmaGUI:
    """主窗口。导航栏 + 顶栏 + 内容区，对应 M3 的布局骨架。"""

    def __init__(self, cfg: Dict[str, Any], lib: Library, program_dir: Path) -> None:
        self.cfg = cfg
        self.lib = lib
        self.program_dir = program_dir
        self.palette = Palette(dark=parse_bool(cfg.get("gui_dark"), False))

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(900, 620)
        self.fonts = FontBook(self.root)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.session: Optional[CrawlSession] = None
        self.worker: Optional[threading.Thread] = None

        # 爬取页状态
        self.ds_vars: Dict[str, tk.BooleanVar] = {}
        self.result_rows: List[Dict[str, Any]] = []

        self._build()
        self._apply_theme()
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        p = self.palette

        # 顶栏（M3 top app bar，高 64）
        self.topbar = tk.Frame(self.root, bg=p.token("surface"), height=COMPONENT["top_app_bar"])
        self.topbar.pack(fill="x", side="top")
        self.topbar.pack_propagate(False)

        title_box = tk.Frame(self.topbar, bg=p.token("surface"))
        title_box.pack(side="left", padx=(20, 0), pady=8)
        tk.Label(title_box, text=APP_TITLE, bg=p.token("surface"),
                 fg=p.token("on_surface"), font=self.fonts.get("title_large"),
                 anchor="w").pack(anchor="w")
        self.subtitle = tk.Label(
            title_box, text="FDA 药品数据 · PubMed 药学文献",
            bg=p.token("surface"), fg=p.token("on_surface_variant"),
            font=self.fonts.get("body_small"), anchor="w",
        )
        self.subtitle.pack(anchor="w")

        right = tk.Frame(self.topbar, bg=p.token("surface"))
        right.pack(side="right", padx=16, pady=10)
        self.theme_btn = M3Button(
            right, p, "深色" if not self.palette.dark else "浅色",
            variant="text", icon=ICONS["dark"] if not self.palette.dark else ICONS["light"],
            command=self._toggle_theme, fonts=self.fonts, height=36,
        )
        self.theme_btn.pack(side="right")

        # 主体：导航栏 + 内容区
        body = tk.Frame(self.root, bg=p.token("surface"))
        body.pack(fill="both", expand=True)

        self.nav = tk.Frame(body, bg=p.token("surface_container_low"), width=COMPONENT["nav_rail"])
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)

        self.content = tk.Frame(body, bg=p.token("surface"))
        self.content.pack(side="left", fill="both", expand=True)

        # 导航按钮
        self.nav_buttons: Dict[str, tk.Canvas] = {}
        self.nav_labels: Dict[str, tk.Label] = {}
        self.pages: Dict[str, tk.Frame] = {}
        self.current_tab = "crawl"

        tk.Frame(self.nav, bg=p.token("surface_container_low"), height=10).pack()
        for key, icon, label, _hint in TABS:
            holder = tk.Frame(self.nav, bg=p.token("surface_container_low"))
            holder.pack(fill="x", pady=3, padx=8)
            canvas = tk.Canvas(holder, width=56, height=32, highlightthickness=0, bd=0,
                               bg=p.token("surface_container_low"))
            canvas.pack(pady=(4, 0))
            canvas.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))
            lab = tk.Label(holder, text=label, bg=p.token("surface_container_low"),
                           fg=p.token("on_surface_variant"),
                           font=self.fonts.get("label_medium"), cursor="hand2")
            lab.pack(pady=(1, 5))
            lab.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))
            self.nav_buttons[key] = canvas
            self.nav_labels[key] = lab

        # 页面
        for key, _i, _l, hint in TABS:
            page = tk.Frame(self.content, bg=p.token("surface"))
            self.pages[key] = page
            self._build_page(key, page, hint)

        self._switch_tab("crawl")

    # ---- 导航 ----

    def _switch_tab(self, key: str) -> None:
        self.current_tab = key
        for k, page in self.pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self._paint_nav()
        if key == "stats":
            self._refresh_stats()
        elif key == "library":
            self._load_filter_options()

    def _paint_nav(self) -> None:
        p = self.palette
        for key, icon, label, _hint in TABS:
            canvas = self.nav_buttons[key]
            lab = self.nav_labels[key]
            active = key == self.current_tab
            canvas.delete("all")
            if active:
                cont = p.token("secondary_container")
                fore = p.token("on_secondary_container")
                pts = rounded_rect_points(1, 1, 55, 31, 16)
                canvas.create_polygon(pts, smooth=True, fill=cont, outline="")
                canvas.create_text(28, 16, text=icon, fill=fore, font=self.fonts.get("body_large"))
                lab.configure(fg=p.token("on_surface"), font=self.fonts.get("label_large"))
            else:
                canvas.create_text(28, 16, text=icon, fill=p.token("on_surface_variant"),
                                   font=self.fonts.get("body_large"))
                lab.configure(fg=p.token("on_surface_variant"), font=self.fonts.get("label_medium"))

    # ------------------------------------------------------------------ 页面：爬取

    def _build_page(self, key: str, page: tk.Frame, hint: str) -> None:
        p = self.palette
        if key == "crawl":
            self._build_crawl_page(page, hint)
        elif key == "library":
            self._build_library_page(page, hint)
        elif key == "stats":
            self._build_stats_page(page, hint)
        elif key == "settings":
            self._build_settings_page(page, hint)
        page.configure(bg=p.token("surface"))

    def _page_header(self, parent: tk.Frame, title: str, hint: str) -> None:
        p = self.palette
        head = tk.Frame(parent, bg=p.token("surface"))
        head.pack(fill="x", padx=24, pady=(18, 10))
        tk.Label(head, text=title, bg=p.token("surface"), fg=p.token("on_surface"),
                 font=self.fonts.get("headline_small"), anchor="w").pack(anchor="w")
        tk.Label(head, text=hint, bg=p.token("surface"), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left").pack(anchor="w", pady=(2, 0))

    def _card(self, parent: tk.Frame, title: str = "", *, pad: int = 16) -> tk.Frame:
        """M3 elevated 卡片：surface-container-low + 圆角 12。"""
        card = RoundedFrame(
            parent, self.palette, radius=COMPONENT["card_radius"],
            fill=self.palette.elevation(1),
            outline=self.palette.token("outline_variant"), outline_width=1, padding=pad,
        )
        if title:
            tk.Label(card.body, text=title, bg=self.palette.elevation(1),
                     fg=self.palette.token("on_surface"),
                     font=self.fonts.get("title_small"), anchor="w").pack(anchor="w", pady=(0, 8))
        return card

    def _field(self, parent: tk.Frame, label: str, *, width: int = 30) -> tk.Entry:
        """M3 填充式文本框（圆角 4、surface-container-highest）。"""
        p = self.palette
        wrap = tk.Frame(parent, bg=self.palette.elevation(1))
        tk.Label(wrap, text=label, bg=self.palette.elevation(1),
                 fg=p.token("on_surface_variant"), font=self.fonts.get("label_medium"),
                 anchor="w").pack(anchor="w")
        entry = tk.Entry(
            wrap, width=width, relief="flat", bd=0,
            bg=p.token("surface_container_highest"), fg=p.token("on_surface"),
            insertbackground=p.token("primary"),
            font=self.fonts.get("body_medium"),
            highlightthickness=1,
            highlightbackground=p.token("outline_variant"),
            highlightcolor=p.token("primary"),
        )
        entry.pack(fill="x", ipady=7, pady=(3, 0))
        wrap.pack(fill="x", pady=(0, 10))
        return entry

    def _build_crawl_page(self, page: tk.Frame, hint: str) -> None:
        p = self.palette
        self._page_header(page, "爬取数据", hint)

        # 上部：配置区（可滚动）
        top = tk.Frame(page, bg=p.token("surface"))
        top.pack(fill="x", padx=24)

        # ---- 数据源选择卡片 ----
        ds_card = self._card(top, "① 选择数据源")
        ds_card.pack(fill="x", pady=(0, 12))
        chips = tk.Frame(ds_card.body, bg=self.palette.elevation(1))
        chips.pack(fill="x")
        for i, (ds, spec) in enumerate(OPENFDA_ENDPOINTS.items()):
            var = tk.BooleanVar(value=ds in (self.cfg.get("openfda_datasets") or []))
            self.ds_vars[ds] = var
            chip = M3Chip(
                chips, self.palette, str(spec["label"]),
                selected=var.get(), fonts=self.fonts,
                command=lambda _t, sel, d=ds: self.ds_vars[d].set(sel),
            )
            chip.pack(side="left", padx=(0, 6), pady=2)
        self.pubmed_chip_var = tk.BooleanVar(value=True)
        pubmed_chip = M3Chip(
            ds_card.body, self.palette, "PubMed 文献", selected=True, fonts=self.fonts,
            command=lambda _t, sel: self.pubmed_chip_var.set(sel),
        )
        pubmed_chip.pack(anchor="w", pady=(8, 0))
        tk.Label(ds_card.body, text="勾选 FDA 端点；PubMed 文献可同时抓取。",
                 bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w").pack(anchor="w", pady=(4, 0))

        # ---- 查询条件卡片 ----
        q_card = self._card(top, "② 查询条件")
        q_card.pack(fill="x", pady=(0, 12))
        two = tk.Frame(q_card.body, bg=self.palette.elevation(1))
        two.pack(fill="x")
        left = tk.Frame(two, bg=self.palette.elevation(1))
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))
        right = tk.Frame(two, bg=self.palette.elevation(1))
        right.pack(side="left", fill="both", expand=True)

        self.e_search = self._field(left, "FDA 查询表达式（openFDA 语法）")
        self.e_search.insert(0, 'openfda.generic_name:"metformin"')
        tk.Label(left, text='示例：openfda.brand_name:"aspirin"　·　classification:"Class I"　·　'
                            'receivedate:[20230101+TO+20231231]',
                 bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left",
                 wraplength=440).pack(anchor="w", pady=(0, 8))

        self.e_term = self._field(right, "PubMed 查询表达式")
        self.e_term.insert(0, "metformin[Title/Abstract] AND 2020:2024[PDAT]")
        tk.Label(right, text='示例：aspirin[Title/Abstract]　·　"Nature"[Journal] AND review[pt]　·　'
                             'drug[AFFL]',
                 bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left",
                 wraplength=440).pack(anchor="w", pady=(0, 8))

        # 日期 + 上限
        row3 = tk.Frame(q_card.body, bg=self.palette.elevation(1))
        row3.pack(fill="x", pady=(4, 0))
        d1 = tk.Frame(row3, bg=self.palette.elevation(1))
        d1.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.e_from = self._field(d1, "起始日期（可空）", width=14)
        d2 = tk.Frame(row3, bg=self.palette.elevation(1))
        d2.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.e_to = self._field(d2, "结束日期（可空）", width=14)
        d3 = tk.Frame(row3, bg=self.palette.elevation(1))
        d3.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.e_max = self._field(d3, "本次最多取多少条（0=不限）", width=12)
        self.e_max.insert(0, "0")
        d4 = tk.Frame(row3, bg=self.palette.elevation(1))
        d4.pack(side="left", fill="x", expand=True)
        self.e_label = self._field(d4, "本次检索备注名（可空）", width=14)

        # ---- 操作行 ----
        ops = tk.Frame(page, bg=p.token("surface"))
        ops.pack(fill="x", padx=24, pady=(4, 8))
        self.btn_estimate = M3Button(ops, self.palette, "预估数量", variant="outlined",
                                     icon=ICONS["chart"], command=self._do_estimate, fonts=self.fonts)
        self.btn_estimate.pack(side="left", padx=(0, 8))
        self.btn_start = M3Button(ops, self.palette, "开始爬取", variant="filled",
                                  icon=ICONS["download"], command=self._do_crawl, fonts=self.fonts)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_stop = M3Button(ops, self.palette, "安全停止", variant="tonal",
                                 icon=ICONS["stop"], command=self._do_stop, fonts=self.fonts)
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_stop.set_enabled(False)
        self.btn_reindex = M3Button(ops, self.palette, "重建索引", variant="text",
                                    icon=ICONS["refresh"], command=self._do_reindex, fonts=self.fonts)
        self.btn_reindex.pack(side="left")

        # ---- 进度 + 日志 ----
        prog_card = self._card(page, "")
        prog_card.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        self.progress = M3Progress(prog_card.body, self.palette, self.fonts)
        self.progress.pack(fill="x", pady=(0, 8))

        self.log = tk.Text(
            prog_card.body, height=12, wrap="word", relief="flat", bd=0,
            bg=p.token("surface_container_lowest"), fg=p.token("on_surface"),
            font=self.fonts.get("body_small", mono=True, size_delta=-1),
            padx=12, pady=10, highlightthickness=1,
            highlightbackground=p.token("outline_variant"), state="disabled",
        )
        vsb = ttk.Scrollbar(prog_card.body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=vsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._log_tag_colors()

    def _log_tag_colors(self) -> None:
        p = self.palette
        try:
            self.log.tag_configure("error", foreground=p.token("error"))
            self.log.tag_configure("ok", foreground=p.token("primary"))
            self.log.tag_configure("warn", foreground=p.token("tertiary"))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 页面：检索

    def _build_library_page(self, page: tk.Frame, hint: str) -> None:
        p = self.palette
        self._page_header(page, "数据检索", hint)

        filt = self._card(page, "")
        filt.pack(fill="x", padx=24, pady=(0, 12))

        row1 = tk.Frame(filt.body, bg=self.palette.elevation(1))
        row1.pack(fill="x")
        self.e_kw = self._field(row1, "关键词", width=30)

        scope_box = tk.Frame(row1, bg=self.palette.elevation(1))
        scope_box.pack(side="left", padx=(10, 0))
        tk.Label(scope_box, text="搜索范围", bg=self.palette.elevation(1),
                 fg=p.token("on_surface_variant"), font=self.fonts.get("label_medium"),
                 anchor="w").pack(anchor="w")
        self.scope_var = tk.StringVar(value="all")
        scope = ttk.Combobox(
            scope_box, textvariable=self.scope_var, state="readonly", width=12,
            values=["all", "title", "abstract", "name", "id"],
            font=self.fonts.get("body_medium"),
        )
        scope.pack(pady=(3, 0), ipady=4)

        row2 = tk.Frame(filt.body, bg=self.palette.elevation(1))
        row2.pack(fill="x", pady=(10, 0))
        self.f_source = self._combo_field(row2, "来源", ["（全部）", "openfda", "pubmed"], 14)
        self.f_substance = self._combo_field(row2, "物质名", ["（全部）"], 20)
        self.f_journal = self._combo_field(row2, "期刊", ["（全部）"], 24)
        self.f_year = self._combo_field(row2, "年份", ["（全部）"], 10)

        row3 = tk.Frame(filt.body, bg=self.palette.elevation(1))
        row3.pack(fill="x", pady=(10, 0))
        self.f_mesh = self._combo_field(row3, "MeSH 主题词", ["（全部）"], 26)
        self.f_pubtype = self._combo_field(row3, "出版类型", ["（全部）"], 20)

        row4 = tk.Frame(filt.body, bg=self.palette.elevation(1))
        row4.pack(fill="x", pady=(10, 0))
        self.v_abs = tk.BooleanVar(value=False)
        self.v_doi = tk.BooleanVar(value=False)
        self.v_all = tk.BooleanVar(value=False)
        for var, text in ((self.v_abs, "仅含有摘要"), (self.v_doi, "仅含 DOI"), (self.v_all, "关键词须全部命中")):
            tk.Checkbutton(
                row4, text=text, variable=var, bg=self.palette.elevation(1),
                fg=p.token("on_surface"), selectcolor=p.token("surface_container_highest"),
                activebackground=self.palette.elevation(1),
                activeforeground=p.token("on_surface"),
                font=self.fonts.get("body_small"), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 16))

        ops = tk.Frame(page, bg=p.token("surface"))
        ops.pack(fill="x", padx=24, pady=(0, 8))
        M3Button(ops, self.palette, "检索", variant="filled", icon=ICONS["search"],
                 command=self._do_search, fonts=self.fonts).pack(side="left", padx=(0, 8))
        M3Button(ops, self.palette, "清空条件", variant="text", command=self._clear_filters,
                 fonts=self.fonts).pack(side="left", padx=(0, 8))
        M3Button(ops, self.palette, "导出结果 Excel", variant="outlined", icon=ICONS["export"],
                 command=self._export_results, fonts=self.fonts).pack(side="left", padx=(0, 8))
        M3Button(ops, self.palette, "打开数据目录", variant="text", icon=ICONS["folder"],
                 command=self._open_library, fonts=self.fonts).pack(side="left")
        self.lbl_hits = tk.Label(ops, text="", bg=p.token("surface"),
                                 fg=p.token("on_surface_variant"),
                                 font=self.fonts.get("body_small"))
        self.lbl_hits.pack(side="right")

        table_card = self._card(page, "")
        table_card.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        self.table = ResultTable(table_card.body, self.palette, self.fonts)
        self.table.pack(fill="both", expand=True)

    def _combo_field(self, parent: tk.Frame, label: str,
                     values: Sequence[str], width: int) -> ttk.Combobox:
        p = self.palette
        box = tk.Frame(parent, bg=self.palette.elevation(1))
        box.pack(side="left", padx=(0, 10))
        tk.Label(box, text=label, bg=self.palette.elevation(1),
                 fg=p.token("on_surface_variant"), font=self.fonts.get("label_medium"),
                 anchor="w").pack(anchor="w")
        cb = ttk.Combobox(box, values=list(values), width=width, state="readonly",
                          font=self.fonts.get("body_medium"))
        cb.set(values[0] if values else "")
        cb.pack(pady=(3, 0), ipady=4)
        return cb

    # ------------------------------------------------------------------ 页面：统计

    def _build_stats_page(self, page: tk.Frame, hint: str) -> None:
        p = self.palette
        self._page_header(page, "数据统计", hint)
        card = self._card(page, "")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        self.stats_text = tk.Text(
            card.body, wrap="word", relief="flat", bd=0,
            bg=p.token("surface_container_lowest"), fg=p.token("on_surface"),
            font=self.fonts.get("body_medium"), padx=16, pady=14,
            highlightthickness=1, highlightbackground=p.token("outline_variant"),
        )
        vsb = ttk.Scrollbar(card.body, orient="vertical", command=self.stats_text.yview)
        self.stats_text.configure(yscrollcommand=vsb.set)
        self.stats_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    # ------------------------------------------------------------------ 页面：设置

    def _build_settings_page(self, page: tk.Frame, hint: str) -> None:
        p = self.palette
        self._page_header(page, "设置", hint)

        wrap = tk.Frame(page, bg=p.token("surface"))
        wrap.pack(fill="both", expand=True, padx=24, pady=(0, 18))

        cred = self._card(wrap, "API 凭据")
        cred.pack(fill="x", pady=(0, 12))
        tk.Label(cred.body,
                 text="openFDA 未鉴权每天仅 1000 次请求；有 key 可到 120,000 次/天（差 120 倍）。\n"
                      "NCBI 无 key 限 3 请求/秒，有 key 可到 10 请求/秒。\n"
                      "凭据保存在用户目录，不会写进项目文件夹。",
                 bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left").pack(anchor="w", pady=(0, 10))
        self.e_ofda_key = self._field(cred.body, "openFDA API key")
        self.e_pm_key = self._field(cred.body, "NCBI API key")
        self.e_pm_mail = self._field(cred.body, "联系邮箱（NCBI 建议填写）")
        self.e_ofda_key.insert(0, str(self.cfg.get("openfda_api_key") or ""))
        self.e_pm_key.insert(0, str(self.cfg.get("pubmed_api_key") or ""))
        self.e_pm_mail.insert(0, str(self.cfg.get("pubmed_email") or ""))
        tk.Label(cred.body,
                 text="申请：openFDA → open.fda.gov/apis/authentication/　·　"
                      "NCBI → account.ncbi.nlm.nih.gov → Account settings → API Key Management",
                 bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left",
                 wraplength=900).pack(anchor="w")

        rate = self._card(wrap, "速率与目录")
        rate.pack(fill="x", pady=(0, 12))
        row = tk.Frame(rate.body, bg=self.palette.elevation(1))
        row.pack(fill="x")
        self.e_rps = self._field(row, "每秒请求数", width=10)
        self.e_rps.insert(0, str(self.cfg.get("requests_per_second") or 3.0))
        self.e_out = self._field(row, "数据目录")
        self.e_out.insert(0, str(self.cfg.get("output_dir") or ""))
        tk.Label(rate.body,
                 text="⚠️ 调高速率会被上游封 IP。openFDA 每分钟上限 240 次（两侧都是），"
                      "PubMed 无 key 每秒 3 次。程序命中限流会自动退避，不必手动降速。",
                 bg=self.palette.elevation(1), fg=p.token("tertiary"),
                 font=self.fonts.get("body_small"), anchor="w", justify="left",
                 wraplength=900).pack(anchor="w")

        ops = tk.Frame(wrap, bg=p.token("surface"))
        ops.pack(fill="x", pady=(4, 10))
        M3Button(ops, self.palette, "保存设置", variant="filled", icon=ICONS["check"],
                 command=self._save_settings, fonts=self.fonts).pack(side="left", padx=(0, 8))
        M3Button(ops, self.palette, "运行自检", variant="outlined", icon=ICONS["lab"],
                 command=self._run_selftest, fonts=self.fonts).pack(side="left", padx=(0, 8))
        M3Button(ops, self.palette, "清空续爬状态", variant="text", icon=ICONS["trash"],
                 command=self._reset_state, fonts=self.fonts).pack(side="left")

        info = self._card(wrap, "关于")
        info.pack(fill="x")
        tk.Label(
            info.body,
            text=f"{APP_TITLE}　v{APP_VERSION}\n\n"
                 "数据来源：\n"
                 "  · openFDA（美国 FDA 开放数据，公有领域 CC0）\n"
                 "  · PubMed / NCBI E-utilities\n"
                 "  · DailyMed（NLM）\n\n"
                 "重要限制：\n"
                 f"  · PubMed 单次查询硬上限为 {PUBMED_MAX_RETMAX:,} 条（实测值，官方文档写的 10000 是错的），\n"
                 "    超出后程序会按时间自动切分（年→月→日）。\n"
                 f"  · openFDA 可达窗口为 {OPENFDA_WINDOW:,} 条，超出后自动改用游标滚动。\n"
                 "  · openFDA 查无结果返回 HTTP 404，这不是故障，程序已正确处理。\n"
                 "  · 数据未经 FDA 校验，不得用于医疗决策。\n"
                 "  · PubMed 摘要可能受版权保护，超合理使用范围的再分发需权利人许可。",
            bg=self.palette.elevation(1), fg=p.token("on_surface_variant"),
            font=self.fonts.get("body_small"), anchor="w", justify="left",
        ).pack(anchor="w")

    # ------------------------------------------------------------------ 日志与进度

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _append_log(self, msg: str) -> None:
        tag = ""
        if msg.startswith("✗") or "[错误]" in msg or "❌" in msg:
            tag = "error"
        elif msg.startswith("✓") or "[✓]" in msg:
            tag = "ok"
        elif msg.startswith("⚠") or "[warn]" in msg or "[!]" in msg:
            tag = "warn"
        try:
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass

    def _log(self, msg: str = "") -> None:
        self.log_queue.put(msg)

    # ------------------------------------------------------------------ 动作

    def _selected_plan(self) -> List[Dict[str, Any]]:
        plan: List[Dict[str, Any]] = []
        search = self.e_search.get().strip()
        label = self.e_label.get().strip()
        picked = [ds for ds, var in self.ds_vars.items() if var.get()]
        if picked and search:
            for ds in picked:
                plan.append({
                    "source": "openfda", "dataset": ds,
                    "search": search, "label": label or search,
                })
        elif picked and not search:
            self._log("⚠ 勾选了 FDA 数据集但没有填查询表达式，已跳过 FDA 部分。")
        term = self.e_term.get().strip()
        if self.pubmed_chip_var.get() and term:
            plan.append({"source": "pubmed", "term": term})
        return plan

    def _apply_dates_to_cfg(self) -> None:
        self.cfg["date_from"] = self.e_from.get().strip()
        self.cfg["date_to"] = self.e_to.get().strip()

    def _do_estimate(self) -> None:
        plan = self._selected_plan()
        if not plan:
            messagebox.showwarning("没有可预估的内容",
                                   "请勾选数据源并填写查询表达式。", parent=self.root)
            return
        self._apply_dates_to_cfg()

        def work() -> None:
            from pharma_crawler import (DateRange, HttpClient, OpenFDAClient,
                                        PubMedClient)
            self._log("── 预估数量（不下载）──")
            http = HttpClient(self.cfg, verbose=False)
            ofda = OpenFDAClient(http, self.cfg, verbose=False)
            pm = PubMedClient(self.cfg, self.cfg, verbose=False)
            rng = DateRange.from_cfg(self.cfg)
            total = 0
            for t in plan:
                try:
                    if t["source"] == "openfda":
                        ds = t["dataset"]
                        nice = OPENFDA_ENDPOINTS.get(ds, {}).get("label", ds)
                        n = ofda.count_total(ds, t["search"])
                        total += n
                        self._log(f"  FDA · {nice}：{n:,} 条")
                        if n > OPENFDA_WINDOW:
                            self._log(f"      ⚠ 超过 {OPENFDA_WINDOW:,} 条窗口，将自动改用游标滚动")
                    else:
                        n = pm.count(t["term"])
                        total += n
                        self._log(f"  PubMed：{n:,} 篇")
                        if n > PUBMED_MAX_RETMAX:
                            self._log(f"      ⚠ 超过 {PUBMED_MAX_RETMAX:,} 条上限，将自动按时间切分")
                except Exception as exc:
                    self._log(f"  ✗ {t.get('search') or t.get('term')}：{exc}")
            self._log(f"合计：{total:,} 条")
            self._log(f"网络统计：{http.summary()}")
            self._log("")

        threading.Thread(target=work, daemon=True).start()

    def _do_crawl(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在运行", "已有爬取任务在进行中。", parent=self.root)
            return
        plan = self._selected_plan()
        if not plan:
            messagebox.showwarning(
                "没有可爬取的内容",
                "请至少勾选一个数据源，并填写对应的查询表达式。",
                parent=self.root,
            )
            return
        self._apply_dates_to_cfg()
        try:
            max_records = int(self.e_max.get().strip() or "0")
        except ValueError:
            max_records = 0

        self.btn_start.set_enabled(False)
        self.btn_stop.set_enabled(True)
        self.btn_estimate.set_enabled(False)
        self.progress.reset()
        self._log("")
        self._log("═" * 60)
        self._log(f"开始爬取　{now_iso()}")
        for t in plan:
            if t["source"] == "openfda":
                nice = OPENFDA_ENDPOINTS.get(str(t["dataset"]), {}).get("label", t["dataset"])
                self._log(f"  ▸ FDA · {nice} ← {truncate_display(t['search'], 60)}")
            else:
                self._log(f"  ▸ PubMed ← {truncate_display(t['term'], 60)}")
        self._log("")

        def work() -> None:
            session = CrawlSession(
                self.cfg, self.lib, verbose=False,
                progress_cb=lambda payload: self.root.after(0, self._on_progress, payload),
            )
            self.session = session
            try:
                summary = session.run(plan, max_records=max_records)
                self._log("")
                self._log(f"✓ 完成：获取 {summary['fetched']:,} 条 · 入库 {summary['stored']:,} 条")
                if summary["failed"]:
                    self._log(f"⚠ 失败 {summary['failed']:,} 条")
                self._log(f"  耗时 {magnitude_seconds(summary['elapsed'])}")
                self._log(f"  {summary['http']}")
                if summary.get("stopped"):
                    self._log("  [已安全停止] 下次运行会从断点继续。")
                if summary["stored"]:
                    self._log("")
                    self._log("正在重建索引与导出…")
                    res = self.lib.rebuild_all(self.cfg)
                    self._log(f"  记录总数 {res['records']:,}")
                    for key in ("csv", "excel", "sqlite", "markdown", "ris", "medline"):
                        if key in res:
                            self._log(f"  {key} → {res[key]}")
                    if "excel_error" in res:
                        self._log(f"  ⚠ Excel 导出失败：{res['excel_error']}")
            except Exception as exc:
                self._log(f"✗ 出错：{exc}")
            finally:
                self.root.after(0, self._crawl_finished)

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_progress(self, payload: Dict[str, Any]) -> None:
        got = int(payload.get("got") or payload.get("fetched") or 0)
        total = int(payload.get("total") or 0)
        stored = int(payload.get("stored") or 0)
        self.progress.set(got, total, f"已获取 {got:,} · 入库 {stored:,}")
        msg = payload.get("message")
        if msg:
            self._log(f"    {msg}")

    def _crawl_finished(self) -> None:
        self.btn_start.set_enabled(True)
        self.btn_stop.set_enabled(False)
        self.btn_estimate.set_enabled(True)
        self.progress.done()
        self._load_filter_options()

    def _do_stop(self) -> None:
        if self.session:
            self.session.stop()
            self._log("⚠ 已请求安全停止，正在收尾（已抓到的数据会正常保存）…")
            self.btn_stop.set_enabled(False)

    def _do_reindex(self) -> None:
        def work() -> None:
            self._log("── 重建索引 ──")
            try:
                res = self.lib.rebuild_all(self.cfg)
                self._log(f"  记录总数 {res['records']:,}")
                for key in ("csv", "excel", "sqlite", "markdown", "ris", "medline"):
                    if key in res:
                        self._log(f"  {key} → {res[key]}")
                self._log("✓ 完成")
            except Exception as exc:
                self._log(f"✗ 失败：{exc}")

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ 检索

    def _load_filter_options(self) -> None:
        recs = self.lib.load_records()

        def fill(cb: ttk.Combobox, field: str, prev: str, limit: int = 300) -> None:
            from pharma_crawler import histogram
            vals = ["（全部）"] + [str(k) for k, _ in histogram(recs, field)[:limit]]
            cb.configure(values=vals)
            cb.set(prev if prev in vals else "（全部）")

        fill(self.f_source, "source", self.f_source.get() or "（全部）")
        fill(self.f_substance, "substance_name", self.f_substance.get() or "（全部）")
        fill(self.f_journal, "journal", self.f_journal.get() or "（全部）")
        fill(self.f_year, "year", self.f_year.get() or "（全部）")
        fill(self.f_mesh, "mesh", self.f_mesh.get() or "（全部）")
        fill(self.f_pubtype, "pubtypes", self.f_pubtype.get() or "（全部）")

    def _do_search(self) -> None:
        recs = self.lib.load_records()
        if not recs:
            messagebox.showinfo("还没有数据", "请先到「爬取」页抓取数据。", parent=self.root)
            return

        def pick(cb: ttk.Combobox) -> List[str]:
            v = cb.get().strip()
            return [] if v in ("", "（全部）") else [v]

        hits = search_records(
            recs,
            keyword=self.e_kw.get().strip(),
            sources=pick(self.f_source),
            substances=pick(self.f_substance),
            journals=pick(self.f_journal),
            years=pick(self.f_year),
            mesh=pick(self.f_mesh),
            pubtypes=pick(self.f_pubtype),
            field_scope=self.scope_var.get(),
            has_abstract=self.v_abs.get(),
            has_doi=self.v_doi.get(),
            match_all=self.v_all.get(),
        )
        self.table.set_rows(hits)
        self.result_rows = hits
        self.lbl_hits.configure(
            text=f"命中 {len(hits):,} 条（库内共 {len(recs):,} 条）"
        )

    def _clear_filters(self) -> None:
        self.e_kw.delete(0, "end")
        for cb in (self.f_source, self.f_substance, self.f_journal,
                   self.f_year, self.f_mesh, self.f_pubtype):
            cb.set("（全部）")
        self.v_abs.set(False)
        self.v_doi.set(False)
        self.v_all.set(False)
        self.scope_var.set("all")
        self.lbl_hits.configure(text="")

    def _export_results(self) -> None:
        if not self.result_rows:
            messagebox.showinfo("没有结果", "请先执行一次检索。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出检索结果",
            defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")],
            initialfile=f"pharma_search_{time.strftime('%Y%m%d_%H%M')}.xlsx",
        )
        if not path:
            return
        try:
            from pharma_crawler import write_xlsx
            write_xlsx(
                Path(path),
                [("检索结果", EXPORT_FIELDS, [Library._row(r) for r in self.result_rows])],
            )
            messagebox.showinfo("导出完成", f"已导出 {len(self.result_rows):,} 条到：\n{path}",
                                parent=self.root)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def _open_library(self) -> None:
        try:
            target = str(self.lib.root)
            self.lib.ensure()
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    # ------------------------------------------------------------------ 统计

    def _refresh_stats(self) -> None:
        st = self.lib.stats()
        lines = [
            f"记录总数　{st['total']:,}（{format_size(st['size'])}）",
            f"含摘要　　{st['with_abstract']:,}",
            f"含 DOI　　{st['with_doi']:,}",
            "",
        ]
        if st["by_source"]:
            lines.append("按来源")
            label_of = {"openfda": "FDA 药品数据", "pubmed": "PubMed 文献"}
            for k, v in sorted(st["by_source"].items(), key=lambda x: -x[1]):
                lines.append(f"  {label_of.get(k, k):<18} {v:>10,}")
            lines.append("")
        if st["by_dataset"]:
            lines.append("按数据集")
            for k, v in sorted(st["by_dataset"].items(), key=lambda x: -x[1]):
                nice = OPENFDA_ENDPOINTS.get(k, {}).get("label", k)
                lines.append(f"  {nice:<22} {v:>10,}")
            lines.append("")
        if st["by_year"]:
            lines.append("按年份（前 25）")
            top = sorted(st["by_year"], reverse=True)[:25]
            peak = max((st["by_year"][y] for y in top), default=1)
            for y in top:
                n = st["by_year"][y]
                bar = "█" * max(1, int(n / peak * 32))
                lines.append(f"  {y}　{bar} {n:,}")
            lines.append("")
        lines.append(f"记录文件　{st['records_path']}")
        if st["total"] == 0:
            lines.append("")
            lines.append("（还没有数据。到「爬取」页开始第一次抓取。）")

        self.stats_text.configure(state="normal")
        self.stats_text.delete("1.0", "end")
        self.stats_text.insert("1.0", "\n".join(lines))
        self.stats_text.configure(state="disabled")

    # ------------------------------------------------------------------ 设置

    def _save_settings(self) -> None:
        from pharma_crawler import CRED_FILE, save_credential_store

        ofda = self.e_ofda_key.get().strip()
        pmk = self.e_pm_key.get().strip()
        mail = self.e_pm_mail.get().strip()
        try:
            save_credential_store(openfda_api_key=ofda, pubmed_api_key=pmk, pubmed_email=mail)
            self.cfg["openfda_api_key"] = ofda
            self.cfg["pubmed_api_key"] = pmk
            self.cfg["pubmed_email"] = mail
        except Exception as exc:
            messagebox.showerror("保存凭据失败", str(exc), parent=self.root)
            return

        out_dir = self.e_out.get().strip()
        if out_dir and out_dir != str(self.cfg.get("output_dir")):
            # 走与 CLI 同一个解析函数：用户在输入框里填相对路径（如 library）时，
            # 必须相对程序目录解析，而不是相对启动 GUI 时的工作目录。
            from pharma_crawler import resolve_output_dir
            resolved = resolve_output_dir(out_dir, self.program_dir)
            self.cfg["output_dir"] = str(resolved)
            self.lib = Library(resolved)
            self.lib.ensure()
            self.e_out.delete(0, "end")
            self.e_out.insert(0, str(resolved))
        try:
            rps = float(self.e_rps.get().strip() or "3.0")
            self.cfg["requests_per_second"] = rps
        except ValueError:
            pass

        # 同步写回 config.json（凭据除外，凭据只进用户目录）
        try:
            import json
            path = self.program_dir / "config.json"
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
            for k in ("output_dir", "requests_per_second"):
                data[k] = self.cfg.get(k)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

        messagebox.showinfo(
            "已保存",
            f"设置已保存。\n\n凭据写入：{CRED_FILE}\n（不会写进项目文件夹）\n\n"
            f"数据目录：{self.cfg.get('output_dir')}\n"
            f"速率：{self.cfg.get('requests_per_second')} 请求/秒",
            parent=self.root,
        )
        self._log(f"✓ 设置已保存 · {now_iso()}")

    def _run_selftest(self) -> None:
        def work() -> None:
            self._log("")
            self._log("── 自检 ──")
            from pharma_crawler import (DateRange, HttpClient, OpenFDAClient,
                                        PubMedClient, parse_pubmed_articles)
            http = HttpClient(self.cfg, verbose=False)
            try:
                ofda = OpenFDAClient(http, self.cfg, verbose=False)
                n = ofda.count_total("label", 'openfda.brand_name:"aspirin"')
                self._log(f"  ✓ openFDA 可达（阿司匹林标签 {n:,} 条）")
                res = ofda.query("label", search='openfda.brand_name:"zzzznotadrugzzzz"', limit=1)
                if res.get("empty"):
                    self._log("  ✓ 404 空结果语义正确（不会误判为故障）")
            except Exception as exc:
                self._log(f"  ✗ openFDA：{exc}")
            try:
                pm = PubMedClient(http, self.cfg, verbose=False)
                cnt = pm.count("aspirin[Title/Abstract]")
                self._log(f"  ✓ PubMed 可达（{cnt:,} 篇）")
                xml = pm.efetch(ids=["33301246"], retmode="xml")
                parsed = parse_pubmed_articles(xml)
                if parsed:
                    p = parsed[0]
                    bits = [k for k, v in (("标题", p.get("title")), ("摘要", p.get("abstract")),
                                           ("作者", p.get("authors")), ("MeSH", p.get("mesh")),
                                           ("DOI", p.get("doi"))) if v]
                    self._log(f"  ✓ XML 解析链路正常（{'/'.join(bits)}）")
                else:
                    self._log("  ✗ XML 解析出 0 篇 —— 上游可能已改版")
            except Exception as exc:
                self._log(f"  ✗ PubMed：{exc}")
            self._log(f"  网络统计：{http.summary()}")
            self._log("")

        threading.Thread(target=work, daemon=True).start()

    def _reset_state(self) -> None:
        if not messagebox.askyesno(
            "确认清空",
            "清空断点续爬状态？\n\n已入库的数据不会删除，只是下次爬取会重新遍历一遍。",
            parent=self.root,
        ):
            return
        from pharma_crawler import IdCache, SegmentStore, SessionState
        SessionState(self.lib.state_dir / "session.json").reset()
        SegmentStore(self.lib.state_dir / "segments.json").reset()
        IdCache(self.lib.state_dir / "pubmed_ids.json").reset()
        self._log("✓ 已清空断点续爬状态")

    # ------------------------------------------------------------------ 主题

    def _toggle_theme(self) -> None:
        self.palette = self.palette.toggle()
        self.cfg["gui_dark"] = self.palette.dark
        self.theme_btn.set_text(
            "深色" if not self.palette.dark else "浅色",
            ICONS["dark"] if not self.palette.dark else ICONS["light"],
        )
        self._apply_theme()

    def _apply_theme(self) -> None:
        """全量重绘。切换明暗时走一遍，保证没有残留的旧色。"""
        p = self.palette
        self.root.configure(bg=p.token("surface"))
        self._recolor(self.root)
        self._paint_nav()
        self._log_tag_colors()
        if hasattr(self, "table"):
            self.table.palette = p
            self.table.apply_style()
        if hasattr(self, "progress"):
            self.progress.palette = p
            self.progress.redraw()

    def _recolor(self, widget: tk.Misc) -> None:
        """递归重上色。

        tkinter 没有全局样式表，切主题只能遍历整棵树。
        带 ``palette_role`` 标记的控件按标记上色，其余按类型走默认映射。
        """
        p = self.palette
        for child in widget.winfo_children():
            try:
                if isinstance(child, (M3Button, M3Chip, M3Progress)):
                    child.palette = p
                    child.refresh()
                elif isinstance(child, RoundedFrame):
                    child.palette = p
                    child.set_colors(
                        fill=p.token("surface_container_low"),
                        outline=p.token("outline_variant"),
                    )
                elif isinstance(child, tk.Canvas):
                    child.configure(bg=p.token("surface"))
                elif isinstance(child, tk.Text):
                    child.configure(
                        bg=p.token("surface_container_lowest"),
                        fg=p.token("on_surface"),
                        highlightbackground=p.token("outline_variant"),
                        insertbackground=p.token("primary"),
                    )
                elif isinstance(child, tk.Entry):
                    child.configure(
                        bg=p.token("surface_container_highest"),
                        fg=p.token("on_surface"),
                        highlightbackground=p.token("outline_variant"),
                        highlightcolor=p.token("primary"),
                        insertbackground=p.token("primary"),
                    )
                elif isinstance(child, tk.Label):
                    child.configure(bg=p.token("surface"))
                elif isinstance(child, tk.Frame):
                    # 顶层页面用 surface，卡片内部用 elevation(1)
                    bg = child.cget("bg")
                    if bg in (m3.LIGHT["surface_container_low"], m3.DARK["surface_container_low"],
                              m3.LIGHT["surface"], m3.DARK["surface"]):
                        child.configure(bg=p.token("surface"))
                    else:
                        child.configure(bg=p.elevation(1))
                elif isinstance(child, tk.Checkbutton):
                    child.configure(
                        bg=p.elevation(1), fg=p.token("on_surface"),
                        selectcolor=p.token("surface_container_highest"),
                        activebackground=p.elevation(1),
                    )
            except tk.TclError:
                pass
            self._recolor(child)

    # ------------------------------------------------------------------ 生命周期

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "爬取进行中",
                "爬取任务还在运行。现在退出会安全停止并保存已抓到的数据。\n\n确定退出吗？",
                parent=self.root,
            ):
                return
            if self.session:
                self.session.stop()
            time.sleep(0.4)
        self.root.destroy()

    def run(self) -> int:
        self._log(f"{APP_TITLE}　v{APP_VERSION}")
        self._log(f"数据目录：{self.lib.root}")
        self._log("提示：先在「设置」页填入 API key，可大幅提高每日配额。")
        self._log("")
        self._load_filter_options()
        self.root.mainloop()
        return 0


class M3Progress(tk.Frame):
    """M3 线性进度条（高 4dp、圆角端点）。

    不确定总量时走循环动画 —— 这比显示一条卡在 0% 的死条诚实得多。
    """

    def __init__(self, master: tk.Misc, palette: Palette, fonts: FontBook) -> None:
        super().__init__(master, bg=palette.elevation(1))
        self.palette = palette
        self.fonts = fonts
        self.canvas = tk.Canvas(self, height=6, highlightthickness=0, bd=0,
                                bg=palette.elevation(1))
        self.canvas.pack(fill="x")
        self.label = tk.Label(self, text="", bg=palette.elevation(1),
                              fg=palette.token("on_surface_variant"),
                              font=fonts.get("body_small"), anchor="w")
        self.label.pack(fill="x", pady=(4, 0))
        self.value = 0.0
        self._anim = 0.0
        self._running = False
        self.canvas.bind("<Configure>", lambda e: self.redraw())

    def reset(self) -> None:
        self.value = 0.0
        self._running = True
        self.label.configure(text="准备中…")
        self.redraw()
        self._tick()

    def set(self, got: int, total: int, text: str = "") -> None:
        self._running = True
        self.value = (got / total) if total > 0 else 0.0
        self.value = max(0.0, min(1.0, self.value))
        if text:
            self.label.configure(text=text)
        self.redraw()

    def done(self) -> None:
        self._running = False
        self.value = 1.0 if self.value > 0 else 0.0
        self.redraw()

    def _tick(self) -> None:
        if not self._running:
            return
        self._anim = (self._anim + 0.035) % 1.0
        self.redraw()
        try:
            self.after(40, self._tick)
        except tk.TclError:
            pass

    def redraw(self) -> None:
        p = self.palette
        try:
            if not self.canvas.winfo_exists():
                return
            self.canvas.delete("all")
        except tk.TclError:
            return
        w = self.canvas.winfo_width()
        h = 6
        if w <= 4:
            return
        # 轨道
        self.canvas.create_polygon(
            rounded_rect_points(0, 0, w, h, h / 2), smooth=True,
            fill=p.token("secondary_container"), outline="",
        )
        if self._running and self.value <= 0:
            # 不确定态：一段来回跑的滑块
            seg = max(40, w * 0.28)
            x = -seg + (w + seg) * (1 - abs(1 - 2 * self._anim))
            self.canvas.create_polygon(
                rounded_rect_points(max(0, x), 0, min(w, x + seg), h, h / 2), smooth=True,
                fill=p.token("primary"), outline="",
            )
        elif self.value > 0:
            filled = max(h, w * self.value)
            self.canvas.create_polygon(
                rounded_rect_points(0, 0, filled, h, h / 2), smooth=True,
                fill=p.token("primary"), outline="",
            )

    def refresh(self) -> None:
        self.configure(bg=self.palette.elevation(1))
        self.canvas.configure(bg=self.palette.elevation(1))
        self.label.configure(bg=self.palette.elevation(1),
                             fg=self.palette.token("on_surface_variant"))
        self.redraw()


# --------------------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------------------


def launch(cfg: Dict[str, Any], lib: Library, program_dir: Path) -> int:
    try:
        app = PharmaGUI(cfg, lib, program_dir)
    except tk.TclError as exc:
        print(f"无法启动图形界面（{exc}）。请改用命令行模式。")
        return 1
    return app.run()
