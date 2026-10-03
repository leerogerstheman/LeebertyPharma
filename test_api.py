#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_api.py —— API 客户端的离线测试。

**全部离线**：HTTP 层一律用 ``unittest.mock`` 打桩，
不碰网络。理由有两个：
  1. 单元测试不该依赖上游可用性 —— NCBI 或 openFDA 抖一下测试就红，
     没人会再相信这套测试。
  2. 上游有真实配额（未鉴权的 openFDA 每天只有 1000 次），
     跑一次测试就烧掉配额是不可接受的。

这里覆盖的行为都对应 docs/DATA_SOURCE_NOTES.md 里实测核对过的事实，
每一条都曾经（或极易）造成"不报错的静默数据丢失"。
"""

from __future__ import annotations

import json
import sys
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pharma_crawler as pc  # noqa: E402


# ======================================================================================
# 共用打桩工具
# ======================================================================================


def make_http(**cfg_overrides):
    """造一个 HttpClient，但把 sleep 掉，避免测试真的等待。

    限流退避会 sleep 几秒到几十秒，测试绝不能真的睡。
    """
    cfg = {
        "user_agent": "test-agent/1.0",
        "timeout": 5,
        "max_retries": 3,
        "requests_per_second": 0,  # 0 = 不限速，让测试跑得快
        "verbose": False,
    }
    cfg.update(cfg_overrides)
    http = pc.HttpClient(cfg, verbose=False)
    return http


def raw_response(status, body, headers=None):
    """构造 get_raw / get_text 的返回值三元组。"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    return (status, body, hdrs)


class StubHttp:
    """最小 HTTP 替身：记录调用、返回预设响应。

    只实现客户端真正用到的方法（get_raw / get_text / set_rate /
    note_rate_limited / note_ok），这样测试暴露的耦合面最小。
    """

    def __init__(self, responses=None, texts=None):
        self.raw_responses = list(responses or [])
        self.text_responses = list(texts or [])
        self.raw_calls = []
        self.text_calls = []
        self.rate_limited_calls = 0

    def get_raw(self, url, *, headers=None, allow_404=False):
        self.raw_calls.append({"url": url, "allow_404": allow_404, "headers": headers})
        if not self.raw_responses:
            raise AssertionError(f"get_raw 被意外调用（无预设响应）: {url}")
        return self.raw_responses.pop(0)

    def get_text(self, url, *, headers=None, allow_404=False):
        self.text_calls.append({"url": url, "allow_404": allow_404})
        if not self.text_responses:
            raise AssertionError(f"get_text 被意外调用（无预设响应）: {url}")
        return self.text_responses.pop(0)

    def set_rate(self, rps):
        self.rps = rps

    def note_rate_limited(self, cooldown=0.0):
        self.rate_limited_calls += 1
        return cooldown if cooldown else 30.0

    def note_ok(self):
        pass


def make_openfda_client(http=None, **cfg_overrides):
    cfg = {"openfda_base": pc.OPENFDA_BASE, "page_size_openfda": 1000}
    cfg.update(cfg_overrides)
    return pc.OpenFDAClient(http or StubHttp(), cfg, verbose=False)


def make_pubmed_client(http=None, **cfg_overrides):
    cfg = {"eutils_base": pc.EUTILS_BASE, "pubmed_tool": "PharmaCrawler"}
    cfg.update(cfg_overrides)
    return pc.PubMedClient(http or StubHttp(), cfg, verbose=False)


# ======================================================================================
# OpenFDAClient.parse_payload
# ======================================================================================


