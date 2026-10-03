#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_state.py —— 断点续爬状态的测试（SessionState / SegmentStore / IdCache）。

全部在临时目录里跑，不发网络请求。

这三个类存在的理由是同一个：**一次全量爬取可能跑几十小时**。
没有状态落盘，中途断掉就得从头再来 —— 而"重跑"的代价是真实的请求数：
每一个已爬完的检索组合至少要多花一次探量请求，
几千个组合就是几个小时加上一大截每日配额（未鉴权的 openFDA
每天只有 1000 次）。

所以这里的测试重点不是"存取能不能用"，而是**续爬语义是否真的成立**：
状态读回来之后，程序是不是真的能据此跳过已完成的工作。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pharma_crawler as pc  # noqa: E402


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pharma_test_state_")
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name):
        return self.dir / name


# ======================================================================================
# SessionState
# ======================================================================================


class TestSessionState(StateTestCase):
    def test_make_key(self):
        self.assertEqual(
            pc.SessionState.make_key("pubmed", "pubmed", "aspirin"),
            "pubmed|pubmed|aspirin",
        )
        self.assertEqual(
            pc.SessionState.make_key("openfda", "label", "pain", "extra"),
            "openfda|label|pain|extra",
        )

    def test_start_empty(self):
        st = pc.SessionState(self.path("session.json"))
        self.assertEqual(st.exhausted, set())
        self.assertFalse(st.incomplete_run)
        self.assertFalse(st.is_exhausted("anything"))

    def test_exhausted_round_trip(self):
        """已耗尽的组合必须落盘并读回。

        这是续爬的核心：一个组合"耗尽"意味着它的结果已经全部取完，
        再次运行时应直接跳过 —— 每次跳过省下的是至少一次请求。
        """
        p = self.path("session.json")
        st = pc.SessionState(p)
        key = pc.SessionState.make_key("openfda", "label", "pain")
        st.mark_exhausted(key)
        st.save([key], run_done=True)

        st2 = pc.SessionState(p)
        self.assertTrue(st2.is_exhausted(key))
        self.assertEqual(st2.exhausted, {key})

    def test_multiple_exhausted_keys(self):
        p = self.path("session.json")
        st = pc.SessionState(p)
        keys = [pc.SessionState.make_key("openfda", "label", f"q{i}") for i in range(5)]
        for k in keys:
            st.mark_exhausted(k)
        st.save(keys, run_done=True)

        st2 = pc.SessionState(p)
        self.assertEqual(len(st2.exhausted), 5)
        for k in keys:
            self.assertTrue(st2.is_exhausted(k))

    def test_incomplete_run_recorded(self):
        """⚠️ 未跑完的运行必须记下来 —— 这是"断点"本身。

        ``run_done=False`` 时要把这次的 keys 记进 incomplete_run，
        下次运行据此从这些组合继续。漏记的话续爬就退化成"从头再来"。
        """
        p = self.path("session.json")
        st = pc.SessionState(p)
        pending = ["k1", "k2", "k3"]
        st.save(pending, run_done=False)

        st2 = pc.SessionState(p)
        self.assertTrue(st2.incomplete_run)
        self.assertEqual(st2.partial, set(pending))

    def test_completed_run_clears_incomplete_marker(self):
        """整轮跑完后不能再留"未完成"标记，否则下次又会重跑一遍。

        真实后果：每轮都以为上一轮没跑完，于是无限重复同一批工作，
        永远在爬同一批组合、永远爬不到新的 —— 且日志显示"一切正常"。
        """
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.save(["k1", "k2"], run_done=False)
        self.assertTrue(pc.SessionState(p).incomplete_run)

        st2 = pc.SessionState(p)
        st2.save(["k1", "k2"], run_done=True)
        st3 = pc.SessionState(p)
        self.assertFalse(st3.incomplete_run)
        self.assertEqual(st3.partial, set())

    def test_save_persists_exhausted_even_when_incomplete(self):
        """一轮没跑完，但**已经**耗尽的组合仍然要保存。

        否则中断后这些组合会被重跑，正是 SessionState 要避免的浪费。
        """
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.mark_exhausted("done-1")
        st.save(["still-pending"], run_done=False)

        st2 = pc.SessionState(p)
        self.assertTrue(st2.is_exhausted("done-1"))
        self.assertTrue(st2.incomplete_run)

    def test_compat_fields_on_disk(self):
        """落盘结构要含 exhausted / exhausted_count / updated_at，便于人工查看。"""
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.mark_exhausted("k")
        st.save(["k"], run_done=True)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("exhausted", data)
        self.assertEqual(data["exhausted_count"], 1)
        self.assertIn("updated_at", data)

    def test_exhausted_sorted_for_stable_diff(self):
        """排序输出让文件可 diff —— 状态文件很可能被人工检查。"""
        p = self.path("session.json")
        st = pc.SessionState(p)
        for k in ("c", "a", "b"):
            st.mark_exhausted(k)
        st.save(["a"], run_done=True)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["exhausted"], ["a", "b", "c"])

    def test_reset_clears_memory_and_file(self):
        """``reset`` 必须同时清内存**和**文件。

        只清内存的话，下次启动又会从文件读到旧状态 ——
        用户点了"重置"却发现什么都没重置，只能手工删文件。
        """
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.mark_exhausted("k")
        st.save(["k"], run_done=False)
        self.assertTrue(p.exists())

        st.reset()
        self.assertEqual(st.exhausted, set())
        self.assertEqual(st.partial, set())
        self.assertFalse(st.incomplete_run)
        self.assertFalse(p.exists(), "reset 必须删除状态文件")

        # 新实例也应该是干净的
        st2 = pc.SessionState(p)
        self.assertEqual(st2.exhausted, set())
        self.assertFalse(st2.incomplete_run)

    def test_reset_on_missing_file_is_safe(self):
        st = pc.SessionState(self.path("never_created.json"))
        st.reset()  # 不该抛

    def test_corrupt_state_file_tolerated(self):
        """⚠️ 状态文件损坏时要以"空状态"启动，而不是崩溃。

        状态文件在 ``_state/`` 下，可能因断电/磁盘满被写坏。
        如果读取直接抛异常，用户会陷入"程序一启动就崩"且
        必须手工找到并删除该文件才能恢复的境地 ——
        而状态本身只是优化，丢了最多重跑一遍。
        """
        p = self.path("session.json")
        p.write_text("{ this is not valid json", encoding="utf-8")
        st = pc.SessionState(p)  # 不该抛
        self.assertEqual(st.exhausted, set())
        self.assertFalse(st.incomplete_run)

    # ==================================================================================
    # ⚠️ 已知生产代码缺陷（测试保持失败，直到代码被修复）
    #
    # 缺陷：``SessionState.load()`` / ``SegmentStore.load()`` / ``IdCache.load()``
    #       都会在状态文件内容是"合法 JSON 但不是对象"时崩溃，
    #       抛出 ``AttributeError: 'list' object has no attribute 'get'``。
    #
    # 根因：``read_json()``（pharma_crawler.py:325-330）只捕获
    #       ``(OSError, ValueError)``，而 `json.load` 成功解析出数组/字符串/数字时
    #       两者都不会触发 —— 于是它把非 dict 原样返回。
    #       三个 load() 随后直接 ``data.get(...)``，没有任何类型保护：
    #         - pharma_crawler.py:2931  ``SessionState.load``
    #         - pharma_crawler.py:2983  ``SegmentStore.load``
    #         - pharma_crawler.py:3039  ``IdCache.load``
    #       注意同文件里已有**正确的写法**可参考：这些 load() 自己
    #       对 "stats" 用了 ``isinstance(..., dict)`` 判断（如 2984 行），
    #       却漏了对顶层 data 本身的判断。
    #
    # 最小复现：
    #   >>> import pathlib, tempfile, pharma_crawler as pc
    #   >>> p = pathlib.Path(tempfile.mkdtemp()) / "session.json"
    #   >>> p.write_text("[1, 2, 3]", encoding="utf-8")
    #   >>> pc.SessionState(p)
    #   AttributeError: 'list' object has no attribute 'get'
    #
    # 影响：状态文件位于 ``library/_state/``，可能因断电、磁盘写满、
    #       或用户手工编辑而被写成非对象形式。此时程序**无法启动**，
    #       而状态本身只是"省请求"的优化 —— 丢了最多重跑一遍，
    #       绝不该让用户陷入必须手工定位并删除该文件才能恢复的境地。
    #
    # 修复方向（一行）：在三个 load() 里加
    #       ``if not isinstance(data, dict): data = {}``
    #   或在 ``read_json`` 里要求调用方声明期望类型。
    #
    # 下面这条测试**故意保持失败**，让缺陷在每次跑测试时都可见 ——
    # 而不是被静默弱化成一个"期望抛异常"的断言（那等于把 bug 写成规格）。
    # ==================================================================================

    def test_non_object_state_file_must_not_crash(self):
        """BUG: 状态文件是合法 JSON 数组时，三个状态类的 load() 都会崩。

        见本类上方注释的完整分析。期望行为：以空状态启动（与"文件损坏"时一致），
        而不是抛 AttributeError 让程序完全无法启动。
        """
        p = self.path("session.json")
        p.write_text("[1, 2, 3]", encoding="utf-8")
        try:
            st = pc.SessionState(p)
        except AttributeError as exc:
            self.fail(
                "生产代码缺陷：SessionState.load() 在状态文件为 JSON 数组时崩溃 "
                f"（pharma_crawler.py:2931）: {exc}"
            )
        self.assertEqual(st.exhausted, set())

    def test_wrong_types_in_lists_are_stringified(self):
        """列表里混进非字符串（手改过文件）时转成字符串，不要崩。"""
        p = self.path("session.json")
        p.write_text(json.dumps({"exhausted": [1, 2, "k"]}), encoding="utf-8")
        st = pc.SessionState(p)
        self.assertEqual(st.exhausted, {"1", "2", "k"})

    def test_save_is_atomic_no_leftover_tmp(self):
        """原子写：不能留下 .tmp 残骸（每次保存都留一个会越积越多）。"""
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.mark_exhausted("k")
        st.save(["k"], run_done=True)
        leftovers = [f for f in self.dir.iterdir() if f.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_reload_from_file_reflects_external_change(self):
        """``load()`` 能重新读盘 —— 续爬时会重新加载。"""
        p = self.path("session.json")
        st = pc.SessionState(p)
        st.mark_exhausted("k1")
        st.save(["k1"], run_done=True)

        st.load()
        self.assertTrue(st.is_exhausted("k1"))


# ======================================================================================
# SegmentStore
# ======================================================================================


class TestSegmentStore(StateTestCase):
    """段级状态 —— 分段爬取的断点续爬。

    为什么必须有段级状态：分段爬取要跑成百上千次请求。
    只记"组合级"状态的话，中断后重跑得把整棵分段树重走一遍，
    每段至少一次探量请求，几百段就是几十分钟的纯浪费。
    """

    def test_make_key(self):
        self.assertEqual(
            pc.SegmentStore.make_key("openfda", "event", "q", "20200101", "20201231"),
            "openfda|event|q|20200101~20201231",
        )

    def test_done_round_trip(self):
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        key = pc.SegmentStore.make_key("openfda", "event", "q", "20200101", "20200630")
        s.mark_done(key, count=1234)
        s.save()

        s2 = pc.SegmentStore(p)
        self.assertTrue(s2.is_done(key))
        self.assertEqual(s2.stats[key], 1234)

    def test_mark_done_without_count(self):
        """不给 count 时只记"已完成"，不写 stats —— 避免存一堆 0。"""
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        s.mark_done("k")
        s.save()
        s2 = pc.SegmentStore(p)
        self.assertTrue(s2.is_done("k"))
        self.assertNotIn("k", s2.stats)

    def test_multiple_segments(self):
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        keys = [self.seg_key(i) for i in range(10)]
        for i, k in enumerate(keys):
            s.mark_done(k, count=i * 100)
        s.save()

        s2 = pc.SegmentStore(p)
        self.assertEqual(len(s2.done), 10)
        for k in keys:
            self.assertTrue(s2.is_done(k))

    def seg_key(self, i):
        return pc.SegmentStore.make_key(
            "openfda", "event", "q", f"2020{i:02d}01", f"2020{i:02d}28"
        )

    def test_not_done_by_default(self):
        s = pc.SegmentStore(self.path("segments.json"))
        self.assertFalse(s.is_done("never-marked"))

    def test_save_extra_stats_merged(self):
        """``save(extra=...)`` 要与已有 stats 合并，不能覆盖。"""
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        s.mark_done("k1", count=5)
        s.save({"round": 1})
        s2 = pc.SegmentStore(p)
        self.assertEqual(s2.stats["k1"], 5)
        self.assertEqual(s2.stats["round"], 1)

    def test_save_extra_does_not_lose_on_second_save(self):
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        s.mark_done("k1", count=5)
        s.save({"a": 1})
        s2 = pc.SegmentStore(p)
        s2.save({"b": 2})
        s3 = pc.SegmentStore(p)
        self.assertEqual(s3.stats.get("k1"), 5)
        self.assertEqual(s3.stats.get("a"), 1)
        self.assertEqual(s3.stats.get("b"), 2)

    def test_done_count_on_disk(self):
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        s.mark_done("a")
        s.mark_done("b")
        s.save()
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["done_count"], 2)
        self.assertEqual(data["done"], ["a", "b"])
        self.assertIn("updated_at", data)

    def test_reset_clears_memory_and_file(self):
        p = self.path("segments.json")
        s = pc.SegmentStore(p)
        s.mark_done("k", count=9)
        s.save()
        self.assertTrue(p.exists())

        s.reset()
        self.assertEqual(s.done, set())
        self.assertEqual(s.stats, {})
        self.assertFalse(p.exists())

        s2 = pc.SegmentStore(p)
        self.assertEqual(s2.done, set())

    def test_corrupt_file_tolerated(self):
        p = self.path("segments.json")
        p.write_text("<<<not json>>>", encoding="utf-8")
        s = pc.SegmentStore(p)
        self.assertEqual(s.done, set())
        self.assertEqual(s.stats, {})

    def test_stats_non_dict_tolerated(self):
        p = self.path("segments.json")
        p.write_text(json.dumps({"done": ["a"], "stats": "nope"}), encoding="utf-8")
        s = pc.SegmentStore(p)
        self.assertEqual(s.done, {"a"})
        self.assertEqual(s.stats, {})

    def test_non_object_state_file_must_not_crash(self):
        """BUG: 同 SessionState —— 非对象 JSON 让 SegmentStore.load() 崩溃。

        触发点是 pharma_crawler.py:2983 的 ``data.get("done")``。
        同类缺陷，见 TestSessionState 里 "已知生产代码缺陷" 一节的完整分析。
        """
        p = self.path("segments.json")
        p.write_text("[1, 2, 3]", encoding="utf-8")
        try:
            s = pc.SegmentStore(p)
        except AttributeError as exc:
            self.fail(
                "生产代码缺陷：SegmentStore.load() 在状态文件为 JSON 数组时崩溃 "
                f"（pharma_crawler.py:2983）: {exc}"
            )
        self.assertEqual(s.done, set())

    def test_segmented_crawl_resume_semantics(self):
        """端到端语义：把区间切片 → 标记一半 → 重载 → 只处理剩下的一半。

        这才是 SegmentStore 的**全部意义**。若 done 没落盘，
        重跑会对已完成的段再次探量（每段一次请求），
        在几百段的规模下白白多花几十分钟和一批配额。
        """
        all_chunks = pc.month_chunks(
            pc._dt.date(2024, 1, 1), pc._dt.date(2024, 12, 31)
        )
        self.assertEqual(len(all_chunks), 12)

        p = self.path("segments.json")
        store = pc.SegmentStore(p)
        # 模拟跑完前 5 段后中断
        for start, end in all_chunks[:5]:
            key = pc.SegmentStore.make_key(
                "openfda", "event", "q",
                pc.date_to_compact(start), pc.date_to_compact(end),
            )
            store.mark_done(key, count=100)
        store.save()

        # 续爬：重新加载后应跳过前 5 段
        store2 = pc.SegmentStore(p)
        remaining = []
        for start, end in all_chunks:
            key = pc.SegmentStore.make_key(
                "openfda", "event", "q",
                pc.date_to_compact(start), pc.date_to_compact(end),
            )
            if not store2.is_done(key):
                remaining.append((start, end))
        self.assertEqual(len(remaining), 7, "续爬必须跳过已完成的 5 段")


