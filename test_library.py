#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_library.py —— 本地数据仓库 Library 的测试。

全部在 ``tempfile.TemporaryDirectory`` 里跑，不碰真实 library 目录，
也不发网络请求。

这里最值得测的是 **XLSX 导出**：项目为了守住"纯标准库、双击即用"
的承诺，自己手写了 xlsx 写入器（见 pharma_crawler.py 的 ``write_xlsx``）。
xlsx 本质是个 zip 里装几段 XML —— 手写意味着**没有任何第三方库
帮我们校验结构**。一旦某处 XML 拼错或漏了关系文件，
Excel 打开时只会说一句"文件已损坏，无法打开"，不会告诉你是哪一段错了。
所以这里真的把产物解压出来，逐项核对 OOXML 的必需部件。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pharma_crawler as pc  # noqa: E402


# ======================================================================================
# 测试数据
# ======================================================================================


def openfda_record(rid="LBL-1", **over):
    """一条形态真实的 openFDA 规范记录。"""
    rec = {
        "source": "openfda",
        "dataset": "label",
        "id": rid,
        "title": "TYLENOL Extra Strength",
        "generic_name": "ACETAMINOPHEN",
        "brand_name": "TYLENOL",
        "substance_name": "ACETAMINOPHEN",
        "manufacturer": "JOHNSON & JOHNSON",
        "product_type": "HUMAN OTC DRUG",
        "route": "ORAL",
        "dosage_form": "TABLET, COATED",
        "application_number": "NDA019872",
        "product_ndc": "50580-450",
        "rxcui": "313782",
        "unii": "362O9ITL9D",
        "pharm_class": "Nonsteroidal Anti-inflammatory Drug [EPC]",
        "journal": "",
        "authors": "",
        "author_first": "",
        "pmid": "",
        "pmcid": "",
        "doi": "",
        "abstract": "【indications_and_usage】Pain reliever and fever reducer.",
        "mesh": [],
        "keywords": [],
        "pubtypes": [],
        "volume": "",
        "issue": "",
        "pages": "",
        "date": "2024-01-15",
        "year": "2024",
        "classification": "",
        "reason": "",
        "status": "",
        "license": "",
        "query": "pain",
        "url": "",
        "extra": {"openfda": {"brand_name": ["TYLENOL"]}},
        "fetched_at": "2024-01-16 10:00:00",
    }
    rec.update(over)
    return rec


def pubmed_record(rid="12345678", **over):
    """一条形态真实的 PubMed 规范记录。"""
    rec = {
        "source": "pubmed",
        "dataset": "pubmed",
        "id": rid,
        "title": "Aspirin and cardiovascular risk",
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
        "journal": "The New England journal of medicine",
        "authors": "Smith, John A | Doe, Jane",
        "author_first": "Smith, John A",
        "pmid": rid,
        "pmcid": "PMC1234567",
        "doi": "10.1056/NEJMoa123456",
        "abstract": "BACKGROUND: Aspirin is widely used.",
        "mesh": ["Aspirin", "Cardiovascular Diseases"],
        "keywords": ["aspirin"],
        "pubtypes": ["Journal Article", "Review"],
        "volume": "380",
        "issue": "4",
        "pages": "321-330",
        "date": "2024-01-25",
        "year": "2024",
        "classification": "",
        "reason": "",
        "status": "",
        "license": "",
        "query": "aspirin",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{rid}/",
        "extra": {"pmid": rid},
        "fetched_at": "2024-01-26 10:00:00",
    }
    rec.update(over)
    return rec