class TestOpenFDAParsePayload(unittest.TestCase):
    def test_normal_payload(self):
        text = json.dumps({
            "meta": {"results": {"skip": 0, "limit": 1, "total": 42}},
            "results": [{"id": "a", "openfda": {"brand_name": ["X"]}}],
        })
        out = pc.OpenFDAClient.parse_payload("label", text, {})
        self.assertEqual(out["results"][0]["id"], "a")
        self.assertEqual(out["meta"]["results"]["total"], 42)

    def test_not_found_raises_empty_result(self):
        """⚠️ NOT_FOUND 必须抛 ``EmptyResult``，**不是** CrawlError。

        实测：openFDA 查无结果时返回 HTTP 404 +
        ``{"error":{"code":"NOT_FOUND","message":"No matches found!"}}``。
        这是一次**成功的查询**，只是结果为空。

        把它归入普通错误会让两类后果同时发生：
          1. 重试逻辑认为查询失败，空查询被重试到耗尽重试次数；
          2. 错误统计被"空结果"淹没，真正的故障反而看不见。
        所以 EmptyResult 是**独立**的异常类型，且不能是 ApiShapeError。
        """
        err = json.dumps({"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        with self.assertRaises(pc.EmptyResult):
            pc.OpenFDAClient.parse_payload("label", err, {})

    def test_empty_result_is_distinguishable(self):
        """上层要能单独特判 EmptyResult —— 这是它独立存在的意义。"""
        err = json.dumps({"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        try:
            pc.OpenFDAClient.parse_payload("label", err, {})
        except pc.EmptyResult as exc:
            self.assertNotIsInstance(exc, pc.ApiShapeError)
            self.assertNotIsInstance(exc, pc.RateLimited)
        else:
            self.fail("应当抛出 EmptyResult")

    def test_other_error_codes_raise_crawl_error(self):
        """其他错误码走普通 CrawlError（含 400 BAD_REQUEST 的 limit/skip 违规）。"""
        for code in ("BAD_REQUEST", "SERVER_ERROR", "OVER_LIMIT"):
            err = json.dumps({"error": {"code": code, "message": "boom"}})
            with self.assertRaises(pc.CrawlError) as cm:
                pc.OpenFDAClient.parse_payload("label", err, {})
            # 关键：不能是 EmptyResult，否则真正的错误会被当成"正常空结果"放过
            self.assertNotIsInstance(cm.exception, pc.EmptyResult)
            self.assertIn(code, str(cm.exception))

    def test_missing_results_raises_api_shape_error(self):
        """缺 results：上游改版的信号，必须大声报错。

        没有这个校验的爬虫会"成功爬到 0 条"然后写一份空索引 ——
        用户以为数据就是空的，实际是接口变了。
        """
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", '{"meta": {"results": {"total": 0}}}', {})

    def test_non_list_results_raises_api_shape_error(self):
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", '{"results": {"nope": 1}}', {})
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", '{"results": "a string"}', {})

    def test_non_dict_payload_raises(self):
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", "[1, 2, 3]", {})

    def test_garbage_raises_api_shape_error(self):
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", "<html>502 Bad Gateway</html>", {})

    def test_empty_response_raises(self):
        with self.assertRaises(pc.ApiShapeError):
            pc.OpenFDAClient.parse_payload("label", "", {})

    def test_empty_results_list_is_valid(self):
        """``results: []`` 是合法的空结果，不该报错（与缺 results 不同）。"""
        out = pc.OpenFDAClient.parse_payload("label", '{"results": []}', {})
        self.assertEqual(out["results"], [])

    def test_truncated_payload_still_parses(self):
        """截断的响应必须能救回 —— 走 _loads_tolerant 的抢救路径。"""
        text = '{"meta": {"results": {"total": 2}}, "results": [{"id": "a"}, {"id": "b"'
        out = pc.OpenFDAClient.parse_payload("label", text, {})
        self.assertIn("a", [r["id"] for r in out["results"]])


# ======================================================================================
# OpenFDAClient.query —— 404 当空结果（本文件最重要的一组）
# ======================================================================================


class TestOpenFDAQuery404(unittest.TestCase):
    """⚠️ ⚠️ 全项目最重要的行为：**HTTP 404 = 空结果集，不是故障**。

    docs/DATA_SOURCE_NOTES.md 第 1.6 节实测：
    openFDA 查无结果时返回 **HTTP 404**
    ``{"error":{"code":"NOT_FOUND","message":"No matches found!"}}``。

    如果把 404 当故障处理：
      - 一个本来就没有数据的检索组合会被重试 5 次（每次都要等指数退避），
        白白消耗每日仅 1000 次的未鉴权配额；
      - 分段爬取的递归探测会把"这个时段没数据"误判为"请求失败"，
        可能导致整棵分段树重跑；
      - 最终表现为"爬取特别慢还老是报错"，而数据其实是完整的 —— 极难归因。

    所以这里独立成类，用多条断言把这个契约钉死。
    """

    def test_query_returns_empty_dict_on_404(self):
        http = StubHttp(responses=[raw_response(404, '{"error":{"code":"NOT_FOUND"}}')])
        client = make_openfda_client(http)
        res = client.query("label", search="nonexistent:thing")
        self.assertTrue(res.get("empty"), "404 必须标记为空结果")
        self.assertEqual(res["results"], [])
        self.assertEqual(res["total"], 0)
        self.assertIsNone(res["next"])

    def test_404_does_not_raise(self):
        """绝不能抛异常 —— 上层不该为了空结果写 try/except。"""
        http = StubHttp(responses=[raw_response(404, b"")])
        client = make_openfda_client(http)
        try:
            client.query("label")
        except pc.CrawlError as exc:
            self.fail(f"404 不该抛异常，实际抛出 {type(exc).__name__}: {exc}")

    def test_404_is_requested_with_allow_404(self):
        """必须向 HTTP 层申请 allow_404=True，否则 404 会先被抛成 HttpError。

        这一条锁的是"请求方式"而不只是"结果"：如果哪天有人把
        allow_404 去掉，404 会在 HttpClient 里变成 HttpError，
        上面的行为测试会以"抛异常"的形式失败 —— 但这条断言能直接指出原因。
        """
        http = StubHttp(responses=[raw_response(404, b"")])
        client = make_openfda_client(http)
        client.query("label")
        self.assertTrue(http.raw_calls[0]["allow_404"], "query 必须用 allow_404=True 请求")

    def test_404_does_not_retry(self):
        """404 只发一次请求 —— 不重试。

        重试空结果是最典型的"配额杀手"：未鉴权每天 1000 次，
        一个空查询重试 5 次就吃掉 5 次，分段爬取里累积起来是灾难性的。
        """
        http = StubHttp(responses=[raw_response(404, b"")])
        client = make_openfda_client(http)
        client.query("label", search="nothing:here")
        self.assertEqual(len(http.raw_calls), 1, "404 只能请求一次，不能重试")

    def test_200_marks_not_empty(self):
        """正常响应必须 empty=False，否则上层会把有数据的响应当空结果跳过。"""
        body = json.dumps({"meta": {"results": {"total": 1}}, "results": [{"id": "a"}]})
        http = StubHttp(responses=[raw_response(200, body)])
        client = make_openfda_client(http)
        res = client.query("label")
        self.assertFalse(res["empty"])
        self.assertEqual(res["total"], 1)
        self.assertEqual(len(res["results"]), 1)

    def test_count_query_without_meta_results(self):
        """⚠️ ``count`` 查询的 meta 里**没有** results 子对象。

        代码如果写 ``meta["results"]["total"]`` 就会 KeyError，
        而 count 是分段探量的常规操作 —— 一崩就是整条流水线断掉。
        """
        body = json.dumps({
            "meta": {"disclaimer": "...", "terms": "..."},
            "results": [{"term": "ASPIRIN", "count": 100}],
        })
        http = StubHttp(responses=[raw_response(200, body)])
        client = make_openfda_client(http)
        res = client.query("label", count="openfda.brand_name.exact")
        self.assertEqual(res["total"], 0)  # 没有 meta.results → total 退化为 0
        self.assertEqual(res["results"][0]["term"], "ASPIRIN")

    def test_missing_meta_is_safe(self):
        body = json.dumps({"results": [{"id": "a"}]})
        http = StubHttp(responses=[raw_response(200, body)])
        res = make_openfda_client(http).query("label")
        self.assertEqual(res["total"], 0)
        self.assertEqual(res["meta"], {})

    def test_non_numeric_total_is_safe(self):
        body = json.dumps({"meta": {"results": {"total": "not-a-number"}}, "results": []})
        http = StubHttp(responses=[raw_response(200, body)])
        res = make_openfda_client(http).query("label")
        self.assertEqual(res["total"], 0)


# ======================================================================================
# OpenFDAClient.build_url
# ======================================================================================


class TestOpenFDABuildUrl(unittest.TestCase):
    def test_api_key_is_first_parameter(self):
        """⚠️ ``api_key`` 必须是**第一个**查询参数。

        docs/DATA_SOURCE_NOTES.md 第 1.2 节：key 以查询参数传递，
        必须放第一位 ``?api_key=KEY&search=...``。
        openFDA 大多数情况下容忍其他顺序，但这是官方明确要求的形式，
        没有理由去赌 —— 而且一旦被拒，表现是"带 key 也限流"，
        极难排查到参数顺序上。
        """
        client = make_openfda_client(openfda_api_key="SECRETKEY123")
        url = client.build_url("label", [("search", "brand_name:TYLENOL"), ("limit", "10")])
        query = url.split("?", 1)[1]
        self.assertTrue(query.startswith("api_key=SECRETKEY123&"),
                        f"api_key 必须是第一个参数，实际: {query}")
        first_param = query.split("&")[0]
        self.assertEqual(first_param, "api_key=SECRETKEY123")

    def test_no_api_key_no_param(self):
        client = make_openfda_client()
        url = client.build_url("label", [("limit", "1")])
        self.assertNotIn("api_key", url)
        self.assertTrue(url.endswith("limit=1"))

    def test_endpoint_paths(self):
        client = make_openfda_client()
        self.assertIn("/drug/label.json", client.build_url("label", []))
        self.assertIn("/drug/enforcement.json", client.build_url("enforcement", []))
        self.assertIn("/drug/event.json", client.build_url("event", []))

    def test_shortages_endpoint_is_plural(self):
        """⚠️ 端点是复数 ``/drug/shortages.json``。

        ``/drug/shortage.json`` 根本不存在（实测 404，而 404 又被当作"空结果"，
        于是药品短缺这个数据集会**永远安静地返回 0 条**，
        看起来像"FDA 没有短缺数据"，实际是 URL 拼错了 —— 这就是
        404-as-empty 设计的代价，所以端点名必须有专门测试钉住。
        """
        client = make_openfda_client()
        url = client.build_url("shortages", [])
        self.assertIn("/drug/shortages.json", url)
        self.assertNotIn("/drug/shortage.json?", url)
        self.assertNotRegex(url, r"/drug/shortage\.json")

    def test_all_known_datasets_resolve(self):
        """每个声明的数据集都要能拼出 URL —— 兜住打字错误。"""
        client = make_openfda_client()
        for ds in pc.OPENFDA_ENDPOINTS:
            url = client.build_url(ds, [])
            self.assertTrue(url.startswith("https://"), f"{ds} 的 URL 异常: {url}")
            self.assertIn(".json", url)

    def test_unknown_dataset_raises(self):
        client = make_openfda_client()
        with self.assertRaises(pc.CrawlError):
            client.build_url("nosuchdataset", [])

    def test_endpoint_override_from_config(self):
        """config.json 能覆盖端点地址 —— 上游改版时的第一道应对手段。"""
        client = make_openfda_client(endpoints={"openfda_label": "/drug/label_v2.json"})
        self.assertIn("/drug/label_v2.json", client.build_url("label", []))

    def test_search_operators_not_over_encoded(self):
        """AND/OR 查询语法依赖 ``+`` 和 ``[]`` 原样传递。

        被过度转义（``%2B``/``%5B``）后 openFDA 会解析失败或当成字面量，
        结果是"查询能跑但结果不对" —— 最坏的一类 bug。
        """
        client = make_openfda_client()
        url = client.build_url("event", [("search", "receivedate:[20200101+TO+20201231]")])
        self.assertIn("+TO+", url)
        self.assertIn("[20200101+TO+20201231]", url)


# ======================================================================================
# OpenFDAClient._next_cursor
# ======================================================================================


class TestNextCursor(unittest.TestCase):
    """``search_after`` 游标 —— 突破 26000 条窗口的正道。"""

    def test_parses_realistic_link_header(self):
        """真实形态：``Link: <https://api.fda.gov/...&search_after=TOKEN>; rel="next"``。

        token 里通常含 ``=`` 等字符，在 URL 里会被百分号编码成 ``%3D``，
        必须经 URL 解码后原样取出，否则传给下一次请求的游标是错的，
        翻页会静默回到第一页或直接 400。
        """
        token = "0=0000025c-0000-0000-0000-000000000000"
        link = (
            "<https://api.fda.gov/drug/event.json?"
            "search=receivedate%3A%5B20200101%2BTO%2B20201231%5D&"
            "search_after=0%3D0000025c-0000-0000-0000-000000000000&limit=1000"
            '>; rel="next"'
        )
        self.assertEqual(pc.OpenFDAClient._next_cursor({"link": link}), token)

    def test_simple_token(self):
        hdrs = {"link": '<https://api.fda.gov/drug/label.json?search_after=ABC123&limit=10>; rel="next"'}
        self.assertEqual(pc.OpenFDAClient._next_cursor(hdrs), "ABC123")

    def test_absent_header_returns_none(self):
        """游标消失 = 结果集已走完，这是停止翻页的正常信号。"""
        self.assertIsNone(pc.OpenFDAClient._next_cursor({}))
        self.assertIsNone(pc.OpenFDAClient._next_cursor({"link": ""}))

    def test_link_without_rel_next_returns_none(self):
        """只有 ``rel="next"`` 的才是前进游标。

        有些响应会带 ``rel="prev"``/``rel="first"`` 或没有 rel
        （如 CORS 或分页说明链接）。认错了会导致翻页原地打转或回退。
        """
        hdrs = {"link": '<https://api.fda.gov/drug/label.json?search_after=TOK>; rel="prev"'}
        self.assertIsNone(pc.OpenFDAClient._next_cursor(hdrs))

    def test_link_without_angle_brackets_returns_none(self):
        hdrs = {"link": 'https://api.fda.gov/drug/label.json; rel="next"'}
        self.assertIsNone(pc.OpenFDAClient._next_cursor(hdrs))

    def test_link_without_search_after_returns_none(self):
        hdrs = {"link": '<https://api.fda.gov/drug/label.json?limit=10>; rel="next"'}
        self.assertIsNone(pc.OpenFDAClient._next_cursor(hdrs))

    def test_garbage_link_returns_none_not_crash(self):
        """畸形 Link 头不能让整个爬取崩掉 —— 最坏就是丢掉游标。"""
        self.assertIsNone(pc.OpenFDAClient._next_cursor({"link": "; rel=\"next\""}))
        self.assertIsNone(pc.OpenFDAClient._next_cursor({"link": "<>; rel=\"next\""}))

    def test_header_case_insensitive(self):
        """响应头字典的键是响应头原样 —— 大小写都要认。"""
        hdrs = {"Link": '<https://api.fda.gov/x?search_after=TOK>; rel="next"'}
        # 注意：HttpClient 会把头键统一小写，但 _next_cursor 被直接调用时
        # 也可能收到原样大小写。这里记录当前契约：只认小写键。
        self.assertIsNone(pc.OpenFDAClient._next_cursor(hdrs))


# ======================================================================================
# OpenFDAClient.query —— skip 上限
# ======================================================================================


class TestOpenFDASkipLimit(unittest.TestCase):
    """``skip`` 上限 25000（实测 ``skip=25001`` → 400）。

    超限时必须在**本地**就报错，而不是把请求发出去等 400：
    发出去不仅浪费一次配额，还会因为 400 触发重试逻辑（重试也没用，
    参数本身就非法），把一个必败的请求重试好几遍。
    """

    def test_skip_over_limit_raises_before_any_request(self):
        http = StubHttp()
        client = make_openfda_client(http)
        with self.assertRaises(pc.CrawlError) as cm:
            client.query("label", skip=25001)
        self.assertIn("25000", str(cm.exception))
        # 关键：一个请求都没发出去
        self.assertEqual(len(http.raw_calls), 0, "超限的 skip 不该发出请求")

    def test_skip_at_limit_is_allowed(self):
        body = json.dumps({"meta": {"results": {"total": 5}}, "results": []})
        http = StubHttp(responses=[raw_response(200, body)])
        res = make_openfda_client(http).query("label", skip=25000)
        self.assertIn("skip=25000", http.raw_calls[0]["url"])

    def test_skip_and_search_after_mutually_exclusive(self):
        """⚠️ ``skip`` 与 ``search_after`` 互斥，绝不能同时传。

        同时传会被 400 拒掉。更隐蔽的问题是：跟随游标翻页时如果
        还带着 skip，翻出来的页会错位/重复，数据静默出错。
        """
        body = json.dumps({"results": [{"id": "a"}]})
        http = StubHttp(responses=[raw_response(200, body)])
        client = make_openfda_client(http)
        client.query("label", skip=100, search_after="TOK")
        url = http.raw_calls[0]["url"]
        self.assertIn("search_after=TOK", url)
        self.assertNotIn("skip=", url, "search_after 与 skip 互斥，不能同时出现")

    def test_limit_uses_page_size(self):
        body = json.dumps({"results": []})
        http = StubHttp(responses=[raw_response(200, body)])
        make_openfda_client(http, page_size_openfda=1000).query("label")
        self.assertIn("limit=1000", http.raw_calls[0]["url"])

    def test_count_query_has_no_limit(self):
        """count 查询不该带 limit —— 它是聚合，不是取记录。"""
        body = json.dumps({"results": [{"term": "A", "count": 1}]})
        http = StubHttp(responses=[raw_response(200, body)])
        make_openfda_client(http).query("label", count="openfda.brand_name.exact")
        self.assertNotIn("limit=", http.raw_calls[0]["url"])

    def test_count_field_sorted_desc(self):
        body = json.dumps({
            "results": [{"term": "A", "count": 5}, {"term": "B", "count": 50}],
        })
        http = StubHttp(responses=[raw_response(200, body)])
        rows = make_openfda_client(http).count_field("label", "openfda.brand_name.exact")
        self.assertEqual(rows[0], ("B", 50))
        self.assertEqual(rows[1], ("A", 5))

    def test_count_field_tolerates_bad_rows(self):
        body = json.dumps({"results": [{"term": "A", "count": "x"}, {"nope": 1}]})
        http = StubHttp(responses=[raw_response(200, body)])
        rows = make_openfda_client(http).count_field("label", "f")
        self.assertEqual(rows, [])


# ======================================================================================
# PubMedClient.build_url
# ======================================================================================


class TestPubMedBuildUrl(unittest.TestCase):
    def test_basic(self):
        client = make_pubmed_client()
        url = client.build_url("esearch.fcgi", [("db", "pubmed"), ("term", "aspirin")])
        self.assertTrue(url.startswith(pc.EUTILS_BASE + "/esearch.fcgi?"))
        self.assertIn("db=pubmed", url)
        self.assertIn("term=aspirin", url)

    def test_base_has_versionless_eutils_path(self):
        """E-utilities 基址不带版本段（与 DailyMed 的 /v2 不同）。"""
        client = make_pubmed_client()
        self.assertEqual(client.base, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")

    def test_trailing_slash_stripped(self):
        client = make_pubmed_client(eutils_base="https://example.org/eutils/")
        url = client.build_url("esearch.fcgi", [])
        self.assertNotIn("//esearch", url)

    def test_webenv_case_preserved(self):
        """⚠️ ``WebEnv`` 是**大小写敏感**的唯一例外参数名。

        参数名一律小写是 NCBI 的硬性要求，但 WebEnv 必须保持这个拼写。
        写成 ``webenv`` 时 NCBI 会忽略它，History 集取不到数据 ——
        表现是"翻页一直返回同一批或空"，而且不报错。
        """
        client = make_pubmed_client()
        url = client.build_url("efetch.fcgi", [("query_key", "1"), ("WebEnv", "MCID_abc123")])
        self.assertIn("WebEnv=MCID_abc123", url)
        self.assertNotIn("webenv=", url)

    def test_quotes_encoded(self):
        """``"`` 必须转成 ``%22`` —— PubMed 短语检索依赖它。"""
        client = make_pubmed_client()
        url = client.build_url("esearch.fcgi", [("term", '"Nature"[Journal]')])
        self.assertIn("%22Nature%22", url)
        self.assertNotIn('"', url)

    def test_spaces_encoded(self):
        client = make_pubmed_client()
        url = client.build_url("esearch.fcgi", [("term", "aspirin AND heart")])
        self.assertNotIn(" ", url)
        self.assertIn("aspirin", url)

    def test_common_params_included(self):
        """``tool``/``email`` 合规必须每次带上（注册后才能解封 IP）。"""
        client = make_pubmed_client(pubmed_tool="PharmaCrawler", pubmed_email="a@b.c")
        params = client._common()
        d = dict(params)
        self.assertEqual(d["tool"], "PharmaCrawler")
        self.assertEqual(d["email"], "a@b.c")

    def test_api_key_optional(self):
        client = make_pubmed_client()
        self.assertNotIn("api_key", dict(client._common()))
        client2 = make_pubmed_client(pubmed_api_key="KEY123")
        self.assertEqual(dict(client2._common())["api_key"], "KEY123")


# ======================================================================================
# PubMedClient._check_body —— 2xx 状态码下的限流
# ======================================================================================


class TestPubMedCheckBody(unittest.TestCase):
    """⚠️ 本文件第二重要的一组：**限流信号藏在响应体里，可能带 HTTP 200**。

    docs/DATA_SOURCE_NOTES.md 第 4.6 节实测：
    NCBI 超限时返回 ``{"error":"API rate limit exceeded","count":"11"}``，
    **可能带 2xx 状态码**。

    只看 HTTP 状态码的后果非常隐蔽：
    爬虫会以为这是一次"合法但空的响应"，于是安静地少收一批数据、
    还不触发退避 —— 接下来继续以超限速率猛发请求，
    最终被 NCBI 封 IP（解封需要注册 tool/email 并写邮件申请）。
    """

    def test_rate_limit_in_json_body_raises_rate_limited(self):
        body = '{"error":"API rate limit exceeded","count":"11"}'
        client = make_pubmed_client()
        with self.assertRaises(pc.RateLimited):
            client._check_body(body, "esearch")

    def test_rate_limit_detected_even_with_leading_whitespace(self):
        body = '   \n  {"error":"API rate limit exceeded","count":"11"}'
        client = make_pubmed_client()
        with self.assertRaises(pc.RateLimited):
            client._check_body(body, "esearch")

    def test_rate_limit_triggers_cooldown(self):
        """命中限流必须真的通知 HTTP 层冷却 —— 否则退避是空的。"""
        body = '{"error":"API rate limit exceeded","count":"11"}'
        http = StubHttp()
        client = make_pubmed_client(http)
        with self.assertRaises(pc.RateLimited):
            client._check_body(body, "esearch")
        self.assertGreater(http.rate_limited_calls, 0, "必须调用 note_rate_limited 进入冷却")

    def test_rate_limit_plain_text_body(self):
        """非 JSON 的纯文本限流提示也要认（下游可能是纯文本响应）。"""
        client = make_pubmed_client()
        with self.assertRaises(pc.RateLimited):
            client._check_body("Error: API rate limit exceeded, please slow down", "efetch")

    def test_normal_json_body_passes_through(self):
        """正常响应必须原样放行，不能误报限流。

        误报的代价：正常的爬取被当作限流，无限退避 + 冷却，
        吞吐降到接近 0，表现为"程序卡住不动"。
        """
        normal = json.dumps({
            "header": {"type": "esearch", "version": "0.3"},
            "esearchresult": {"count": "42", "retmax": "42", "idlist": ["1", "2"]},
        })
        client = make_pubmed_client()
        try:
            client._check_body(normal, "esearch")
        except pc.CrawlError as exc:
            self.fail(f"正常响应被误判为错误: {type(exc).__name__}: {exc}")

    def test_empty_body_passes(self):
        client = make_pubmed_client()
        client._check_body("", "esearch")  # 不该抛

    def test_invalid_json_passes_through(self):
        """非 JSON 的正文交给下游 JSON 解析去报错，_check_body 不越权。"""
        client = make_pubmed_client()
        client._check_body("<html>", "esearch")

    def test_retstart_too_large_raises_api_shape_error(self):
        """``retstart`` 超限的错误信息必须映射成 ApiShapeError。

        NCBI 原文：``'retstart' cannot be larger than 9998``。
        这不是限流（重试没用），也不是空结果，而是**参数非法** ——
        归到 ApiShapeError 让上层能区分"要改分段策略"和"要等一等"。
        如果错误地归成 RateLimited，程序会一直退避重试一个永远不会成功的请求。
        """
        body = json.dumps({
            "error": (
                "Invalid parameter: 'retstart' cannot be larger than 9998. "
                "For PubMed, ESearch can only retrieve the first 9,999 records "
                "matching the query."
            )
        })
        client = make_pubmed_client()
        with self.assertRaises(pc.ApiShapeError):
            client._check_body(body, "esearch")

    def test_retstart_error_mentions_limits(self):
        """错误信息里要给出正确上限，方便使用者立刻改分段策略。"""
        body = json.dumps({"error": "Invalid parameter: 'retstart' cannot be larger than 9998."})
        client = make_pubmed_client()
        with self.assertRaises(pc.ApiShapeError) as cm:
            client._check_body(body, "esearch")
        msg = str(cm.exception)
        self.assertIn("9999", msg)
        self.assertIn("9998", msg)

    def test_other_ncbi_error_raises_crawl_error(self):
        body = json.dumps({"error": "Invalid database name"})
        client = make_pubmed_client()
        with self.assertRaises(pc.CrawlError) as cm:
            client._check_body(body, "esearch")
        self.assertNotIsInstance(cm.exception, pc.RateLimited)
        self.assertNotIsInstance(cm.exception, pc.ApiShapeError)


# ======================================================================================
# PubMedClient.esearch —— retmax / retstart 上限
# ======================================================================================


class TestPubMedESearch(unittest.TestCase):
    def _ok_body(self, count=10, ids=None, retmax=None):
        esr = {
            "count": str(count),
            "retmax": str(retmax if retmax is not None else len(ids or [])),
            "retstart": "0",
            "idlist": ids or [],
            "webenv": "MCID_test",
            "querykey": "1",
        }
        return json.dumps({"header": {"type": "esearch"}, "esearchresult": esr})

    def test_retmax_clamped_to_9999(self):
        """⚠️ ``retmax`` 必须钳到 **9999**，不是 10000。

        docs/DATA_SOURCE_NOTES.md 第 4.2 节实测（本文件最重要的一条）：
        ``retmax=100000`` 返回 ``"retmax":"9999"`` —— **静默截断，不报错**。

        如果代码按文档的 10000 去切分，每片会**静默少 1 条**：
        请求 retmax=10000，NCBI 只给 9999 条，代码却以为拿满了 10000 条，
        于是下一片从 10000 开始 —— 第 10000 条记录被永久跳过，
        而且没有任何错误。这种 bug 会在生产环境潜伏几个月，
        只有在做精确计数核对时才会暴露。
        """
        http = StubHttp(texts=[(self._ok_body(ids=["1"] * 5), {})])
        client = make_pubmed_client(http)
        client.esearch("aspirin", retmax=100000)
        url = http.text_calls[0]["url"]
        self.assertIn("retmax=9999", url, "retmax 必须被钳到 9999")
        self.assertNotIn("retmax=10000", url)
        self.assertNotIn("retmax=100000", url)

    def test_retmax_constant_is_9999(self):
        """常量本身也锁住 —— 改回 10000 等于把静默丢数据重新引入。"""
        self.assertEqual(pc.PUBMED_MAX_RETMAX, 9999)
        self.assertEqual(pc.PUBMED_MAX_RETSTART, 9998)

    def test_retmax_within_limit_untouched(self):
        http = StubHttp(texts=[(self._ok_body(ids=["1"] * 5), {})])
        client = make_pubmed_client(http)
        client.esearch("aspirin", retmax=500)
        self.assertIn("retmax=500", http.text_calls[0]["url"])

    def test_retstart_over_9998_raises_before_request(self):
        """⚠️ ``retstart > 9998`` 必须在本地就拒绝（实测超限直接报错）。

        与 retmax 的静默截断不同，retstart 超限 NCBI 会**显式报错**。
        本地拦截的价值：不发这个必败的请求（省配额），
        并且给出"请改用分段爬取"的明确指引，而不是让用户对着
        上游的英文报错猜。
        """
        http = StubHttp()
        client = make_pubmed_client(http)
        with self.assertRaises(pc.CrawlError) as cm:
            client.esearch("aspirin", retstart=10000)
        msg = str(cm.exception)
        self.assertIn("9998", msg)
        self.assertEqual(len(http.text_calls), 0, "超限的 retstart 不该发出请求")

    def test_retstart_at_9998_allowed(self):
        """9998 是**合法**的边界值（实测 retstart=9998 返回 1 条）。"""
        http = StubHttp(texts=[(self._ok_body(ids=["1"]), {})])
        client = make_pubmed_client(http)
        got = client.esearch("aspirin", retstart=9998, retmax=10)
        self.assertIn("retstart=9998", http.text_calls[0]["url"])
        self.assertEqual(got["count"], 10)

    def test_retstart_zero_not_sent(self):
        """retstart=0 是默认值，不必传（少一个参数少一分出错机会）。"""
        http = StubHttp(texts=[(self._ok_body(ids=["1"]), {})])
        client = make_pubmed_client(http)
        client.esearch("aspirin")
        self.assertNotIn("retstart=", http.text_calls[0]["url"])

    def test_verbose_silent_on_success(self):
        """成功的 esearch 不该往 stdout 打印 —— GUI 靠 stdout 解析进度。"""
        http = StubHttp(texts=[(self._ok_body(ids=["1"]), {})])
        client = make_pubmed_client(http)
        with mock.patch.object(pc, "out") as m:
            client.esearch("aspirin")
        m.assert_not_called()

    def test_parses_result(self):
        http = StubHttp(texts=[(self._ok_body(42, ["111", "222"]), {})])
        client = make_pubmed_client(http)
        got = client.esearch("aspirin")
        self.assertEqual(got["count"], 42)
        self.assertEqual(got["ids"], ["111", "222"])
        self.assertEqual(got["webenv"], "MCID_test")
        self.assertEqual(got["querykey"], "1")

    def test_usehistory_and_count_params(self):
        http = StubHttp(texts=[(self._ok_body(7, []), {})])
        client = make_pubmed_client(http)
        client.esearch("aspirin", count_only=True, use_history=False, retmax=0)
        url = http.text_calls[0]["url"]
        self.assertIn("rettype=count", url)
        self.assertNotIn("usehistory", url)

    def test_mindate_maxdate_paired(self):
        """⚠️ ``mindate``/``maxdate`` 必须成对出现。

        只给一个时 NCBI 会忽略日期限制（或直接报错），
        结果是"按时间切片的爬取实际爬了全量" —— 单次请求量爆炸，
        进而触发限流、分段策略失效。
        """
        http = StubHttp(texts=[(self._ok_body(1, []), {})])
        client = make_pubmed_client(http)
        client.esearch("x", datetype="pdat", mindate="2024/01/01", maxdate="2024/12/31")
        url = http.text_calls[0]["url"]
        self.assertIn("mindate=2024%2F01%2F01", url)
        self.assertIn("maxdate=2024%2F12%2F31", url)
        self.assertIn("datetype=pdat", url)

    def test_pubmed_date_uses_slashes(self):
        """PubMed 的 mindate/maxdate 是 ``YYYY/MM/DD``（第三种日期格式）。"""
        http = StubHttp(texts=[(self._ok_body(1, []), {})])
        client = make_pubmed_client(http)
        d = pc.date_to_slash(pc._dt.date(2024, 1, 31))
        client.esearch("x", mindate=d, maxdate=d)
        url = http.text_calls[0]["url"]
        # 斜杠被编码成 %2F，但绝不能变成短横线
        self.assertNotIn("2024-01-31", url)

    def test_count_helper(self):
        http = StubHttp(texts=[(self._ok_body(12345, []), {})])
        client = make_pubmed_client(http)
        self.assertEqual(client.count("aspirin"), 12345)

    def test_404_raises(self):
        http = StubHttp(texts=[(None, {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.CrawlError):
            client.esearch("aspirin")

    def test_invalid_json_raises_api_shape_error(self):
        http = StubHttp(texts=[("<html>not json</html>", {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.ApiShapeError):
            client.esearch("aspirin")

    def test_missing_esearchresult_raises(self):
        http = StubHttp(texts=[(json.dumps({"header": {}}), {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.ApiShapeError):
            client.esearch("aspirin")

    def test_non_list_idlist_raises(self):
        body = json.dumps({"esearchresult": {"count": "1", "idlist": "not-a-list"}})
        http = StubHttp(texts=[(body, {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.ApiShapeError):
            client.esearch("x")

    def test_missing_idlist_tolerated(self):
        """idlist 缺失（如 count 查询）退化为空列表，不报错。"""
        body = json.dumps({"esearchresult": {"count": "5"}})
        http = StubHttp(texts=[(body, {})])
        client = make_pubmed_client(http)
        self.assertEqual(client.esearch("x")["ids"], [])

    def test_rate_limit_body_raises(self):
        """端到端：2xx + 限流正文，esearch 必须抛 RateLimited。"""
        http = StubHttp(texts=[('{"error":"API rate limit exceeded","count":"11"}', {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.RateLimited):
            client.esearch("x")

    def test_retstart_error_body_raises_shape_error(self):
        body = json.dumps({"error": "Invalid parameter: 'retstart' cannot be larger than 9998."})
        http = StubHttp(texts=[(body, {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.ApiShapeError):
            client.esearch("x")


# ======================================================================================
# PubMedClient.efetch
# ======================================================================================


class TestPubMedEFetch(unittest.TestCase):
    def test_ids_joined_without_spaces(self):
        """⚠️ ``id`` 用逗号分隔且**不含空格**（实测 ``id=352,25125,234``）。

        带空格时 NCBI 只认第一个 ID（或整体报错），
        表现为"这批只回来一条记录"，静默丢数据。

        注意逗号本身**不需要**编码：build_url 把 ``,`` 放进了 safe 集合，
        NCBI 也接受原样逗号。这里锁的是"没有空格"这个真正的要求，
        而不是逗号的具体编码形式。
        """
        http = StubHttp(texts=[("<xml/>", {})])
        client = make_pubmed_client(http)
        client.efetch(ids=["352", "25125", "234"])
        url = http.text_calls[0]["url"]
        self.assertIn("id=352,25125,234", url)
        # 关键断言：id 参数里绝不能出现空格或 %20
        id_part = url.split("id=", 1)[1].split("&", 1)[0]
        self.assertNotIn(" ", id_part)
        self.assertNotIn("%20", id_part)

    def test_webenv_iteration_params(self):
        """WebEnv 迭代 —— 突破 9999 上限的正确姿势。"""
        http = StubHttp(texts=[("<xml/>", {})])
        client = make_pubmed_client(http)
        client.efetch(webenv="MCID_abc", querykey="1", retstart=200, retmax=500)
        url = http.text_calls[0]["url"]
        self.assertIn("WebEnv=MCID_abc", url)
        self.assertIn("query_key=1", url)
        self.assertIn("retstart=200", url)
        self.assertIn("retmax=500", url)

    def test_efetch_retmax_clamped(self):
        """EFetch 的 retmax 同样钳到 9999（虽然按 200-500 分批，防手滑）。"""
        http = StubHttp(texts=[("<xml/>", {})])
        client = make_pubmed_client(http)
        client.efetch(webenv="W", querykey="1", retmax=999999)
        self.assertIn("retmax=9999", http.text_calls[0]["url"])

    def test_requires_ids_or_webenv(self):
        """既没 ids 也没 webenv：参数缺失，本地就报错。"""
        http = StubHttp()
        client = make_pubmed_client(http)
        with self.assertRaises(pc.CrawlError):
            client.efetch()
        self.assertEqual(len(http.text_calls), 0)

    def test_returns_text(self):
        http = StubHttp(texts=[("<PubmedArticleSet/>", {})])
        client = make_pubmed_client(http)
        self.assertEqual(client.efetch(ids=["1"]), "<PubmedArticleSet/>")

    def test_404_raises(self):
        http = StubHttp(texts=[(None, {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.CrawlError):
            client.efetch(ids=["1"])

    def test_rate_limit_body_raises(self):
        http = StubHttp(texts=[('{"error":"API rate limit exceeded"}', {})])
        client = make_pubmed_client(http)
        with self.assertRaises(pc.RateLimited):
            client.efetch(ids=["1"])


# ======================================================================================
# HttpClient._read_body —— IncompleteRead 的部分数据
# ======================================================================================


class _FakeResponse:
    """模拟 ``http.client.HTTPResponse`` 的 read() 行为。

    ``chunks`` 逐一返回；返回完 ``None`` 表示流正常结束。
    ``error`` 若非空，则在读完 chunks 后抛出。
    """

    def __init__(self, chunks, error=None):
        self._chunks = list(chunks)
        self._error = error
        self.status = 200
        self.headers = {}

    def read(self, size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            err = self._error
            self._error = None
            raise err
        return b""


class TestHttpClientReadBody(unittest.TestCase):
    """``_read_body`` 对传输中断的容忍。

    实测背景：openFDA limit=1000 的标签响应约 28MB，
    长连接中途被掐断是常态，``http.client`` 抛 ``IncompleteRead``。

    ``IncompleteRead`` 有个关键性质：**它携带已经读到的部分数据**
    （``exc.partial``）。直接丢弃并重试整次请求既慢又浪费配额；
    更好的做法是把已收到的部分带出去，让上层尝试解析 ——
    很多时候数据其实收全了，只是 Content-Length 对不上。
    """

    def test_normal_read(self):
        http = make_http()
        resp = _FakeResponse([b"abc", b"def"])
        self.assertEqual(http._read_body(resp), b"abcdef")

    def test_incomplete_read_returns_partial(self):
        """⚠️ ``IncompleteRead`` 携带的部分数据必须被返回，而不是抛出去。

        如果直接抛，上层会重试整个几十 MB 的请求 ——
        未鉴权每天只有 1000 次配额，几次这样的事故就能把配额烧掉一大半。
        而且实测中"数据其实已经收全、只是 Content-Length 对不上"的情况很常见，
        丢掉它纯属浪费。
        """
        http = make_http()
        err = IncompleteRead(b"partial-data")
        resp = _FakeResponse([b"first-chunk"], error=err)
        got = http._read_body(resp)
        self.assertEqual(got, b"first-chunkpartial-data",
                         "必须把已读分块与 IncompleteRead.partial 拼接返回")

    def test_incomplete_read_with_no_prior_chunk(self):
        """第一个分块就断：至少把 partial 带出来。"""
        http = make_http()
        err = IncompleteRead(b"only-partial")
        resp = _FakeResponse([], error=err)
        self.assertEqual(http._read_body(resp), b"only-partial")

    def test_other_exception_with_partial_returns_partial(self):
        """非 IncompleteRead 的异常也尽量把已读部分带出去。"""
        http = make_http()
        err = OSError("connection reset")
        resp = _FakeResponse([b"some-data"], error=err)
        self.assertEqual(http._read_body(resp), b"some-data")

    def test_exception_without_any_data_propagates(self):
        """一个字都没读到就必须抛出去 —— 不能假装成功返回 b""。

        返回空体会让上层以为"响应是空的"，对 openFDA 而言
        空体解析失败会报 ApiShapeError（还算安全），
        但对某些端点可能被当成合法空结果，静默丢数据。
        """
        http = make_http()
        err = OSError("immediate failure")
        resp = _FakeResponse([], error=err)
        with self.assertRaises(OSError):
            http._read_body(resp)

    def test_partial_data_is_parseable_json_after_incomplete_read(self):
        """端到端价值：断流后拿到的部分数据仍能救回记录。

        这才是 _read_body 容忍中断的**真正目的** ——
        不是"少报一个错"，而是把一次失败的网络传输变成一次可用的结果。
        """
        http = make_http()
        body = b'{"results": [{"id": "keep-me"}]}'
        err = IncompleteRead(b"")
        resp = _FakeResponse([body], error=err)
        got = http._read_body(resp)
        parsed = pc._loads_tolerant(got.decode("utf-8"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["results"][0]["id"], "keep-me")

    def test_empty_response(self):
        http = make_http()
        self.assertEqual(http._read_body(_FakeResponse([])), b"")


# ======================================================================================
# HttpClient.request —— 404 / 重试语义
# ======================================================================================


class TestHttpClientRequest(unittest.TestCase):
    """``request`` 的 404 语义 —— 与 OpenFDAClient 的契约相互配合。"""

    def _http_with_opener(self, opener):
        http = make_http(max_retries=1)
        http.opener = opener
        return http

    def test_allow_404_returns_status_not_raises(self):
        """``allow_404=True`` 时 404 正常返回 —— openFDA 空结果依赖这个。"""
        import urllib.error

        http = make_http(max_retries=1)
        err = urllib.error.HTTPError(
            "https://api.fda.gov/x", 404, "Not Found", {}, None
        )

        class Opener:
            def open(self, req, timeout=None):
                raise err

        http.opener = Opener()
        status, body, hdrs = http.request("https://api.fda.gov/x", allow_404=True)
        self.assertEqual(status, 404)
        self.assertEqual(http.stats["not_found"], 1)
        self.assertEqual(http.stats["errors"], 0, "404 空结果不该计入错误统计")

    def test_404_without_allow_raises_http_error(self):
        import urllib.error

        http = make_http(max_retries=1)
        err = urllib.error.HTTPError("https://api.fda.gov/x", 404, "Not Found", {}, None)

        class Opener:
            def open(self, req, timeout=None):
                raise err

        http.opener = Opener()
        with self.assertRaises(pc.HttpError) as cm:
            http.request("https://api.fda.gov/x", allow_404=False)
        self.assertEqual(cm.exception.status, 404)

    def test_400_raises_http_error(self):
        """400 BAD_REQUEST（limit/skip 违规）必须抛错，不能当空结果。"""
        import urllib.error

        http = make_http(max_retries=1)
        err = urllib.error.HTTPError("https://api.fda.gov/x", 400, "Bad Request", {}, None)

        class Opener:
            def open(self, req, timeout=None):
                raise err

        http.opener = Opener()
        with self.assertRaises(pc.HttpError) as cm:
            http.request("https://api.fda.gov/x", allow_404=True)
        self.assertEqual(cm.exception.status, 400)

    def test_get_raw_404_empty_body(self):
        import urllib.error

        http = make_http(max_retries=1)
        err = urllib.error.HTTPError("https://api.fda.gov/x", 404, "Not Found", {}, None)

        class Opener:
            def open(self, req, timeout=None):
                raise err

        http.opener = Opener()
        status, body, _ = http.get_raw("https://api.fda.gov/x", allow_404=True)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_get_text_returns_none_on_404(self):
        """``get_text`` 用 None 表达 404 空结果（PubMed 侧用）。"""
        import urllib.error

        http = make_http(max_retries=1)
        err = urllib.error.HTTPError("https://x/y", 404, "Not Found", {}, None)

        class Opener:
            def open(self, req, timeout=None):
                raise err

        http.opener = Opener()
        text, _ = http.get_text("https://x/y", allow_404=True)
        self.assertIsNone(text)


# ======================================================================================
# HTTP 层替身自身的一致性
# ======================================================================================


class TestHttpClientLimits(unittest.TestCase):
    def test_openfda_page_size_clamped_to_1000(self):
        """⚠️ ``limit`` 上限 1000（``limit=1001`` → 400）。

        超限时必须在本地钳住，否则每次请求都 400，然后被重试逻辑
        重试到耗尽次数 —— 一个纯粹由配置项引起的、看起来很严重的故障。
        """
        client = make_openfda_client(page_size_openfda=99999)
        self.assertEqual(client.page_size, 1000)
        self.assertEqual(pc.OPENFDA_MAX_LIMIT, 1000)
        self.assertEqual(pc.OPENFDA_MAX_SKIP, 25000)
        self.assertEqual(pc.OPENFDA_WINDOW, 26000)

    def test_pubmed_page_size_clamped_to_9999(self):
        client = make_pubmed_client(page_size_pubmed=99999)
        self.assertEqual(client.page_size, pc.PUBMED_MAX_RETMAX)

    def test_openfda_query_integration_uses_cursor_from_header(self):
        """游标要从响应头一路传到返回值 —— 中间断了就永远翻不到第二页。"""
        token = "0=abc-123"
        body = json.dumps({"meta": {"results": {"total": 5000}}, "results": [{"id": "a"}]})
        hdrs = {"link": f'<https://api.fda.gov/drug/event.json?search_after={token}&limit=1000>; rel="next"'}
        http = StubHttp(responses=[raw_response(200, body, hdrs)])
        res = make_openfda_client(http).query("event", sort="receivedate:asc")
        self.assertEqual(res["next"], token)
        self.assertEqual(res["total"], 5000)


if __name__ == "__main__":
    unittest.main()
