# -*- coding: utf-8 -*-
"""网页界面：用标准库 http.server 提供界面，浏览器打开即用。

与 pharma_gui.py 的关系
-----------------------
pharma_gui.py（tkinter）原样保留，两者并存：

    启动.bat / python pharma_gui.py      桌面版
    启动网页版.bat / python webui.py      网页版（本文件）

两者共用 pharma_crawler.py 的同一套逻辑：抓取、检索、图库、导出、分类全部复用，
一个字都没改。所以网页版看到的检索结果、统计数字、导出文件与桌面版完全一致。

为什么不用 pywebview
--------------------
本项目**全部依赖都是标准库**（README 明确写着「纯 Python 标准库实现」，并解释了
为什么坚持这一点：双击即用、不需要 pip install、不会因为某个依赖库停止维护而烂掉）。
pywebview 会破坏这条原则。而界面需要的只是「把 HTML 给浏览器看」——http.server
是标准库，一行依赖都不用加。

抓取是长任务
------------
CrawlSession 通过 progress_cb 回调实时上报。网页版把回调收到的内容存进一个有界的
日志缓冲与计数快照，前端轮询 /api/crawl/status 拿进度。停止走 session.stop()，
是安全停止——已抓到的数据照常落盘，下次从断点继续。

接口
----
    GET  /                    界面
    GET  /app.css /app.js     静态资源
    GET  /api/meta            端点清单、配置摘要、图库统计、时间范围
    POST /api/plan            由表单生成抓取计划（纯计算，不联网）
    POST /api/crawl/start     开始抓取（后台线程）
    POST /api/crawl/stop      安全停止
    GET  /api/crawl/status    进度、计数、日志
    POST /api/search          本地多维检索
    GET  /api/record          单条记录全文
    GET  /api/library/stats   图库统计
    POST /api/library/rebuild 重建索引与分类
    POST /api/export          导出（csv/excel/sqlite/markdown/ris/bibtex/medline）
    POST /api/config          保存设置

运行
----
    python webui.py                   默认端口 8758，自动打开浏览器
    python webui.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pharma_crawler as pc

PROGRAM_DIR = Path(__file__).resolve().parent
WEBUI_DIR = PROGRAM_DIR / "webui"

# 日志缓冲：抓取过程会刷很多行，只留最近的，避免内存无限涨。
LOG_LIMIT = 400
# 检索结果最多一次带回多少条给表格（全量仍在服务端，详情按需取）。
SEARCH_PAGE = 300


def _log_file() -> Path:
    return PROGRAM_DIR / "logs" / "webui.log"


def log_line(text: str) -> None:
    try:
        p = _log_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:      # noqa: BLE001
        pass


# ---------------------------------------------------------------- 状态 --

class State:
    """进程级状态。http.server 是多线程的，所有可变状态都走这把锁。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cfg: dict = {}
        self.lib = None
        self.session = None            # 正在跑的 CrawlSession
        self.thread: threading.Thread | None = None
        self.running = False
        self.counters = {"fetched": 0, "skipped": 0, "failed": 0, "stored": 0}
        self.progress_total = 0
        self.log: deque = deque(maxlen=LOG_LIMIT)
        self.summary: dict | None = None
        self.error = ""
        self.started_at = 0.0
        self.last_search: list = []    # 上一次检索的完整结果，供详情按需取


STATE = State()


def _say(text: str) -> None:
    with STATE.lock:
        STATE.log.append(text)
    log_line(text)


def _bootstrap(out_dir: str = "") -> None:
    """载入配置并准备图库。

    空 Namespace 就够——prepare_cfg 里全是 getattr 带默认值；--out 与命令行版同义，
    用来把数据目录指到别处（临时库、另一份拷贝），不动 config.json。
    """
    args = argparse.Namespace(out=out_dir or None)
    cfg, lib = pc.prepare_cfg(args, PROGRAM_DIR)
    with STATE.lock:
        STATE.cfg = cfg
        STATE.lib = lib
    log_line("webui 启动 · 数据目录 %s" % cfg.get("output_dir"))