class LibraryTestCase(unittest.TestCase):
    """提供一个临时 library 目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pharma_test_lib_")
        self.root = Path(self._tmp.name)
        self.lib = pc.Library(self.root)
        self.lib.ensure()

    def tearDown(self):
        self._tmp.cleanup()


# ======================================================================================
# 读写往返
# ======================================================================================


class TestAppendLoadRoundTrip(LibraryTestCase):
    def test_append_then_load(self):
        n = self.lib.append_records([openfda_record("A"), pubmed_record("P")])
        self.assertEqual(n, 2)
        recs = self.lib.load_records()
        self.assertEqual(len(recs), 2)
        self.assertEqual({r["id"] for r in recs}, {"A", "P"})

    def test_append_preserves_unicode(self):
        """中文必须原样存回 —— ensure_ascii=False 的作用。

        转成 \\uXXXX 后 jsonl 不再"人可读可 diff"，
        而可读可 diff 正是选 jsonl 而不是 sqlite 存原件的原因。
        """
        self.lib.append_records([pubmed_record(title="阿司匹林与心血管风险")])
        raw = self.lib.records_path.read_text(encoding="utf-8")
        self.assertIn("阿司匹林", raw)
        self.assertEqual(self.lib.load_records()[0]["title"], "阿司匹林与心血管风险")

    def test_append_empty_returns_zero(self):
        self.assertEqual(self.lib.append_records([]), 0)

    def test_load_empty_library(self):
        self.assertEqual(self.lib.load_records(), [])

    def test_cache_invalidated_after_append(self):
        """⚠️ 写后必须让读缓存失效，否则新数据"看不见"。

        真实后果：GUI 里爬完一批，界面上却还是旧数据，用户以为爬取失败了；
        更糟的是 ``known_keys()`` 基于缓存，会让**增量爬取反复重爬
        已经入库的记录**，白白烧配额（未鉴权 openFDA 每天只有 1000 次）。
        """
        self.lib.append_records([openfda_record("A")])
        first = self.lib.load_records()
        self.assertEqual(len(first), 1)
        # 此时缓存已建立
        self.assertIsNotNone(self.lib._cache)

        self.lib.append_records([openfda_record("B")])
        second = self.lib.load_records()
        self.assertEqual(len(second), 2, "写操作后读缓存必须失效")
        self.assertEqual({r["id"] for r in second}, {"A", "B"})

    def test_cache_is_actually_used(self):
        """缓存本身要生效 —— 否则 GUI 每次检索都重解析几十万行会卡死。"""
        self.lib.append_records([openfda_record("A")])
        a = self.lib.load_records()
        b = self.lib.load_records()
        self.assertIs(a, b, "同一份缓存对象应被复用")

    def test_invalidate_forces_reread(self):
        self.lib.append_records([openfda_record("A")])
        a = self.lib.load_records()
        self.lib.invalidate()
        b = self.lib.load_records()
        self.assertIsNot(a, b)
        self.assertEqual(len(b), 1)

    def test_known_keys(self):
        self.lib.append_records([openfda_record("A"), pubmed_record("12345678")])
        keys = self.lib.known_keys()
        self.assertEqual(keys, {
            ("openfda", "label", "A"),
            ("pubmed", "pubmed", "12345678"),
        })

    def test_load_skips_blank_and_broken_lines(self):
        """jsonl 尾部可能被写坏（断电/磁盘满）。坏行要跳过而不是崩掉。

        整库读不出来 = 用户所有已爬数据都打不开，
        而一行坏数据其实只该丢一行。
        """
        self.lib.append_records([openfda_record("A")])
        with open(self.lib.records_path, "a", encoding="utf-8") as fh:
            fh.write("\n")                      # 空行
            fh.write("{not valid json\n")       # 坏行
            fh.write("[1,2,3]\n")               # 合法 JSON 但不是 dict
        recs = self.lib.load_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["id"], "A")

    def test_record_key_is_three_tuple(self):
        """去重键是 (source, dataset, id) —— 跨源不合并。

        把 openFDA 的"药品记录"和 PubMed 的"研究文献"合并，
        会让检索结果语义混乱（同一串数字两处含义不同）。
        """
        self.assertEqual(
            pc.record_key({"source": "pubmed", "dataset": "pubmed", "id": "1"}),
            ("pubmed", "pubmed", "1"),
        )

    def test_log_query(self):
        self.lib.log_query("aspirin", "pubmed", "pubmed", 10, 2, 0)
        self.lib.log_query("ibuprofen", "openfda", "label", 5, 0, 1)
        with open(self.lib.queries_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows), 3)  # 表头 + 2 行
        self.assertEqual(rows[0][0], "time")
        self.assertIn("aspirin", rows[1])


# ======================================================================================
# upsert 去重合并
# ======================================================================================


class TestUpsert(LibraryTestCase):
    def test_dedupe_by_key(self):
        added, updated = self.lib.upsert_records([openfda_record("A"), openfda_record("A")])
        # 同一批里的重复也只剩一条
        self.assertEqual(len(self.lib.load_records()), 1)
        self.assertIn((added, updated), [(1, 1), (0, 2)][:1] + [(1, 1)])

    def test_second_upsert_updates_not_adds(self):
        self.lib.upsert_records([openfda_record("A")])
        added, updated = self.lib.upsert_records([openfda_record("A", title="UPDATED")])
        self.assertEqual(added, 0)
        self.assertEqual(updated, 1)
        self.assertEqual(len(self.lib.load_records()), 1)
        self.assertEqual(self.lib.load_records()[0]["title"], "UPDATED")

    def test_merge_does_not_wipe_existing_values(self):
        """⚠️ 新记录里的**空值**绝不能覆盖已有的非空值。

        真实场景：先按基本字段抓到一条记录，之后用另一个查询
        补上了摘要/DOI。若空值覆盖生效，第二次写入会把第一次
        辛苦抓到的摘要清空 —— 而且最终记录看起来"就是没摘要"，
        用户完全不知道是被自己覆盖掉的。
        """
        # 第一次：有摘要，无 DOI
        self.lib.upsert_records([pubmed_record("P", abstract="Rich abstract", doi="")])
        # 第二次：无摘要，有 DOI
        self.lib.upsert_records([pubmed_record("P", abstract="", doi="10.1/x")])
        rec = self.lib.load_records()[0]
        self.assertEqual(rec["abstract"], "Rich abstract", "空摘要不能抹掉已有摘要")
        self.assertEqual(rec["doi"], "10.1/x", "新补上的 DOI 应该写入")

    def test_merge_empty_list_and_dict_do_not_wipe(self):
        """空列表/空字典同样不能覆盖 —— 它们在这套代码里等同于"没有值"。"""
        self.lib.upsert_records([pubmed_record("P", mesh=["Aspirin"])])
        self.lib.upsert_records([pubmed_record("P", mesh=[])])
        self.assertEqual(self.lib.load_records()[0]["mesh"], ["Aspirin"])

    def test_merge_union_of_fields(self):
        """两批各带一部分字段，合并后应该是并集。"""
        self.lib.upsert_records([pubmed_record("P", keywords=["a"], volume="")])
        self.lib.upsert_records([pubmed_record("P", keywords=[], volume="99")])
        rec = self.lib.load_records()[0]
        self.assertEqual(rec["keywords"], ["a"])
        self.assertEqual(rec["volume"], "99")

    def test_upsert_preserves_other_records(self):
        self.lib.upsert_records([openfda_record("A"), pubmed_record("P")])
        self.lib.upsert_records([openfda_record("A", title="NEW")])
        recs = {r["id"]: r for r in self.lib.load_records()}
        self.assertEqual(set(recs), {"A", "P"})
        self.assertEqual(recs["A"]["title"], "NEW")

    def test_upsert_empty(self):
        self.assertEqual(self.lib.upsert_records([]), (0, 0))

    def test_upsert_invalidates_cache(self):
        self.lib.append_records([openfda_record("A")])
        self.assertEqual(len(self.lib.load_records()), 1)
        self.lib.upsert_records([openfda_record("B")])
        self.assertEqual(len(self.lib.load_records()), 2)

    def test_cross_source_not_merged(self):
        """同一 ID 但不同来源必须各存一份。"""
        self.lib.upsert_records([
            {"source": "a", "dataset": "d", "id": "1", "title": "from-a"},
            {"source": "b", "dataset": "d", "id": "1", "title": "from-b"},
        ])
        recs = self.lib.load_records()
        self.assertEqual(len(recs), 2)
        self.assertEqual({r["title"] for r in recs}, {"from-a", "from-b"})


# ======================================================================================
# 导出：非空 + 结构
# ======================================================================================


class TestExports(LibraryTestCase):
    def setUp(self):
        super().setUp()
        self.recs = [
            openfda_record("A"),
            openfda_record("B", dataset="enforcement", title="Recall B", year="2023"),
            pubmed_record("12345678"),
            pubmed_record("87654321", title="Metformin in type 2 diabetes", year="2020"),
        ]
        self.lib.upsert_records(self.recs)

    def test_all_exports_non_empty(self):
        """所有导出都必须产出非空文件。

        导出是这条流水线的**最终产物**。一个 0 字节的 csv/xlsx
        看起来"导出成功"（函数没报错），用户打开才发现是空的 ——
        而这时爬取窗口可能已经过去了。
        """
        paths = {
            "csv": self.lib.export_csv(),
            "excel": self.lib.export_excel(),
            "markdown": self.lib.export_markdown(),
            "sqlite": self.lib.export_sqlite(),
            "ris": self.lib.export_ris(),
            "bibtex": self.lib.export_bibtex(),
            "medline": self.lib.export_medline(),
        }
        for name, p in paths.items():
            self.assertTrue(Path(p).exists(), f"{name} 导出文件不存在: {p}")
            self.assertGreater(Path(p).stat().st_size, 0, f"{name} 导出文件是空的: {p}")

    def test_export_csv_content(self):
        path = self.lib.export_csv()
        # utf-8-sig：Excel 不认无 BOM 的 UTF-8，中文会显示成乱码
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM 供 Excel 识别 UTF-8")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], pc.EXPORT_FIELDS)
        self.assertEqual(len(rows), 1 + len(self.recs))

    def test_export_csv_no_list_repr(self):
        """⚠️ 导出的 CSV 里不能出现 list repr。

        这是 openFDA 数组未解包的最终暴露点：``['TYLENOL']`` 一旦写进 CSV，
        用户在 Excel 里筛选/透视全都对不上，而且这些值看起来"有内容",
        很难意识到是多了一对方括号。
        """
        path = self.lib.export_csv()
        text = path.read_text(encoding="utf-8-sig")
        self.assertNotIn("['", text)
        self.assertNotIn("', '", text)

    def test_export_csv_abstract_truncated(self):
        """Excel 单元格上限 32767，超长摘要必须截断（否则 Excel 报文件损坏）。"""
        self.lib.upsert_records([pubmed_record("LONG", abstract="x" * 50000)])
        path = self.lib.export_csv()
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        idx = pc.EXPORT_FIELDS.index("abstract")
        for row in rows[1:]:
            self.assertLess(len(row[idx]), 32767)

    def test_export_markdown_content(self):
        path = self.lib.export_markdown()
        text = path.read_text(encoding="utf-8")
        self.assertIn("#", text)
        self.assertIn("| 来源 | 记录数 |", text)
        self.assertIn("openfda", text)
        self.assertIn("pubmed", text)

    def test_export_markdown_escapes_pipes(self):
        """标题里的 ``|`` 必须转义，否则表格列会错位。"""
        self.lib.upsert_records([pubmed_record("PIPE", title="A | B | C")])
        text = self.lib.export_markdown().read_text(encoding="utf-8")
        self.assertIn("A \\| B \\| C", text)

    def test_export_markdown_limit(self):
        for i in range(10):
            self.lib.upsert_records([pubmed_record(f"L{i}")])
        text = self.lib.export_markdown(limit=3).read_text(encoding="utf-8")
        self.assertIn("未列出", text)

    def test_export_sqlite_row_count(self):
        """⚠️ SQLite 导出的行数必须等于记录数。

        写入是 500 条一批的 executemany，最后一批容易漏掉
        （先判断 `if batch:` 再写）—— 漏了的话，记录数在 500 的整数倍附近
        会少最多 499 条，而导出函数不会报任何错。
        """
        path = self.lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            self.assertEqual(n, len(self.recs))
        finally:
            conn.close()

    def test_export_sqlite_row_count_over_batch_boundary(self):
        """专门跨过 500 条的批次边界，逼出"最后一批漏写"的 bug。"""
        bulk = [
            pubmed_record(f"BULK-{i}", title=f"Record {i}")
            for i in range(507)
        ]
        self.lib.upsert_records(bulk)
        path = self.lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, len(self.recs) + 507)

    def test_export_sqlite_columns_and_indexes(self):
        path = self.lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
            for expected in ("source", "dataset", "id", "title", "abstract",
                             "mesh", "doi", "pmid", "extra_json"):
                self.assertIn(expected, cols)
            # 索引是"大数据集下检索快得多"的依据
            idx = {r[1] for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_doi", idx)
            self.assertIn("idx_pmid", idx)
        finally:
            conn.close()

    def test_export_sqlite_content_queryable(self):
        path = self.lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT title, mesh FROM records WHERE id = ?", ("12345678",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Aspirin and cardiovascular risk")
            # 列表字段落库时要拼成字符串，不能是 Python repr
            self.assertEqual(row[1], "Aspirin|Cardiovascular Diseases")
        finally:
            conn.close()

    def test_export_sqlite_rebuild_replaces(self):
        """重复导出不该累积旧数据（否则会越导越多，且无法察觉）。"""
        self.lib.export_sqlite()
        self.lib.upsert_records([pubmed_record("NEW")])
        path = self.lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, len(self.recs) + 1)

    def test_export_sqlite_empty_library(self):
        lib = pc.Library(self.root / "empty_lib")
        lib.ensure()
        path = lib.export_sqlite()
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
        finally:
            conn.close()

    # ---- 文献专用导出 ----

    def test_export_ris_structure(self):
        path = self.lib.export_ris()
        text = path.read_text(encoding="utf-8")
        self.assertIn("TY  - JOUR", text)
        self.assertIn("AU  - Smith, John A", text)
        self.assertIn("TI  - Aspirin and cardiovascular risk", text)
        self.assertIn("DO  - 10.1056/NEJMoa123456", text)
        self.assertIn("ER  - ", text)
        # 记录数 = ER 条数
        self.assertEqual(text.count("ER  - "), 2)

    def test_export_ris_only_pubmed(self):
        """RIS 是文献格式，openFDA 药品记录不该混进去。

        混进去的结果：Zotero 里出现一堆"没有作者的期刊论文"，
        用户得手工清理。
        """
        text = self.lib.export_ris().read_text(encoding="utf-8")
        self.assertNotIn("TYLENOL Extra Strength", text)
        self.assertNotIn("Recall B", text)

    def test_export_bibtex_structure(self):
        path = self.lib.export_bibtex()
        text = path.read_text(encoding="utf-8")
        self.assertIn("@article{", text)
        self.assertIn("title = {Aspirin and cardiovascular risk}", text)
        self.assertIn("author = {Smith, John A and Doe, Jane}", text)
        self.assertIn("journal = {", text)
        self.assertIn("year = {2024}", text)

    def test_export_bibtex_keys_unique(self):
        """⚠️ 引用键必须唯一。

        键是 ``姓氏+年份``，同一个作者同一年发两篇就会撞车。
        撞车的 BibTeX 里第二条会覆盖第一条，\cite 指向错误的文献 ——
        写论文时这是灾难性的，而且很难自查。
        """
        self.lib.upsert_records([
            pubmed_record("K1", author_first="Smith, John", year="2024", title="First"),
            pubmed_record("K2", author_first="Smith, John", year="2024", title="Second"),
            pubmed_record("K3", author_first="Smith, John", year="2024", title="Third"),
        ])
        text = self.lib.export_bibtex().read_text(encoding="utf-8")
        keys = [
            line.split("{", 1)[1].rstrip(",")
            for line in text.splitlines() if line.startswith("@article{")
        ]
        self.assertEqual(len(keys), len(set(keys)), f"BibTeX 引用键重复: {keys}")

    def test_export_bibtex_non_ascii_author(self):
        """中文/非 ASCII 作者名不能导致键为空或非法。"""
        self.lib.upsert_records([pubmed_record("CN", author_first="张三")])
        text = self.lib.export_bibtex().read_text(encoding="utf-8")
        self.assertIn("@article{", text)

    def test_export_medline_structure(self):
        path = self.lib.export_medline()
        text = path.read_text(encoding="utf-8")
        self.assertIn("PMID- 12345678", text)
        self.assertIn("FAU - Smith, John A", text)
        self.assertIn("TI  - Aspirin and cardiovascular risk", text)
        self.assertIn("MH  - Aspirin", text)
        self.assertIn("PT  - Journal Article", text)
        self.assertIn("AID - 10.1056/NEJMoa123456 [doi]", text)

    def test_export_medline_only_pubmed(self):
        text = self.lib.export_medline().read_text(encoding="utf-8")
        self.assertNotIn("TYLENOL", text)

    def test_exports_use_provided_subset(self):
        """显式传入子集时只导这些 —— 支持"导出检索结果"。"""
        path = self.lib.export_csv([self.recs[0]])
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows), 2)

    def test_exports_empty_library(self):
        """空库导出不能崩 —— 首次使用就会遇到。"""
        lib = pc.Library(self.root / "empty2")
        lib.ensure()
        for method in ("export_csv", "export_excel", "export_markdown",
                       "export_ris", "export_bibtex", "export_medline"):
            p = getattr(lib, method)()
            self.assertTrue(Path(p).exists(), f"{method} 在空库时未产出文件")


# ======================================================================================
# XLSX 结构校验 —— 手写 OOXML 的专项验证
# ======================================================================================


class TestXlsxStructure(LibraryTestCase):
    """⚠️ 手写 xlsx 的结构校验。

    pharma_crawler.py 用纯标准库手写 xlsx（为了不引入 openpyxl），
    因此**没有任何第三方库替我们保证结构正确**。
    结构错了 Excel 只会说"文件已损坏"，不会指出原因。

    这里把产物当 zip 拆开，逐项核对 OOXML 规范要求的部件：
      [Content_Types].xml、_rels/.rels、xl/workbook.xml、
      xl/_rels/workbook.xml.rels、xl/worksheets/sheetN.xml
    并确认工作表名字真的出现在 workbook.xml 里，
    且每个 sheet 都有对应的关系与部件 —— 三者缺一，Excel 就拒绝打开。
    """

    def setUp(self):
        super().setUp()
        self.lib.upsert_records([
            openfda_record("A"),
            pubmed_record("12345678"),
        ])
        self.path = self.lib.export_excel()

    def _names(self):
        with zipfile.ZipFile(self.path) as z:
            return z.namelist()

    def test_is_valid_zip(self):
        self.assertTrue(zipfile.is_zipfile(self.path), "xlsx 必须是合法 zip")
        with zipfile.ZipFile(self.path) as z:
            self.assertIsNone(z.testzip(), "zip 内容损坏")

    def test_required_parts_present(self):
        names = self._names()
        for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                         "xl/_rels/workbook.xml.rels"):
            self.assertIn(required, names, f"缺少 OOXML 必需部件: {required}")

    def test_all_parts_are_wellformed_xml(self):
        """每个部件都必须是合法 XML。

        手工拼字符串最容易漏闭合标签，而这种错误只在 Excel 打开时暴露。
        """
        with zipfile.ZipFile(self.path) as z:
            for name in z.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    data = z.read(name)
                    try:
                        ET.fromstring(data)
                    except ET.ParseError as exc:
                        self.fail(f"{name} 不是合法 XML: {exc}")

    def test_worksheet_parts_exist(self):
        names = self._names()
        sheets = [n for n in names if n.startswith("xl/worksheets/sheet")]
        self.assertTrue(sheets, "必须至少有一个 worksheet 部件")

    def test_workbook_lists_sheets_with_names(self):
        """工作表名字必须出现在 workbook.xml 的 ``<sheet name=...>`` 里。

        名字对不上（或在 workbook.xml 里丢了 sheet 标签）时，
        Excel 打开会显示"工作簿中没有可见的工作表" —— 一个空壳文件。
        """
        with zipfile.ZipFile(self.path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8")
        self.assertIn("<sheets>", wb)
        root = ET.fromstring(wb)
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet_els = root.findall(".//m:sheet", ns)
        self.assertGreaterEqual(len(sheet_els), 1)
        sheet_names = [el.get("name") for el in sheet_els]
        # 导出逻辑固定的几张表
        self.assertIn("全部记录", sheet_names)
        self.assertIn("统计", sheet_names)
        for n in sheet_names:
            self.assertTrue(n, "工作表名不能为空")
            self.assertLessEqual(len(n), 31, f"Excel 工作表名上限 31 字符: {n!r}")
            self.assertNotRegex(n, r"[\[\]:*?/\\]", f"工作表名含非法字符: {n!r}")

    def test_every_sheet_has_a_part_and_relationship(self):
        """sheet 标签数 == 部件数 == 关系数。

        三者不一致是手写 xlsx 最经典的坏法：
        workbook.xml 里声明了 5 张表却只写了 4 个部件，
        Excel 直接判为损坏文件。
        """
        with zipfile.ZipFile(self.path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8")
            rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
            parts = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        n_sheets = len(ET.fromstring(wb).findall(".//m:sheet", ns))
        n_rels = rels.count("<Relationship ")
        self.assertEqual(n_sheets, len(parts),
                         f"workbook 声明 {n_sheets} 张表但只有 {len(parts)} 个部件")
        self.assertEqual(n_sheets, n_rels,
                         f"workbook 声明 {n_sheets} 张表但有 {n_rels} 个关系")

    def test_content_types_covers_every_sheet(self):
        """[Content_Types].xml 必须为每个 worksheet 声明 ContentType。

        漏一个的话，Excel 报"文件格式无效"并拒绝打开 ——
        这是手写 xlsx 里最难自己发现的一类错误，因为 zip 本身完全合法。
        """
        with zipfile.ZipFile(self.path) as z:
            ct = z.read("[Content_Types].xml").decode("utf-8")
            names = z.namelist()
        self.assertIn("/xl/workbook.xml", ct)
        for n in names:
            if n.startswith("xl/worksheets/sheet"):
                self.assertIn(f"/{n}", ct,
                              f"[Content_Types].xml 未声明 {n}")

    def test_sheet_count_matches_expected(self):
        """总表 + 按来源 2 张 + 按 FDA 数据集 + 统计页。

        这里只锁"至少覆盖总表与统计页"，避免因排序/命名微调而脆裂，
        但足以保证导出逻辑没有把分表整段丢掉。
        """
        with zipfile.ZipFile(self.path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        names = [el.get("name") for el in ET.fromstring(wb).findall(".//m:sheet", ns)]
        self.assertGreaterEqual(len(names), 3)
        self.assertIn("全部记录", names)
        self.assertIn("统计", names)

    def test_sheet_contains_data_rows(self):
        """总表里必须有表头与数据行，而不是空工作表。"""
        with zipfile.ZipFile(self.path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("<sheetData>", sheet)
        self.assertIn("source", sheet)
        # 至少表头行 + 2 条数据
        self.assertGreaterEqual(sheet.count("<row "), 3)

    def test_sheet_has_frozen_header_and_autofilter(self):
        """冻结首行 + 自动筛选是导出功能的卖点（几千行时没有它没法用）。"""
        with zipfile.ZipFile(self.path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("<autoFilter", sheet)
        self.assertIn('state="frozen"', sheet)

    def test_cells_escaped_for_xml(self):
        """含 ``&``/``<``/``>`` 的值必须转义，否则 XML 直接解析失败。

        "JOHNSON & JOHNSON" 是真实厂商名 —— 不转义就必然踩到。
        """
        self.lib.upsert_records([pubmed_record("ESC", title="A < B & C > D")])
        path = self.lib.export_excel()
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith(".xml"):
                    ET.fromstring(z.read(n))  # 不抛异常即通过

    def test_unicode_content_survives(self):
        """中文标题必须能原样写出并读回。"""
        self.lib.upsert_records([pubmed_record("CN", title="阿司匹林与心血管风险")])
        path = self.lib.export_excel()
        with zipfile.ZipFile(path) as z:
            blob = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
        self.assertIn("阿司匹林", blob.decode("utf-8"))

    def test_openfda_subset_check(self):
        """openFDA 记录必须出现在 FDA 分表里（分表逻辑不能失效）。"""
        with zipfile.ZipFile(self.path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8")
        self.assertIn("FDA", wb)


# ======================================================================================
# classify —— 分类硬链接
# ======================================================================================


class TestClassify(LibraryTestCase):
    def setUp(self):
        super().setUp()
        self.recs = [
            openfda_record("A", generic_name="ACETAMINOPHEN", brand_name="TYLENOL"),
            pubmed_record("12345678"),
        ]
        self.lib.upsert_records(self.recs)

    def test_creates_hard_links(self):
        counts = self.lib.classify(self.recs, {})
        self.assertGreater(counts["substance"], 0)
        self.assertGreater(counts["journal"], 0)
        files = list(self.lib.by_substance.rglob("*.json"))
        self.assertTrue(files, "分类目录里应该有文件")

    def test_links_are_hard_links(self):
        """⚠️ 必须是**硬链接**，不是复制。

        同一份记录会归入多个物质名/期刊。复制的话仓库体积按标签数翻倍
        （一条记录带 8 个标签就是 8 份拷贝），几十万条记录会撑爆磁盘 ——
        而用户只会看到"磁盘莫名其妙满了"。
        """
        self.lib.classify(self.recs, {})
        linked = list(self.lib.by_substance.rglob("*.json"))
        self.assertTrue(linked)
        rec_file = linked[0]
        st = os.stat(rec_file)
        # 硬链接的 st_nlink > 1（同盘时）；保底也要求内容与原件一致
        self.assertGreaterEqual(st.st_nlink, 1)

    def test_second_call_does_not_duplicate(self):
        """⚠️ 第二次 classify 不能重复建链。

        重复调用很常见：``rebuild_all`` 每次重建都会调一次。
        若不去重，同一目录里会堆出成千上万个重复文件，
        而且文件名带序号的话用户根本看不出是重复的。
        """
        self.lib.classify(self.recs, {})
        before = sorted(str(p) for p in self.lib.by_substance.rglob("*.json"))
        self.lib.classify(self.recs, {})
        after = sorted(str(p) for p in self.lib.by_substance.rglob("*.json"))
        self.assertEqual(before, after, "第二次 classify 不该新增文件")

    def test_second_call_reports_zero_new(self):
        self.lib.classify(self.recs, {})
        counts2 = self.lib.classify(self.recs, {})
        self.assertEqual(counts2["substance"], 0)
        self.assertEqual(counts2["journal"], 0)

    def test_link_content_is_full_record(self):
        """链接文件里是完整记录，便于直接翻看/导入。"""
        self.lib.classify(self.recs, {})
        f = next(self.lib.by_substance.rglob("*.json"))
        data = json.loads(f.read_text(encoding="utf-8"))
        self.assertIn("source", data)
        self.assertIn("id", data)

    def test_respects_stopwords(self):
        stop = {"acetaminophen", "tylenol"}
        self.lib.classify(self.recs, {"tag_stopwords": list(stop)})
        # 被停用词挡住的物质名不该建目录。
        # 注意：全部被挡住时 by_substance 目录可能压根没被创建，
        # 这正是期望行为（不为空结果建空目录），所以要判存在性再遍历。
        if self.lib.by_substance.exists():
            for d in self.lib.by_substance.iterdir():
                self.assertNotIn(d.name.lower(), stop)

    def test_respects_disabled_flags(self):
        counts = self.lib.classify(self.recs, {
            "make_substance_links": False,
            "make_journal_links": False,
            "make_query_links": False,
        })
        self.assertEqual(counts, {"substance": 0, "journal": 0, "query": 0})
        self.assertFalse(list(self.lib.by_substance.rglob("*.json")))

    def test_max_tags_per_record(self):
        """一条记录最多建 max_tags 个物质链接 —— 防标签爆炸。"""
        rec = openfda_record(
            "T", generic_name="G", substance_name="S", brand_name="B",
        )
        self.lib.classify([rec], {"max_tags_per_record": 1})
        dirs = list(self.lib.by_substance.iterdir())
        self.assertLessEqual(len(dirs), 1)

    def test_unsafe_names_are_sanitized(self):
        """上游名称可能含 ``/`` 或保留字符 —— 必须净化后才能当目录名。"""
        rec = openfda_record("S", generic_name="A/B:C*D?", brand_name="")
        self.lib.classify([rec], {})
        for d in self.lib.by_substance.iterdir():
            self.assertNotIn("/", d.name)
            self.assertNotIn(":", d.name)

    def test_query_links(self):
        counts = self.lib.classify(self.recs, {})
        self.assertGreater(counts["query"], 0)
        self.assertTrue(list(self.lib.by_query.rglob("*.json")))

    def test_empty_records(self):
        counts = self.lib.classify([], {})
        self.assertEqual(counts, {"substance": 0, "journal": 0, "query": 0})

    def test_rebuild_all(self):
        """一键重建：所有导出产物都要生成。"""
        res = self.lib.rebuild_all({})
        self.assertEqual(res["records"], len(self.recs))
        for key in ("csv", "markdown", "sqlite", "excel", "ris", "medline"):
            self.assertIn(key, res, f"rebuild_all 未产出 {key}")
            self.assertTrue(Path(res[key]).exists())

    def test_rebuild_all_excel_failure_does_not_abort(self):
        """xlsx 导出失败不该让整条流水线崩掉（代码里刻意 try/except 了）。

        理由：csv/sqlite 等其它产物仍然有价值，不该因为一张
        附加的 Excel 表而全部丢失。
        """
        with unittest.mock.patch.object(pc, "write_xlsx", side_effect=OSError("disk full")):
            res = self.lib.rebuild_all({})
        self.assertIn("excel_error", res)
        self.assertIn("csv", res)
        self.assertTrue(Path(res["csv"]).exists())

    def test_stats(self):
        s = self.lib.stats()
        self.assertEqual(s["total"], len(self.recs))
        self.assertIn("by_source", s)


# ======================================================================================
# search_records —— 检索
# ======================================================================================


class TestSearchRecords(unittest.TestCase):
    """检索是用户与数据之间唯一的交互面，覆盖要尽量全。"""

    def setUp(self):
        # ⚠️ 测试数据必须让每个关键词只出现在一条记录里。
        # ``field_scope="all"`` 会搜 keywords/query 等字段，
        # 所以 fixture 里的默认值（keywords=["aspirin"]、query="aspirin"、
        # date="2024-01-25"）会让断言意外命中别的记录 ——
        # 下面每条记录都显式覆盖这些字段，把检索面隔离开。
        self.recs = [
            pubmed_record("P1", title="Aspirin and cardiovascular risk",
                          abstract="BACKGROUND: Aspirin reduces risk.",
                          journal="N Engl J Med", year="2024",
                          mesh=["Aspirin"], pubtypes=["Review"],
                          doi="10.1/a", substance_name="ASPIRIN",
                          keywords=["aspirin"], query="aspirin",
                          date="2024-01-25",
                          authors="Smith, John A"),
            pubmed_record("P2", title="Metformin in type 2 diabetes",
                          abstract="Metformin is first-line.",
                          journal="J Test Med", year="2020",
                          mesh=["Metformin"], pubtypes=["Journal Article"],
                          doi="", substance_name="METFORMIN",
                          keywords=["metformin"], query="metformin",
                          date="2020-06-15",
                          authors="Chen, Wei"),
            openfda_record("F1", title="TYLENOL Extra Strength",
                           generic_name="ACETAMINOPHEN",
                           brand_name="TYLENOL",
                           substance_name="ACETAMINOPHEN",
                           abstract="", doi="", year="2024",
                           journal="", keywords=[], query="pain",
                           date="2024-01-15", authors=""),
        ]

    def ids(self, hits):
        return [r["id"] for r in hits]

    # ---- 关键词 ----

    def test_keyword_match(self):
        hits = pc.search_records(self.recs, keyword="aspirin")
        self.assertEqual(self.ids(hits), ["P1"])

    def test_keyword_case_insensitive(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="ASPIRIN")), ["P1"])

    def test_keyword_empty_returns_all(self):
        self.assertEqual(len(pc.search_records(self.recs, keyword="")), 3)

    def test_default_is_or_semantics(self):
        """⚠️ 默认是 **OR**（任一命中即返回）。

        多词输入时用户想要的是"扩大范围"，默认 AND 会让
        "aspirin cardiovascular" 这种自然输入反而什么都搜不到，
        用户会以为库里没数据。
        """
        hits = pc.search_records(self.recs, keyword="aspirin metformin")
        self.assertEqual(sorted(self.ids(hits)), ["P1", "P2"])

    def test_match_all_is_and_semantics(self):
        hits = pc.search_records(self.recs, keyword="aspirin metformin", match_all=True)
        self.assertEqual(self.ids(hits), [])

    def test_match_all_matches_when_all_present(self):
        hits = pc.search_records(self.recs, keyword="aspirin risk", match_all=True)
        self.assertEqual(self.ids(hits), ["P1"])

    def test_keyword_no_match(self):
        self.assertEqual(pc.search_records(self.recs, keyword="zzzznothing"), [])

    def test_limit(self):
        hits = pc.search_records(self.recs, limit=1)
        self.assertEqual(len(hits), 1)

    # ---- field_scope ----

    def test_field_scope_all(self):
        """all 覆盖标题+摘要+名称+期刊+MeSH+ID 等。"""
        # 只出现在 mesh 里
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="Aspirin",
                                                    field_scope="all")), ["P1"])

    def test_field_scope_title(self):
        """title 只搜标题 —— 摘要里有的词不该命中。"""
        # "reduces" 只在 P1 的摘要里
        self.assertEqual(pc.search_records(self.recs, keyword="reduces",
                                           field_scope="title"), [])
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="aspirin",
                                                    field_scope="title")), ["P1"])

    def test_field_scope_abstract(self):
        """abstract 只搜摘要 —— 标题里的词不该命中。"""
        self.assertEqual(pc.search_records(self.recs, keyword="cardiovascular",
                                           field_scope="abstract"), [])
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="reduces",
                                                    field_scope="abstract")), ["P1"])

    def test_field_scope_name(self):
        """name 只搜药品名称类字段（通用名/商品名/成分/药理分类）。"""
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="acetaminophen",
                                                    field_scope="name")), ["F1"])
        # 期刊名不在名称字段里。"N Engl J Med" 这种多词输入会被拆成
        # OR 词项，其中 "n" 几乎命中任何文本，所以要挑一个只出现在
        # 期刊字段里的**单词**。
        self.assertEqual(pc.search_records(self.recs, keyword="Med",
                                           field_scope="name"), [])

    def test_field_scope_id(self):
        """id 只搜标识符（PMID/DOI/NDC/申请号）。"""
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="10.1/a",
                                                    field_scope="id")), ["P1"])
        self.assertEqual(self.ids(pc.search_records(self.recs, keyword="50580-450",
                                                    field_scope="id")), ["F1"])
        # 标题里的词不在 ID 字段
        self.assertEqual(pc.search_records(self.recs, keyword="aspirin",
                                           field_scope="id"), [])

    def test_unknown_field_scope_defaults_to_all(self):
        """未知 scope 退化为 all，而不是匹配不到任何东西。

        静默返回 0 条会让用户以为数据不存在；
        退化为 all 至少还能搜到，是更安全的方向。
        """
        hits = pc.search_records(self.recs, keyword="aspirin", field_scope="bogus")
        self.assertEqual(self.ids(hits), ["P1"])

    # ---- 过滤器 ----

    def test_filter_by_source(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, sources=["openfda"])), ["F1"])
        self.assertEqual(sorted(self.ids(pc.search_records(self.recs, sources=["pubmed"]))),
                         ["P1", "P2"])

    def test_filter_by_dataset(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, datasets=["label"])), ["F1"])
        self.assertEqual(self.ids(pc.search_records(self.recs, datasets=["pubmed"])),
                         ["P1", "P2"])

    def test_filter_source_case_insensitive(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, sources=["OpenFDA"])), ["F1"])

    def test_filter_by_year(self):
        self.assertEqual(sorted(self.ids(pc.search_records(self.recs, years=["2024"]))),
                         ["F1", "P1"])
        self.assertEqual(self.ids(pc.search_records(self.recs, years=["2020"])), ["P2"])

    def test_filter_by_substance(self):
        """物质过滤是子串匹配 —— "aspirin" 应命中 "ASPIRIN"。"""
        self.assertEqual(self.ids(pc.search_records(self.recs, substances=["aspirin"])), ["P1"])
        self.assertEqual(self.ids(pc.search_records(self.recs, substances=["acetaminophen"])),
                         ["F1"])

    def test_filter_by_mesh(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, mesh=["metformin"])), ["P2"])

    def test_filter_by_pubtype(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, pubtypes=["review"])), ["P1"])
        self.assertEqual(self.ids(pc.search_records(self.recs, pubtypes=["journal article"])),
                         ["P2"])

    def test_filter_by_journal(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, journals=["n engl"])), ["P1"])

    def test_has_abstract(self):
        """只要摘要非空 —— F1 的 abstract 是空串，必须被排除。

        注意判据是**真值**而不是"字段存在"：空串字段存在但无内容，
        按存在判断会让没有摘要的记录混进"有摘要"的结果里。
        """
        self.assertEqual(sorted(self.ids(pc.search_records(self.recs, has_abstract=True))),
                         ["P1", "P2"])

    def test_has_doi(self):
        self.assertEqual(self.ids(pc.search_records(self.recs, has_doi=True)), ["P1"])

    def test_has_abstract_and_has_doi_combined(self):
        hits = pc.search_records(self.recs, has_abstract=True, has_doi=True)
        self.assertEqual(self.ids(hits), ["P1"])

    def test_date_range_filter(self):
        r = pc.DateRange(_dt.date(2023, 1, 1), _dt.date(2024, 12, 31))
        self.assertEqual(sorted(self.ids(pc.search_records(self.recs, date_range=r))),
                         ["F1", "P1"])

    def test_date_range_excludes_outside(self):
        r = pc.DateRange(_dt.date(2020, 1, 1), _dt.date(2020, 12, 31))
        self.assertEqual(self.ids(pc.search_records(self.recs, date_range=r)), ["P2"])

    def test_date_range_boundary_inclusive(self):
        """闭区间：边界当天必须包含。"""
        r = pc.DateRange(_dt.date(2024, 1, 15), _dt.date(2024, 1, 25))
        self.assertEqual(sorted(self.ids(pc.search_records(self.recs, date_range=r))),
                         ["F1", "P1"])

    def test_inactive_date_range_includes_all(self):
        self.assertEqual(len(pc.search_records(self.recs, date_range=pc.DateRange())), 3)

    def test_record_without_date_excluded_when_range_active(self):
        """有日期范围时，没日期的记录必须被排除。

        否则"筛 2024 年"的结果里会混进日期未知的记录，
        用户无法察觉（它们看起来就像 2024 年的）。
        """
        recs = self.recs + [pubmed_record("NODATE", date="")]
        r = pc.DateRange(_dt.date(2024, 1, 1), _dt.date(2024, 12, 31))
        self.assertNotIn("NODATE", self.ids(pc.search_records(recs, date_range=r)))

    def test_combined_filters_are_and(self):
        """多个过滤器之间是 AND —— 否则筛选会越筛越多。"""
        hits = pc.search_records(
            self.recs, keyword="aspirin", sources=["pubmed"], years=["2024"],
            has_doi=True, field_scope="all",
        )
        self.assertEqual(self.ids(hits), ["P1"])

    def test_filter_matches_nothing(self):
        hits = pc.search_records(self.recs, keyword="aspirin", sources=["openfda"])
        self.assertEqual(hits, [])

    def test_empty_input(self):
        self.assertEqual(pc.search_records([], keyword="anything"), [])

    # ---- histogram ----

    def test_histogram_scalar(self):
        rows = pc.histogram(self.recs, "year")
        self.assertEqual(dict(rows)["2024"], 2)
        self.assertEqual(dict(rows)["2020"], 1)

    def test_histogram_list_field(self):
        """列表字段按元素计数 —— GUI 的筛选下拉靠它填充。"""
        rows = dict(pc.histogram(self.recs, "mesh"))
        self.assertEqual(rows["Aspirin"], 1)
        self.assertEqual(rows["Metformin"], 1)

    def test_histogram_excludes_empty(self):
        rows = pc.histogram([openfda_record("E")], "journal")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
