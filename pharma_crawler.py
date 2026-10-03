#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PharmaCrawler 核心库。

药学数据爬虫：FDA（openFDA / DailyMed / drugs@FDA）与 PubMed（NCBI E-utilities）。

设计原则
--------
1. **纯标准库**。不依赖 requests / pandas / openpyxl，双击即用，打包体积小。
2. **接口结构校验**。上游改版时大声报错，绝不静默假成功。
   （pixiv 爬虫踩过的坑：接口变了但代码不报错，结果"爬完了"却是空的。）
3. **限流自适应**。命中限流就指数退避拉长间隔，长期正常再慢慢收回。
4. **断点续爬**。状态落盘到 ``_state/``，中断后重跑不重复请求。
5. **404 不等故障**。openFDA 查无结果返回 404 NOT_FOUND，必须当空结果处理。

本模块同时是 CLI 入口与 GUI 后端，见 ``main()``。
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

APP_NAME = "PharmaCrawler"
APP_VERSION = "1.0.0"
APP_TITLE = "药学数据爬虫 PharmaCrawler"
APP_SUBTITLE = "FDA 药品数据 · PubMed 药学文献"

PROGRAM_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------------------
# 常量：数据源定义
# --------------------------------------------------------------------------------------

OPENFDA_BASE = "https://api.fda.gov"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PMC_S3_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"

#: openFDA 药品类端点。`date_field` 用于日期切分；`sort_key` 用于 search_after 游标滚动。
OPENFDA_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "label": {
        "path": "/drug/label.json",
        "label": "药品说明书标签",
        "date_field": "effective_time",
        "daily": False,
        "id_field": "id",
        "note": "SPL 标签，2009 至今，每周更新",
    },
    "enforcement": {
        "path": "/drug/enforcement.json",
        "label": "药品召回",
        "date_field": "report_date",
        "daily": False,
        "id_field": "recall_number",
        "note": "每周更新",
    },
    "event": {
        "path": "/drug/event.json",
        "label": "不良事件",
        "date_field": "receivedate",
        "daily": False,
        "id_field": "safetyreportid",
        "note": "FAERS，百万级，数据量最大",
    },
    "ndc": {
        "path": "/drug/ndc.json",
        "label": "NDC 目录",
        "date_field": "",
        "daily": True,
        "id_field": "product_ndc",
        "note": "每日更新",
    },
    "drugsfda": {
        "path": "/drug/drugsfda.json",
        "label": "drugs@FDA 批准信息",
        "date_field": "",
        "daily": True,
        "id_field": "application_number",
        "note": "周一至周五每日更新，1939 至今",
    },
    "shortages": {
        "path": "/drug/shortages.json",
        "label": "药品短缺",
        "date_field": "",
        "daily": True,
        "id_field": "package_ndc",
        "note": "每日更新。注意端点是复数 shortages",
    },
    "orangebook": {
        "path": "/drug/orangebook.json",
        "label": "橙皮书",
        "date_field": "",
        "daily": False,
        "id_field": "application_number",
        "note": "每月更新",
    },
}

#: 各端点的日期字段格式（用于构造区间查询）。
#: 不良事件用纯数字 YYYYMMDD，召回用带短横线的 YYYY-MM-DD —— 实测确认，不能统一。
OPENFDA_DATE_FORMATS: Dict[str, str] = {
    "effective_time": "compact",   # YYYYMMDD
    "report_date": "dashed",       # YYYY-MM-DD
    "receivedate": "compact",      # YYYYMMDD
}

#: PubMed 常用字段标签（供 GUI 下拉与查询构造器使用）。
PUBMED_FIELD_TAGS: List[Tuple[str, str]] = [
    ("[All Fields]", "全部字段"),
    ("[Title]", "标题"),
    ("[Title/Abstract]", "标题与摘要"),
    ("[MeSH Terms]", "MeSH 主题词"),
    ("[MeSH Major Topic]", "MeSH 主要主题词"),
    ("[MeSH Subheading]", "MeSH 副主题词"),
    ("[Author]", "作者"),
    ("[Author - Corporate]", "团体作者"),
    ("[Journal]", "期刊"),
    ("[Affiliation]", "作者单位"),
    ("[Publication Type]", "出版类型"),
    ("[Date - Publication]", "出版日期"),
    ("[Date - Entry]", "入库日期"),
    ("[Substance Name]", "物质名称"),
    ("[Pharmacological Action]", "药理作用"),
    ("[EC/RN Number]", "EC/RN 号"),
    ("[Language]", "语言"),
    ("[Text Word]", "全文词"),
    ("[UID]", "PMID"),
]

#: PubMed 分段单元上限 —— 实测 9,999，不是 10,000。
#: 详见 docs/DATA_SOURCE_NOTES.md 第 4.2 节。
PUBMED_MAX_RETMAX = 9999
PUBMED_MAX_RETSTART = 9998

#: openFDA 分页上限
OPENFDA_MAX_LIMIT = 1000
OPENFDA_MAX_SKIP = 25000
#: 可达窗口：limit + skip
OPENFDA_WINDOW = OPENFDA_MAX_LIMIT + OPENFDA_MAX_SKIP

DESKTOP_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 {APP_NAME}/{APP_VERSION}"
)


# --------------------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------------------


class CrawlError(Exception):
    """所有可预期爬取错误的基类。"""


class HttpError(CrawlError):
    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        detail = ""
        if body:
            try:
                detail = " · " + body.decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
        super().__init__(f"HTTP {status}: {url}{detail}")


class RateLimited(CrawlError):
    """明确命中限流（429 或上游限流响应体）。"""


class ApiShapeError(CrawlError):
    """接口返回的结构与预期不符 —— 多半是上游改版了。"""


class EmptyResult(CrawlError):
    """查无结果。openFDA 用 HTTP 404 表达，属正常情况而非故障。"""


def _shape_msg(what: str, want: Any, got: Any) -> str:
    return (
        f"接口结构校验失败：{what} 期望 {want!r}，实际拿到 {type(got).__name__} {str(got)[:180]!r}。"
        f"上游很可能已改版；请查看 docs/DATA_SOURCE_NOTES.md，"
        f"并可在 config.json 的 endpoints 中覆盖接口地址。"
    )


# --------------------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------------------


def out(msg: str = "") -> None:
    """统一的标准输出入口（带 flush，便于 GUI 实时读取与日志重定向）。"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 老终端不支持 UTF-8
        enc = sys.stdout.encoding or "utf-8"
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)


def native_path(p: "os.PathLike[str] | str") -> str:
    """把 pathlib 路径转成原生字符串。

    Windows 上超过 260 字符的长路径需要 ``\\\\?\\`` 前缀，否则 os 层会报错。
    本项目会按标签/关键词建立深层目录，很容易踩到，所以统一走这里。
    """
    s = str(p)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + s[2:]
    if len(s) >= 240 and os.path.isabs(s):
        return "\\\\?\\" + s
    return s


def parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是", "开")


def format_size(num: int) -> str:
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def display_width(text: Any) -> int:
    """终端显示宽度。CJK 字符占 2 列，字符串里的日文药名/中文期刊名全靠这个对齐。"""
    w = 0
    for ch in str(text):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad_display(text: Any, width: int, align: str = "left") -> str:
    text = str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def truncate_display(text: Any, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，保证不会把 CJK 字符截成半个。"""
    text = str(text)
    if display_width(text) <= width:
        return text
    budget = width - display_width(ellipsis)
    acc = 0
    res = []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc + w > budget:
            break
        res.append(ch)
        acc += w
    return "".join(res) + ellipsis


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso() -> str:
    return _dt.date.today().isoformat()


def ensure_dir(p: "os.PathLike[str] | str") -> None:
    os.makedirs(native_path(p), exist_ok=True)


def file_size(p: "os.PathLike[str] | str") -> int:
    try:
        return os.path.getsize(native_path(p))
    except OSError:
        return 0


def atomic_write_json(p: "os.PathLike[str] | str", obj: Any) -> None:
    """原子写 JSON：先写临时文件再 replace，避免中断留下半截文件。"""
    p = Path(p)
    ensure_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(native_path(tmp), native_path(p))