# ======================================================================================
# IdCache
# ======================================================================================


class TestIdCache(StateTestCase):
    """PubMed 的 ID 缓存。

    正确姿势是"先列举 ID 再取详情"两步走。ID 只放内存的话，
    中断后重跑得重新列举一遍（几千次请求）。落盘后可以直接
    从"取详情"那一步继续。
    """

    def test_put_get_round_trip(self):
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("key1", ["111", "222", "333"])
        c.save()

        c2 = pc.IdCache(p)
        self.assertEqual(c2.get("key1"), ["111", "222", "333"])

    def test_get_missing_returns_none(self):
        c = pc.IdCache(self.path("ids.json"))
        self.assertIsNone(c.get("nope"))

    def test_ids_stringified(self):
        """ID 统一转字符串 —— PubMed 的 PMID 有时以数字形式给出。

        混用 int/str 会让后续的集合去重失效（1 != "1"），
        导致同一篇文献被重复抓取。
        """
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("k", [111, 222])
        c.save()
        c2 = pc.IdCache(p)
        self.assertEqual(c2.get("k"), ["111", "222"])
        for v in c2.get("k"):
            self.assertIsInstance(v, str)

    def test_empty_ids(self):
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("empty", [])
        c.save()
        self.assertEqual(pc.IdCache(p).get("empty"), [])

    def test_non_list_entries_skipped(self):
        """文件里混进非列表值时跳过，不要崩。"""
        p = self.path("ids.json")
        p.write_text(json.dumps({"entries": {"good": ["1"], "bad": "nope"}}), encoding="utf-8")
        c = pc.IdCache(p)
        self.assertEqual(c.get("good"), ["1"])
        self.assertIsNone(c.get("bad"))

    def test_corrupt_file_tolerated(self):
        p = self.path("ids.json")
        p.write_text("garbage", encoding="utf-8")
        c = pc.IdCache(p)
        self.assertEqual(c.data, {})

    def test_missing_entries_key_tolerated(self):
        p = self.path("ids.json")
        p.write_text(json.dumps({"updated_at": "x"}), encoding="utf-8")
        self.assertEqual(pc.IdCache(p).data, {})

    def test_non_object_state_file_must_not_crash(self):
        """BUG: 同 SessionState —— 非对象 JSON 让 IdCache.load() 崩溃。

        触发点是 pharma_crawler.py:3037 的 ``raw.get("entries")``。
        同类缺陷，见 TestSessionState 里 "已知生产代码缺陷" 一节的完整分析。
        """
        p = self.path("ids.json")
        p.write_text("[1, 2, 3]", encoding="utf-8")
        try:
            c = pc.IdCache(p)
        except AttributeError as exc:
            self.fail(
                "生产代码缺陷：IdCache.load() 在状态文件为 JSON 数组时崩溃 "
                f"（pharma_crawler.py:3037）: {exc}"
            )
        self.assertEqual(c.data, {})

    def test_caps_retained_entries(self):
        """⚠️ 缓存条目数必须有上限（裁到最近 ~400 条）。

        每批最多 9999 个 PMID。一个大规模爬取会产生成千上万批，
        无上限的话这个 json 会膨胀到几百 MB：每次 save 都要
        全量序列化 + 原子写盘，CPU 和 IO 迅速成为瓶颈，
        最后表现为"程序越跑越慢直到假死"。
        """
        p = self.path("ids.json")
        c = pc.IdCache(p)
        for i in range(500):
            c.put(f"batch-{i}", [str(j) for j in range(10)])
        c.save()

        c2 = pc.IdCache(p)
        self.assertLessEqual(len(c2.data), 400, "缓存必须被裁剪，不能无上限增长")

    def test_cap_keeps_most_recent(self):
        """⚠️ 裁剪必须保留**最近**的条目，而不是最早的。

        续爬总是从最近中断的地方继续，最有用的就是最新的 ID 列表。
        如果保留最旧的 400 条（比如错误地用 ``keys[400:]``），
        续爬时**每一个需要的 ID 列表都命中不了缓存** ——
        功能看起来正常（缓存文件里有 400 条），实际完全失效，
        白白多花几千次列举请求。
        """
        p = self.path("ids.json")
        c = pc.IdCache(p)
        for i in range(500):
            c.put(f"batch-{i}", [f"id-{i}"])
        c.save()

        c2 = pc.IdCache(p)
        # 最新的那批必须在
        self.assertEqual(c2.get("batch-499"), ["id-499"])
        self.assertEqual(c2.get("batch-498"), ["id-498"])
        # 最旧的应该已被裁掉
        self.assertIsNone(c2.get("batch-0"), "最旧的条目应被裁掉")
        self.assertIsNone(c2.get("batch-99"))

    def test_cap_boundary_exactly_400(self):
        """恰好 400 条时不该裁 —— 边界不能误伤。"""
        p = self.path("ids.json")
        c = pc.IdCache(p)
        for i in range(400):
            c.put(f"b{i}", [str(i)])
        c.save()
        c2 = pc.IdCache(p)
        self.assertEqual(len(c2.data), 400)
        self.assertEqual(c2.get("b0"), ["0"])

    def test_does_not_grow_unbounded_on_disk(self):
        """500 批之后，落盘文件里的条目数也要受限。

        只裁内存不裁文件等于没裁 —— 文件照样无限膨胀。
        """
        p = self.path("ids.json")
        c = pc.IdCache(p)
        for i in range(500):
            c.put(f"batch-{i}", [f"id-{i}"])
            if i % 50 == 49:
                c.save()
                c = pc.IdCache(p)  # 模拟重启后继续
        c.save()
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertLessEqual(len(data["entries"]), 400)

    def test_reset_clears_memory_and_file(self):
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("k", ["1"])
        c.save()
        self.assertTrue(p.exists())

        c.reset()
        self.assertEqual(c.data, {})
        self.assertFalse(p.exists())
        self.assertEqual(pc.IdCache(p).data, {})

    def test_overwrite_same_key(self):
        """同一个 key 再 put 应覆盖，而不是拼接（拼接会让 ID 无限重复）。"""
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("k", ["1", "2"])
        c.put("k", ["3"])
        c.save()
        self.assertEqual(pc.IdCache(p).get("k"), ["3"])

    def test_unicode_and_weird_keys(self):
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("pubmed|pubmed|阿司匹林|2020~2021", ["1"])
        c.save()
        self.assertEqual(pc.IdCache(p).get("pubmed|pubmed|阿司匹林|2020~2021"), ["1"])

    def test_updated_at_present(self):
        p = self.path("ids.json")
        c = pc.IdCache(p)
        c.put("k", ["1"])
        c.save()
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("updated_at", data)