# ---------------------------------------------------------------- 元信息 --

def _safe_config() -> dict:
    """给界面看的配置摘要。API key 只回是否已填，绝不回明文。"""
    cfg = STATE.cfg
    return {
        "outputDir": cfg.get("output_dir", ""),
        "requestsPerSecond": cfg.get("requests_per_second"),
        "concurrency": cfg.get("concurrency"),
        "timeout": cfg.get("timeout"),
        "maxRetries": cfg.get("max_retries"),
        "proxy": cfg.get("proxy") or "",
        "dateFrom": cfg.get("date_from") or "",
        "dateTo": cfg.get("date_to") or "",
        "segmentStrategy": cfg.get("segment_strategy") or "auto",
        "maxRecordsPerRun": cfg.get("max_records_per_run") or 0,
        "hasOpenfdaKey": bool(str(cfg.get("openfda_api_key") or "").strip()),
        "hasPubmedKey": bool(str(cfg.get("pubmed_api_key") or "").strip()),
        "pubmedEmail": cfg.get("pubmed_email") or "",
        "pubmedSort": cfg.get("pubmed_sort") or "",
        "pubmedReldays": cfg.get("pubmed_reldate") or "",
        "export": {
            "csv": bool(cfg.get("export_csv")),
            "excel": bool(cfg.get("export_excel")),
            "sqlite": bool(cfg.get("export_sqlite")),
            "markdown": bool(cfg.get("export_markdown")),
            "ris": bool(cfg.get("export_ris")),
            "bibtex": bool(cfg.get("export_bibtex")),
            "medline": bool(cfg.get("export_medline")),
        },
    }


def meta_payload() -> dict:
    endpoints = [
        {"key": k, "label": v.get("label", k), "note": v.get("note", ""),
         "daily": bool(v.get("daily"))}
        for k, v in pc.OPENFDA_ENDPOINTS.items()
    ]
    default_ds = [d.strip() for d in str(STATE.cfg.get("openfda_datasets") or "").split(",") if d.strip()]
    try:
        rng = pc.DateRange.from_cfg(STATE.cfg)
        date_range = rng.describe()
    except Exception:      # noqa: BLE001
        date_range = ""

    return {
        "title": pc.APP_TITLE,
        "version": pc.APP_VERSION,
        "endpoints": endpoints,
        "defaultDatasets": default_ds,
        "config": _safe_config(),
        "dateRange": date_range,
        "credentials": pc.credentials_source(STATE.cfg),
        "stats": _stats_safe(),
        "exportDir": STATE.cfg.get("output_dir", ""),
    }


def _stats_safe() -> dict:
    try:
        with STATE.lock:
            lib = STATE.lib
        return lib.stats() if lib else {}
    except Exception as exc:      # noqa: BLE001
        return {"error": str(exc)}


# ---------------------------------------------------------------- 抓取 --

def _namespace(payload: dict) -> argparse.Namespace:
    datasets = payload.get("datasets") or []
    if isinstance(datasets, str):
        datasets = [d.strip() for d in datasets.split(",") if d.strip()]
    ns = argparse.Namespace()
    ns.dataset = ",".join(datasets) if datasets else None
    ns.all_datasets = bool(payload.get("allDatasets"))
    ns.search = str(payload.get("search") or "")
    ns.label = str(payload.get("label") or "")
    ns.term = str(payload.get("term") or "")
    ns.sort = str(payload.get("sort") or "")
    ns.reldate = str(payload.get("reldate") or "")
    ns.max_records = int(payload.get("maxRecords") or 0)
    return ns


def make_plan(payload: dict) -> list:
    with STATE.lock:
        cfg = STATE.cfg
    return pc.build_plan(_namespace(payload), cfg)