def read_json(p: "os.PathLike[str] | str", default: Any = None) -> Any:
    try:
        with open(native_path(p), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    res: List[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


def sanitize_component(text: Any, max_len: int = 80, fallback: str = "") -> str:
    """把任意文本变成合法的单层目录/文件名。

    去掉 Windows 保留字符与结尾的点/空格，并规避 CON/PRN 等设备名。
    """
    s = str(text or "")
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s.rstrip(".")
    if not s:
        s = fallback
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if s.upper() in reserved:
        s = "_" + s
    return s or (fallback or "unnamed")


def safe_join(*parts: Any) -> str:
    """跨平台安全拼路径（用于 URL 片段拼接，不做转义）。"""
    return "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))


def sha1_short(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def jdump(obj: Any, *, max_len: int = 0) -> str:
    """紧凑 JSON 序列化，用于把嵌套字段塞进单元格。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(obj)
    if max_len and len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def flatten(value: Any, sep: str = " | ", limit: int = 12) -> str:
    """把可能是标量/列表/嵌套字典的字段拍平成可读字符串。

    openFDA 的 ``openfda`` 里所有值都是数组（哪怕只有一个元素），
    统一走这里可以省掉满屏的 ``[0]`` 判断。
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts = [f"{k}={flatten(v, sep, limit)}" for k, v in list(value.items())[:limit]]
        return sep.join(p for p in parts if p)
    if isinstance(value, (list, tuple, set)):
        parts = [flatten(v, sep, limit) for v in list(value)[:limit]]
        return sep.join(p for p in parts if p)
    return str(value)


def first_of(value: Any, default: str = "") -> str:
    """取 openFDA 数组字段的第一个值。

    ⚠️ openFDA 的 ``openfda.*`` 值**永远**是数组，即使只有一个元素。
    直接 ``record["openfda"]["brand_name"]`` 会拿到 list，写进 CSV 就成了
    ``['TYLENOL']``。这个函数是唯一正确的取法。
    """
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        for v in value:
            if v not in (None, ""):
                return str(v)
        return default
    if isinstance(value, dict):
        return flatten(value)
    return str(value)


def join_list(value: Any, sep: str = "|") -> str:
    """把列表字段拼成单格字符串（CSV/Excel 友好）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return sep.join(str(v) for v in value if v not in (None, ""))
    return str(value)


# --------------------------------------------------------------------------------------
# 日期处理
# --------------------------------------------------------------------------------------


def parse_user_date(text: Any, mode: str = "start") -> Optional[_dt.date]:
    """解析用户输入的日期，容忍多种写法。

    认：``2024-01-31`` / ``2024/01/31`` / ``2024年1月31日`` / ``20240131`` / ``2024-01`` / ``2024``。
    mode="start" 时缺省部分补最小，mode="end" 补最大。
    """
    if text in (None, ""):
        return None
    if isinstance(text, _dt.datetime):
        return text.date()
    if isinstance(text, _dt.date):
        return text

    s = str(text).strip()
    if not s:
        return None
    s = s.replace("年", "-").replace("月", "-").replace("日", "").strip()
    s = re.sub(r"[./]", "-", s)
    s = re.sub(r"\s+.*$", "", s)  # 丢掉时间部分
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", s)
    if not m:
        m2 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
        if not m2:
            return None
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    else:
        y = int(m.group(1))
        if m.group(2) is None:
            mo, d = (1, 1) if mode == "start" else (12, 31)
        elif m.group(3) is None:
            if mode == "start":
                mo, d = int(m.group(2)), 1
            else:
                mo = int(m.group(2))
                d = _last_day_of_month(y, mo)
        else:
            mo, d = int(m.group(2)), int(m.group(3))
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def _last_day_of_month(y: int, m: int) -> int:
    if m == 12:
        nxt = _dt.date(y + 1, 1, 1)
    else:
        nxt = _dt.date(y, m + 1, 1)
    return (nxt - _dt.timedelta(days=1)).day


class DateRange:
    """闭区间日期范围。两端都可以为空（不限制）。"""

    def __init__(self, start: Optional[_dt.date] = None, end: Optional[_dt.date] = None) -> None:
        self.start = start
        self.end = end

    def active(self) -> bool:
        return self.start is not None or self.end is not None

    def describe(self) -> str:
        if not self.active():
            return "不限时间"
        a = self.start.isoformat() if self.start else "最早"
        b = self.end.isoformat() if self.end else "至今"
        return f"{a} ~ {b}"

    def contains(self, d: Optional[_dt.date]) -> bool:
        if not self.active():
            return True
        if d is None:
            return False
        if self.start and d < self.start:
            return False
        if self.end and d > self.end:
            return False
        return True

    @staticmethod
    def from_cfg(cfg: Dict[str, Any]) -> "DateRange":
        return DateRange(
            parse_user_date(cfg.get("date_from"), "start"),
            parse_user_date(cfg.get("date_to"), "end"),
        )


def date_to_compact(d: _dt.date) -> str:
    """YYYYMMDD —— openFDA 不良事件/标签的日期格式。"""
    return d.strftime("%Y%m%d")


def date_to_dashed(d: _dt.date) -> str:
    """YYYY-MM-DD —— openFDA 召回的日期格式。"""
    return d.strftime("%Y-%m-%d")


def date_to_slash(d: _dt.date) -> str:
    """YYYY/MM/DD —— PubMed mindate/maxdate 的格式（斜杠，实测确认）。"""
    return d.strftime("%Y/%m/%d")


def format_range_for(date_field: str, start: _dt.date, end: _dt.date) -> str:
    """按端点要求的格式输出日期。绝不统一 —— 各端点格式不同。"""
    style = OPENFDA_DATE_FORMATS.get(date_field, "compact")
    if style == "dashed":
        return f"{date_to_dashed(start)}+TO+{date_to_dashed(end)}"
    return f"{date_to_compact(start)}+TO+{date_to_compact(end)}"


def split_span(start: _dt.date, end: _dt.date) -> Optional[Tuple[Tuple[_dt.date, _dt.date], Tuple[_dt.date, _dt.date]]]:
    """把区间二分。区间只有一天时返回 None（无法再分）。"""
    if end <= start:
        return None
    days = (end - start).days
    if days < 1:
        return None
    mid = start + _dt.timedelta(days=days // 2)
    if mid <= start or mid >= end:
        return None
    return (start, mid), (mid + _dt.timedelta(days=1), end)


def year_chunks(start: _dt.date, end: _dt.date) -> List[Tuple[_dt.date, _dt.date]]:
    """把区间按自然年切开。PubMed 分段切分的首选粒度。"""
    chunks: List[Tuple[_dt.date, _dt.date]] = []
    cur = _dt.date(start.year, 1, 1)
    if cur < start:
        cur = start
    while cur <= end:
        yend = _dt.date(cur.year, 12, 31)
        stop = min(yend, end)
        chunks.append((cur, stop))
        if stop >= end:
            break
        cur = _dt.date(cur.year + 1, 1, 1)
    return chunks


def month_chunks(start: _dt.date, end: _dt.date) -> List[Tuple[_dt.date, _dt.date]]:
    """把区间按自然月切开（年切片仍然过载时用）。"""
    chunks: List[Tuple[_dt.date, _dt.date]] = []
    cur = start
    while cur <= end:
        last = _last_day_of_month(cur.year, cur.month)
        stop = min(_dt.date(cur.year, cur.month, last), end)
        chunks.append((cur, stop))
        if stop >= end:
            break
        cur = stop + _dt.timedelta(days=1)
    return chunks


# --------------------------------------------------------------------------------------
# 数量级的中文表达（用于预估提示）
# --------------------------------------------------------------------------------------


def magnitude_cn(n: float, unit: str = "") -> str:
    """把数字说成人话：12345 -> 约 1.2 万。"""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return f"—{unit}"
    if v < 0:
        return f"—{unit}"
    if v < 10000:
        s = f"{v:,.0f}"
    elif v < 100_000_000:
        s = f"约 {v / 10000:.1f} 万"
    else:
        s = f"约 {v / 100_000_000:.2f} 亿"
    return s + unit


def magnitude_bytes(num_bytes: float) -> str:
    return format_size(int(num_bytes))


def magnitude_seconds(sec: float) -> str:
    sec = float(sec)
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec / 60:.0f} 分钟"
    if sec < 86400:
        return f"{sec / 3600:.1f} 小时"
    return f"{sec / 86400:.1f} 天"


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------

#: 各数据源的默认速率（请求/秒）。
#:
#: 这些数字**不是拍脑袋**，而是照抄上游明文规定：
#:   - openFDA：240 请求/分钟 = 4/s，取 3.5 留余量；未鉴权还有 1000 次/天的硬上限。
#:   - PubMed 无 key：官方明说 3/s；有 key：10/s。
#:   - DailyMed：无公开限制，保守取 1/s。
DEFAULT_RPS = {
    "openfda": 3.5,
    "pubmed": 2.8,      # 3/s 是上限，取 2.8 避免贴边触发
    "pubmed_key": 9.0,  # 有 key 时 10/s 上限
    "dailymed": 1.0,
}


def default_config(program_dir: Path) -> Dict[str, Any]:
    return {
        "_说明": "PharmaCrawler 配置文件。改完保存即生效；命令行参数优先级更高，会覆盖同名项。",
        "_输出目录": "所有数据都放在这个目录下",
        "output_dir": str(program_dir / "library"),

        "_openfda_key": (
            "强烈建议申请（免费）：https://open.fda.gov/apis/authentication/ 。"
            "不填：240 次/分钟、1000 次/天；填了：240 次/分钟、120000 次/天 —— 差 120 倍。"
            "留空也能跑，只是每天爬不了多少。"
        ),
        "openfda_api_key": "",

        "_pubmed_key": (
            "NCBI API key（可选但推荐）：登录 NCBI -> Account settings -> API Key Management 创建。"
            "不填 3 请求/秒，填了 10 请求/秒。"
            "注意：重新生成 key 会让旧 key 立刻失效。"
        ),
        "pubmed_api_key": "",
        "_pubmed_email_tool": (
            "强烈建议填写。NCBI 明文规定：只有把 tool 和 email 注册到 eutils@ncbi.nlm.nih.gov "
            "才能在封 IP 后解封；光在请求里带上是不够的。"
        ),
        "pubmed_email": "",
        "pubmed_tool": APP_NAME,

        "_代理": "国内直连不通时填写，如 http://127.0.0.1:7890（Clash）或 http://127.0.0.1:10809（v2ray）",
        "proxy": "",
        "verify_ssl": True,

        "_速率": (
            "各数据源的请求间隔由 requests_per_second 控制；命中限流会自动指数退避。"
            "不要盲目调高 —— 上游会封 IP。"
        ),
        "requests_per_second": 3.0,
        "max_retries": 5,
        "timeout": 60,
        "concurrency": 4,

        "_分页": (
            "openFDA：limit 上限 1000，skip 上限 25000（可达窗口 26000 条），"
            "超出请用 search_after 游标或日期切分。"
            "PubMed：retmax 上限 9999（不是 10000！），retstart 上限 9998。"
        ),
        "page_size_openfda": 1000,
        "page_size_pubmed": 500,
        "pubmed_batch_size": 200,

        "_采集范围": "全部 / 仅某个端点。GUI 与 CLI 均可覆盖。",
        "openfda_datasets": ["label", "enforcement", "ndc", "drugsfda", "shortages", "orangebook"],

        "_PubMed 默认查询": "留空即由命令行/GUI 传入",
        "pubmed_term": "",
        "pubmed_sort": "pub_date",
        "pubmed_datetype": "pdat",
        "pubmed_mindate": "",
        "pubmed_maxdate": "",
        "pubmed_reldate": "",
        "pubmed_retmode": "xml",

        "_时间范围": "只收这个时间范围内发布的记录。格式 2024-01-31 或 2024/01/31。留空=不限制。",
        "date_from": "",
        "date_to": "",

        "_突破上限的策略": "auto=自动判断；year=按年切分；month=按月切分；none=不切分（到上限就停）",
        "segment_strategy": "auto",

        "_每次最多处理多少条": "0 = 不限制（推荐）。填非 0 会硬性截断，容易误以为爬不全。",
        "max_records_per_run": 0,

        "_导出": "爬完自动重建索引；Excel 导出为纯标准库实现的多工作表 xlsx",
        "auto_export": True,
        "export_excel": True,
        "export_csv": True,
        "export_sqlite": True,
        "export_markdown": True,
        "export_ris": True,
        "export_bibtex": False,
        "export_medline": True,

        "_正文抓取": "PMC 全文走 S3（旧 oa.fcgi 已于 2026-08 下线，见 docs/）",
        "fetch_pmc_fulltext": False,
        "download_pdf": False,

        "_分类规则": "按治疗领域/物质名/期刊建立硬链接分类目录（同盘不额外占空间）",
        "max_tags_per_record": 8,
        "make_substance_links": True,
        "make_journal_links": True,
        "make_query_links": True,
        "tag_stopwords": [
            "human", "humans", "male", "female", "adult", "aged", "middle aged",
            "Animals", "Mice", "Rats", "English Abstract", "Review", "Letter",
        ],

        "user_agent": DESKTOP_UA,
    }


def find_config_path(program_dir: Path, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for name in ("config.json", "pharma_config.json"):
        p = program_dir / name
        if p.exists():
            return p
    return None


def load_config(program_dir: Path, explicit: Optional[str]) -> Tuple[Dict[str, Any], Optional[Path]]:
    """读配置，缺失项用默认值补齐。用户配置里的未知键也会保留。"""
    cfg = default_config(program_dir)
    path = find_config_path(program_dir, explicit)
    if path:
        data = read_json(path, {}) or {}
        if isinstance(data, dict):
            cfg.update(data)
    return cfg, path


# --------------------------------------------------------------------------------------
# HTTP 客户端（自适应限流 + 重试 + 断点续传下载）
# --------------------------------------------------------------------------------------


class HttpClient:
    """带限流与重试的 HTTP 客户端。

    自适应限流的必要性：全量爬取动辄上万次请求、跑几十小时，
    匀速请求必然撞上限流。命中后如果只是"重试"，会被反复掐；
    正确做法是**拉长间隔进入冷却**，等上游消气再逐步恢复。
    没有这个机制，长跑一定会静默漏数据。
    """

    def __init__(self, cfg: Dict[str, Any], *, verbose: bool = True) -> None:
        self.cfg = cfg
        self.verbose = verbose
        self.ua = str(cfg.get("user_agent") or DESKTOP_UA)
        self.timeout = float(cfg.get("timeout") or 60)
        self.max_retries = int(cfg.get("max_retries") or 5)

        rps = float(cfg.get("requests_per_second") or 3.0)
        self.base_interval = 1.0 / rps if rps > 0 else 0.0
        self.min_interval = self.base_interval

        self._limit_until = 0.0
        self._ok_streak = 0
        self._lock = threading.Lock()
        self._last_request = 0.0

        proxy = str(cfg.get("proxy") or "").strip()
        handlers: List[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if not parse_bool(cfg.get("verify_ssl"), True):
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

        self.stats: Dict[str, Any] = {
            "requests": 0, "bytes": 0, "retries": 0, "errors": 0,
            "rate_limited": 0, "total_wait": 0.0, "not_found": 0,
        }

    # ---- 限流 ----

    def _throttle(self) -> None:
        if self.min_interval <= 0 and self._limit_until <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._limit_until > now:
                wait = self._limit_until - now
                self.stats["total_wait"] += wait
                if self.verbose:
                    out(f"    [限流冷却] 等待 {wait:.0f}s（已自适应降速到 {self.min_interval:.2f}s/请求）")
                time.sleep(wait)
                now = time.monotonic()
            delta = now - self._last_request
            wait = self.min_interval - delta
            if wait > 0:
                self.stats["total_wait"] += wait
                time.sleep(wait)
            self._last_request = time.monotonic()

    def note_rate_limited(self, cooldown: float = 0.0) -> float:
        """记录一次限流：拉长间隔并进入冷却。返回冷却秒数。"""
        with self._lock:
            self.stats["rate_limited"] += 1
            n = int(self.stats["rate_limited"])
            self.min_interval = min(30.0, max(self.base_interval, 0.5) * (2 ** min(n, 6)))
            cool = cooldown or min(600.0, 20.0 * (2 ** min(n - 1, 5)))
            self._limit_until = time.monotonic() + cool
            self._ok_streak = 0
            return cool

    def note_ok(self) -> None:
        """连续顺利足够多次后，把间隔慢慢收回基线（避免永久龟速）。"""
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= 50 and self.min_interval > self.base_interval:
                self.min_interval = max(self.base_interval, self.min_interval / 2.0)
                self._ok_streak = 0

    def in_cooldown(self) -> bool:
        return self._limit_until > time.monotonic()

    def set_rate(self, rps: float) -> None:
        """按数据源切换速率（PubMed 有 key / 无 key 差别很大）。"""
        if rps <= 0:
            return
        with self._lock:
            self.base_interval = 1.0 / rps
            self.min_interval = min(self.min_interval, self.base_interval) if self.min_interval else self.base_interval
            if self.min_interval < self.base_interval:
                self.min_interval = self.base_interval

    # ---- 请求 ----

    def _read_body(self, resp: Any) -> bytes:
        """分块读取响应体，容忍连接提前中断。

        为什么不能直接用 ``resp.read()``：openFDA 的响应动辄几十 MB
        （limit=1000 的标签数据实测 28MB），长连接在传输中途被掐断是常态，
        ``http.client`` 此时抛 ``IncompleteRead``。

        而 ``IncompleteRead`` 有个有用的性质：它**携带着已经读到的部分数据**。
        直接重试整次请求既慢又浪费配额；更好的做法是——
        若已读到的部分能解析成合法 JSON（即数据其实完整，只是 Content-Length 对不上），
        就照常使用；否则把已读部分保留下来，让上层重试。
        """
        chunks: List[bytes] = []
        try:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception as exc:
            # IncompleteRead 携带部分数据；其他异常也尽量把已读部分带出去
            partial = getattr(exc, "partial", None)
            if partial:
                chunks.append(partial)
            body = b"".join(chunks)
            if body:
                if self.verbose:
                    out(f"    [warn] 响应传输中断（{type(exc).__name__}），"
                        f"已收到 {format_size(len(body))}，尝试按已收数据解析")
                return body
            raise
        return b"".join(chunks)

    def _headers(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        h = {"User-Agent": self.ua, "Accept": "application/json, application/xml, text/xml, */*"}
        if extra:
            h.update(extra)
        return h

    def request(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        method: Optional[str] = None,
        allow_404: bool = False,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """返回 (status, body, response_headers)。

        ``allow_404=True`` 时 404 返回 (404, b"", hdrs) 而不抛异常 ——
        openFDA 用 404 表达"查无结果"，这是正常路径，不是故障。
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    body = self._read_body(resp)
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    with self._lock:
                        self.stats["requests"] += 1
                        self.stats["bytes"] += len(body)
                    self.note_ok()
                    return int(resp.status), body, hdrs
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                body = b""
                try:
                    body = exc.read(65536)
                except Exception:
                    pass
                hdrs = {}
                try:
                    hdrs = {k.lower(): v for k, v in exc.headers.items()}
                except Exception:
                    pass
                try:
                    exc.close()
                except Exception:
                    pass

                if status == 404 and allow_404:
                    with self._lock:
                        self.stats["not_found"] += 1
                    return 404, body, hdrs

                # 429 是明确的限流信号，进入冷却
                if status == 429:
                    wait = max(10.0, min(120.0, (2.0 ** attempt) + random.uniform(0, 2.0)))
                    self.note_rate_limited(cooldown=wait)
                    self.stats["retries"] += 1
                    if attempt < self.max_retries:
                        if self.verbose:
                            out(f"    [warn] HTTP 429 限流，冷却 {wait:.0f}s 后重试 ({attempt}/{self.max_retries})")
                        time.sleep(wait)
                        last_exc = HttpError(status, url, body)
                        continue

                if status in (500, 502, 503, 504):
                    wait = min(60.0, (2.0 ** attempt) + random.uniform(0, 1.5))
                    self.stats["retries"] += 1
                    if attempt < self.max_retries:
                        if self.verbose:
                            out(f"    [warn] HTTP {status}，{wait:.1f}s 后重试 ({attempt}/{self.max_retries})")
                        time.sleep(wait)
                        last_exc = HttpError(status, url, body)
                        continue

                self.stats["errors"] += 1
                raise HttpError(status, url, body)

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.stats["retries"] += 1
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(30.0, (2.0 ** attempt) + random.uniform(0, 1.0))
                    if self.verbose:
                        out(f"    [warn] 网络异常 {type(exc).__name__}，{wait:.1f}s 后重试 ({attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                self.stats["errors"] += 1
                raise CrawlError(f"网络请求失败: {url} ({exc})") from exc

        raise CrawlError(f"重试 {self.max_retries} 次仍失败: {url} ({last_exc})")

    def get_raw(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        return self.request(url, headers=headers, allow_404=allow_404)

    def get_text(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
    ) -> Tuple[Optional[str], Dict[str, str]]:
        """取文本。返回 (文本或 None, 响应头)。None 表示 404 空结果。"""
        status, body, hdrs = self.request(url, headers=headers, allow_404=allow_404)
        if status == 404:
            return None, hdrs
        return body.decode("utf-8", "replace"), hdrs

    def download(self, url: str, dest: Path, *, headers: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
        """下载到文件，支持断点续传与 gzip 自动解压判定。

        返回 (是否成功, 说明)。已存在且非空的文件直接跳过。
        """
        dest = Path(dest)
        ensure_dir(dest.parent)
        if os.path.exists(native_path(dest)) and file_size(dest) > 0:
            return True, f"已存在，跳过（{format_size(file_size(dest))}）"

        tmp = dest.with_name(dest.name + ".part")
        req_headers = dict(headers or {})
        resume_from = file_size(tmp)
        if resume_from > 0:
            req_headers["Range"] = f"bytes={resume_from}-"

        self._throttle()
        req = urllib.request.Request(url, headers=self._headers(req_headers))
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                status = int(resp.status)
                mode = "ab" if (resume_from > 0 and status == 206) else "wb"
                if mode == "wb":
                    resume_from = 0
                written = resume_from
                with open(native_path(tmp), mode) as fh:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                with self._lock:
                    self.stats["bytes"] += written - resume_from
                    self.stats["requests"] += 1
                self.note_ok()
            os.replace(native_path(tmp), native_path(dest))
            return True, f"已下载 {format_size(file_size(dest))}"
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:
                pass
            if int(exc.code) == 416 and file_size(tmp) > 0:  # Range 越界 = 其实已经下完
                os.replace(native_path(tmp), native_path(dest))
                return True, f"已下载 {format_size(file_size(dest))}"
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"网络异常 {type(exc).__name__}: {exc}"

    def summary(self) -> str:
        s = self.stats
        return (
            f"请求 {s['requests']} 次 · 传输 {format_size(int(s['bytes']))} · "
            f"重试 {s['retries']} · 限流 {s['rate_limited']} · 空结果 {s['not_found']} · "
            f"等待 {magnitude_seconds(float(s['total_wait']))}"
        )


# --------------------------------------------------------------------------------------
# 容错 JSON 解析
# --------------------------------------------------------------------------------------


def _loads_tolerant(text: str) -> Any:
    """解析 JSON，对"传输被截断"的情况尽量抢救。

    背景：openFDA 单次响应可达几十 MB，长连接中途被掐断是常态。
    截断的结果是 JSON 尾部不完整（``...},{...`` 缺收尾括号）。
    这种响应里**前面的记录其实是完整可用的**，直接丢弃等于白跑一趟还浪费配额。

    抢救策略：从尾部逐步回退，尝试把已完整的部分补上收尾符号后解析。
    只在确实能救回 ``results`` 数组时才返回，救不回就返回 None 让上层重试。
    """
    if not text or not text.strip():
        return None
    s = text.strip()

    # 正常路径
    try:
        return json.loads(s)
    except ValueError:
        pass

    if not s.startswith("{"):
        return None

    # 截断抢救：找到最后一个完整对象的边界，补上括号
    for back in range(0, min(len(s), 400000), 1):
        probe = s if back == 0 else s[:-back]
        probe = probe.rstrip()
        if not probe or not probe.endswith("}"):
            continue
        # 逐级补闭合符号，覆盖 results 数组被截断的典型形态
        for suffix in ("]}", "}]}", "]}}", "}]}}", "}}", "}"):
            candidate = probe + suffix
            try:
                obj = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("results"):
                return obj
        # 每退 2000 字符试一次即可，逐字符试太慢
        if back % 2000 == 1999:
            continue
    return None


# --------------------------------------------------------------------------------------
# openFDA 客户端
# --------------------------------------------------------------------------------------


class OpenFDAClient:
    """openFDA 药品端点客户端。

    三个必须记住的坑（都已实测确认，详见 docs/DATA_SOURCE_NOTES.md）：

    1. **404 ≠ 故障**。查无结果时 openFDA 返回 HTTP 404 + ``NOT_FOUND``。
       若把它当错误重试，空查询会白白重试 5 次，还会污染错误统计。
    2. **端点名是复数**。``/drug/shortages.json`` 存在，``/drug/shortage.json`` 不存在。
    3. **skip 上限 25000**，``limit`` 上限 1000，可达窗口 26000。
       超过就得用 ``search_after`` 游标或日期切分，硬翻页会被 400 拒掉。
    """

    def __init__(self, http: HttpClient, cfg: Dict[str, Any], *, verbose: bool = True) -> None:
        self.http = http
        self.cfg = cfg
        self.verbose = verbose
        self.base = str(cfg.get("openfda_base") or OPENFDA_BASE).rstrip("/")
        self.api_key = str(cfg.get("openfda_api_key") or "").strip()
        self.page_size = max(1, min(OPENFDA_MAX_LIMIT, int(cfg.get("page_size_openfda") or OPENFDA_MAX_LIMIT)))
        self.http.set_rate(float(cfg.get("requests_per_second") or 3.0))

    # ---- URL 构造 ----

    def endpoint_path(self, dataset: str) -> str:
        if dataset not in OPENFDA_ENDPOINTS:
            raise CrawlError(
                f"未知的 openFDA 数据集: {dataset!r}；"
                f"可用：{', '.join(sorted(OPENFDA_ENDPOINTS))}"
            )
        override = (self.cfg.get("endpoints") or {}).get(f"openfda_{dataset}")
        return str(override) if override else OPENFDA_ENDPOINTS[dataset]["path"]

    def build_url(self, dataset: str, params: Sequence[Tuple[str, str]]) -> str:
        """拼 URL。

        ⚠️ ``api_key`` **必须排在第一位** —— openFDA 文档明确要求，
        放后面虽然多数情况也能用，但不要赌。
        """
        pairs: List[Tuple[str, str]] = []
        if self.api_key:
            pairs.append(("api_key", self.api_key))
        pairs.extend(params)
        qs = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="+[]{}:\"*")
        return f"{self.base}{self.endpoint_path(dataset)}?{qs}"

    # ---- 解析 ----

    @staticmethod
    def parse_payload(dataset: str, text: str, hdrs: Dict[str, str]) -> Dict[str, Any]:
        """解析响应，做结构校验。

        校验的意义：openFDA 改版时若静默返回了别的结构，
        没有校验的爬虫会"成功爬到 0 条"，然后安静地写一份空索引。
        这里宁可大声报错。
        """
        payload = _loads_tolerant(text)
        if payload is None:
            raise ApiShapeError(
                f"openFDA {dataset} 返回的不是合法 JSON：{text[:200]!r}"
            )
        if not isinstance(payload, dict):
            raise ApiShapeError(_shape_msg(f"openFDA {dataset} 响应", "dict", payload))

        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            msg = str(err.get("message") or "")
            if code == "NOT_FOUND":
                raise EmptyResult(f"openFDA {dataset} 查无结果")
            raise CrawlError(f"openFDA {dataset} 报错 [{code}]: {msg}")

        results = payload.get("results")
        if results is None:
            raise ApiShapeError(_shape_msg(f"openFDA {dataset} 的 results", "list", payload))
        if not isinstance(results, list):
            raise ApiShapeError(_shape_msg(f"openFDA {dataset} 的 results", "list", results))
        return payload

    # ---- 单次查询 ----

    def query(
        self,
        dataset: str,
        *,
        search: str = "",
        limit: Optional[int] = None,
        skip: Optional[int] = None,
        sort: str = "",
        search_after: str = "",
        count: str = "",
    ) -> Dict[str, Any]:
        """发一次查询。返回 ``{"results": [...], "meta": {...}, "total": int, "next": str|None}``。

        查无结果时返回空 results 而不是抛异常 —— 上层不该为了空结果写 try/except。
        """
        params: List[Tuple[str, str]] = []
        if search:
            params.append(("search", search))
        if sort:
            params.append(("sort", sort))
        if count:
            params.append(("count", count))
        if search_after:
            # ⚠️ skip 与 search_after 互斥，同时传会被 400 拒掉
            params.append(("search_after", search_after))
        else:
            if skip:
                if skip > OPENFDA_MAX_SKIP:
                    raise CrawlError(
                        f"skip={skip} 超过 openFDA 上限 {OPENFDA_MAX_SKIP}；"
                        f"请改用 search_after 游标或日期切分。"
                    )
                params.append(("skip", str(skip)))
        if not count:
            params.append(("limit", str(limit if limit is not None else self.page_size)))

        url = self.build_url(dataset, params)
        status, body, hdrs = self.http.get_raw(url, allow_404=True)

        if status == 404:
            return {"results": [], "meta": {}, "total": 0, "next": None, "empty": True}

        payload = self.parse_payload(dataset, body.decode("utf-8", "replace"), hdrs)

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total = 0
        # ⚠️ count 查询的 meta 里**没有** results 子对象，不能假设它存在
        mres = meta.get("results") if isinstance(meta.get("results"), dict) else {}
        if isinstance(mres, dict):
            try:
                total = int(mres.get("total") or 0)
            except (TypeError, ValueError):
                total = 0

        return {
            "results": payload.get("results") or [],
            "meta": meta,
            "total": total,
            "next": self._next_cursor(hdrs),
            "empty": False,
        }

    @staticmethod
    def _next_cursor(hdrs: Dict[str, str]) -> Optional[str]:
        """从 ``Link: <...>; rel="next"`` 响应头里取出 search_after 游标。

        search_after 是突破 26000 条窗口的正道：跟随这个游标可以滚动任意大小的结果集。
        代价是游标 token 内嵌了排序键，所以 ``sort`` 字段必须**唯一且稳定**。
        """
        link = hdrs.get("link") or ""
        if not link or 'rel="next"' not in link:
            return None
        m = re.search(r"<([^>]+)>", link)
        if not m:
            return None
        try:
            qs = urllib.parse.urlparse(m.group(1)).query
            vals = urllib.parse.parse_qs(qs)
            sa = vals.get("search_after")
            return sa[0] if sa else None
        except Exception:
            return None

    def count_total(self, dataset: str, search: str = "") -> int:
        """只取总数（``limit=1``，超轻量）。用于分段前的探量。"""
        res = self.query(dataset, search=search, limit=1)
        return int(res.get("total") or 0)

    def count_field(self, dataset: str, field: str, search: str = "", limit: int = 100) -> List[Tuple[str, int]]:
        """按字段聚合统计。⚠️ 统计整句要加 ``.exact``，否则会被分词切碎。"""
        res = self.query(dataset, search=search, count=field)
        rows: List[Tuple[str, int]] = []
        for item in res.get("results") or []:
            if isinstance(item, dict) and "term" in item:
                try:
                    rows.append((str(item.get("term")), int(item.get("count") or 0)))
                except (TypeError, ValueError):
                    continue
        rows.sort(key=lambda x: -x[1])
        return rows[:limit]

    # ---- 分页枚举 ----

    def iter_records(
        self,
        dataset: str,
        *,
        search: str = "",
        max_records: int = 0,
        sort: str = "",
        stop_flag: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """流式枚举记录，自动选择分页策略。

        策略选择（按优先级）：
          1. 总数 ≤ 26000  -> 传统 skip/limit 翻页（最简单可靠）
          2. 总数 > 26000  -> search_after 游标滚动（可无限）
          3. 游标不可用    -> 退回 skip 翻页，到 26000 停并**明确告知**

        绝不静默截断：一旦够不到全部数据，必须让用户知道，否则会误以为爬全了。
        """
        total = self.count_total(dataset, search)
        if progress:
            progress({"phase": "probe", "total": total, "dataset": dataset})
        if total == 0:
            return

        limit = self.page_size
        use_cursor = total > OPENFDA_WINDOW

        if use_cursor:
            if self.verbose:
                out(f"    总数 {total:,} 条 > 窗口 {OPENFDA_WINDOW:,}，改用 search_after 游标滚动")
            yield from self._iter_by_cursor(
                dataset, search=search, sort=sort, limit=limit,
                max_records=max_records, stop_flag=stop_flag, progress=progress, total=total,
            )
            return

        yield from self._iter_by_skip(
            dataset, search=search, sort=sort, limit=limit,
            max_records=max_records, stop_flag=stop_flag, progress=progress, total=total,
        )

    def _iter_by_skip(
        self, dataset: str, *, search: str, sort: str, limit: int,
        max_records: int, stop_flag: Optional[Callable[[], bool]],
        progress: Optional[Callable[[Dict[str, Any]], None]], total: int,
    ) -> Iterator[Dict[str, Any]]:
        skip = 0
        emitted = 0
        while True:
            if stop_flag and stop_flag():
                return
            res = self.query(dataset, search=search, limit=limit, skip=skip, sort=sort)
            rows = res.get("results") or []
            if not rows:
                return
            for rec in rows:
                yield rec
                emitted += 1
                if max_records and emitted >= max_records:
                    return
            if progress:
                progress({"phase": "page", "got": emitted, "total": res.get("total") or total,
                          "skip": skip, "dataset": dataset})
            if len(rows) < limit:
                return
            skip += limit
            if skip > OPENFDA_MAX_SKIP:
                if self.verbose:
                    out(
                        f"    [warn] 已达 openFDA skip 上限 {OPENFDA_MAX_SKIP}，"
                        f"本次共取 {emitted:,} 条；剩余数据请用日期切分（--segment year）继续。"
                    )
                return

    def _iter_by_cursor(
        self, dataset: str, *, search: str, sort: str, limit: int,
        max_records: int, stop_flag: Optional[Callable[[], bool]],
        progress: Optional[Callable[[Dict[str, Any]], None]], total: int,
    ) -> Iterator[Dict[str, Any]]:
        """跟随 ``Link: rel="next"`` 游标滚动。

        ⚠️ 游标 token 内嵌排序键，所以排序字段**必须唯一且稳定**；
        否则会漏记录或原地打转。这里对"游标没变化"做了检测，防止死循环。
        """
        cursor = ""
        emitted = 0
        seen_cursors: Set[str] = set()
        pages = 0
        while True:
            if stop_flag and stop_flag():
                return
            res = self.query(dataset, search=search, limit=limit, sort=sort, search_after=cursor)
            rows = res.get("results") or []
            if not rows:
                return
            for rec in rows:
                yield rec
                emitted += 1
                if max_records and emitted >= max_records:
                    return
            pages += 1
            if progress:
                progress({"phase": "page", "got": emitted, "total": total,
                          "skip": emitted, "dataset": dataset, "cursor": True})
            nxt = res.get("next")
            if not nxt:
                return
            if nxt in seen_cursors:
                if self.verbose:
                    out("    [warn] search_after 游标重复，停止滚动以避免死循环")
                return
            seen_cursors.add(nxt)
            cursor = nxt
            if len(rows) < limit:
                return


# --------------------------------------------------------------------------------------
# PubMed 客户端（NCBI E-utilities）
# --------------------------------------------------------------------------------------


def _xml_text(node: Optional[ET.Element], default: str = "") -> str:
    if node is None:
        return default
    return "".join(node.itertext()).strip() or default


class PubMedClient:
    """NCBI E-utilities 客户端。

    ⚠️ 本类围绕两个实测事实设计（官方文档与实测不符，以实测为准）：

    1. **retmax 上限是 9999，不是 10000**；``retstart`` 上限 9998。
       按 10000 切分每片会静默少 1 条 —— 这种 bug 能潜伏几个月。
    2. **超限响应体可能是 2xx**。``{"error":"API rate limit exceeded"}``
       可能跟着 HTTP 200 回来，所以**必须检查响应体**，不能只看状态码。

    9,999 的天花板管的是 **ESearch 的 ID 列举**，不是 **EFetch 的 WebEnv 迭代**。
    所以正确姿势是：把查询切成 <9999 的片，每片存进 History server，
    再用 EFetch 分批抽干 —— 而不是硬翻 retstart。
    """

    def __init__(self, http: HttpClient, cfg: Dict[str, Any], *, verbose: bool = True) -> None:
        self.http = http
        self.cfg = cfg
        self.verbose = verbose
        self.base = str(cfg.get("eutils_base") or EUTILS_BASE).rstrip("/")
        self.api_key = str(cfg.get("pubmed_api_key") or "").strip()
        self.email = str(cfg.get("pubmed_email") or "").strip()
        self.tool = str(cfg.get("pubmed_tool") or APP_NAME).strip() or APP_NAME
        self.batch_size = max(1, min(1000, int(cfg.get("pubmed_batch_size") or 200)))
        self.page_size = max(1, min(PUBMED_MAX_RETMAX, int(cfg.get("page_size_pubmed") or 500)))
        # 官方：无 key 3/s，有 key 10/s。取略低的值避免贴边触发
        self.http.set_rate(DEFAULT_RPS["pubmed_key"] if self.api_key else DEFAULT_RPS["pubmed"])

    # ---- 公共参数 ----

    def _common(self) -> List[Tuple[str, str]]:
        """每次都带的身份参数。

        ``tool`` 与 ``email`` 每请求非必须，但**合规必须**：
        NCBI 只有在你把它们注册到 eutils@ncbi.nlm.nih.gov 之后才肯解封 IP，
        光在请求里带上是不够的。所以无论如何都带。
        """
        params: List[Tuple[str, str]] = []
        if self.tool:
            params.append(("tool", self.tool))
        if self.email:
            params.append(("email", self.email))
        if self.api_key:
            params.append(("api_key", self.api_key))
        return params

    def build_url(self, tool_name: str, params: Sequence[Tuple[str, str]]) -> str:
        """拼 URL。

        ⚠️ NCBI 要求除 ``WebEnv`` 外**所有参数名小写**，
        空格用 ``+``，``"`` 转 ``%22``，``#`` 转 ``%23``。
        """
        qs = urllib.parse.urlencode(list(params), quote_via=urllib.parse.quote, safe="+,:|")
        return f"{self.base}/{tool_name}?{qs}"

    def _check_body(self, text: str, what: str) -> None:
        """检查响应体里的限流信号。

        ⚠️ NCBI 的 ``{"error":"API rate limit exceeded","count":"11"}``
        **可能带 2xx 状态码返回**。只看 HTTP 状态码会漏掉它，
        然后爬虫会以为"查无结果"，安静地少收一批数据。
        """
        if not text:
            return
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except ValueError:
                return
            if isinstance(payload, dict):
                err = payload.get("error")
                if err:
                    msg = str(err)
                    if "rate limit" in msg.lower():
                        cool = self.http.note_rate_limited()
                        raise RateLimited(f"NCBI 限流（{what}）：{msg}；已冷却 {cool:.0f}s")
                    if "cannot be larger than" in msg:
                        raise ApiShapeError(
                            f"NCBI 参数超限（{what}）：{msg} —— "
                            f"retmax 上限 {PUBMED_MAX_RETMAX}，retstart 上限 {PUBMED_MAX_RETSTART}"
                        )
                    raise CrawlError(f"NCBI 报错（{what}）：{msg}")
        if "API rate limit exceeded" in text:
            cool = self.http.note_rate_limited()
            raise RateLimited(f"NCBI 限流（{what}）：已达请求上限；已冷却 {cool:.0f}s")

    # ---- ESearch ----

    def esearch(
        self,
        term: str,
        *,
        retmax: int = 0,
        retstart: int = 0,
        use_history: bool = True,
        sort: str = "",
        datetype: str = "",
        mindate: str = "",
        maxdate: str = "",
        reldate: str = "",
        retmode: str = "json",
        count_only: bool = False,
    ) -> Dict[str, Any]:
        """执行 ESearch。

        返回 ``{"count": int, "ids": [...], "webenv": str, "querykey": str}``。
        """
        retmax = max(0, min(PUBMED_MAX_RETMAX, int(retmax)))
        if retstart > PUBMED_MAX_RETSTART:
            raise CrawlError(
                f"retstart={retstart} 超过 PubMed 上限 {PUBMED_MAX_RETSTART}"
                f"（ESearch 只能列举前 9,999 条）。请改用分段爬取。"
            )

        params: List[Tuple[str, str]] = [("db", "pubmed"), ("term", term), ("retmode", retmode)]
        if count_only:
            params.append(("rettype", "count"))
        params.append(("retmax", str(retmax)))
        if retstart:
            params.append(("retstart", str(retstart)))
        if use_history:
            params.append(("usehistory", "y"))
        if sort:
            params.append(("sort", sort))
        if datetype:
            params.append(("datetype", datetype))
        # ⚠️ mindate/maxdate 必须成对出现
        if mindate:
            params.append(("mindate", mindate))
        if maxdate:
            params.append(("maxdate", maxdate))
        if reldate:
            params.append(("reldate", str(reldate)))
        params.extend(self._common())

        url = self.build_url("esearch.fcgi", params)
        text, _ = self.http.get_text(url)
        if text is None:
            raise CrawlError(f"ESearch 返回 404：{url}")
        self._check_body(text, "esearch")

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ApiShapeError(
                f"ESearch 返回的不是合法 JSON（多半是上游改版）：{text[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ApiShapeError(_shape_msg("ESearch 响应", "dict", payload))

        esr = payload.get("esearchresult")
        if not isinstance(esr, dict):
            raise ApiShapeError(_shape_msg("ESearch 的 esearchresult", "dict", payload))

        try:
            count = int(esr.get("count") or 0)
        except (TypeError, ValueError):
            count = 0

        idlist = esr.get("idlist")
        if idlist is None:
            idlist = []
        if not isinstance(idlist, list):
            raise ApiShapeError(_shape_msg("ESearch 的 idlist", "list", idlist))

        return {
            "count": count,
            "ids": [str(i) for i in idlist],
            "webenv": str(esr.get("webenv") or ""),
            "querykey": str(esr.get("querykey") or ""),
        }

    def count(self, term: str, **kw: Any) -> int:
        """只取命中总数 —— 分段前的探量。"""
        kw.pop("retmax", None)
        got = self.esearch(term, retmax=0, count_only=True, use_history=False, **kw)
        return int(got.get("count") or 0)

    # ---- EFetch ----

    def efetch(
        self,
        *,
        ids: Sequence[str] = (),
        webenv: str = "",
        querykey: str = "",
        retstart: int = 0,
        retmax: int = 200,
        retmode: str = "xml",
        rettype: str = "",
    ) -> str:
        """取完整记录，返回原始 XML/文本。

        两种用法：直接给 ``ids``，或给 ``webenv``+``querykey`` 迭代 History 集。
        后者是突破 9,999 的关键 —— WebEnv 迭代不受 ESearch 的列举上限约束。
        """
        if not ids and not (webenv and querykey):
            raise CrawlError("efetch 需要 ids 或 (webenv + querykey) 之一")

        params: List[Tuple[str, str]] = [("db", "pubmed"), ("retmode", retmode)]
        if rettype:
            params.append(("rettype", rettype))
        if ids:
            # ⚠️ id 列表用逗号分隔且**不能有空格**
            params.append(("id", ",".join(str(i) for i in ids)))
        else:
            params.append(("query_key", str(querykey)))
            params.append(("WebEnv", webenv))  # 这个参数名是大小写敏感的
            params.append(("retstart", str(max(0, int(retstart)))))
            params.append(("retmax", str(max(1, min(PUBMED_MAX_RETMAX, int(retmax))))))
        params.extend(self._common())

        url = self.build_url("efetch.fcgi", params)
        text, _ = self.http.get_text(url)
        if text is None:
            raise CrawlError(f"EFetch 返回 404：{url}")
        self._check_body(text, "efetch")
        return text


# --------------------------------------------------------------------------------------
# PubMed XML 解析
# --------------------------------------------------------------------------------------


def _parse_pubmed_date(node: Optional[ET.Element]) -> Tuple[str, str]:
    """从 ``<PubDate>`` 里取日期。

    返回 (ISO 日期, 原始文本)。⚠️ 日期可能是结构化的
    ``<Year>/<Month>/<Day>``，也可能是自由文本 ``<MedlineDate>``（如 "2020 Jan-Feb"）
    —— 后者没有固定格式，只能尽力解析，解析不出就保留原文。
    """
    if node is None:
        return "", ""
    year = _xml_text(node.find("Year"))
    month = _xml_text(node.find("Month"))
    day = _xml_text(node.find("Day"))
    medline = _xml_text(node.find("MedlineDate"))

    raw = medline or " ".join(p for p in (year, month, day) if p)
    if medline and not year:
        m = re.search(r"(\d{4})", medline)
        year = m.group(1) if m else ""
        mm = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
            medline, re.IGNORECASE,
        )
        month = mm.group(1) if mm else ""

    if not year:
        return "", raw

    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    mo = 1
    ms = str(month).strip().lower()[:3]
    if ms.isdigit():
        mo = max(1, min(12, int(ms)))
    elif ms in months:
        mo = months[ms]
    dd = int(day) if str(day).strip().isdigit() else 1
    try:
        return _dt.date(int(year), mo, dd).isoformat(), raw
    except ValueError:
        return f"{year}", raw


def parse_pubmed_articles(xml_text: str) -> List[Dict[str, Any]]:
    """把 ``PubmedArticleSet`` XML 解析成记录列表。

    字段路径依据实测的 PubMed DTD（pubmed_250101.dtd）。
    对每个字段都做了防御 —— 缺字段是常态，不能让它把整批解析搞崩。
    """
    if not xml_text or not xml_text.strip():
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        # 有些响应带 XML 声明之外的前导空白/噪声，重试去掉首行
        cleaned = xml_text[xml_text.find("<PubmedArticleSet"):]
        if cleaned and cleaned != xml_text:
            try:
                root = ET.fromstring(cleaned)
            except ET.ParseError:
                raise ApiShapeError(f"PubMed 返回的 XML 无法解析：{exc}；开头 {xml_text[:200]!r}") from exc
        else:
            raise ApiShapeError(f"PubMed 返回的 XML 无法解析：{exc}；开头 {xml_text[:200]!r}") from exc

    if root.tag != "PubmedArticleSet":
        # 单篇时根节点可能是 PubmedArticle
        if root.tag == "PubmedArticle":
            articles = [root]
        else:
            raise ApiShapeError(_shape_msg("PubMed XML 根节点", "PubmedArticleSet", root.tag))
    else:
        articles = root.findall("PubmedArticle")

    records: List[Dict[str, Any]] = []
    for art in articles:
        rec = _parse_one_article(art)
        if rec:
            records.append(rec)
    return records


def _parse_one_article(art: ET.Element) -> Optional[Dict[str, Any]]:
    citation = art.find("MedlineCitation")
    if citation is None:
        return None

    pmid = _xml_text(citation.find("PMID"))
    if not pmid:
        return None

    article = citation.find("Article")
    title = _xml_text(article.find("ArticleTitle")) if article is not None else ""

    # ---- 摘要：可重复，且带 Label/NlmCategory（BACKGROUND/METHODS/...）----
    abstract_parts: List[str] = []
    abstract_sections: List[Dict[str, str]] = []
    if article is not None:
        abs_node = article.find("Abstract")
        if abs_node is not None:
            for at in abs_node.findall("AbstractText"):
                txt = _xml_text(at)
                if not txt:
                    continue
                label = at.get("Label") or at.get("NlmCategory") or ""
                abstract_parts.append(f"{label}: {txt}" if label else txt)
                abstract_sections.append({"label": label, "text": txt})

    # ---- 作者：个人作者 + 团体作者 ----
    authors: List[str] = []
    author_details: List[Dict[str, str]] = []
    if article is not None:
        alist = article.find("AuthorList")
        if alist is not None:
            for au in alist.findall("Author"):
                collective = _xml_text(au.find("CollectiveName"))
                if collective:
                    authors.append(collective)
                    author_details.append({"name": collective, "type": "collective"})
                    continue
                last = _xml_text(au.find("LastName"))
                fore = _xml_text(au.find("ForeName")) or _xml_text(au.find("Initials"))
                name = ", ".join(p for p in (last, fore) if p) if last else fore
                if not name:
                    continue
                authors.append(name)
                affs = [_xml_text(a) for a in au.findall("AffiliationInfo/Affiliation")]
                author_details.append({
                    "name": name,
                    "type": "personal",
                    "affiliation": " | ".join(a for a in affs if a),
                })

    # ---- 期刊 ----
    journal_full = journal_abbrev = issn = volume = issue = ""
    pubdate_iso = pubdate_raw = ""
    medline_ta = country = ""
    if article is not None:
        j = article.find("Journal")
        if j is not None:
            journal_full = _xml_text(j.find("Title"))
            journal_abbrev = _xml_text(j.find("ISOAbbreviation"))
            issn = _xml_text(j.find("ISSN"))
            ji = j.find("JournalIssue")
            if ji is not None:
                volume = _xml_text(ji.find("Volume"))
                issue = _xml_text(ji.find("Issue"))
                pubdate_iso, pubdate_raw = _parse_pubmed_date(ji.find("PubDate"))
    mji = citation.find("MedlineJournalInfo")
    if mji is not None:
        medline_ta = _xml_text(mji.find("MedlineTA"))
        country = _xml_text(mji.find("Country"))

    # ---- 页码 ----
    pages = ""
    if article is not None:
        pag = article.find("Pagination")
        if pag is not None:
            pg = _xml_text(pag.find("MedlinePgn"))
            if pg:
                pages = pg
            else:
                sp = _xml_text(pag.find("StartPage"))
                ep = _xml_text(pag.find("EndPage"))
                pages = f"{sp}-{ep}" if sp and ep else (sp or ep)

    # ---- 标识符：DOI 在**两个**独立位置，都要读 ----
    doi = pmcid = ""
    if article is not None:
        for el in article.findall("ELocationID"):
            if (el.get("EIdType") or "").lower() == "doi" and not doi:
                doi = _xml_text(el)
    article_ids: Dict[str, str] = {}
    pdata = art.find("PubmedData")
    if pdata is not None:
        idlist = pdata.find("ArticleIdList")
        if idlist is not None:
            for aid in idlist.findall("ArticleId"):
                t = (aid.get("IdType") or "").lower()
                v = _xml_text(aid)
                if t and v:
                    article_ids[t] = v
                    if t == "doi" and not doi:
                        doi = v
                    elif t == "pmc" and not pmcid:
                        pmcid = v

    # ---- MeSH：带 UI 与 MajorTopicYN ----
    mesh_terms: List[str] = []
    mesh_details: List[Dict[str, Any]] = []
    mhl = citation.find("MeshHeadingList")
    if mhl is not None:
        for mh in mhl.findall("MeshHeading"):
            dn = mh.find("DescriptorName")
            if dn is None:
                continue
            name = _xml_text(dn)
            if not name:
                continue
            quals = [(_xml_text(q), q.get("MajorTopicYN") or "N") for q in mh.findall("QualifierName")]
            mesh_terms.append(name)
            mesh_details.append({
                "term": name,
                "ui": dn.get("UI") or "",
                "major": (dn.get("MajorTopicYN") or "N") == "Y",
                "qualifiers": [q for q, _ in quals if q],
            })

    # ---- 关键词 ----
    keywords = [_xml_text(k) for k in citation.findall("KeywordList/Keyword")]
    keywords = [k for k in keywords if k]

    # ---- 出版类型 ----
    pubtypes: List[str] = []
    if article is not None:
        ptl = article.find("PublicationTypeList")
        if ptl is not None:
            pubtypes = [_xml_text(p) for p in ptl.findall("PublicationType")]
            pubtypes = [p for p in pubtypes if p]

    # ---- 历史日期 ----
    history: Dict[str, str] = {}
    if pdata is not None:
        hist = pdata.find("History")
        if hist is not None:
            for pd in hist.findall("PubMedPubDate"):
                st = (pd.get("PubStatus") or "").lower()
                iso, _ = _parse_pubmed_date(pd)
                if st and iso:
                    history[st] = iso

    languages = [_xml_text(l) for l in citation.findall("Language")]
    languages = [l for l in languages if l]

    elocation = ""
    if article is not None:
        for el in article.findall("ELocationID"):
            if (el.get("EIdType") or "").lower() in ("pii", "doi") and not elocation:
                elocation = _xml_text(el)

    return {
        "source": "pubmed",
        "id": pmid,
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": title,
        "abstract": "\n\n".join(abstract_parts),
        "abstract_sections": abstract_sections,
        "authors": authors,
        "author_details": author_details,
        "author_first": authors[0] if authors else "",
        "author_count": len(authors),
        "journal": journal_full,
        "journal_abbrev": journal_abbrev or medline_ta,
        "issn": issn,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "elocation": elocation,
        "pubdate": pubdate_iso,
        "pubdate_raw": pubdate_raw,
        "year": pubdate_iso[:4] if pubdate_iso else "",
        "mesh": mesh_terms,
        "mesh_details": mesh_details,
        "mesh_major": [m["term"] for m in mesh_details if m["major"]],
        "keywords": keywords,
        "pubtypes": pubtypes,
        "languages": languages,
        "country": country,
        "history": history,
        "article_ids": article_ids,
    }


# --------------------------------------------------------------------------------------
# XLSX 导出（纯标准库）
#
# 为什么自己写：xlsx 本质就是个 zip 里装几个 XML。为了导出一张表而引入 openpyxl
# 会让"纯标准库、双击即用"的承诺失效。这里实现最小可用的多工作表写入器，
# 支持表头冻结、列宽、自动筛选与数字/文本区分。
# --------------------------------------------------------------------------------------

_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx_col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _xlsx_escape(text: Any) -> str:
    s = str(text if text is not None else "")
    # 去掉 XML 非法控制字符，否则 Excel 会报"文件已损坏"
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xlsx_cell(ref: str, value: Any) -> str:
    """生成一个单元格。数字写成数字类型，其余写成 inlineStr。

    用 inlineStr 而不是 sharedStrings 是为了省掉一张共享表，
    对导出场景完全够用，且代码量少一半。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    s = str(value)
    # 超长文本（如整段摘要）截断，Excel 单元格上限 32767 字符
    if len(s) > 32000:
        s = s[:32000] + "…[已截断]"
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xlsx_escape(s)}</t></is></c>'


def write_xlsx(
    path: "os.PathLike[str] | str",
    sheets: Sequence[Tuple[str, Sequence[str], Sequence[Sequence[Any]]]],
    *,
    col_widths: Optional[Dict[str, Dict[str, int]]] = None,
) -> None:
    """写多工作表 xlsx。``sheets`` 是 [(表名, 表头, 行数据), ...]。"""
    import zipfile

    path = Path(path)
    ensure_dir(path.parent)
    col_widths = col_widths or {}

    def sheet_xml(name: str, header: Sequence[str], rows: Sequence[Sequence[Any]], widths: Dict[str, int]) -> str:
        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<worksheet xmlns="{_XLSX_NS}">',
        ]
        if widths:
            parts.append("<cols>")
            for i in range(len(header)):
                col = _xlsx_col_letter(i)
                w = widths.get(col)
                if w:
                    parts.append(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>')
            parts.append("</cols>")
        parts.append("<sheetData>")
        # 表头
        parts.append('<row r="1">')
        for i, h in enumerate(header):
            parts.append(_xlsx_cell(f"{_xlsx_col_letter(i)}1", h))
        parts.append("</row>")
        for ri, row in enumerate(rows, start=2):
            parts.append(f'<row r="{ri}">')
            for ci, val in enumerate(row):
                if val is None or val == "":
                    continue
                parts.append(_xlsx_cell(f"{_xlsx_col_letter(ci)}{ri}", val))
            parts.append("</row>")
        parts.append("</sheetData>")
        # 冻结首行 + 自动筛选
        if header:
            last = _xlsx_col_letter(max(0, len(header) - 1))
            parts.append(
                f'<autoFilter ref="A1:{last}{max(1, len(rows) + 1)}"/>'
            )
        parts.append(
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
        )
        parts.append("</worksheet>")
        return "".join(parts)

    with zipfile.ZipFile(native_path(path), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(len(sheets))
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )

        sheet_tags = []
        rel_tags = []
        for i, (name, _h, _r) in enumerate(sheets, start=1):
            safe = re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31] or f"Sheet{i}"
            sheet_tags.append(f'<sheet name="{_xlsx_escape(safe)}" sheetId="{i}" r:id="rId{i}"/>')
            rel_tags.append(
                f'<Relationship Id="rId{i}" '
                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
            )

        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_XLSX_NS}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{''.join(sheet_tags)}</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rel_tags)
            + "</Relationships>",
        )
        for i, (name, header, rows) in enumerate(sheets, start=1):
            z.writestr(
                f"xl/worksheets/sheet{i}.xml",
                sheet_xml(name, header, rows, col_widths.get(name, {})),
            )


# --------------------------------------------------------------------------------------
# 记录规范化：把异构的上游数据压成统一的平表结构
# --------------------------------------------------------------------------------------

#: 统一记录字段。所有来源最终都映射到这套字段，检索与导出才可能统一。
RECORD_FIELDS: List[str] = [
    "source", "dataset", "id", "title", "generic_name", "brand_name",
    "substance_name", "manufacturer", "product_type", "route", "dosage_form",
    "application_number", "product_ndc", "rxcui", "unii", "pharm_class",
    "journal", "authors", "author_first", "pmid", "pmcid", "doi",
    "abstract", "mesh", "keywords", "pubtypes", "volume", "issue", "pages",
    "date", "year", "classification", "reason", "status", "license",
    "query", "url", "extra", "fetched_at",
]

#: 导出到 CSV/Excel 的列（比 RECORD_FIELDS 多几列派生信息）
EXPORT_FIELDS: List[str] = RECORD_FIELDS + ["abstract_len", "extra_json"]


def normalize_openfda(dataset: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 openFDA 记录规范成统一结构。

    ⚠️ ``openfda`` 里所有值都是数组（哪怕只有一个元素），
    统一用 ``first_of()`` 取值，否则 CSV 里会出现 ``['TYLENOL']`` 这种脏数据。
    """
    if not isinstance(rec, dict):
        return {}
    ofda = rec.get("openfda") if isinstance(rec.get("openfda"), dict) else {}
    spec = OPENFDA_ENDPOINTS.get(dataset, {})
    date_field = str(spec.get("date_field") or "")

    rid = first_of(rec.get(spec.get("id_field") or "id")) or first_of(rec.get("id"))
    if not rid:
        rid = first_of(rec.get("set_id")) or first_of(rec.get("safetyreportid"))
    if not rid:
        rid = sha1_short(jdump(rec, max_len=400), 16)

    # 日期：不同端点字段名不同，且格式不统一（YYYYMMDD vs YYYY-MM-DD）
    raw_date = ""
    for f in (date_field, "effective_time", "report_date", "receivedate",
              "date_of_manufacture", "report_date", "marketing_start_date"):
        if f and rec.get(f):
            raw_date = str(rec.get(f))
            break
    iso_date = ""
    digits = re.sub(r"\D", "", raw_date)
    if len(digits) >= 8:
        try:
            iso_date = _dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        except ValueError:
            iso_date = ""
    elif len(digits) == 4:
        iso_date = digits

    # 标题：各端点的"标题"语义差别很大，逐个端点取名
    title = ""
    if dataset == "label":
        gens = ofda.get("generic_name") or []
        brands = ofda.get("brand_name") or []
        title = first_of(brands) or first_of(gens) or first_of(rec.get("description"))
    elif dataset == "enforcement":
        title = first_of(rec.get("product_description")) or first_of(ofda.get("brand_name"))
    elif dataset == "event":
        reactions = rec.get("patient", {}).get("reaction", []) if isinstance(rec.get("patient"), dict) else []
        rnames = [first_of(r.get("reactionmeddrapt")) for r in reactions if isinstance(r, dict)]
        rnames = [r for r in rnames if r]
        title = " / ".join(rnames[:3]) or first_of(ofda.get("generic_name"))
    elif dataset == "ndc":
        title = first_of(rec.get("brand_name")) or first_of(rec.get("generic_name"))
    elif dataset == "drugsfda":
        title = first_of(rec.get("sponsor_name")) or first_of(ofda.get("brand_name"))
    elif dataset == "shortages":
        title = (
            first_of(rec.get("generic_name")) or first_of(rec.get("company_name"))
            or first_of(ofda.get("generic_name"))
        )
    elif dataset == "orangebook":
        prods = rec.get("products") if isinstance(rec.get("products"), list) else []
        p0 = prods[0] if prods and isinstance(prods[0], dict) else {}
        title = first_of(p0.get("brand_name")) or first_of(rec.get("trade_name"))
    if not title:
        title = first_of(ofda.get("brand_name")) or first_of(ofda.get("generic_name")) or rid

    ingredients: List[str] = []
    if isinstance(rec.get("active_ingredients"), list):
        for ai in rec["active_ingredients"]:
            if isinstance(ai, dict):
                nm = first_of(ai.get("name"))
                st = first_of(ai.get("strength"))
                ingredients.append(f"{nm} {st}".strip() if nm else "")
            else:
                ingredients.append(str(ai))
    if isinstance(rec.get("products"), list):
        for p in rec["products"]:
            if isinstance(p, dict) and isinstance(p.get("active_ingredients"), list):
                for ai in p["active_ingredients"]:
                    if isinstance(ai, dict):
                        nm = first_of(ai.get("name"))
                        st = first_of(ai.get("strength"))
                        ingredients.append(f"{nm} {st}".strip() if nm else "")

    substance = first_of(ofda.get("substance_name")) or ", ".join(
        i for i in ingredients[:3] if i
    )
    pharm_classes = [
        first_of(ofda.get(k)) for k in
        ("pharm_class_epc", "pharm_class_moa", "pharm_class_pe", "pharm_class_cs")
    ]

    # 详情正文：能给的都拼进去，检索时才有东西可搜
    extra_bits: List[str] = []
    for k in ("reason_for_recall", "classification", "status", "distribution_pattern",
              "recalling_firm", "voluntary_mandated", "initial_posting_date",
              "boxed_warning", "indications_and_usage", "dosage_and_administration",
              "warnings", "adverse_reactions", "contraindications"):
        v = rec.get(k)
        if v:
            extra_bits.append(f"【{k}】{flatten(v)[:2000]}")

    return {
        "source": "openfda",
        "dataset": dataset,
        "id": str(rid),
        "title": str(title)[:500],
        "generic_name": first_of(rec.get("generic_name")) or first_of(ofda.get("generic_name")),
        "brand_name": first_of(rec.get("brand_name")) or first_of(ofda.get("brand_name")),
        "substance_name": substance,
        "manufacturer": (
            first_of(rec.get("recalling_firm")) or first_of(rec.get("labeler_name"))
            or first_of(rec.get("company_name")) or first_of(ofda.get("manufacturer_name"))
        ),
        "product_type": first_of(rec.get("product_type")) or first_of(ofda.get("product_type")),
        "route": first_of(rec.get("route")) or first_of(ofda.get("route")),
        "dosage_form": first_of(rec.get("dosage_form")) or first_of(ofda.get("dosage_form")),
        "application_number": first_of(rec.get("application_number")) or first_of(ofda.get("application_number")),
        "product_ndc": first_of(rec.get("product_ndc")) or first_of(ofda.get("product_ndc")),
        "rxcui": first_of(ofda.get("rxcui")),
        "unii": first_of(ofda.get("unii")),
        "pharm_class": " | ".join(p for p in pharm_classes if p),
        "journal": "",
        "authors": "",
        "author_first": "",
        "pmid": "",
        "pmcid": "",
        "doi": "",
        "abstract": "\n".join(extra_bits)[:20000],
        "mesh": [],
        "keywords": [],
        "pubtypes": [],
        "volume": "",
        "issue": "",
        "pages": "",
        "date": iso_date,
        "year": iso_date[:4] if iso_date else "",
        "classification": first_of(rec.get("classification")),
        "reason": first_of(rec.get("reason_for_recall")),
        "status": first_of(rec.get("status")) or first_of(rec.get("marketing_status")),
        "license": "",
        "query": "",
        "url": "",
        "extra": rec,  # 原始记录全量保留，字段改版也不丢数据
        "fetched_at": now_iso(),
    }


def normalize_pubmed(rec: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 PubMed 记录规范成统一结构（字段已在 parse_pubmed_articles 抽好）。"""
    doi = str(rec.get("doi") or "")
    return {
        "source": "pubmed",
        "dataset": "pubmed",
        "id": str(rec.get("pmid") or ""),
        "title": str(rec.get("title") or "")[:1000],
        "generic_name": "",
        "brand_name": "",
        "substance_name": "",
        "manufacturer": "",
        "product_type": "literature",
        "route": "",
        "dosage_form": "",
        "application_number": "",
        "product_ndc": "",
        "rxcui": "",
        "unii": "",
        "pharm_class": "",
        "journal": str(rec.get("journal") or ""),
        "authors": " | ".join(rec.get("authors") or [])[:4000],
        "author_first": str(rec.get("author_first") or ""),
        "pmid": str(rec.get("pmid") or ""),
        "pmcid": str(rec.get("pmcid") or ""),
        "doi": doi,
        "abstract": str(rec.get("abstract") or "")[:50000],
        "mesh": list(rec.get("mesh") or []),
        "keywords": list(rec.get("keywords") or []),
        "pubtypes": list(rec.get("pubtypes") or []),
        "volume": str(rec.get("volume") or ""),
        "issue": str(rec.get("issue") or ""),
        "pages": str(rec.get("pages") or ""),
        "date": str(rec.get("pubdate") or ""),
        "year": str(rec.get("year") or ""),
        "classification": "",
        "reason": "",
        "status": "",
        "license": "",
        "query": "",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{rec.get('pmid')}/" if rec.get("pmid") else "",
        "extra": rec,
        "fetched_at": now_iso(),
    }


def record_abstract_len(rec: Dict[str, Any]) -> int:
    return len(str(rec.get("abstract") or ""))


def record_key(rec: Dict[str, Any]) -> Tuple[str, str, str]:
    """去重键：(来源, 数据集, ID)。

    跨源不合并 —— 同一个药在 openFDA 和 PubMed 里是两种东西，
    强行合并会把"药品记录"和"研究文献"混为一谈。
    """
    return (
        str(rec.get("source") or ""),
        str(rec.get("dataset") or ""),
        str(rec.get("id") or ""),
    )


# --------------------------------------------------------------------------------------
# 数据仓库（索引 / 落盘 / 导出）
# --------------------------------------------------------------------------------------


class Library:
    """本地数据仓库。

    目录结构::

        library/
          _records/           按来源分片的 jsonl（本人可读、可 diff、可增量追加）
          _index/             派生索引：csv / md / sqlite / xlsx
          _state/             断点续爬状态
          by_substance/       按物质名（通用名/成分）的硬链接分类
          by_journal/         按期刊的硬链接分类
          by_query/           按检索词的硬链接分类
          _export/            文献导出（RIS / BibTeX / MEDLINE）

    原件永远只存一份（``_records``），分类目录里是**硬链接**，同盘不额外占空间。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.records_dir = self.root / "_records"
        self.index_dir = self.root / "_index"
        self.state_dir = self.root / "_state"
        self.export_dir = self.root / "_export"
        self.by_substance = self.root / "by_substance"
        self.by_journal = self.root / "by_journal"
        self.by_query = self.root / "by_query"

        self.records_path = self.records_dir / "records.jsonl"
        self.csv_path = self.index_dir / "index.csv"
        self.md_path = self.index_dir / "index.md"
        self.sqlite_path = self.index_dir / "catalog.sqlite"
        self.xlsx_path = self.index_dir / "catalog.xlsx"
        self.queries_path = self.index_dir / "queries.csv"
        self._lock = threading.Lock()
        self._cache: Optional[List[Dict[str, Any]]] = None

    def ensure(self) -> None:
        for p in (self.root, self.records_dir, self.index_dir, self.state_dir):
            ensure_dir(p)

    # ---- 读写 ----

    def load_records(self, *, use_cache: bool = True) -> List[Dict[str, Any]]:
        """读全部记录。

        缓存的意义：GUI 每次检索都要读全量，几十万行 jsonl 反复解析会卡死界面。
        写操作会主动失效缓存。
        """
        if use_cache and self._cache is not None:
            return self._cache
        recs: List[Dict[str, Any]] = []
        if os.path.isfile(native_path(self.records_path)):
            with open(native_path(self.records_path), "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        recs.append(obj)  # noqa: PERF401
        if use_cache:
            self._cache = recs
        return recs

    def invalidate(self) -> None:
        self._cache = None

    def known_keys(self) -> Set[Tuple[str, str, str]]:
        """已入库的记录键集合 —— 增量爬取靠它跳过重复。"""
        return {record_key(r) for r in self.load_records()}

    def append_records(self, recs: Sequence[Dict[str, Any]]) -> int:
        if not recs:
            return 0
        ensure_dir(self.records_dir)
        with self._lock:
            with open(native_path(self.records_path), "a", encoding="utf-8", newline="\n") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.invalidate()
        return len(recs)

    def upsert_records(self, recs: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
        """按 record_key 去重合并。返回 (新增, 更新)。

        合并策略：新值只覆盖非空字段。这样"先抓到摘要、后补上 DOI"
        这类分次补全的场景不会把已有数据抹掉。
        """
        if not recs:
            return 0, 0
        existing: Dict[Tuple[str, str, str], Dict[str, Any]] = {
            record_key(r): r for r in self.load_records()
        }
        added = updated = 0
        for r in recs:
            k = record_key(r)
            if k in existing:
                merged = dict(existing[k])
                for kk, vv in r.items():
                    if vv not in (None, "", [], {}):
                        merged[kk] = vv
                existing[k] = merged
                updated += 1
            else:
                existing[k] = r
                added += 1
        ensure_dir(self.records_dir)
        tmp = self.records_path.with_name("records.jsonl.tmp")
        with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
            for r in existing.values():
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(native_path(tmp), native_path(self.records_path))
        self.invalidate()
        return added, updated

    def log_query(self, query: str, source: str, dataset: str,
                  fetched: int, skipped: int, failed: int) -> None:
        ensure_dir(self.index_dir)
        new = not os.path.isfile(native_path(self.queries_path))
        with open(native_path(self.queries_path), "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "query", "source", "dataset", "fetched", "skipped", "failed"])
            w.writerow([now_iso(), query, source, dataset, fetched, skipped, failed])

    # ---- 记录 -> 导出行 ----

    @staticmethod
    def _row(rec: Dict[str, Any]) -> List[Any]:
        row: List[Any] = []
        for f in EXPORT_FIELDS:
            if f == "abstract_len":
                row.append(record_abstract_len(rec))
                continue
            if f == "extra_json":
                row.append(jdump(rec.get("extra"), max_len=4000) if rec.get("extra") else "")
                continue
            v = rec.get(f)
            if f == "abstract":
                # Excel 单元格上限 32767，摘要动辄几万字符，截断
                s = str(v or "")
                row.append(s[:20000] + "…[截断]" if len(s) > 20000 else s)
                continue
            if isinstance(v, (list, tuple)):
                row.append(join_list(v))
            elif isinstance(v, dict):
                row.append(jdump(v, max_len=2000))
            else:
                row.append("" if v is None else v)
        return row

    # ---- 导出 ----

    def export_csv(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        recs = list(recs if recs is not None else self.load_records())
        ensure_dir(self.index_dir)
        with open(native_path(self.csv_path), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(EXPORT_FIELDS)
            for r in recs:
                w.writerow(self._row(r))
        return self.csv_path

    def export_excel(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        """导出多工作表 xlsx。

        分表逻辑：总表 + 按来源分表 + 统计页。上游导出的 xlsx 常常只有一张大表，
        按来源拆开后药品与文献互不干扰，做子集分析时清爽得多。
        """
        recs = list(recs if recs is not None else self.load_records())
        sheets: List[Tuple[str, Sequence[str], Sequence[Sequence[Any]]]] = []
        widths = {c: (60 if c in ("title", "abstract") else 18) for c in EXPORT_FIELDS}

        sheets.append(("全部记录", EXPORT_FIELDS, [self._row(r) for r in recs]))

        by_source: Dict[str, List[Dict[str, Any]]] = {}
        for r in recs:
            by_source.setdefault(str(r.get("source") or "unknown"), []).append(r)
        label_of = {"openfda": "FDA 药品数据", "pubmed": "PubMed 文献"}

        for src, rows in sorted(by_source.items()):
            sheets.append((
                label_of.get(src, src)[:31],
                EXPORT_FIELDS,
                [self._row(r) for r in rows],
            ))

        # 分数据集再拆一层（FDA 内部各端点差异极大，混在一起没法用）
        by_ds: Dict[str, List[Dict[str, Any]]] = {}
        for r in recs:
            if str(r.get("source")) == "openfda":
                by_ds.setdefault(str(r.get("dataset") or "unknown"), []).append(r)
        for ds, rows in sorted(by_ds.items()):
            nice = OPENFDA_ENDPOINTS.get(ds, {}).get("label", ds)
            sheets.append((f"FDA-{nice}"[:31], EXPORT_FIELDS, [self._row(r) for r in rows]))

        # 统计页
        stats: List[Sequence[Any]] = [["总计", len(recs)]]
        for src, rows in sorted(by_source.items()):
            stats.append([f"来源：{label_of.get(src, src)}", len(rows)])
        for ds, rows in sorted(by_ds.items()):
            stats.append([f"FDA 数据集：{OPENFDA_ENDPOINTS.get(ds, {}).get('label', ds)}", len(rows)])
        years: Dict[str, int] = {}
        for r in recs:
            y = str(r.get("year") or "")
            if y:
                years[y] = years.get(y, 0) + 1
        stats.append(["—— 按年份 ——", ""])
        for y in sorted(years, reverse=True)[:60]:
            stats.append([y, years[y]])
        sheets.append(("统计", ["项目", "数量"], stats))

        write_xlsx(self.xlsx_path, sheets, col_widths={s[0]: widths for s in sheets})
        return self.xlsx_path

    def export_markdown(self, recs: Optional[Sequence[Dict[str, Any]]] = None,
                        *, limit: int = 500) -> Path:
        recs = list(recs if recs is not None else self.load_records())
        ensure_dir(self.index_dir)
        lines = [
            f"# {APP_TITLE} 数据索引",
            "",
            f"生成时间：{now_iso()}　·　记录数：{len(recs):,}",
            "",
        ]
        by_source: Dict[str, List[Dict[str, Any]]] = {}
        for r in recs:
            by_source.setdefault(str(r.get("source") or "unknown"), []).append(r)

        lines.append("## 概览")
        lines.append("")
        lines.append("| 来源 | 记录数 |")
        lines.append("| --- | ---: |")
        for src, rows in sorted(by_source.items()):
            lines.append(f"| {src} | {len(rows):,} |")
        lines.append("")

        for src, rows in sorted(by_source.items()):
            lines.append(f"## {src}（{len(rows):,} 条）")
            lines.append("")
            lines.append("| ID | 标题 | 日期 | 来源/期刊 |")
            lines.append("| --- | --- | --- | --- |")
            for r in rows[:limit]:
                title = truncate_display(r.get("title") or "", 80).replace("|", "\\|")
                srcname = r.get("journal") or r.get("manufacturer") or r.get("dataset") or ""
                lines.append(
                    f"| {r.get('id') or ''} | {title} | {r.get('date') or ''} | "
                    f"{truncate_display(srcname, 40).replace('|', chr(92) + '|')} |"
                )
            if len(rows) > limit:
                lines.append(f"| … | 另有 {len(rows) - limit:,} 条未列出 | | |")
            lines.append("")

        with open(native_path(self.md_path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return self.md_path

    def export_sqlite(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        """建 SQLite 索引并加常用索引列 —— 大数据集下检索会快得多。"""
        recs = list(recs if recs is not None else self.load_records())
        ensure_dir(self.index_dir)
        if os.path.exists(native_path(self.sqlite_path)):
            os.remove(native_path(self.sqlite_path))

        cols = [
            ("source", "TEXT"), ("dataset", "TEXT"), ("id", "TEXT"), ("title", "TEXT"),
            ("generic_name", "TEXT"), ("brand_name", "TEXT"), ("substance_name", "TEXT"),
            ("manufacturer", "TEXT"), ("product_type", "TEXT"), ("route", "TEXT"),
            ("dosage_form", "TEXT"), ("application_number", "TEXT"), ("product_ndc", "TEXT"),
            ("rxcui", "TEXT"), ("unii", "TEXT"), ("pharm_class", "TEXT"),
            ("journal", "TEXT"), ("authors", "TEXT"), ("author_first", "TEXT"),
            ("pmid", "TEXT"), ("pmcid", "TEXT"), ("doi", "TEXT"), ("abstract", "TEXT"),
            ("mesh", "TEXT"), ("keywords", "TEXT"), ("pubtypes", "TEXT"),
            ("volume", "TEXT"), ("issue", "TEXT"), ("pages", "TEXT"),
            ("date", "TEXT"), ("year", "TEXT"), ("classification", "TEXT"),
            ("reason", "TEXT"), ("status", "TEXT"), ("query", "TEXT"), ("url", "TEXT"),
            ("fetched_at", "TEXT"),
        ]
        conn = sqlite3.connect(native_path(self.sqlite_path))
        try:
            conn.execute(
                "CREATE TABLE records ("
                + ", ".join(f'"{n}" {t}' for n, t in cols)
                + ", extra_json TEXT)"
            )
            placeholders = ", ".join("?" for _ in range(len(cols) + 1))
            names = ", ".join(f'"{n}"' for n, _ in cols) + ", extra_json"
            batch: List[List[Any]] = []
            for r in recs:
                vals: List[Any] = []
                for n, _ in cols:
                    v = r.get(n)
                    if isinstance(v, (list, tuple)):
                        v = join_list(v)
                    elif isinstance(v, dict):
                        v = jdump(v, max_len=2000)
                    vals.append("" if v is None else v)
                vals.append(jdump(r.get("extra"), max_len=8000) if r.get("extra") else "")
                batch.append(vals)
                if len(batch) >= 500:
                    conn.executemany(f"INSERT INTO records ({names}) VALUES ({placeholders})", batch)
                    batch.clear()
            if batch:
                conn.executemany(f"INSERT INTO records ({names}) VALUES ({placeholders})", batch)

            for idx_col in ("source", "dataset", "id", "year", "substance_name",
                            "journal", "pmid", "doi", "product_ndc"):
                conn.execute(f'CREATE INDEX idx_{idx_col} ON records("{idx_col}")')
            conn.commit()
        finally:
            conn.close()
        return self.sqlite_path

    # ---- 文献专用导出 ----

    def export_ris(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        """导出 RIS —— 可直接拖进 EndNote / Zotero / NoteExpress。"""
        recs = [r for r in (recs if recs is not None else self.load_records())
                if str(r.get("source")) == "pubmed"]
        ensure_dir(self.export_dir)
        path = self.export_dir / "pubmed.ris"
        lines: List[str] = []
        for r in recs:
            lines.append("TY  - JOUR")
            for a in str(r.get("authors") or "").split("|"):
                a = a.strip()
                if a:
                    lines.append(f"AU  - {a}")
            if r.get("title"):
                lines.append(f"TI  - {r['title']}")
            if r.get("journal"):
                lines.append(f"JO  - {r['journal']}")
            if r.get("abstract"):
                lines.append(f"AB  - {str(r['abstract'])[:8000]}")
            if r.get("volume"):
                lines.append(f"VL  - {r['volume']}")
            if r.get("issue"):
                lines.append(f"IS  - {r['issue']}")
            if r.get("pages"):
                lines.append(f"SP  - {r['pages']}")
            if r.get("date"):
                lines.append(f"DA  - {r['date']}")
            if r.get("doi"):
                lines.append(f"DO  - {r['doi']}")
            if r.get("pmid"):
                lines.append(f"AN  - {r['pmid']}")
            if r.get("url"):
                lines.append(f"UR  - {r['url']}")
            for kw in (r.get("mesh") or [])[:20]:
                lines.append(f"KW  - {kw}")
            lines.append("ER  - ")
            lines.append("")
        with open(native_path(path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return path

    def export_bibtex(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        """导出 BibTeX —— 写论文时直接 \\cite。"""
        recs = [r for r in (recs if recs is not None else self.load_records())
                if str(r.get("source")) == "pubmed"]
        ensure_dir(self.export_dir)
        path = self.export_dir / "pubmed.bib"
        entries: List[str] = []
        used: Set[str] = set()
        for r in recs:
            first = str(r.get("author_first") or "anon")
            last_name = re.split(r"[,\s]", first.strip())[0].lower() or "anon"
            last_name = re.sub(r"[^a-z]", "", last_name) or "anon"
            year = str(r.get("year") or "0000")
            key = f"{last_name}{year}"
            n = 1
            base = key
            while key in used:
                n += 1
                key = f"{base}{chr(96 + n)}"
            used.add(key)

            authors = " and ".join(
                a.strip() for a in str(r.get("authors") or "").split("|") if a.strip()
            )
            fields = [
                ("title", r.get("title")),
                ("author", authors),
                ("journal", r.get("journal")),
                ("year", year),
                ("volume", r.get("volume")),
                ("number", r.get("issue")),
                ("pages", r.get("pages")),
                ("doi", r.get("doi")),
                ("pmid", r.get("pmid")),
                ("url", r.get("url")),
            ]
            body = ",\n".join(
                f"  {k} = {{{v}}}" for k, v in fields if v not in (None, "")
            )
            entries.append(f"@article{{{key},\n{body}\n}}")
        with open(native_path(path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n\n".join(entries) + "\n")
        return path

    def export_medline(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        """导出 MEDLINE 文本格式（PubMed 的原生交换格式）。"""
        recs = [r for r in (recs if recs is not None else self.load_records())
                if str(r.get("source")) == "pubmed"]
        ensure_dir(self.export_dir)
        path = self.export_dir / "pubmed.medline"
        lines: List[str] = []
        for r in recs:
            if r.get("pmid"):
                lines.append(f"PMID- {r['pmid']}")
            for a in str(r.get("authors") or "").split("|"):
                a = a.strip()
                if a:
                    lines.append(f"FAU - {a}")
            if r.get("title"):
                lines.append(f"TI  - {r['title']}")
            if r.get("journal"):
                lines.append(f"JT  - {r['journal']}")
            if r.get("abstract"):
                lines.append(f"AB  - {str(r['abstract'])[:8000]}")
            if r.get("date"):
                lines.append(f"DP  - {r['date']}")
            if r.get("volume"):
                lines.append(f"VI  - {r['volume']}")
            if r.get("issue"):
                lines.append(f"IP  - {r['issue']}")
            if r.get("pages"):
                lines.append(f"PG  - {r['pages']}")
            if r.get("doi"):
                lines.append(f"AID - {r['doi']} [doi]")
            for m in (r.get("mesh") or [])[:30]:
                lines.append(f"MH  - {m}")
            for pt in (r.get("pubtypes") or [])[:10]:
                lines.append(f"PT  - {pt}")
            lines.append("")
        with open(native_path(path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return path

    # ---- 分类硬链接 ----

    def _link_into(self, category: str, name: str, rec: Dict[str, Any]) -> bool:
        """把记录做成分类目录里的一个 .json 硬链接。

        为什么用硬链接而不是复制：同一份记录可能归入多个物质名/期刊，
        复制会让仓库体积翻几倍；硬链接同盘零额外开销。
        """
        folder = {
            "substance": self.by_substance,
            "journal": self.by_journal,
            "query": self.by_query,
        }.get(category)
        if folder is None:
            return False
        safe = sanitize_component(name, 80, "unnamed")
        target_dir = folder / safe
        ensure_dir(target_dir)
        fname = sanitize_component(f"{rec.get('source')}_{rec.get('dataset')}_{rec.get('id')}", 120, "rec")
        target = target_dir / f"{fname}.json"
        if os.path.exists(native_path(target)):
            return False
        tmp = target_dir / f".{fname}.tmp"
        try:
            with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(rec, fh, ensure_ascii=False)
            try:
                os.link(native_path(tmp), native_path(target))
                ok = True
            except OSError:
                # 跨盘或文件系统不支持硬链接时退回复制 —— 功能不能因此缺失
                os.replace(native_path(tmp), native_path(target))
                ok = True
            return ok
        except OSError:
            try:
                if os.path.exists(native_path(tmp)):
                    os.remove(native_path(tmp))
            except OSError:
                pass
            return False

    def classify(self, recs: Optional[Sequence[Dict[str, Any]]] = None,
                 cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        """按物质名 / 期刊 / 检索词建立分类硬链接。"""
        recs = list(recs if recs is not None else self.load_records())
        cfg = cfg or {}
        stop = {str(s).lower() for s in (cfg.get("tag_stopwords") or [])}
        max_tags = max(1, int(cfg.get("max_tags_per_record") or 8))

        counts = {"substance": 0, "journal": 0, "query": 0}
        for r in recs:
            if parse_bool(cfg.get("make_substance_links"), True):
                names: List[str] = []
                for f in ("generic_name", "substance_name", "brand_name"):
                    v = str(r.get(f) or "").strip()
                    if v and v.lower() not in stop:
                        names.append(v)
                for nm in dedupe_keep_order(names)[:max_tags]:
                    if self._link_into("substance", nm, r):
                        counts["substance"] += 1

            if parse_bool(cfg.get("make_journal_links"), True):
                j = str(r.get("journal") or "").strip()
                if j and j.lower() not in stop and self._link_into("journal", j, r):
                    counts["journal"] += 1

            if parse_bool(cfg.get("make_query_links"), True):
                q = str(r.get("query") or "").strip()
                if q and self._link_into("query", q, r):
                    counts["query"] += 1
        return counts

    def rebuild_all(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """重建全部索引与导出。"""
        recs = self.load_records()
        res: Dict[str, Any] = {"records": len(recs)}
        cfg = cfg or {}
        if parse_bool(cfg.get("export_csv"), True):
            res["csv"] = str(self.export_csv(recs))
        if parse_bool(cfg.get("export_markdown"), True):
            res["markdown"] = str(self.export_markdown(recs))
        if parse_bool(cfg.get("export_sqlite"), True):
            res["sqlite"] = str(self.export_sqlite(recs))
        if parse_bool(cfg.get("export_excel"), True):
            try:
                res["excel"] = str(self.export_excel(recs))
            except Exception as exc:  # 导出失败不该让整条流水线崩掉
                res["excel_error"] = str(exc)
        if parse_bool(cfg.get("export_ris"), True):
            res["ris"] = str(self.export_ris(recs))
        if parse_bool(cfg.get("export_bibtex"), False):
            res["bibtex"] = str(self.export_bibtex(recs))
        if parse_bool(cfg.get("export_medline"), True):
            res["medline"] = str(self.export_medline(recs))
        res["links"] = self.classify(recs, cfg)
        return res

    def stats(self) -> Dict[str, Any]:
        recs = self.load_records()
        by_source: Dict[str, int] = {}
        by_dataset: Dict[str, int] = {}
        by_year: Dict[str, int] = {}
        with_abstract = with_doi = 0
        for r in recs:
            by_source[str(r.get("source") or "?")] = by_source.get(str(r.get("source") or "?"), 0) + 1
            by_dataset[str(r.get("dataset") or "?")] = by_dataset.get(str(r.get("dataset") or "?"), 0) + 1
            y = str(r.get("year") or "")
            if y:
                by_year[y] = by_year.get(y, 0) + 1
            if r.get("abstract"):
                with_abstract += 1
            if r.get("doi"):
                with_doi += 1
        return {
            "total": len(recs),
            "by_source": by_source,
            "by_dataset": by_dataset,
            "by_year": by_year,
            "with_abstract": with_abstract,
            "with_doi": with_doi,
            "records_path": str(self.records_path),
            "size": file_size(self.records_path),
        }


# --------------------------------------------------------------------------------------
# 断点续爬状态
# --------------------------------------------------------------------------------------


class SessionState:
    """记录每个检索组合的进度，支持中断续爬、跳过已耗尽的组合。

    为什么需要它：一次全量爬取可能跑几十小时。若中途断掉就得从头再来，
    已经爬完的组合会白白重跑一遍（每组合至少一次请求，几千次就是几小时）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.exhausted: Set[str] = set()
        self.partial: Set[str] = set()
        self.incomplete_run: bool = False
        self.load()

    @staticmethod
    def make_key(source: str, dataset: str, query: str, extra: str = "") -> str:
        return f"{source}|{dataset}|{query}|{extra}" if extra else f"{source}|{dataset}|{query}"

    def load(self) -> None:
        # ⚠️ 必须校验 data 是 dict。read_json 对「合法 JSON 但不是对象」
        # （例如文件被截断成 `[1,2,3]`、或被手工改成数组）不会报错，
        # 直接 .get() 会抛 AttributeError，导致程序**完全无法启动**。
        # 状态文件只是「省请求」的优化，它坏掉最多该退化成重爬一次，
        # 绝不该逼用户去手工找文件删掉。
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        self.exhausted = {str(k) for k in (data.get("exhausted") or [])}
        run = data.get("incomplete_run") or {}
        if not isinstance(run, dict):
            run = {}
        self.partial = {str(k) for k in (run.get("keys") or [])} if run else set()
        self.incomplete_run = bool(run)

    def save(self, run_keys: Sequence[str], *, run_done: bool) -> None:
        payload: Dict[str, Any] = {
            "updated_at": now_iso(),
            "exhausted": sorted(self.exhausted),
            "exhausted_count": len(self.exhausted),
        }
        if run_keys and not run_done:
            payload["incomplete_run"] = {
                "started_at": now_iso(),
                "keys": sorted(run_keys),
                "note": "这次运行尚未全部走完；下次运行会从这些组合继续",
            }
        atomic_write_json(self.path, payload)

    def mark_exhausted(self, key: str) -> None:
        self.exhausted.add(key)

    def is_exhausted(self, key: str) -> bool:
        return key in self.exhausted

    def reset(self) -> None:
        self.exhausted.clear()
        self.partial.clear()
        self.incomplete_run = False
        try:
            if os.path.exists(native_path(self.path)):
                os.remove(native_path(self.path))
        except OSError:
            pass


class SegmentStore:
    """记录"哪些时间段已经爬完"，用于分段爬取的断点续爬。

    段级状态是必须的：分段爬取要跑成百上千次请求。
    只记"组合级"状态的话，中断后重跑得把整棵分段树重走一遍，
    每段至少一次探量请求，几百段就是几十分钟的纯浪费。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.done: Set[str] = set()
        self.stats: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):   # 同上：坏掉的状态文件不该让程序起不来
            data = {}
        self.done = {str(k) for k in (data.get("done") or [])}
        self.stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}

    def save(self, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "updated_at": now_iso(),
            "done_count": len(self.done),
            "done": sorted(self.done),
        }
        if self.stats or extra:
            payload["stats"] = {**self.stats, **(extra or {})}
        atomic_write_json(self.path, payload)

    @staticmethod
    def make_key(source: str, dataset: str, query: str, start: str, end: str) -> str:
        return f"{source}|{dataset}|{query}|{start}~{end}"

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str, *, count: int = 0) -> None:
        self.done.add(key)
        if count:
            self.stats[key] = count

    def reset(self) -> None:
        self.done.clear()
        self.stats.clear()
        try:
            if os.path.exists(native_path(self.path)):
                os.remove(native_path(self.path))
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# ID 缓存
#
# PubMed 的正确姿势是"先列举 ID 再取详情"两步走。若把 ID 只放在内存里，
# 中断后重跑就得重新列举一遍（几千次请求）。把 ID 落盘后，
# 续爬可以直接从"取详情"那一步继续。
# --------------------------------------------------------------------------------------


class IdCache:
    """按 查询+切片 缓存 PubMed 的 PMID 列表。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: Dict[str, List[str]] = {}
        self.load()

    def load(self) -> None:
        raw = read_json(self.path, {}) or {}
        if not isinstance(raw, dict):    # 同上：ID 缓存坏掉只该导致重新列举
            raw = {}
        entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
        for k, v in entries.items():
            if isinstance(v, list):
                self.data[str(k)] = [str(x) for x in v]

    def save(self) -> None:
        # 只保留最近的若干批，避免无限膨胀（每批最多 9999 个 ID）
        keys = list(self.data.keys())
        if len(keys) > 400:
            for k in keys[:-400]:
                self.data.pop(k, None)
        atomic_write_json(self.path, {
            "updated_at": now_iso(),
            "entries": self.data,
        })

    def get(self, key: str) -> Optional[List[str]]:
        return self.data.get(key)

    def put(self, key: str, ids: Sequence[str]) -> None:
        self.data[key] = [str(i) for i in ids]

    def reset(self) -> None:
        self.data.clear()
        try:
            if os.path.exists(native_path(self.path)):
                os.remove(native_path(self.path))
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# 凭据管理
#
# 优先级（后者覆盖前者）：
#   内置 config.json  ->  用户凭据库 ~/.pharma_crawler/credentials.json
#     ->  环境变量  ->  命令行参数
#
# 凭据等价于账号配额，绝不能写进项目目录（已 gitignore）。
# --------------------------------------------------------------------------------------

CRED_DIR = Path.home() / ".pharma_crawler"
CRED_FILE = CRED_DIR / "credentials.json"
ENV_OPENFDA_KEY = "OPENFDA_API_KEY"
ENV_PUBMED_KEY = "NCBI_API_KEY"
ENV_PUBMED_EMAIL = "NCBI_EMAIL"


def _restrict_permissions(path: Path) -> None:
    """把凭据文件权限收紧到仅本人可读写。

    Windows 上 os.chmod 基本无效，所以只在类 Unix 上执行；
    Windows 下靠用户目录本身的 ACL 隔离。
    """
    if os.name == "nt":
        return
    try:
        os.chmod(native_path(path), 0o600)
    except OSError:
        pass


def load_credential_store() -> Dict[str, Any]:
    data = read_json(CRED_FILE, {}) or {}
    return data if isinstance(data, dict) else {}


def save_credential_store(**updates: str) -> None:
    data = load_credential_store()
    for k, v in updates.items():
        data[k] = str(v or "")
    data["updated_at"] = now_iso()
    ensure_dir(CRED_DIR)
    atomic_write_json(CRED_FILE, data)
    _restrict_permissions(CRED_FILE)


def clear_credential_store() -> bool:
    try:
        if os.path.exists(native_path(CRED_FILE)):
            os.remove(native_path(CRED_FILE))
            return True
    except OSError:
        pass
    return False


def resolve_credentials(cfg: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> None:
    """按优先级把凭据合并进 cfg（就地修改）。

    顺序：config.json < 凭据库 < 环境变量 < 命令行。
    靠后的一律覆盖靠前的 —— 命令行最高，便于临时切换账号。
    """
    store = load_credential_store()

    def pick(name: str, store_key: str, env_key: str, arg_name: str) -> str:
        val = str(cfg.get(name) or "")
        if store.get(store_key):
            val = str(store[store_key])
        if os.environ.get(env_key):
            val = str(os.environ[env_key])
        if args is not None:
            av = getattr(args, arg_name, None)
            if av:
                val = str(av)
        return val

    cfg["openfda_api_key"] = pick("openfda_api_key", "openfda_api_key", ENV_OPENFDA_KEY, "openfda_key")
    cfg["pubmed_api_key"] = pick("pubmed_api_key", "pubmed_api_key", ENV_PUBMED_KEY, "pubmed_key")
    cfg["pubmed_email"] = pick("pubmed_email", "pubmed_email", ENV_PUBMED_EMAIL, "pubmed_email")


def credentials_source(cfg: Dict[str, Any]) -> str:
    """说明当前凭据来自哪里，让用户知道改了配置为什么没生效。"""
    bits: List[str] = []
    store = load_credential_store()
    if cfg.get("openfda_api_key"):
        where = "凭据库" if store.get("openfda_api_key") else (
            "环境变量" if os.environ.get(ENV_OPENFDA_KEY) else "config.json"
        )
        bits.append(f"openFDA key（{where}）")
    else:
        bits.append("openFDA 无 key（每天限 1000 次）")
    if cfg.get("pubmed_api_key"):
        where = "凭据库" if store.get("pubmed_api_key") else (
            "环境变量" if os.environ.get(ENV_PUBMED_KEY) else "config.json"
        )
        bits.append(f"NCBI key（{where}）")
    else:
        bits.append("NCBI 无 key（限 3 请求/秒）")
    if cfg.get("pubmed_email"):
        bits.append("已填 email")
    else:
        bits.append("未填 email（NCBI 要求注册后才能解封 IP）")
    return " · ".join(bits)


# --------------------------------------------------------------------------------------
# 爬取会话
# --------------------------------------------------------------------------------------


class CrawlSession:
    """一次爬取运行的全部上下文。

    负责：进度上报、安全停止、记录规范化、落盘、增量去重。
    线程安全靠 ``_lock``；GUI 通过 ``report`` 回调拿到实时进度。
    """

    def __init__(self, cfg: Dict[str, Any], lib: Library, *, verbose: bool = True,
                 progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.cfg = cfg
        self.lib = lib
        self.verbose = verbose
        self.progress_cb = progress_cb
        self.http = HttpClient(cfg, verbose=verbose)
        self.openfda = OpenFDAClient(self.http, cfg, verbose=verbose)
        self.pubmed = PubMedClient(self.http, cfg, verbose=verbose)

        self.session = SessionState(lib.state_dir / "session.json")
        self.segments = SegmentStore(lib.state_dir / "segments.json")
        self.id_cache = IdCache(lib.state_dir / "pubmed_ids.json")

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.counters: Dict[str, int] = {
            "fetched": 0, "skipped": 0, "failed": 0, "stored": 0,
        }
        # 实时累计取到的条数。
        # 为什么不直接用 counters["fetched"]：那是在每个爬取方法**结束时**才一次性加上去的，
        # 运行过程中一直是 0。用它做 max_records 判断会导致多切片场景下上限彻底失效
        # （实测：设 --max-records 40 却入了 200 条）。
        self._live_fetched = 0
        self.run_keys: List[str] = []
        self._pending: List[Dict[str, Any]] = []

    def total_fetched(self) -> int:
        """已取到的总条数（实时）。"""
        with self._lock:
            return self._live_fetched

    def _add_fetched(self, n: int) -> None:
        with self._lock:
            self._live_fetched += n
            self.counters["fetched"] = self._live_fetched

    # ---- 控制 ----

    def stop(self) -> None:
        """请求安全停止：立刻停下，已抓到的数据照常落盘。"""
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def report(self, **kw: Any) -> None:
        payload = {**kw, **self.counters, "elapsed": self.http.stats["total_wait"]}
        if self.progress_cb:
            try:
                self.progress_cb(payload)
            except Exception:
                pass
        if self.verbose:
            msg = kw.get("message")
            if msg:
                out(f"    {msg}")

    def _progress_forward(self, payload: Dict[str, Any]) -> None:
        """把 API 客户端的进度回调转成统一的会话进度。"""
        if self.progress_cb:
            try:
                self.progress_cb({**payload, **self.counters, "kind": "sub"})
            except Exception:
                pass

    # ---- 入库 ----

    def _store(self, recs: Sequence[Dict[str, Any]], *, flush: bool = False) -> int:
        """把规范化后的记录攒批写入。

        攒批的意义：逐条落盘会在几十万条时把磁盘 I/O 变成瓶颈
        （每条一次 open/write/close）。攒到 200 条写一次，快一个数量级。
        """
        if not recs:
            return 0
        with self._lock:
            self._pending.extend(recs)
            if flush or len(self._pending) >= 200:
                batch = self._pending[:]
                self._pending.clear()
            else:
                return 0
        n = self.lib.append_records(batch)
        with self._lock:
            self.counters["stored"] += n
        return n

    def flush(self) -> int:
        """把攒批的剩余记录写盘。停止或结束时必须调用。"""
        with self._lock:
            batch = self._pending[:]
            self._pending.clear()
        if not batch:
            return 0
        n = self.lib.append_records(batch)
        with self._lock:
            self.counters["stored"] += n
        return n

    # ---- 空结果诊断 ----

    def _diagnose_empty(self, dataset: str, search: str, effective_search: str, label: str) -> None:
        """查无结果时给出可操作的诊断。

        为什么值得专门写：openFDA 的字段名在各端点之间**不统一** ——
        ``ndc``/``shortages`` 的 ``generic_name`` 在顶层，而 ``label`` 的
        同名概念在 ``openfda.generic_name`` 里。写错了查询语法完全合法，
        只是安静地返回 0 条，用户根本无从判断是"数据库里没有"还是"字段名写错"。

        这里在 0 条时**去掉字段前缀再试一次**，若去掉后能查到，就说明是字段名问题，
        并把正确写法直接告诉用户。
        """
        if not self.verbose:
            return

        out(f"    [空] {label} 该条件命中 0 条")

        # 诊断 1：把 openfda. 前缀去掉重试
        if "openfda." in search:
            stripped = search.replace("openfda.", "")
            try:
                if self.openfda.count_total(dataset, stripped) > 0:
                    out(f"    ↳ 但去掉 openfda. 前缀后能查到数据。")
                    out(f"       该端点的字段在**顶层**，请改用：{stripped}")
                    return
            except (EmptyResult, CrawlError):
                pass

        # 诊断 2：该端点整体是否有数据（排除"数据集本身是空的"）
        try:
            dataset_total = self.openfda.count_total(dataset, "")
        except (EmptyResult, CrawlError):
            dataset_total = 0
        if dataset_total > 0:
            out(f"    ↳ 该端点本身有 {dataset_total:,} 条数据，所以是**查询条件**没匹配上。")
            out(f"       请核对字段名与取值；字段清单见 docs/DATA_SOURCE_NOTES.md 第 1.8 节。")
            if dataset == "orangebook":
                out(f"       注意：橙皮书记录**没有 openfda 嵌套对象**，"
                    f"请用顶层字段如 product_number / approval_date。")
        else:
            out(f"    ↳ 该端点当前返回不到任何数据，可能是上游端点地址已变更。")

    # ---- openFDA 爬取 ----

    def crawl_openfda(
        self,
        dataset: str,
        *,
        search: str = "",
        query_label: str = "",
        max_records: int = 0,
        sort: str = "",
        date_range: Optional[DateRange] = None,
        segment: str = "auto",
    ) -> Dict[str, int]:
        """爬一个 openFDA 数据集。

        分段策略：openFDA 的可达窗口是 26000 条（limit 1000 + skip 25000）。
        超出后由客户端自动切到 search_after 游标滚动；
        若游标不可用（排序键不唯一），则回退到按日期二分切分。
        """
        spec = OPENFDA_ENDPOINTS.get(dataset, {})
        date_field = str(spec.get("date_field") or "")
        label = str(spec.get("label") or dataset)
        query = search or query_label or "*"

        if self.verbose:
            out(f"  ▸ {label}（{dataset}）")
            if search:
                out(f"    查询: {search}")

        # 字段名提示：不同端点的字段前缀不一样，用错了会安静地返回 0 条。
        # 这属于"查询语法正确、字段名不对"的坑，光看 0 条结果无从判断，
        # 所以在探测到 0 条且用了 openfda. 前缀时给出明确提示。
        if search and "openfda." in search and dataset in ("orangebook",):
            if self.verbose:
                out(f"    [提示] {label} 的记录**没有 openfda 嵌套对象**，"
                    f"请直接用顶层字段，如 product_number / approval_date")

        # 日期范围限定
        rng = date_range or DateRange()
        effective_search = search
        if rng.active() and date_field:
            span = format_range_for(date_field, rng.start or _dt.date(1900, 1, 1), rng.end or _dt.date.today())
            clause = f"{date_field}:[{span}]"
            effective_search = f"({search}) AND {clause}" if search else clause
            if self.verbose:
                out(f"    时间范围: {rng.describe()} → {clause}")
        elif rng.active() and not date_field:
            if self.verbose:
                out(f"    [提示] {label} 没有日期字段，时间范围对该数据集无效，已忽略")

        key = SessionState.make_key("openfda", dataset, effective_search)
        if self.session.is_exhausted(key) and not max_records:
            if self.verbose:
                out("    [跳过] 该查询此前已爬完（如要重爬请先 reset）")
            with self._lock:
                self.counters["skipped"] += 1
            return dict(self.counters)

        self.run_keys.append(key)
        got = 0
        # 把"本次上限"折算成"本端点还能取多少"，避免多数据集时每个都各取满上限
        budget = max_records
        if max_records:
            budget = max(0, max_records - self.total_fetched())
            if budget <= 0:
                if self.verbose:
                    out(f"    [跳过] 已达本次上限 {max_records:,} 条")
                return dict(self.counters)

        # 事先探一次总量：0 条时给出可操作的诊断，而不是让用户对着
        # "完成：本次 0 条"发愣 —— 字段名写错是最常见的失败原因。
        try:
            probe_total = self.openfda.count_total(dataset, effective_search)
        except (EmptyResult, CrawlError):
            probe_total = 0
        if probe_total == 0:
            self._diagnose_empty(dataset, search, effective_search, label)
            if not self.should_stop():
                self.session.mark_exhausted(key)
            return dict(self.counters)

        try:
            for raw in self.openfda.iter_records(
                dataset,
                search=effective_search,
                max_records=budget,
                sort=sort,
                stop_flag=self.should_stop,
                progress=self._progress_forward,
            ):
                if self.should_stop():
                    break
                rec = normalize_openfda(dataset, raw)
                if not rec:
                    with self._lock:
                        self.counters["failed"] += 1
                    continue
                rec["query"] = query_label or query
                self._store([rec])
                got += 1
                self._add_fetched(1)
                if got % 100 == 0:
                    self.report(message=f"已获取 {got:,} 条 · 入库 {self.counters['stored']:,} 条")
        except RateLimited as exc:
            if self.verbose:
                out(f"    [限流] {exc}")
            with self._lock:
                self.counters["failed"] += 1
        except ApiShapeError:
            raise
        except CrawlError as exc:
            if self.verbose:
                out(f"    [错误] {exc}")
            with self._lock:
                self.counters["failed"] += 1
        finally:
            self.flush()

        if not self.should_stop():
            self.session.mark_exhausted(key)
        # 报告**本次任务自己**取到多少，而不是累计值。
        # 曾经写成 self.counters，多数据集时会打印出累计数，
        # 于是第二个源明明一条没取也显示"完成：本次 60 条"，让人误以为两个源都爬到了。
        self.report(message=f"{label} 完成：本次 {got:,} 条")
        return dict(self.counters)

    # ---- PubMed 爬取 ----

    @staticmethod
    def _pubmed_term(term: str, date_range: DateRange, datetype: str) -> Tuple[str, str, str]:
        """把日期范围拆成 mindate/maxdate 两个参数。

        ⚠️ NCBI 要求 mindate 与 maxdate **必须成对出现**，
        且格式是**斜杠** ``YYYY/MM/DD``（不是短横线）。
        """
        mind = date_to_slash(date_range.start) if date_range.start else ""
        maxd = date_to_slash(date_range.end) if date_range.end else ""
        if mind and not maxd:
            maxd = date_to_slash(_dt.date.today())
        if maxd and not mind:
            mind = "1800/01/01"
        return term, mind, maxd

    def crawl_pubmed(
        self,
        term: str,
        *,
        max_records: int = 0,
        sort: str = "",
        date_range: Optional[DateRange] = None,
        datetype: str = "pdat",
        segment: str = "auto",
        reldate: str = "",
    ) -> Dict[str, int]:
        """爬 PubMed 文献。

        核心难点是 **9,999 条上限**（实测值，不是文档说的 10,000）。
        策略：
          1. 先探量；
          2. ≤9999 直接走 History server；
          3. >9999 则按年切分，某年仍超限就递归到月、再到日；
          4. 每片用 usehistory 建集，再用 EFetch 分批抽干。

        ⚠️ 9,999 的天花板管的是 **ESearch 的 ID 列举**，不是 **EFetch 的 WebEnv 迭代**，
        所以每片必须严格 <9999，再用 EFetch 分页取 —— 而不是硬翻 retstart。
        """
        if not term.strip():
            raise CrawlError("PubMed 查询词不能为空")

        rng = date_range or DateRange()
        _, mind, maxd = self._pubmed_term(term, rng, datetype)

        if self.verbose:
            out("  ▸ PubMed 文献检索")
            out(f"    查询: {term}")
            if mind or maxd:
                out(f"    时间范围: {mind or '最早'} ~ {maxd or '至今'}（字段 {datetype}）")

        total = self.pubmed.count(term, datetype=datetype, mindate=mind, maxdate=maxd, reldate=reldate)
        if self.verbose:
            out(f"    命中总数: {total:,}")

        if total == 0:
            if self.verbose:
                out("    [空] 该查询没有命中任何文献")
            return dict(self.counters)

        # 记录进入本任务前的累计数，用来算出"本次任务自己取了多少"
        before = self.total_fetched()

        # 上限已在前面用光时明确说明，否则用户会看到"完成：本次 0 条"而不知所然
        if max_records and before >= max_records:
            if self.verbose:
                out(f"    [跳过] 已达本次上限 {max_records:,} 条，本任务未执行。")
                out(f"      如需同时爬取多个源，请调高 --max-records 或设为 0（不限）。")
            return dict(self.counters)

        if total <= PUBMED_MAX_RETMAX:
            self._pubmed_fetch_slice(
                term, mind=mind, maxd=maxd, datetype=datetype, sort=sort,
                reldate=reldate, max_records=max_records, label="全部",
            )
        else:
            if self.verbose:
                out(
                    f"    ⚠️ 命中 {total:,} 条 > {PUBMED_MAX_RETMAX:,} 上限，"
                    f"改按时间切分（这是 NCBI 的硬限制，不是本程序的问题）"
                )
            self._pubmed_segmented(
                term, rng=rng, datetype=datetype, sort=sort, reldate=reldate,
                max_records=max_records, segment=segment,
            )

        self.flush()
        self.report(message=f"PubMed 完成：本次 {self.total_fetched() - before:,} 条")
        return dict(self.counters)

    def _pubmed_segmented(
        self, term: str, *, rng: DateRange, datetype: str, sort: str,
        reldate: str, max_records: int, segment: str,
    ) -> None:
        """按时间递归切分直到每片 <9999。

        为什么要递归到月甚至日：PubMed 持续增长，
        十年前按年切分够用，今天热门主题单年就可能破万。
        把"年→月→日"的递归内置，才不会过几年就失效。
        """
        start = rng.start or _dt.date(1800, 1, 1)
        end = rng.end or _dt.date.today()

        if segment in ("year", "auto"):
            chunks = year_chunks(start, end)
        elif segment == "month":
            chunks = month_chunks(start, end)
        else:
            if self.verbose:
                out("    [提示] segment=none：不做切分，只能取到前 9,999 条")
            self._pubmed_fetch_slice(
                term, mind=date_to_slash(start), maxd=date_to_slash(end),
                datetype=datetype, sort=sort, reldate=reldate,
                max_records=max_records, label="全部",
            )
            return

        if self.verbose:
            out(f"    切分为 {len(chunks)} 个时间片")

        for idx, (cs, ce) in enumerate(chunks, start=1):
            if self.should_stop():
                if self.verbose:
                    out("    [停止] 收到停止请求，已保存进度")
                return
            if max_records and self.total_fetched() >= max_records:
                if self.verbose:
                    out(f"    [停止] 已达本次上限 {max_records:,} 条")
                return
            self._pubmed_slice_recursive(
                term, cs, ce, datetype=datetype, sort=sort, reldate=reldate,
                max_records=max_records, depth=0,
                label=f"[{idx}/{len(chunks)}] {cs.isoformat()}~{ce.isoformat()}",
            )

    def _pubmed_slice_recursive(
        self, term: str, start: _dt.date, end: _dt.date, *, datetype: str,
        sort: str, reldate: str, max_records: int, depth: int, label: str,
    ) -> None:
        """递归处理单个时间片：超限就二分，直到能一次取完。"""
        if self.should_stop() or depth > 12:
            return

        seg_key = SegmentStore.make_key("pubmed", "pubmed", term, start.isoformat(), end.isoformat())
        if self.segments.is_done(seg_key):
            if self.verbose:
                out(f"    [跳过] {label} 此前已爬完")
            return

        mind, maxd = date_to_slash(start), date_to_slash(end)
        try:
            n = self.pubmed.count(term, datetype=datetype, mindate=mind, maxdate=maxd, reldate=reldate)
        except RateLimited:
            raise

        if n == 0:
            self.segments.mark_done(seg_key, count=0)
            self.segments.save()
            return

        if n <= PUBMED_MAX_RETMAX:
            got = self._pubmed_fetch_slice(
                term, mind=mind, maxd=maxd, datetype=datetype, sort=sort,
                reldate=reldate, max_records=max_records, label=label,
            )
            if not self.should_stop():
                self.segments.mark_done(seg_key, count=got)
                self.segments.save()
            return

        # 仍然超限：二分继续切
        halves = split_span(start, end)
        if halves is None:
            if self.verbose:
                out(
                    f"    ⚠️ {label} 单日仍 {n:,} 条 > {PUBMED_MAX_RETMAX:,}，"
                    f"无法再分，只能取前 {PUBMED_MAX_RETMAX:,} 条"
                )
            got = self._pubmed_fetch_slice(
                term, mind=mind, maxd=maxd, datetype=datetype, sort=sort,
                reldate=reldate, max_records=max_records, label=label, cap=PUBMED_MAX_RETMAX,
            )
            if not self.should_stop():
                self.segments.mark_done(seg_key, count=got)
                self.segments.save()
            return

        if self.verbose:
            out(f"    {label} 有 {n:,} 条，继续二分")
        for hs, he in halves:
            if self.should_stop():
                return
            self._pubmed_slice_recursive(
                term, hs, he, datetype=datetype, sort=sort, reldate=reldate,
                max_records=max_records, depth=depth + 1,
                label=f"{hs.isoformat()}~{he.isoformat()}",
            )

    def _pubmed_fetch_slice(
        self, term: str, *, mind: str, maxd: str, datetype: str, sort: str,
        reldate: str, max_records: int, label: str, cap: int = 0,
    ) -> int:
        """取一个切片：先建 History 集，再用 EFetch 分批抽干。"""
        cache_key = f"{term}|{mind}|{maxd}|{datetype}|{reldate}"
        ids = self.id_cache.get(cache_key)

        if ids is None:
            try:
                found = self.pubmed.esearch(
                    term, retmax=cap or PUBMED_MAX_RETMAX, use_history=True,
                    sort=sort, datetype=datetype if (mind or maxd) else "",
                    mindate=mind, maxdate=maxd, reldate=reldate,
                )
            except RateLimited as exc:
                if self.verbose:
                    out(f"    [限流] {exc}；跳过 {label}")
                return 0
            ids = found.get("ids") or []
            if ids:
                self.id_cache.put(cache_key, ids)
                self.id_cache.save()
            if self.verbose:
                out(f"    {label}：{len(ids):,} 个 ID 已列出")

        if not ids:
            return 0

        batch = self.pubmed.batch_size
        got = 0
        for i in range(0, len(ids), batch):
            if self.should_stop():
                return got
            # 预算裁剪必须在**取数之前**完成。
            # 曾经写成"先判断、后按剩余额度截断"，但判断条件用的是
            # total_fetched()+got，首次循环时两者都是 0、条件恒为假，
            # 于是整整一批（默认 200 条）会先被取回来，--max-records 40 实际入了 200 条。
            chunk = ids[i:i + batch]
            if max_records:
                remain = max_records - self.total_fetched() - got
                if remain <= 0:
                    return got
                if remain < len(chunk):
                    chunk = chunk[:remain]
            if not chunk:
                return got
            try:
                xml = self.pubmed.efetch(ids=chunk, retmode="xml")
                parsed = parse_pubmed_articles(xml)
            except RateLimited as exc:
                if self.verbose:
                    out(f"    [限流] {exc}；等待后继续")
                time.sleep(15)
                try:
                    xml = self.pubmed.efetch(ids=chunk, retmode="xml")
                    parsed = parse_pubmed_articles(xml)
                except (CrawlError, ApiShapeError) as exc2:
                    if self.verbose:
                        out(f"    [错误] 重试仍失败：{exc2}")
                    with self._lock:
                        self.counters["failed"] += len(chunk)
                    continue
            except ApiShapeError:
                raise
            except CrawlError as exc:
                if self.verbose:
                    out(f"    [错误] 取记录失败：{exc}")
                with self._lock:
                    self.counters["failed"] += len(chunk)
                continue

            if not parsed and chunk:
                # 拿到 XML 但解析不出文章 —— 多半是上游改版，必须响
                if self.verbose:
                    out(f"    [warn] 本批 {len(chunk)} 个 ID 未解析出任何文章，疑似上游改版")

            recs = []
            for p in parsed:
                rec = normalize_pubmed(p)
                rec["query"] = term
                recs.append(rec)
            self._store(recs)
            got += len(recs)
            self._add_fetched(len(recs))
            if got and got % 500 < batch:
                self.report(message=f"{label} 已取 {got:,} 条")
            time.sleep(0.05)

        if self.verbose and got:
            out(f"    {label}：入库 {got:,} 条")
        return got

    # ---- 统一入口 ----

    def run(self, plan: Sequence[Dict[str, Any]], *, max_records: int = 0) -> Dict[str, Any]:
        """按计划执行一批爬取任务。

        ``plan`` 里每项形如::

            {"source": "openfda", "dataset": "label", "search": "openfda.brand_name:aspirin"}
            {"source": "pubmed", "term": "aspirin[Title/Abstract]"}
        """
        self.lib.ensure()
        started = time.time()
        date_range = DateRange.from_cfg(self.cfg)
        segment = str(self.cfg.get("segment_strategy") or "auto")

        try:
            for task in plan:
                if self.should_stop():
                    break
                src = str(task.get("source") or "")
                try:
                    if src == "openfda":
                        self.crawl_openfda(
                            str(task.get("dataset") or "label"),
                            search=str(task.get("search") or ""),
                            query_label=str(task.get("label") or task.get("search") or ""),
                            max_records=max_records,
                            date_range=date_range,
                            segment=segment,
                        )
                    elif src == "pubmed":
                        self.crawl_pubmed(
                            str(task.get("term") or ""),
                            max_records=max_records,
                            sort=str(task.get("sort") or self.cfg.get("pubmed_sort") or ""),
                            date_range=date_range,
                            datetype=str(self.cfg.get("pubmed_datetype") or "pdat"),
                            segment=segment,
                            reldate=str(task.get("reldate") or ""),
                        )
                    else:
                        if self.verbose:
                            out(f"  [跳过] 未知来源: {src!r}")
                except ApiShapeError as exc:
                    # 结构校验失败必须立刻中断并大声报错 —— 静默继续只会产出空数据
                    out(f"\n❌ {exc}\n")
                    self.flush()
                    raise
                except CrawlError as exc:
                    if self.verbose:
                        out(f"  [错误] {exc}")
                    with self._lock:
                        self.counters["failed"] += 1
        finally:
            self.flush()

        run_done = not self.should_stop()
        self.session.save(self.run_keys, run_done=run_done)
        elapsed = time.time() - started

        summary = {
            **self.counters,
            "elapsed": elapsed,
            "stopped": self.should_stop(),
            "http": self.http.summary(),
        }
        with self._lock:
            summary["dropped"] = 0  # 占位：保留字段以便旧调用方兼容
        return summary


# --------------------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------------------


def search_records(
    recs: Sequence[Dict[str, Any]],
    *,
    keyword: str = "",
    sources: Optional[Sequence[str]] = None,
    datasets: Optional[Sequence[str]] = None,
    substances: Optional[Sequence[str]] = None,
    journals: Optional[Sequence[str]] = None,
    mesh: Optional[Sequence[str]] = None,
    pubtypes: Optional[Sequence[str]] = None,
    years: Optional[Sequence[str]] = None,
    date_range: Optional[DateRange] = None,
    field_scope: str = "all",
    has_abstract: bool = False,
    has_doi: bool = False,
    match_all: bool = False,
    limit: int = 0,
) -> List[Dict[str, Any]]:
    """多维检索。

    ``field_scope`` 决定关键词搜哪些字段：
      - ``all``      标题 + 摘要 + 各类名称（默认，最宽松）
      - ``title``    仅标题
      - ``abstract`` 仅摘要
      - ``name``     仅药品名称类字段
      - ``id``       仅各类标识符（PMID/DOI/NDC/申请号）
    """
    kw = keyword.strip().lower()
    terms = [t for t in re.split(r"\s+", kw) if t] if kw else []

    src_set = {s.lower() for s in (sources or []) if s}
    ds_set = {s.lower() for s in (datasets or []) if s}
    sub_set = {s.lower() for s in (substances or []) if s}
    jr_set = {j.lower() for j in (journals or []) if j}
    mesh_set = {m.lower() for m in (mesh or []) if m}
    pt_set = {p.lower() for p in (pubtypes or []) if p}
    yr_set = {str(y) for y in (years or []) if y}

    def field_text(r: Dict[str, Any]) -> str:
        if field_scope == "title":
            return str(r.get("title") or "").lower()
        if field_scope == "abstract":
            return str(r.get("abstract") or "").lower()
        if field_scope == "name":
            return " ".join(str(r.get(f) or "") for f in (
                "generic_name", "brand_name", "substance_name", "pharm_class"
            )).lower()
        if field_scope == "id":
            return " ".join(str(r.get(f) or "") for f in (
                "id", "pmid", "pmcid", "doi", "product_ndc",
                "application_number", "rxcui", "unii"
            )).lower()
        # all
        parts = [
            str(r.get("title") or ""), str(r.get("abstract") or ""),
            str(r.get("generic_name") or ""), str(r.get("brand_name") or ""),
            str(r.get("substance_name") or ""), str(r.get("manufacturer") or ""),
            str(r.get("journal") or ""), str(r.get("authors") or ""),
            str(r.get("pharm_class") or ""), str(r.get("reason") or ""),
            join_list(r.get("mesh")), join_list(r.get("keywords")),
            str(r.get("id") or ""), str(r.get("doi") or ""), str(r.get("pmid") or ""),
        ]
        return " ".join(parts).lower()

    hits: List[Dict[str, Any]] = []
    for r in recs:
        if src_set and str(r.get("source") or "").lower() not in src_set:
            continue
        if ds_set and str(r.get("dataset") or "").lower() not in ds_set:
            continue
        if yr_set and str(r.get("year") or "") not in yr_set:
            continue
        if has_abstract and not r.get("abstract"):
            continue
        if has_doi and not r.get("doi"):
            continue

        if sub_set:
            names = " ".join(str(r.get(f) or "") for f in
                             ("generic_name", "substance_name", "brand_name")).lower()
            if not any(s in names for s in sub_set):
                continue
        if jr_set:
            j = str(r.get("journal") or "").lower()
            if not any(x in j for x in jr_set):
                continue
        if mesh_set:
            m = " ".join(str(x) for x in (r.get("mesh") or [])).lower()
            if not any(x in m for x in mesh_set):
                continue
        if pt_set:
            p = " ".join(str(x) for x in (r.get("pubtypes") or [])).lower()
            if not any(x in p for x in pt_set):
                continue
        if date_range and date_range.active():
            d = parse_user_date(str(r.get("date") or "")[:10], "start")
            if not date_range.contains(d):
                continue

        if terms:
            hay = field_text(r)
            ok = all(t in hay for t in terms) if match_all else any(t in hay for t in terms)
            if not ok:
                continue

        hits.append(r)
        if limit and len(hits) >= limit:
            break
    return hits


def histogram(recs: Sequence[Dict[str, Any]], field: str) -> List[Tuple[str, int]]:
    """按字段做频次统计（GUI 的筛选下拉靠它填充）。"""
    counts: Dict[str, int] = {}
    for r in recs:
        v = r.get(field)
        if isinstance(v, (list, tuple)):
            for item in v:
                s = str(item).strip()
                if s:
                    counts[s] = counts.get(s, 0) + 1
        else:
            s = str(v or "").strip()
            if s:
                counts[s] = counts.get(s, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def print_records(hits: Sequence[Dict[str, Any]], *, limit: int = 30, show_abstract: bool = False) -> None:
    if not hits:
        out("（无结果）")
        return
    out(f"共 {len(hits):,} 条，显示前 {min(limit, len(hits))} 条：")
    out("")
    for i, r in enumerate(hits[:limit], start=1):
        tag = "文献" if str(r.get("source")) == "pubmed" else str(
            OPENFDA_ENDPOINTS.get(str(r.get("dataset")), {}).get("label") or r.get("dataset") or "药品"
        )
        out(f"[{i}] {truncate_display(r.get('title') or '(无标题)', 90)}")
        meta: List[str] = [f"来源={tag}"]
        if r.get("id"):
            meta.append(f"ID={r['id']}")
        if r.get("date"):
            meta.append(str(r["date"]))
        if r.get("journal"):
            meta.append(truncate_display(r["journal"], 40))
        if r.get("manufacturer"):
            meta.append(truncate_display(r["manufacturer"], 30))
        if r.get("doi"):
            meta.append(f"DOI={r['doi']}")
        out("    " + " · ".join(meta))
        if show_abstract and r.get("abstract"):
            out("    " + truncate_display(str(r["abstract"]).replace("\n", " "), 300))
        out("")


# --------------------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------------------


def cmd_selftest(args: argparse.Namespace, program_dir: Path) -> int:
    """自检：离线检查本地设施，联网验证接口可达与结构未变。

    设计意图：上游改版是本项目最大的持续性风险。
    自检把"接口还能不能用、结构还是不是那个结构"变成一条命令，
    让问题在爬取之前暴露，而不是爬完之后才发现收了一堆空数据。
    """
    cfg, cfg_path = load_config(program_dir, getattr(args, "config", None))
    resolve_credentials(cfg, args)
    lib = Library(Path(cfg["output_dir"]))
    offline = parse_bool(getattr(args, "offline", False))

    ok_n = warn_n = fail_n = 0

    def ok(msg: str) -> None:
        nonlocal ok_n
        ok_n += 1
        out(f"  [✓] {msg}")

    def warn(msg: str) -> None:
        nonlocal warn_n
        warn_n += 1
        out(f"  [!] {msg}")

    def fail(msg: str) -> None:
        nonlocal fail_n
        fail_n += 1
        out(f"  [✗] {msg}")

    out(f"\n{APP_TITLE} 自检　v{APP_VERSION}")
    out("=" * 66)

    # ---- 本地 ----
    out("\n【本地环境】")
    out(f"  Python {sys.version.split()[0]}")

    try:
        import tkinter  # noqa: F401

        ok("tkinter 可用（图形界面可启动）")
    except Exception as exc:
        warn(f"tkinter 不可用（{exc}），只能用命令行")

    if cfg_path:
        ok(f"配置文件：{cfg_path}")
    else:
        warn("未找到 config.json，将使用内置默认值")

    try:
        lib.ensure()
        ok(f"数据目录可写：{lib.root}")
    except OSError as exc:
        fail(f"数据目录不可写：{lib.root}（{exc}）")
        return 1

    # 磁盘空间
    try:
        import shutil as _sh

        usage = _sh.disk_usage(str(lib.root))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1:
            warn(f"磁盘剩余仅 {free_gb:.2f} GB")
        else:
            ok(f"磁盘剩余 {free_gb:.1f} GB")
    except Exception:
        pass

    # 记录库
    n = len(lib.load_records())
    ok(f"已有记录 {n:,} 条")

    # XLSX 导出自测（纯标准库实现，值得每次验证）
    try:
        probe = lib.index_dir / "_selftest.xlsx"
        write_xlsx(probe, [("测试", ["列1", "列2"], [[1, "中文"], [2, None]])])
        size = file_size(probe)
        try:
            os.remove(native_path(probe))
        except OSError:
            pass
        if size > 0:
            ok(f"xlsx 导出可用（{format_size(size)}）")
        else:
            fail("xlsx 导出产出为空文件")
    except Exception as exc:
        fail(f"xlsx 导出失败：{exc}")

    # ---- 凭据 ----
    out("\n【凭据】")
    out(f"  {credentials_source(cfg)}")
    if not cfg.get("openfda_api_key"):
        warn("未配置 openFDA API key —— 每天仅 1000 次请求，大规模爬取会被卡住")
    if not cfg.get("pubmed_api_key"):
        warn("未配置 NCBI API key —— 限 3 请求/秒（有 key 可到 10/秒）")
    if not cfg.get("pubmed_email"):
        warn("未填写 email —— NCBI 明确要求注册 tool+email 才能在封 IP 后解封")

    if offline:
        out("\n【联网检查】已跳过（--offline）")
        out("=" * 66)
        out(f"结果：{ok_n} 项通过 · {warn_n} 项提醒 · {fail_n} 项失败")
        return 0 if fail_n == 0 else 1

    # ---- 联网 ----
    out("\n【联网与接口结构】")
    http = HttpClient(cfg, verbose=False)
    openfda = OpenFDAClient(http, cfg, verbose=False)
    pubmed = PubMedClient(http, cfg, verbose=False)

    # openFDA：探量 + 结构校验
    try:
        total = openfda.count_total("label", "openfda.brand_name:\"aspirin\"")
        if total > 0:
            ok(f"openFDA 可达（阿司匹林标签 {total:,} 条）")
        else:
            warn("openFDA 可达但查询返回 0 条，请确认查询语法未变")
    except RateLimited as exc:
        warn(f"openFDA 限流：{exc}")
    except EmptyResult:
        warn("openFDA 查到空结果（可能查询语法变了）")
    except ApiShapeError as exc:
        fail(f"openFDA 结构校验失败：{exc}")
    except CrawlError as exc:
        fail(f"openFDA 不可达：{exc}")

    # 404 空结果语义 —— 这是最容易踩的坑，明确验证
    try:
        res = openfda.query("label", search='openfda.brand_name:"zzzznotadrugzzzz"', limit=1)
        if res.get("empty") or not res.get("results"):
            ok("openFDA 的 404「查无结果」被正确识别为空结果（不会误判为故障）")
        else:
            warn("预期空结果却拿到了数据，请复核该测试查询")
    except Exception as exc:
        fail(f"openFDA 空结果语义验证失败：{exc}")

    # 分页上限校验
    try:
        openfda.query("label", search="", limit=1001)
        warn("openFDA 接受了 limit=1001（上限应为 1000，可能已放宽）")
    except HttpError as exc:
        if exc.status == 400:
            ok("openFDA 正确拒绝 limit>1000")
        else:
            warn(f"limit 上限探测返回 HTTP {exc.status}")
    except CrawlError as exc:
        warn(f"limit 上限探测异常：{exc}")

    # PubMed
    try:
        cnt = pubmed.count("aspirin[Title/Abstract]")
        if cnt > 0:
            ok(f"PubMed E-utilities 可达（阿司匹林文献 {cnt:,} 篇）")
        else:
            warn("PubMed 可达但计数为 0")
    except RateLimited as exc:
        warn(f"PubMed 限流：{exc}")
    except ApiShapeError as exc:
        fail(f"PubMed 结构校验失败：{exc}")
    except CrawlError as exc:
        fail(f"PubMed 不可达：{exc}")

    # 解析链路：取一篇真实文献走通全流程
    try:
        xml = pubmed.efetch(ids=["33301246"], retmode="xml")
        parsed = parse_pubmed_articles(xml)
        if parsed:
            p = parsed[0]
            checks = []
            if p.get("title"):
                checks.append("标题")
            if p.get("abstract"):
                checks.append("摘要")
            if p.get("authors"):
                checks.append("作者")
            if p.get("mesh"):
                checks.append("MeSH")
            if p.get("doi"):
                checks.append("DOI")
            ok(f"PubMed XML 解析链路正常（取到 {'/'.join(checks) or '空记录'}）")
            if not p.get("doi"):
                warn("该样本无 DOI（部分老文献确实没有，非故障）")
        else:
            fail("PubMed XML 解析出 0 篇 —— 上游可能已改版")
    except ApiShapeError as exc:
        fail(f"PubMed XML 结构校验失败：{exc}")
    except CrawlError as exc:
        fail(f"PubMed 取记录失败：{exc}")

    # retmax 上限实测值验证（9,999 而非文档说的 10,000）
    try:
        probe = pubmed.esearch("cancer", retmax=100000, use_history=False, count_only=True)
        got = len(probe.get("ids") or [])
        if probe.get("count", 0) > 0:
            ok(f"PubMed retmax 上限确认（请求 100000，返回 {got} 个 ID，上限应为 {PUBMED_MAX_RETMAX}）")
    except ApiShapeError as exc:
        msg = str(exc)
        if "cannot be larger" in msg or "retmax" in msg.lower():
            warn(f"retmax 上限行为有变化：{msg}")
        else:
            warn(f"retmax 探测异常：{exc}")
    except CrawlError as exc:
        warn(f"retmax 探测异常：{exc}")

    out("")
    out("=" * 66)
    out(f"结果：{ok_n} 项通过 · {warn_n} 项提醒 · {fail_n} 项失败")
    out(f"网络统计：{http.summary()}")
    if fail_n:
        out("\n存在失败项 —— 上游接口很可能已改版。")
        out("请查阅 docs/DATA_SOURCE_NOTES.md，并优先尝试在 config.json 的 endpoints 中覆盖地址。")
    return 0 if fail_n == 0 else 1


# --------------------------------------------------------------------------------------
# CLI 命令
# --------------------------------------------------------------------------------------


def prepare_cfg(args: argparse.Namespace, program_dir: Path) -> Tuple[Dict[str, Any], Library]:
    cfg, cfg_path = load_config(program_dir, getattr(args, "config", None))
    resolve_credentials(cfg, args)

    out_dir = getattr(args, "out", None) or cfg.get("output_dir")
    if out_dir:
        cfg["output_dir"] = str(out_dir)

    # 命令行覆盖配置
    for arg_name, cfg_name in (
        ("proxy", "proxy"), ("concurrency", "concurrency"),
        ("max_retries", "max_retries"), ("timeout", "timeout"),
        ("date_from", "date_from"), ("date_to", "date_to"),
        ("segment", "segment_strategy"), ("sort", "pubmed_sort"),
        ("datetype", "pubmed_datetype"),
    ):
        v = getattr(args, arg_name, None)
        if v not in (None, ""):
            cfg[cfg_name] = v
    if getattr(args, "rps", None):
        cfg["requests_per_second"] = float(args.rps)

    lib = Library(Path(cfg["output_dir"]))
    lib.ensure()
    return cfg, lib


def build_plan(args: argparse.Namespace, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把命令行参数转成爬取计划。

    一条命令可以同时指定 FDA 端点与 PubMed 查询 —— 药学研究的常见场景
    正是"既要知道这个药批了什么，也要知道别人研究了什么"。
    """
    plan: List[Dict[str, Any]] = []

    datasets = getattr(args, "dataset", None)
    if datasets:
        ds_list = [d.strip() for d in str(datasets).split(",") if d.strip()]
    elif getattr(args, "all_datasets", False):
        ds_list = list(cfg.get("openfda_datasets") or list(OPENFDA_ENDPOINTS))
    else:
        ds_list = []

    search = str(getattr(args, "search", "") or "").strip()
    if ds_list:
        if not search:
            raise CrawlError(
                "爬取 openFDA 需要提供 --search 查询条件。\n"
                "例如：--search 'openfda.brand_name:\"aspirin\"'\n"
                "语法见 docs/DATA_SOURCE_NOTES.md 第 1.4 节。"
            )
        for ds in ds_list:
            if ds not in OPENFDA_ENDPOINTS:
                raise CrawlError(
                    f"未知数据集 {ds!r}；可用：{', '.join(sorted(OPENFDA_ENDPOINTS))}"
                )
            plan.append({
                "source": "openfda",
                "dataset": ds,
                "search": search,
                "label": str(getattr(args, "label", "") or search),
            })

    term = str(getattr(args, "term", "") or "").strip()
    if term:
        plan.append({
            "source": "pubmed",
            "term": term,
            "sort": str(getattr(args, "sort", "") or ""),
            "reldate": str(getattr(args, "reldate", "") or ""),
        })

    if not plan:
        raise CrawlError(
            "没有指定要爬什么。请用以下之一：\n"
            "  --dataset label --search 'openfda.brand_name:\"aspirin\"'   # FDA 药品标签\n"
            "  --term 'aspirin[Title/Abstract]'                            # PubMed 文献\n"
            "  --all-datasets --search '...'                               # 所有 FDA 药品端点"
        )
    return plan


def cmd_crawl(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)

    try:
        plan = build_plan(args, cfg)
    except CrawlError as exc:
        out(f"\n✗ {exc}\n")
        return 2

    max_records = int(getattr(args, "max_records", 0) or cfg.get("max_records_per_run") or 0)

    out(f"\n{APP_TITLE}　v{APP_VERSION}")
    out("=" * 66)
    out(f"数据目录　{lib.root}")
    out(f"凭据　　　{credentials_source(cfg)}")
    rng = DateRange.from_cfg(cfg)
    out(f"时间范围　{rng.describe()}")
    if max_records:
        out(f"本次上限　{max_records:,} 条")
    out("")
    out("爬取计划：")
    for i, t in enumerate(plan, start=1):
        if t["source"] == "openfda":
            nice = OPENFDA_ENDPOINTS.get(str(t["dataset"]), {}).get("label", t["dataset"])
            out(f"  {i}. FDA · {nice} ← {truncate_display(t['search'], 70)}")
        else:
            out(f"  {i}. PubMed ← {truncate_display(t['term'], 70)}")
    out("")

    session = CrawlSession(cfg, lib, verbose=True)
    try:
        summary = session.run(plan, max_records=max_records)
    except ApiShapeError as exc:
        out(f"\n✗ {exc}\n")
        return 3

    out("")
    out("-" * 66)
    out(f"本次获取　{summary['fetched']:,} 条")
    out(f"入库　　　{summary['stored']:,} 条")
    if summary["failed"]:
        out(f"失败　　　{summary['failed']:,} 条")
    out(f"耗时　　　{magnitude_seconds(summary['elapsed'])}")
    out(f"网络　　　{summary['http']}")
    if summary.get("stopped"):
        out("\n[已安全停止] 进度已保存，下次运行会从这里继续。")

    if summary["stored"] and parse_bool(cfg.get("auto_export"), True):
        out("\n正在重建索引与导出…")
        res = lib.rebuild_all(cfg)
        out(f"  记录总数　{res['records']:,}")
        for key, label in (
            ("csv", "CSV"), ("excel", "Excel"), ("sqlite", "SQLite"),
            ("markdown", "Markdown"), ("ris", "RIS"), ("bibtex", "BibTeX"),
            ("medline", "MEDLINE"),
        ):
            if key in res:
                out(f"  {label:<9} {res[key]}")
        if "excel_error" in res:
            out(f"  [!] Excel 导出失败：{res['excel_error']}")
        links = res.get("links") or {}
        if any(links.values()):
            out(f"  分类链接　物质 {links.get('substance', 0)}　"
                f"期刊 {links.get('journal', 0)}　检索词 {links.get('query', 0)}")

    out("")
    return 0


def cmd_search(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    recs = lib.load_records()
    if not recs:
        out("本地还没有数据，先跑一次 crawl 吧。")
        return 1

    rng = DateRange(
        parse_user_date(getattr(args, "date_from", None), "start"),
        parse_user_date(getattr(args, "date_to", None), "end"),
    )
    sources = [s.strip() for s in str(getattr(args, "source", "") or "").split(",") if s.strip()]
    datasets = [s.strip() for s in str(getattr(args, "dataset", "") or "").split(",") if s.strip()]

    hits = search_records(
        recs,
        keyword=str(getattr(args, "keyword", "") or ""),
        sources=sources,
        datasets=datasets,
        substances=[s.strip() for s in str(getattr(args, "substance", "") or "").split(",") if s.strip()],
        journals=[s.strip() for s in str(getattr(args, "journal", "") or "").split(",") if s.strip()],
        mesh=[s.strip() for s in str(getattr(args, "mesh", "") or "").split(",") if s.strip()],
        years=[s.strip() for s in str(getattr(args, "year", "") or "").split(",") if s.strip()],
        date_range=rng,
        field_scope=str(getattr(args, "scope", "all") or "all"),
        has_abstract=parse_bool(getattr(args, "has_abstract", False)),
        has_doi=parse_bool(getattr(args, "has_doi", False)),
        match_all=parse_bool(getattr(args, "match_all", False)),
    )

    out("")
    print_records(hits, limit=int(getattr(args, "limit", 30) or 30),
                  show_abstract=parse_bool(getattr(args, "show_abstract", False)))

    if getattr(args, "export", None) and hits:
        path = Path(args.export)
        write_xlsx(
            path,
            [("检索结果", EXPORT_FIELDS, [Library._row(r) for r in hits])],
        )
        out(f"已导出 {len(hits):,} 条到 {path}")
    return 0


def cmd_stats(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    st = lib.stats()
    out(f"\n{APP_TITLE} 数据统计")
    out("=" * 66)
    out(f"记录总数　{st['total']:,}（{format_size(st['size'])}）")
    out(f"含摘要　　{st['with_abstract']:,}")
    out(f"含 DOI　　{st['with_doi']:,}")
    out("")
    if st["by_source"]:
        out("按来源：")
        label_of = {"openfda": "FDA 药品数据", "pubmed": "PubMed 文献"}
        for k, v in sorted(st["by_source"].items(), key=lambda x: -x[1]):
            out(f"  {label_of.get(k, k):<16} {v:>10,}")
    if st["by_dataset"]:
        out("\n按数据集：")
        for k, v in sorted(st["by_dataset"].items(), key=lambda x: -x[1]):
            nice = OPENFDA_ENDPOINTS.get(k, {}).get("label", k)
            out(f"  {nice:<20} {v:>10,}")
    if st["by_year"]:
        out("\n按年份（前 20）：")
        for k in sorted(st["by_year"], reverse=True)[:20]:
            out(f"  {k}　{st['by_year'][k]:>8,}")
    out("")
    out(f"记录文件　{st['records_path']}")
    out("")
    return 0


def cmd_reindex(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    out("\n正在重建索引与导出…")
    res = lib.rebuild_all(cfg)
    out(f"  记录总数　{res['records']:,}")
    for key in ("csv", "excel", "sqlite", "markdown", "ris", "bibtex", "medline"):
        if key in res:
            out(f"  {key:<9} {res[key]}")
    if "excel_error" in res:
        out(f"  [!] Excel 导出失败：{res['excel_error']}")
    links = res.get("links") or {}
    out(f"  分类链接　物质 {links.get('substance', 0)}　期刊 {links.get('journal', 0)}　"
        f"检索词 {links.get('query', 0)}")
    out("")
    return 0


def cmd_reset(args: argparse.Namespace, program_dir: Path) -> int:
    """清空断点状态（不是清空数据）。"""
    cfg, lib = prepare_cfg(args, program_dir)
    what = str(getattr(args, "what", "all") or "all")
    if what in ("all", "session"):
        SessionState(lib.state_dir / "session.json").reset()
        out("  [✓] 已清空会话状态")
    if what in ("all", "segments"):
        SegmentStore(lib.state_dir / "segments.json").reset()
        out("  [✓] 已清空分段进度")
    if what in ("all", "ids"):
        IdCache(lib.state_dir / "pubmed_ids.json").reset()
        out("  [✓] 已清空 PubMed ID 缓存")
    out("\n下次运行将从头爬取（已入库的记录仍会按 ID 去重）。\n")
    return 0


def cmd_auth(args: argparse.Namespace, program_dir: Path) -> int:
    """管理 API 凭据。

    凭据写到用户目录（``~/.pharma_crawler/``），**不写进项目目录** ——
    项目目录常被同步/分享/提交，把 key 放在那里迟早会泄漏。
    """
    cfg, _lib = prepare_cfg(args, program_dir)
    action = str(getattr(args, "action", "status") or "status")

    if action == "status":
        out(f"\n凭据状态：{credentials_source(cfg)}")
        store = load_credential_store()
        out(f"凭据库：{CRED_FILE}（{'存在' if store else '未创建'}）")
        out("\n申请地址：")
        out("  openFDA  https://open.fda.gov/apis/authentication/")
        out("  NCBI     https://account.ncbi.nlm.nih.gov/ → Account settings → API Key Management")
        out("")
        return 0

    if action == "set":
        updates: Dict[str, str] = {}
        if getattr(args, "openfda_key", None):
            updates["openfda_api_key"] = str(args.openfda_key).strip()
        if getattr(args, "pubmed_key", None):
            updates["pubmed_api_key"] = str(args.pubmed_key).strip()
        if getattr(args, "pubmed_email", None):
            updates["pubmed_email"] = str(args.pubmed_email).strip()
        if not updates:
            out("没有要保存的内容。用 --openfda-key / --pubmed-key / --pubmed-email 指定。")
            return 2
        save_credential_store(**updates)
        out(f"\n[✓] 已保存到 {CRED_FILE}")
        for k in updates:
            out(f"    {k} = {'*' * 8}{updates[k][-4:] if len(updates[k]) > 4 else ''}")
        out("\n提示：凭据库优先级高于 config.json，低于环境变量与命令行参数。")
        out("")
        return 0

    if action == "clear":
        if clear_credential_store():
            out(f"[✓] 已删除 {CRED_FILE}")
        else:
            out("凭据库不存在，无需清理。")
        return 0

    out(f"未知操作：{action}（可用：status / set / clear）")
    return 2


def cmd_gui(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        out(f"此 Python 没有 tkinter（{exc}），只能用命令行模式。")
        return 1
    try:
        from pharma_gui import launch
    except ImportError:
        sys.path.insert(0, str(program_dir))
        try:
            from pharma_gui import launch
        except ImportError as exc:
            out(f"无法加载图形界面模块 pharma_gui.py：{exc}")
            return 1
    return launch(cfg, lib, program_dir)


# --------------------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pharma_crawler.py",
        description=f"{APP_TITLE}　v{APP_VERSION}　—　FDA 药品数据 + PubMed 药学文献",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
常用示例
--------
  # ① FDA 药品说明书标签（按品牌名）
  python pharma_crawler.py crawl --dataset label --search 'openfda.brand_name:"aspirin"'

  # ② FDA 药品召回（按治疗类别）
  python pharma_crawler.py crawl --dataset enforcement --search 'classification:"Class I"'

  # ③ 一次爬多个 FDA 端点
  python pharma_crawler.py crawl --dataset label,ndc,shortages --search 'openfda.generic_name:"metformin"'

  # ④ PubMed 文献
  python pharma_crawler.py crawl --term 'metformin[Title/Abstract] AND 2020:2024[PDAT]'

  # ⑤ FDA + PubMed 一起爬（药学研究的典型需求）
  python pharma_crawler.py crawl \\
      --dataset drugsfda --search 'sponsor_name:"Pfizer"' \\
      --term 'pfizer[Affiliation] AND vaccine[Title/Abstract]'

  # ⑥ 只探量不下载
  python pharma_crawler.py crawl --dataset event --search 'receivedate:[20230101+TO+20231231]' --dry-run

  # ⑦ 检索本地库
  python pharma_crawler.py search 阿司匹林 --scope all
  python pharma_crawler.py search --mesh "Diabetes Mellitus, Type 2" --year 2023,2024

  # ⑧ 其他
  python pharma_crawler.py stats
  python pharma_crawler.py selftest
  python pharma_crawler.py auth set --openfda-key XXX --pubmed-key YYY --pubmed-email me@x.com
  python pharma_crawler.py gui

重要提示
--------
  · openFDA 未鉴权每天仅 1000 次请求，建议先 `auth set --openfda-key`。
  · PubMed 硬上限为 9,999 条/查询（实测值，官方文档写的 10000 是错的），
    超出需按时间切分，程序会自动处理。
  · 上游改版时程序会明确报错而非静默失败；接口地址可在 config.json 覆盖。
""",
    )
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    p.add_argument("--config", help="指定配置文件路径（默认用程序目录下的 config.json）")
    p.add_argument("--out", help="覆盖数据输出目录")

    sub = p.add_subparsers(dest="command", metavar="<命令>")

    # ---- crawl ----
    c = sub.add_parser("crawl", help="爬取 FDA 药品数据与 PubMed 文献")
    c.add_argument("--dataset", help="openFDA 端点，逗号分隔："
                                     "label/enforcement/event/ndc/drugsfda/shortages/orangebook")
    c.add_argument("--all-datasets", action="store_true", help="爬取全部 FDA 药品端点")
    c.add_argument("--search", help="openFDA 查询表达式，如 'openfda.brand_name:\"aspirin\"'")
    c.add_argument("--label", help="本次检索的备注名（会写进记录的 query 字段）")
    c.add_argument("--term", help="PubMed 查询表达式")
    c.add_argument("--reldate", help="PubMed：最近 N 天内入库的文献")
    c.add_argument("--sort", help="PubMed 排序：pub_date / relevance / Author / JournalName")
    c.add_argument("--datetype", choices=["pdat", "mdat", "edat"], help="PubMed 日期字段类型")
    c.add_argument("--date-from", help="起始日期，如 2020-01-01")
    c.add_argument("--date-to", help="结束日期，如 2024-12-31")
    c.add_argument("--segment", choices=["auto", "year", "month", "none"],
                   help="突破上限的切分策略（默认 auto）")
    c.add_argument("--max-records", type=int, default=0, help="本次最多取多少条（0=不限）")
    c.add_argument("--dry-run", action="store_true", help="只探量不下载")
    c.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7890")
    c.add_argument("--rps", type=float, help="每秒请求数（谨慎调高，上游会封 IP）")
    c.add_argument("--timeout", type=float, help="单次请求超时秒数")
    c.add_argument("--max-retries", type=int, help="失败重试次数")
    c.add_argument("--concurrency", type=int, help="保留参数（当前为串行请求）")
    c.add_argument("--openfda-key", help="openFDA API key（临时覆盖）")
    c.add_argument("--pubmed-key", help="NCBI API key（临时覆盖）")
    c.add_argument("--pubmed-email", help="联系邮箱（NCBI 建议填写）")

    # ---- search ----
    s = sub.add_parser("search", help="检索本地已爬取的数据")
    s.add_argument("keyword", nargs="?", default="", help="关键词")
    s.add_argument("--source", help="来源过滤：openfda / pubmed，逗号分隔")
    s.add_argument("--dataset", help="数据集过滤，逗号分隔")
    s.add_argument("--substance", help="物质名（通用名/成分），逗号分隔")
    s.add_argument("--journal", help="期刊名，逗号分隔")
    s.add_argument("--mesh", help="MeSH 主题词，逗号分隔")
    s.add_argument("--year", help="年份，逗号分隔")
    s.add_argument("--date-from", help="起始日期")
    s.add_argument("--date-to", help="结束日期")
    s.add_argument("--scope", choices=["all", "title", "abstract", "name", "id"],
                   default="all", help="关键词搜索范围")
    s.add_argument("--has-abstract", action="store_true", help="只看有摘要的")
    s.add_argument("--has-doi", action="store_true", help="只看有 DOI 的")
    s.add_argument("--match-all", action="store_true", help="关键词必须全部命中（默认任一命中）")
    s.add_argument("--limit", type=int, default=30, help="显示条数")
    s.add_argument("--show-abstract", action="store_true", help="显示摘要片段")
    s.add_argument("--export", help="把结果导出为 xlsx")
    s.add_argument("--openfda-key", help=argparse.SUPPRESS)
    s.add_argument("--pubmed-key", help=argparse.SUPPRESS)
    s.add_argument("--pubmed-email", help=argparse.SUPPRESS)

    # ---- stats / reindex ----
    for name, helper in (("stats", "显示数据统计"), ("reindex", "重建索引与导出")):
        sp = sub.add_parser(name, help=helper)
        sp.add_argument("--openfda-key", help=argparse.SUPPRESS)
        sp.add_argument("--pubmed-key", help=argparse.SUPPRESS)
        sp.add_argument("--pubmed-email", help=argparse.SUPPRESS)

    # ---- reset ----
    r = sub.add_parser("reset", help="清空断点续爬状态（不清数据）")
    r.add_argument("--what", choices=["all", "session", "segments", "ids"], default="all")

    # ---- auth ----
    a = sub.add_parser("auth", help="管理 API 凭据")
    a.add_argument("action", nargs="?", choices=["status", "set", "clear"], default="status")
    a.add_argument("--openfda-key", help="openFDA API key")
    a.add_argument("--pubmed-key", help="NCBI API key")
    a.add_argument("--pubmed-email", help="联系邮箱")

    # ---- selftest ----
    st = sub.add_parser("selftest", help="自检：本地环境 + 接口连通性 + 结构校验")
    st.add_argument("--offline", action="store_true", help="跳过联网检查")
    st.add_argument("--openfda-key", help=argparse.SUPPRESS)
    st.add_argument("--pubmed-key", help=argparse.SUPPRESS)
    st.add_argument("--pubmed-email", help=argparse.SUPPRESS)

    # ---- gui ----
    g = sub.add_parser("gui", help="启动图形界面")
    g.add_argument("--openfda-key", help=argparse.SUPPRESS)
    g.add_argument("--pubmed-key", help=argparse.SUPPRESS)
    g.add_argument("--pubmed-email", help=argparse.SUPPRESS)

    return p


def cmd_dry_run(args: argparse.Namespace, program_dir: Path) -> int:
    """只探量、不下载 —— 让用户在真正开跑前知道量级。"""
    cfg, lib = prepare_cfg(args, program_dir)
    plan = build_plan(args, cfg)

    out(f"\n{APP_TITLE}　预估（干跑，不下载）")
    out("=" * 66)

    http = HttpClient(cfg, verbose=False)
    openfda = OpenFDAClient(http, cfg, verbose=False)
    pubmed = PubMedClient(http, cfg, verbose=False)
    rng = DateRange.from_cfg(cfg)

    grand_total = 0
    for task in plan:
        if task["source"] == "openfda":
            ds = str(task["dataset"])
            nice = OPENFDA_ENDPOINTS.get(ds, {}).get("label", ds)
            search = str(task["search"])
            date_field = str(OPENFDA_ENDPOINTS.get(ds, {}).get("date_field") or "")
            if rng.active() and date_field:
                span = format_range_for(
                    date_field, rng.start or _dt.date(1900, 1, 1), rng.end or _dt.date.today()
                )
                search = f"({search}) AND {date_field}:[{span}]"
            try:
                n = openfda.count_total(ds, search)
            except EmptyResult:
                n = 0
            except CrawlError as exc:
                out(f"  FDA · {nice}：查询失败（{exc}）")
                continue
            grand_total += n
            out(f"  FDA · {nice}")
            out(f"      命中 {n:,} 条")
            if n > OPENFDA_WINDOW:
                out(f"      ⚠️ 超过 {OPENFDA_WINDOW:,} 条窗口，将自动改用 search_after 游标滚动")
            # 请求数估算
            pages = (min(n, OPENFDA_WINDOW) + 999) // 1000
            out(f"      预计请求 ≈ {pages + 1:,} 次")
        else:
            term = str(task["term"])
            _, mind, maxd = CrawlSession._pubmed_term(term, rng, "pdat")
            try:
                n = pubmed.count(term, datetype="pdat" if (mind or maxd) else "",
                                 mindate=mind, maxdate=maxd,
                                 reldate=str(task.get("reldate") or ""))
            except CrawlError as exc:
                out(f"  PubMed：查询失败（{exc}）")
                continue
            grand_total += n
            out("  PubMed")
            out(f"      命中 {n:,} 篇")
            if n > PUBMED_MAX_RETMAX:
                slices = (n + PUBMED_MAX_RETMAX - 1) // PUBMED_MAX_RETMAX
                out(f"      ⚠️ 超过 {PUBMED_MAX_RETMAX:,} 条上限，将切分为约 {slices} 个时间片")
                out(f"      预计请求 ≈ {n // 200 + slices * 2:,} 次（批量 200 条/次）")
            else:
                out(f"      预计请求 ≈ {n // 200 + 2:,} 次")

    out("")
    out("-" * 66)
    out(f"合计命中　　{grand_total:,} 条")
    if cfg.get("openfda_api_key"):
        out("配额　　　　openFDA 有 key：120,000 次/天")
    else:
        out(f"配额　　　　openFDA 无 key：1,000 次/天"
            f"{'  ⚠️ 可能不够' if grand_total > 50000 else ''}")
    out(f"速率　　　　{cfg.get('requests_per_second')} 请求/秒")
    out("")
    out("估算仅供参考；实际以运行结果为准。去掉 --dry-run 即开始下载。")
    out("")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 让输出在管道/重定向下也不乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None) or "gui"

    handlers = {
        "crawl": lambda: cmd_dry_run(args, PROGRAM_DIR) if getattr(args, "dry_run", False)
        else cmd_crawl(args, PROGRAM_DIR),
        "search": lambda: cmd_search(args, PROGRAM_DIR),
        "stats": lambda: cmd_stats(args, PROGRAM_DIR),
        "reindex": lambda: cmd_reindex(args, PROGRAM_DIR),
        "reset": lambda: cmd_reset(args, PROGRAM_DIR),
        "auth": lambda: cmd_auth(args, PROGRAM_DIR),
        "selftest": lambda: cmd_selftest(args, PROGRAM_DIR),
        "gui": lambda: cmd_gui(args, PROGRAM_DIR),
    }
    handler = handlers.get(command)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler()
    except KeyboardInterrupt:
        out("\n\n[已中断] 进度已保存，下次运行会继续。")
        return 130
    except CrawlError as exc:
        out(f"\n✗ {exc}\n")
        return 1
    except ApiShapeError as exc:
        out(f"\n✗ {exc}\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