# ======================================================================================
# 三个状态类的共同约定
# ======================================================================================


class TestStateCommon(StateTestCase):
    """三者共享的行为约定 —— 任一缺失都会让"重置"变成半成品。"""

    def test_all_support_reset(self):
        for cls, name in (
            (pc.SessionState, "session.json"),
            (pc.SegmentStore, "segments.json"),
            (pc.IdCache, "ids.json"),
        ):
            p = self.path(name)
            obj = cls(p)
            obj.reset()  # 不该抛
            self.assertFalse(p.exists(), f"{cls.__name__}.reset 未删除文件")

    def test_all_tolerate_missing_file(self):
        for cls, name in (
            (pc.SessionState, "s.json"),
            (pc.SegmentStore, "g.json"),
            (pc.IdCache, "i.json"),
        ):
            cls(self.path(name))  # 不该抛

    def test_all_tolerate_corrupt_file(self):
        """损坏的状态文件绝不能让程序无法启动。

        状态是**优化**而非数据本体：丢了最多重跑一遍，
        而崩掉会让用户以为数据坏了。
        """
        for cls, name in (
            (pc.SessionState, "s.json"),
            (pc.SegmentStore, "g.json"),
            (pc.IdCache, "i.json"),
        ):
            p = self.path(name)
            p.write_text("!!!not json!!!", encoding="utf-8")
            cls(p)  # 不该抛

    def test_states_are_independent_files(self):
        """三类状态各存各的文件 —— 共用一个文件会互相覆盖。"""
        sp, gp, ip = self.path("s.json"), self.path("g.json"), self.path("i.json")
        s = pc.SessionState(sp)
        g = pc.SegmentStore(gp)
        i = pc.IdCache(ip)
        s.mark_exhausted("k")
        s.save(["k"], run_done=True)
        g.mark_done("seg", count=1)
        g.save()
        i.put("ids", ["1"])
        i.save()
        self.assertEqual(pc.SessionState(sp).exhausted, {"k"})
        self.assertTrue(pc.SegmentStore(gp).is_done("seg"))
        self.assertEqual(pc.IdCache(ip).get("ids"), ["1"])


if __name__ == "__main__":
    unittest.main()