def _crawl_worker(plan: list, max_records: int) -> None:
    def on_progress(payload: dict) -> None:
        with STATE.lock:
            for k in ("fetched", "skipped", "failed", "stored"):
                if k in payload:
                    STATE.counters[k] = int(payload.get(k) or 0)
            total = payload.get("total")
            if total:
                STATE.progress_total = int(total)
        msg = payload.get("message")
        if msg:
            _say(str(msg))

    try:
        session = pc.CrawlSession(STATE.cfg, STATE.lib, verbose=False, progress_cb=on_progress)
        with STATE.lock:
            STATE.session = session
        _say("开始抓取 · %d 个任务" % len(plan))

        summary = session.run(plan, max_records=max_records)
        with STATE.lock:
            STATE.summary = summary

        _say("✓ 完成：获取 {fetched:,} 条 · 入库 {stored:,} 条".format(**{
            "fetched": int(summary.get("fetched") or 0),
            "stored": int(summary.get("stored") or 0)}))
        if summary.get("failed"):
            _say("⚠ 失败 %s 条" % summary["failed"])
        if summary.get("stopped"):
            _say("[已安全停止] 下次运行会从断点继续。")

        if summary.get("stored"):
            _say("正在重建索引与导出…")
            res = STATE.lib.rebuild_all(STATE.cfg)
            _say("  记录总数 {:,}".format(int(res.get("records") or 0)))
            for key in ("csv", "excel", "sqlite", "markdown", "ris", "medline"):
                if key in res:
                    _say("  %s → %s" % (key, res[key]))
            if "excel_error" in res:
                _say("  ⚠ Excel 导出失败：%s" % res["excel_error"])
    except Exception as exc:      # noqa: BLE001
        with STATE.lock:
            STATE.error = str(exc)
        _say("✗ 出错：%s" % exc)
        log_line(traceback.format_exc())
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.session = None


def start_crawl(payload: dict) -> dict:
    with STATE.lock:
        if STATE.running:
            raise ValueError("已有抓取在进行中")
        plan = make_plan(payload)
        max_records = int(payload.get("maxRecords") or 0) or int(STATE.cfg.get("max_records_per_run") or 0)
        STATE.running = True
        STATE.error = ""
        STATE.summary = None
        STATE.counters = {"fetched": 0, "skipped": 0, "failed": 0, "stored": 0}
        STATE.progress_total = 0
        STATE.started_at = time.time()
        STATE.log.clear()

    STATE.thread = threading.Thread(target=_crawl_worker, args=(plan, max_records), daemon=True)
    STATE.thread.start()
    return {"ok": True, "plan": plan, "maxRecords": max_records}


def crawl_status() -> dict:
    with STATE.lock:
        return {
            "running": STATE.running,
            "counters": dict(STATE.counters),
            "total": STATE.progress_total,
            "elapsed": round(time.time() - STATE.started_at, 1) if STATE.started_at else 0,
            "log": list(STATE.log),
            "summary": STATE.summary,
            "error": STATE.error,
        }


def stop_crawl() -> dict:
    with STATE.lock:
        session = STATE.session
    if session is None:
        return {"ok": True, "stopped": False, "message": "没有正在进行的抓取"}
    session.stop()
    _say("已请求停止，正在收尾…")
    return {"ok": True, "stopped": True}


# ---------------------------------------------------------------- 检索 --

def do_search(payload: dict) -> dict:
    with STATE.lock:
        lib = STATE.lib
    recs = lib.load_records()

    date_range = None
    dfrom = str(payload.get("dateFrom") or "").strip()
    dto = str(payload.get("dateTo") or "").strip()
    if dfrom or dto:
        try:
            date_range = pc.DateRange(
                pc.parse_user_date(dfrom) if dfrom else None,
                pc.parse_user_date(dto) if dto else None)
        except Exception:      # noqa: BLE001
            date_range = None

    def lst(key: str) -> list:
        v = payload.get(key) or []
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        return list(v)

    hits = pc.search_records(
        recs,
        keyword=str(payload.get("keyword") or ""),
        sources=lst("sources"),
        datasets=lst("datasets"),
        substances=lst("substances"),
        journals=lst("journals"),
        mesh=lst("mesh"),
        pubtypes=lst("pubtypes"),
        years=lst("years"),
        date_range=date_range,
        field_scope=str(payload.get("fieldScope") or "all"),
        has_abstract=bool(payload.get("hasAbstract")),
        has_doi=bool(payload.get("hasDoi")),
        match_all=bool(payload.get("matchAll")),
        limit=0,
    )

    with STATE.lock:
        STATE.last_search = hits

    rows = [_row(r) for r in hits[:SEARCH_PAGE]]
    return {
        "ok": True,
        "total": len(hits),
        "shown": len(rows),
        "rows": rows,
        "facets": _facets(hits),
    }


def _key(r: dict) -> str:
    """记录键。record_key() 返回的是元组，这里拼成字符串给前端当 id 用。"""
    try:
        return "|".join(str(x) for x in pc.record_key(r))
    except Exception:      # noqa: BLE001
        return str(r.get("id") or r.get("pmid") or r.get("doi") or "")


def _row(r: dict) -> dict:
    """表格用的投影。全文留在服务端，详情按需取。"""
    return {
        "key": _key(r),
        "source": r.get("source") or "",
        "dataset": r.get("dataset") or "",
        "title": pc.truncate_display(r.get("title") or "", 160),
        "date": str(r.get("date") or r.get("pub_date") or ""),
        "journal": pc.truncate_display(r.get("journal") or "", 60),
        "hasAbstract": bool(str(r.get("abstract") or "").strip()),
        "hasDoi": bool(str(r.get("doi") or "").strip()),
    }


def _facets(recs: list) -> dict:
    """给筛选下拉框用：从命中结果里统计可选的来源/数据集/年份。"""
    def tally(key: str, limit: int = 60) -> list:
        c: dict = {}
        for r in recs:
            v = r.get(key)
            if v in (None, ""):
                continue
            v = str(v)
            c[v] = c.get(v, 0) + 1
        return [{"value": k, "count": n}
                for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]

    return {"sources": tally("source"), "datasets": tally("dataset"), "years": tally("year", 40)}


def get_record(key: str) -> dict:
    with STATE.lock:
        hits = list(STATE.last_search)
    for r in hits:
        if _key(r) == key:
            return {"ok": True, "record": r}
    raise ValueError("找不到这条记录（可能已重新检索）")


# ---------------------------------------------------------------- 图库 --

def do_rebuild() -> dict:
    with STATE.lock:
        lib = STATE.lib
        cfg = STATE.cfg
    res = lib.rebuild_all(cfg)
    return {"ok": True, "result": {k: (str(v) if isinstance(v, Path) else v) for k, v in res.items()}}


EXPORT_FORMATS = {
    "csv": "export_csv", "excel": "export_excel", "sqlite": "export_sqlite",
    "markdown": "export_markdown", "ris": "export_ris",
    "bibtex": "export_bibtex", "medline": "export_medline",
}


def do_export(payload: dict) -> dict:
    fmt = str(payload.get("format") or "csv").lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError("不支持的导出格式：%s" % fmt)
    with STATE.lock:
        lib = STATE.lib
    path = getattr(lib, EXPORT_FORMATS[fmt])()
    return {"ok": True, "format": fmt, "path": str(path),
            "name": os.path.basename(str(path)),
            "size": os.path.getsize(str(path)) if os.path.exists(str(path)) else 0}


# 允许从网页改的配置项（白名单，避免网页改到不该改的键）
CONFIG_KEYS = {
    "requestsPerSecond": ("requests_per_second", float),
    "concurrency": ("concurrency", int),
    "timeout": ("timeout", int),
    "maxRetries": ("max_retries", int),
    "proxy": ("proxy", str),
    "dateFrom": ("date_from", str),
    "dateTo": ("date_to", str),
    "segmentStrategy": ("segment_strategy", str),
    "maxRecordsPerRun": ("max_records_per_run", int),
    "pubmedEmail": ("pubmed_email", str),
    "pubmedSort": ("pubmed_sort", str),
    "pubmedReldays": ("pubmed_reldate", str),
}


def do_save_config(payload: dict) -> dict:
    changed = {}
    with STATE.lock:
        cfg = STATE.cfg
        for web_key, (cfg_key, cast) in CONFIG_KEYS.items():
            if web_key not in payload:
                continue
            raw = payload.get(web_key)
            try:
                cfg[cfg_key] = cast(raw) if raw not in (None, "") else ("" if cast is str else 0)
            except Exception:      # noqa: BLE001
                raise ValueError("配置项 %s 的值不合法：%r" % (web_key, raw))
            changed[cfg_key] = cfg[cfg_key]

    if not changed:
        return {"ok": True, "changed": {}}

    # 写回 config.json：只改这几个键，其余原样保留
    path = PROGRAM_DIR / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except Exception:      # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(changed)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _say("配置已保存：%s" % ", ".join(changed.keys()))
    return {"ok": True, "changed": changed}


# ---------------------------------------------------------------- HTTP --

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PharmaCrawlerWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):      # noqa: A003
        if os.environ.get("PC_WEB_VERBOSE"):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self, limit: int = 8 * 1024 * 1024) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:      # noqa: BLE001
            n = 0
        if n <= 0:
            return {}
        if n > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:      # noqa: BLE001
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是对象")
        return data

    def _static(self, rel: str) -> None:
        target = (WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(WEBUI_DIR.resolve())
        except ValueError:
            self._error("禁止访问", 403)
            return
        if not target.is_file():
            self._error("找不到 %s" % rel, 404)
            return
        with open(target, "rb") as fh:
            self._send(200, fh.read(), CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def do_GET(self):       # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path in ("/app.css", "/app.js"):
                self._static(path.lstrip("/"))
            elif path == "/api/meta":
                self._json(meta_payload())
            elif path == "/api/crawl/status":
                self._json(crawl_status())
            elif path == "/api/library/stats":
                self._json({"ok": True, "stats": _stats_safe()})
            elif path == "/api/record":
                self._json(get_record((query.get("key") or [""])[0]))
            else:
                self._error("未知路径 %s" % path, 404)
        except Exception as exc:      # noqa: BLE001
            log_line(traceback.format_exc())
            self._error(str(exc), 500)

    def do_POST(self):      # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/plan":
                self._json({"ok": True, "plan": make_plan(self._body())})
            elif path == "/api/crawl/start":
                self._json(start_crawl(self._body()))
            elif path == "/api/crawl/stop":
                self._json(stop_crawl())
            elif path == "/api/search":
                self._json(do_search(self._body()))
            elif path == "/api/library/rebuild":
                self._json(do_rebuild())
            elif path == "/api/export":
                self._json(do_export(self._body()))
            elif path == "/api/config":
                self._json(do_save_config(self._body()))
            else:
                self._error("未知路径 %s" % path, 404)
        except Exception as exc:      # noqa: BLE001
            log_line(traceback.format_exc())
            self._error(str(exc), 500)


# ---------------------------------------------------------------- 启动 --

def _free_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port, open_browser, out_dir = 8758, True, ""
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
        elif a == "--no-browser":
            open_browser = False
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0

    if not WEBUI_DIR.is_dir():
        print("找不到界面目录：%s" % WEBUI_DIR)
        return 1

    _bootstrap(out_dir)
    port = _free_port(port)
    url = "http://127.0.0.1:%d/" % port

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    print()
    print("  " + "=" * 58)
    print("   %s v%s · 网页版" % (pc.APP_TITLE, pc.APP_VERSION))
    print("  " + "=" * 58)
    print()
    print("   地址 / URL    %s" % url)
    print("   数据目录      %s" % STATE.cfg.get("output_dir"))
    print("   凭据          %s" % pc.credentials_source(STATE.cfg))
    print("   时间范围      %s" % meta_payload().get("dateRange"))
    print()
    print("   只监听本机。按 Ctrl+C 停止。")
    print()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
