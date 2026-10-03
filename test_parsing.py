#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_parsing.py —— 数据解析 / 规范化的离线单元测试。

只依赖标准库（unittest + unittest.mock），不需要任何 pip install，
也不发任何网络请求 —— 这与项目"纯标准库、双击即用"的承诺保持一致：
测试套件本身如果引入第三方依赖，就等于给项目加了一条隐性安装门槛。

这里覆盖的三类东西最容易在生产环境静默出错：

1. **openFDA 的数组字段**。``openfda.*`` 的值**永远**是数组，即使只有一个元素。
   一旦某处忘了解包，CSV 里就会出现 ``['TYLENOL']`` 这种脏数据 ——
   它不会报错、不会崩，只会安静地把整库数据污染掉，事后极难回溯。
2. **PubMed XML 的两个 DOI 位置**。``Article/ELocationID`` 与
   ``PubmedData/ArticleIdList/ArticleId`` 是**互相独立**的两处，
   只读一处会随机丢掉约一半文献的 DOI，而"DOI 缺失"看起来只像数据稀疏，不像 bug。
3. **日期格式的不对称**。openFDA 不良事件/标签要 YYYYMMDD，
   召回要 YYYY-MM-DD。把两者"统一"成一个格式会让召回查询**全部返回 0 条**，
   且 openFDA 不会报错（只是查不到），排查成本极高。
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
from pathlib import Path

# 测试文件与 pharma_crawler.py 同目录，直接 import 即可；
# 显式插 path 是为了在从别的目录用 -m unittest discover 调用时也能找到模块。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pharma_crawler as pc  # noqa: E402


# ======================================================================================
# _loads_tolerant —— 传输被截断时的抢救
# ======================================================================================


class TestLoadsTolerant(unittest.TestCase):
    """容错 JSON 解析。

    背景：openFDA 单次响应实测可达 28MB，长连接中途被掐断是常态。
    截断后尾部不完整，但**前面的记录其实是完整可用的**。
    直接丢弃等于白跑一趟还白白消耗了每日 1000 次的配额。
    """

    def test_normal_json(self):
        obj = pc._loads_tolerant('{"meta": {"results": {"total": 2}}, "results": [{"id": "a"}]}')
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["results"], [{"id": "a"}])

    def test_truncated_mid_array_recovers_results(self):
        """数组中间被截断，必须仍能救回 results 列表。

        这是 _loads_tolerant 存在的唯一理由：截断发生在数组中间时，
        已收到的前几条记录是完好的，丢掉了就白白浪费了一次配额
        （未鉴权每天只有 1000 次，一次几十 MB 的响应很贵）。
        """
        full = (
            '{"meta": {"results": {"skip": 0, "limit": 3, "total": 3}}, '
            '"results": ['
            '{"id": "rec-1", "openfda": {"brand_name": ["A"]}}, '
            '{"id": "rec-2", "openfda": {"brand_name": ["B"]}}, '
            '{"id": "rec-3", "openfda": {"brand'  # 在这里被掐断
        )
        obj = pc._loads_tolerant(full)
        self.assertIsNotNone(obj, "截断的响应必须能抢救出已完整接收的记录")
        self.assertIsInstance(obj, dict)
        self.assertIn("results", obj)
        # 前两条是完整的，应该至少救回它们
        ids = [r.get("id") for r in obj["results"]]
        self.assertIn("rec-1", ids)
        self.assertIn("rec-2", ids)

    def test_truncated_immediately_after_object(self):
        """截断点正好落在某个对象收尾的 ``}`` 之后 —— 最常见的形态。"""
        text = '{"results": [{"id": "x"}, {"id": "y"'
        obj = pc._loads_tolerant(text)
        self.assertIsNotNone(obj)
        self.assertTrue(obj.get("results"))

    def test_garbage_returns_none(self):
        """纯垃圾输入必须返回 None，让上层重试，而不是抛异常或返回半个对象。"""
        self.assertIsNone(pc._loads_tolerant("<<<not json at all>>>"))
        self.assertIsNone(pc._loads_tolerant("null"))
        self.assertIsNone(pc._loads_tolerant("[1, 2, 3"))

    def test_empty_returns_none(self):
        self.assertIsNone(pc._loads_tolerant(""))
        self.assertIsNone(pc._loads_tolerant("   \n\t  "))

    def test_valid_json_without_results_is_returned_as_is(self):
        """完整的合法 JSON 走正常路径原样返回，不做 results 校验。

        校验 results 是 ``OpenFDAClient.parse_payload`` 的职责：
        ``_loads_tolerant`` 只负责"把文本变成对象"。
        两个职责分开，count 查询这种本来就没有 results 的响应才不会被误判。
        """
        obj = pc._loads_tolerant('{"meta": {"total": 0}}')
        self.assertEqual(obj, {"meta": {"total": 0}})

    def test_truncated_without_results_returns_none(self):
        """**截断**且救不回 results 时，必须返回 None 让上层重试。

        与上面那条的区别：这里是截断的（非法 JSON），只在确实能救回
        results 时才返回。否则返回一个残缺对象，上层会以为数据到手了，
        安静地少存一批记录。
        """
        # 截断位置在 meta 里，回退补括号也拼不出 results
        self.assertIsNone(pc._loads_tolerant('{"meta": {"results": {"total": 5}, "license": "CC0"'))


# ======================================================================================
# first_of / flatten / join_list —— openFDA 数组解包
# ======================================================================================


class TestFirstOf(unittest.TestCase):
    """``first_of`` 是 openFDA 字段取值的唯一正确入口。

    真实 bug 场景：``record["openfda"]["brand_name"]`` 拿到的是
    ``['TYLENOL']`` 而不是 ``'TYLENOL'``，直接写进 CSV 就成了
    ``['TYLENOL']``；检索时用户搜 "TYLENOL" 反而搜不到，
    而且这一列在 Excel 里看着"有值"，肉眼很难发现。
    """

    def test_single_element_list_unwrapped(self):
        # 这是 openFDA 最常见的形态：只有一个品牌名，但它仍然是数组
        self.assertEqual(pc.first_of(["TYLENOL"]), "TYLENOL")

    def test_repr_never_leaks(self):
        """绝不能把 list 的 repr 带出去 —— 这是本测试最重要的断言。"""
        got = pc.first_of(["TYLENOL"])
        self.assertNotIn("[", got)
        self.assertNotIn("'", got)
        self.assertNotEqual(got, "['TYLENOL']")

    def test_multi_element_takes_first(self):
        self.assertEqual(pc.first_of(["A", "B", "C"]), "A")

    def test_skips_empty_and_none_entries(self):
        """数组首元素可能是空串/None，不能因此返回空。"""
        self.assertEqual(pc.first_of(["", None, "REAL"]), "REAL")

    def test_all_empty_returns_default(self):
        self.assertEqual(pc.first_of([], "dflt"), "dflt")
        self.assertEqual(pc.first_of([None, ""], "dflt"), "dflt")

    def test_scalar_passthrough(self):
        """openFDA 偶尔也用标量（如 ndc 的顶层字段），不能因此崩掉。"""
        self.assertEqual(pc.first_of("PLAIN"), "PLAIN")
        self.assertEqual(pc.first_of(42), "42")
        self.assertEqual(pc.first_of(None, "d"), "d")

    def test_dict_flattened(self):
        self.assertEqual(pc.first_of({"a": 1}), "a=1")


class TestFlattenJoinList(unittest.TestCase):
    def test_flatten_list(self):
        self.assertEqual(pc.flatten(["a", "b"]), "a | b")

    def test_flatten_none_and_scalar(self):
        self.assertEqual(pc.flatten(None), "")
        self.assertEqual(pc.flatten("x"), "x")
        self.assertEqual(pc.flatten(7), "7")

    def test_flatten_respects_limit(self):
        """嵌套列表可能极长（如 distribution_pattern），limit 是防爆的保护。"""
        self.assertEqual(pc.flatten(list("abcdef"), limit=3), "a | b | c")

    def test_join_list(self):
        self.assertEqual(pc.join_list(["a", "b"]), "a|b")
        self.assertEqual(pc.join_list(["a", "b"], ","), "a,b")
        self.assertEqual(pc.join_list(None), "")
        self.assertEqual(pc.join_list(["a", None, ""]), "a")
        self.assertEqual(pc.join_list("solo"), "solo")


# ======================================================================================
# normalize_openfda —— 每个数据集
# ======================================================================================


class TestNormalizeOpenfda(unittest.TestCase):
    """各数据集的规范化。

    整体要求：``openfda`` 里的数组必须解成标量，
    否则 CSV / Excel / SQLite 三处导出会同时被污染。
    """

    def test_openfda_arrays_unwrapped_everywhere(self):
        """一条记录了所有数组字段 —— 逐个断言它们都没变成 list repr。"""
        rec = {
            "set_id": "SET-1",
            "effective_time": "20240115",
            "openfda": {
                "brand_name": ["TYLENOL"],
                "generic_name": ["ACETAMINOPHEN"],
                "substance_name": ["ACETAMINOPHEN"],
                "manufacturer_name": ["JOHNSON & JOHNSON"],
                "product_type": ["HUMAN OTC DRUG"],
                "route": ["ORAL"],
                "application_number": ["NDA019872"],
                "product_ndc": ["50580-450"],
                "rxcui": ["313782"],
                "unii": ["362O9ITL9D"],
                "pharm_class_epc": ["Nonsteroidal Anti-inflammatory Drug [EPC]"],
            },
        }
        out = pc.normalize_openfda("label", rec)
        for field in ("brand_name", "generic_name", "substance_name", "manufacturer",
                      "product_type", "route", "application_number", "product_ndc",
                      "rxcui", "unii", "pharm_class", "title"):
            val = out[field]
            self.assertIsInstance(val, str, f"{field} 必须是 str，实际 {type(val).__name__}")
            # 判据用 "['" —— pharm_class 的合法值里本来就带方括号
            # （如 "Nonsteroidal Anti-inflammatory Drug [EPC]"），
            # 只用 "[" 判会误伤真实数据。
            self.assertNotIn("['", val, f"{field} 泄漏了 list repr: {val!r}")
            self.assertFalse(val.startswith("["), f"{field} 泄漏了 list repr: {val!r}")
        self.assertEqual(out["brand_name"], "TYLENOL")
        self.assertEqual(out["substance_name"], "ACETAMINOPHEN")

    def test_multiple_openfda_values_are_flattened_not_reprd(self):
        """多元素数组也不能漏成 repr —— 拼接是允许的，repr 不是。"""
        rec = {
            "id": "x",
            "openfda": {"substance_name": ["IBUPROFEN", "PSEUDOEPHEDRINE"]},
        }
        out = pc.normalize_openfda("label", rec)
        self.assertNotIn("['", out["substance_name"])
        self.assertNotIn("', '", out["substance_name"])
        # 只取第一个，与 first_of 的语义一致
        self.assertEqual(out["substance_name"], "IBUPROFEN")

    def test_label(self):
        rec = {
            "id": "label-1",
            "effective_time": "20240115",
            "openfda": {"brand_name": ["Advil"], "generic_name": ["IBUPROFEN"]},
        }
        out = pc.normalize_openfda("label", rec)
        self.assertEqual(out["source"], "openfda")
        self.assertEqual(out["dataset"], "label")
        self.assertEqual(out["id"], "label-1")
        # 标题优先用品牌名，回退到通用名
        self.assertEqual(out["title"], "Advil")
        # effective_time 是 YYYYMMDD，要转成 ISO
        self.assertEqual(out["date"], "2024-01-15")
        self.assertEqual(out["year"], "2024")

    def test_label_title_falls_back_to_generic(self):
        rec = {"set_id": "s", "openfda": {"generic_name": ["IBUPROFEN"]}}
        self.assertEqual(pc.normalize_openfda("label", rec)["title"], "IBUPROFEN")

    def test_enforcement(self):
        rec = {
            "recall_number": "D-1234-2024",
            "report_date": "2024-03-05",
            "product_description": "Ibuprofen Tablets, 200mg",
            "classification": "Class II",
            "reason_for_recall": "subpotent drug",
            "recalling_firm": "Acme Pharma",
            "status": "Ongoing",
            "openfda": {"brand_name": ["IBUPROFEN"]},
        }
        out = pc.normalize_openfda("enforcement", rec)
        self.assertEqual(out["dataset"], "enforcement")
        self.assertEqual(out["title"], "Ibuprofen Tablets, 200mg")
        self.assertEqual(out["classification"], "Class II")
        self.assertEqual(out["reason"], "subpotent drug")
        self.assertEqual(out["manufacturer"], "Acme Pharma")
        self.assertEqual(out["status"], "Ongoing")
        # report_date 是带短横线的 YYYY-MM-DD，也要能转 ISO
        self.assertEqual(out["date"], "2024-03-05")
        # 召回正文要拼进 abstract，否则检索无内容可搜
        self.assertIn("subpotent drug", out["abstract"])

    def test_event(self):
        rec = {
            "safetyreportid": "10001234",
            "receivedate": "20240131",
            "serious": "1",
            "patient": {
                "reaction": [
                    {"reactionmeddrapt": "NAUSEA"},
                    {"reactionmeddrapt": "HEADACHE"},
                ]
            },
            "openfda": {"generic_name": ["IBUPROFEN"]},
        }
        out = pc.normalize_openfda("event", rec)
        self.assertEqual(out["dataset"], "event")
        # 不良事件没有现成标题，用前三个反应名拼
        self.assertEqual(out["title"], "NAUSEA / HEADACHE")
        self.assertEqual(out["date"], "2024-01-31")
        self.assertEqual(out["id"], "10001234")

    def test_event_without_patient(self):
        """缺少 patient 节点是常态，不能让规范化崩掉。"""
        rec = {"safetyreportid": "1", "receivedate": "20240101"}
        out = pc.normalize_openfda("event", rec)
        self.assertTrue(out["title"])

    def test_ndc(self):
        rec = {
            "product_ndc": "50580-450",
            "brand_name": "TYLENOL",
            "generic_name": "ACETAMINOPHEN",
            "labeler_name": "JOHNSON & JOHNSON",
            "dosage_form": "TABLET, COATED",
            "active_ingredients": [
                {"name": "ACETAMINOPHEN", "strength": "500 mg/1"},
            ],
        }
        out = pc.normalize_openfda("ndc", rec)
        self.assertEqual(out["dataset"], "ndc")
        self.assertEqual(out["id"], "50580-450")
        self.assertEqual(out["title"], "TYLENOL")
        self.assertEqual(out["manufacturer"], "JOHNSON & JOHNSON")
        self.assertEqual(out["dosage_form"], "TABLET, COATED")
        # 没有 openfda.substance_name 时，回退到 active_ingredients 拼出的成分
        self.assertIn("ACETAMINOPHEN", out["substance_name"])

    def test_drugsfda(self):
        rec = {
            "application_number": "NDA019872",
            "sponsor_name": "ACME PHARMA INC",
            "products": [
                {
                    "brand_name": "ACME-IBU",
                    "active_ingredients": [{"name": "IBUPROFEN", "strength": "200MG"}],
                }
            ],
        }
        out = pc.normalize_openfda("drugsfda", rec)
        self.assertEqual(out["dataset"], "drugsfda")
        self.assertEqual(out["id"], "NDA019872")
        self.assertEqual(out["title"], "ACME PHARMA INC")
        # products[].active_ingredients 也要挖出来
        self.assertIn("IBUPROFEN", out["substance_name"])

    def test_shortages(self):
        """⚠️ 端点是复数 shortages —— /drug/shortage.json 根本不存在。"""
        rec = {
            "package_ndc": "12345-678-90",
            "generic_name": "Cisplatin Injection",
            "company_name": "Acme Pharma",
            "status": "Currently in Shortage",
            "openfda": {"generic_name": ["CISPLATIN"]},
        }
        out = pc.normalize_openfda("shortages", rec)
        self.assertEqual(out["dataset"], "shortages")
        self.assertEqual(out["id"], "12345-678-90")
        self.assertEqual(out["title"], "Cisplatin Injection")
        self.assertEqual(out["manufacturer"], "Acme Pharma")
        self.assertEqual(out["status"], "Currently in Shortage")

    def test_orangebook(self):
        rec = {
            "application_number": "NDA019872",
            "trade_name": "ACME-IBU",
            "products": [{"brand_name": "ACME-IBU", "active_ingredients": [{"name": "IBUPROFEN"}]}],
        }
        out = pc.normalize_openfda("orangebook", rec)
        self.assertEqual(out["dataset"], "orangebook")
        self.assertEqual(out["id"], "NDA019872")
        self.assertEqual(out["title"], "ACME-IBU")
        self.assertIn("IBUPROFEN", out["substance_name"])

    def test_id_fallback_chain(self):
        """没有 id/set_id/safetyreportid 时，最后回退到内容哈希。

        没有这个回退，无 ID 的记录会全部塌缩成同一个空 key，
        去重时互相覆盖，最后只剩一条。
        """
        out = pc.normalize_openfda("label", {"description": "some label"})
        self.assertTrue(out["id"])
        self.assertNotEqual(out["id"], "")

    def test_non_dict_returns_empty(self):
        self.assertEqual(pc.normalize_openfda("label", None), {})
        self.assertEqual(pc.normalize_openfda("label", ["x"]), {})

    def test_extra_keeps_raw_record(self):
        """原始记录必须全量保留 —— 上游字段改版时不丢数据。"""
        rec = {"id": "x", "openfda": {"brand_name": ["B"]}, "weird_field": 42}
        out = pc.normalize_openfda("label", rec)
        self.assertEqual(out["extra"]["weird_field"], 42)


# ======================================================================================
# normalize_pubmed
# ======================================================================================


class TestNormalizePubmed(unittest.TestCase):
    def test_basic(self):
        rec = {
            "pmid": "12345678",
            "title": "Aspirin and cardiovascular risk",
            "abstract": "BACKGROUND: ...",
            "authors": ["Smith, John", "Doe, Jane"],
            "author_first": "Smith, John",
            "journal": "N Engl J Med",
            "volume": "380",
            "issue": "4",
            "pages": "321-330",
            "pubdate": "2024-01-25",
            "year": "2024",
            "doi": "10.1056/NEJMoa123456",
            "pmcid": "PMC1234567",
            "mesh": ["Aspirin", "Cardiovascular Diseases"],
            "keywords": ["aspirin"],
            "pubtypes": ["Journal Article"],
        }
        out = pc.normalize_pubmed(rec)
        self.assertEqual(out["source"], "pubmed")
        self.assertEqual(out["dataset"], "pubmed")
        self.assertEqual(out["id"], "12345678")
        self.assertEqual(out["title"], "Aspirin and cardiovascular risk")
        self.assertEqual(out["journal"], "N Engl J Med")
        self.assertEqual(out["volume"], "380")
        self.assertEqual(out["issue"], "4")
        self.assertEqual(out["pages"], "321-330")
        self.assertEqual(out["date"], "2024-01-25")
        self.assertEqual(out["year"], "2024")
        self.assertEqual(out["doi"], "10.1056/NEJMoa123456")
        self.assertEqual(out["pmcid"], "PMC1234567")
        self.assertEqual(out["mesh"], ["Aspirin", "Cardiovascular Diseases"])
        self.assertEqual(out["pubtypes"], ["Journal Article"])
        self.assertEqual(out["product_type"], "literature")
        self.assertIn("pubmed.ncbi.nlm.nih.gov/12345678", out["url"])

    def test_authors_joined_for_csv(self):
        """authors 是列表 → 必须拼成单格字符串，否则 CSV 里是 list repr。"""
        out = pc.normalize_pubmed({"pmid": "1", "authors": ["A, B", "C, D"]})
        self.assertIsInstance(out["authors"], str)
        self.assertNotIn("[", out["authors"])
        self.assertEqual(out["authors"], "A, B | C, D")

    def test_missing_fields_are_safe(self):
        """PubMed 记录字段缺失是常态，不能 KeyError。"""
        out = pc.normalize_pubmed({"pmid": "999"})
        self.assertEqual(out["id"], "999")
        self.assertEqual(out["mesh"], [])
        self.assertEqual(out["pubtypes"], [])
        self.assertEqual(out["authors"], "")

    def test_no_pmid_no_url(self):
        out = pc.normalize_pubmed({})
        self.assertEqual(out["url"], "")

    def test_license_present_for_tos(self):
        """PubMed 摘要可能受版权保护，license 字段必须存在以便合规展示。"""
        self.assertIn("license", pc.normalize_pubmed({"pmid": "1"}))


# ======================================================================================
# parse_pubmed_articles —— 手写一份真实形态的 PubmedArticleSet
# ======================================================================================

#: 两篇文章的 XML。刻意做成"真实形态"而不是最小可用：
#:  - 第一篇：结构化日期 + 分段摘要(带 Label) + 个人作者 + 团体作者 +
#:    DOI 在 Article/ELocationID + MeSH 带 MajorTopicYN + 关键词 + 出版类型
#:  - 第二篇：MedlineDate 自由文本日期("2020 Jan-Feb") + DOI 只在
#:    PubmedData/ArticleIdList + PMCID + MedlinePgn 页码
PUBMED_XML = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">12345678</PMID>
      <DateCompleted><Year>2024</Year><Month>02</Month><Day>10</Day></DateCompleted>
      <Article PubModel="Print">
        <Journal>
          <ISSN IssnType="Electronic">0028-4793</ISSN>
          <JournalIssue CitedMedium="Internet">
            <Volume>380</Volume>
            <Issue>4</Issue>
            <PubDate>
              <Year>2024</Year>
              <Month>Jan</Month>
              <Day>25</Day>
            </PubDate>
          </JournalIssue>
          <Title>The New England journal of medicine</Title>
          <ISOAbbreviation>N Engl J Med</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Aspirin and cardiovascular risk.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Aspirin is widely used.</AbstractText>
          <AbstractText Label="METHODS">We enrolled 1000 patients.</AbstractText>
          <AbstractText Label="RESULTS">Risk was reduced.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author ValidYN="Y">
            <LastName>Smith</LastName>
            <ForeName>John A</ForeName>
            <Initials>JA</Initials>
            <AffiliationInfo>
              <Affiliation>Dept of Medicine, Harvard Medical School</Affiliation>
            </AffiliationInfo>
          </Author>
          <Author ValidYN="Y">
            <LastName>Doe</LastName>
            <ForeName>Jane</ForeName>
            <Initials>J</Initials>
          </Author>
          <Author ValidYN="Y">
            <CollectiveName>SPRINT Research Group</CollectiveName>
          </Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D016454">Review</PublicationType>
        </PublicationTypeList>
        <ELocationID EIdType="doi" ValidYN="Y">10.1056/NEJMoa123456</ELocationID>
        <ELocationID EIdType="pii" ValidYN="Y">NEJMoa123456</ELocationID>
      </Article>
      <MedlineJournalInfo>
        <Country>United States</Country>
        <MedlineTA>N Engl J Med</MedlineTA>
      </MedlineJournalInfo>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName UI="D001241" MajorTopicYN="Y">Aspirin</DescriptorName>
          <QualifierName UI="Q000493" MajorTopicYN="N">therapeutic use</QualifierName>
        </MeshHeading>
        <MeshHeading>
          <DescriptorName UI="D002318" MajorTopicYN="N">Cardiovascular Diseases</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
      <KeywordList Owner="NOTNLM">
        <Keyword MajorTopicYN="N">aspirin</Keyword>
        <Keyword MajorTopicYN="Y">primary prevention</Keyword>
      </KeywordList>
      <Language>eng</Language>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="received"><Year>2023</Year><Month>10</Month><Day>1</Day></PubMedPubDate>
        <PubMedPubDate PubStatus="pubmed"><Year>2024</Year><Month>1</Month><Day>26</Day></PubMedPubDate>
      </History>
      <PublicationStatus>ppublish</PublicationStatus>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="pmc">PMC1234567</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="2">87654321</PMID>
      <Article PubModel="Electronic">
        <Journal>
          <JournalIssue CitedMedium="Internet">
            <Volume>12</Volume>
            <Issue>1-2</Issue>
            <PubDate>
              <MedlineDate>2020 Jan-Feb</MedlineDate>
            </PubDate>
          </JournalIssue>
          <Title>Journal of test medicine</Title>
          <ISOAbbreviation>J Test Med</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Metformin in type 2 diabetes.</ArticleTitle>
        <Abstract>
          <AbstractText>Metformin remains first-line therapy.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author ValidYN="Y">
            <LastName>Chen</LastName>
            <ForeName>Wei</ForeName>
          </Author>
        </AuthorList>
        <Pagination>
          <MedlinePgn>45-52</MedlinePgn>
        </Pagination>
      </Article>
      <MedlineJournalInfo>
        <Country>England</Country>
      </MedlineJournalInfo>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">87654321</ArticleId>
        <ArticleId IdType="doi">10.1000/jtm.2020.001</ArticleId>
        <ArticleId IdType="pmc">PMC7654321</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class TestParsePubmedArticles(unittest.TestCase):
    """PubMed XML 解析。

    这些断言直接对应 docs/DATA_SOURCE_NOTES.md 第 4.5 节的字段路径表。
    上游改版时最先坏掉的就是这些小众路径（团体作者、MedlineDate、
    第二处 DOI），而它们坏掉时不会报错，只会安静地少一批数据。
    """

    @classmethod
    def setUpClass(cls):
        cls.records = pc.parse_pubmed_articles(PUBMED_XML)
        cls.by_id = {r["pmid"]: r for r in cls.records}

    def test_two_articles(self):
        self.assertEqual(len(self.records), 2)
        self.assertEqual(set(self.by_id), {"12345678", "87654321"})

    def test_pmid_and_id(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["id"], "12345678")
        self.assertEqual(r["source"], "pubmed")

    def test_title(self):
        self.assertEqual(self.by_id["12345678"]["title"], "Aspirin and cardiovascular risk.")

    def test_abstract_with_label_sections(self):
        """分段摘要的 Label(BACKGROUND/METHODS/...) 必须保留。

        丢掉 Label 后，一段"METHODS"内容看起来就只是普通正文，
        做证据分级/方法学筛选时完全没有依据。
        """
        r = self.by_id["12345678"]
        self.assertIn("BACKGROUND: Aspirin is widely used.", r["abstract"])
        self.assertIn("METHODS: We enrolled 1000 patients.", r["abstract"])
        self.assertIn("RESULTS: Risk was reduced.", r["abstract"])
        labels = [s["label"] for s in r["abstract_sections"]]
        self.assertEqual(labels, ["BACKGROUND", "METHODS", "RESULTS"])
        self.assertEqual(r["abstract_sections"][1]["text"], "We enrolled 1000 patients.")

    def test_abstract_without_label(self):
        """无 Label 的摘要照样要取到。"""
        self.assertEqual(
            self.by_id["87654321"]["abstract"],
            "Metformin remains first-line therapy.",
        )

    def test_authors_personal(self):
        r = self.by_id["12345678"]
        self.assertIn("Smith, John A", r["authors"])
        self.assertIn("Doe, Jane", r["authors"])
        self.assertEqual(r["author_first"], "Smith, John A")
        self.assertEqual(r["author_count"], 3)

    def test_collective_name_group_author(self):
        """团体作者走 CollectiveName —— 漏了它，大型试验的署名就没了。

        像 "SPRINT Research Group" 这种团体署名在循证医学里是关键信息，
        漏掉后记录看起来只是"作者不全"，不会报错。
        """
        r = self.by_id["12345678"]
        self.assertIn("SPRINT Research Group", r["authors"])
        types = {d["name"]: d["type"] for d in r["author_details"]}
        self.assertEqual(types["SPRINT Research Group"], "collective")
        self.assertEqual(types["Smith, John A"], "personal")

    def test_affiliation(self):
        r = self.by_id["12345678"]
        smith = next(d for d in r["author_details"] if d["name"] == "Smith, John A")
        self.assertIn("Harvard Medical School", smith["affiliation"])

    def test_journal(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["journal"], "The New England journal of medicine")
        self.assertEqual(r["journal_abbrev"], "N Engl J Med")
        self.assertEqual(r["issn"], "0028-4793")

    def test_volume_issue_pages(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["volume"], "380")
        self.assertEqual(r["issue"], "4")
        r2 = self.by_id["87654321"]
        self.assertEqual(r2["volume"], "12")
        # MedlinePgn 是页码的另一种写法，必须也能取到
        self.assertEqual(r2["pages"], "45-52")

    def test_year_and_date(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["pubdate"], "2024-01-25")
        self.assertEqual(r["year"], "2024")

    def test_doi_from_article_elocationid(self):
        """DOI 位置一：``Article/ELocationID[@EIdType='doi']``。"""
        self.assertEqual(self.by_id["12345678"]["doi"], "10.1056/NEJMoa123456")

    def test_doi_from_pubmeddata_articleidlist(self):
        """DOI 位置二：``PubmedData/ArticleIdList/ArticleId[@IdType='doi']``。

        ⚠️ 这两处**互相独立**：不同期刊/journal 流程只往其中一处写。
        只读一处会随机丢掉约一半文献的 DOI，而表现只是"有些记录没 DOI"，
        看起来像上游数据稀疏，不像 bug —— 所以两处都必须有专门断言。
        """
        self.assertEqual(self.by_id["87654321"]["doi"], "10.1000/jtm.2020.001")

    def test_article_ids_dict(self):
        r = self.by_id["87654321"]
        self.assertEqual(r["article_ids"]["doi"], "10.1000/jtm.2020.001")
        self.assertEqual(r["article_ids"]["pubmed"], "87654321")

    def test_pmcid(self):
        """PMCID 带 PMC 前缀（如 PMC7745181），必须原样保留。"""
        self.assertEqual(self.by_id["12345678"]["pmcid"], "PMC1234567")
        self.assertEqual(self.by_id["87654321"]["pmcid"], "PMC7654321")

    def test_mesh_with_major_topic(self):
        r = self.by_id["12345678"]
        self.assertIn("Aspirin", r["mesh"])
        self.assertIn("Cardiovascular Diseases", r["mesh"])
        major = {m["term"]: m["major"] for m in r["mesh_details"]}
        # MajorTopicYN="Y" 才是主要主题词 —— 做 MeSH 主词筛选全靠它
        self.assertTrue(major["Aspirin"])
        self.assertFalse(major["Cardiovascular Diseases"])
        self.assertEqual(r["mesh_major"], ["Aspirin"])

    def test_mesh_qualifiers_and_ui(self):
        r = self.by_id["12345678"]
        asp = next(m for m in r["mesh_details"] if m["term"] == "Aspirin")
        self.assertEqual(asp["ui"], "D001241")
        self.assertEqual(asp["qualifiers"], ["therapeutic use"])

    def test_keywords(self):
        self.assertEqual(
            self.by_id["12345678"]["keywords"],
            ["aspirin", "primary prevention"],
        )

    def test_publication_types(self):
        self.assertEqual(
            self.by_id["12345678"]["pubtypes"],
            ["Journal Article", "Review"],
        )

    def test_medline_date_free_text(self):
        """``MedlineDate`` 是自由文本（如 "2020 Jan-Feb"），没有固定格式。

        这种日期在旧文献里非常普遍。解析失败就得回退到原文，
        绝不能因此抛异常或把整篇记录丢掉。
        """
        r = self.by_id["87654321"]
        self.assertEqual(r["pubdate"], "2020-01-01")
        self.assertEqual(r["pubdate_raw"], "2020 Jan-Feb")
        self.assertEqual(r["year"], "2020")

    def test_history_dates(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["history"].get("received"), "2023-10-01")
        self.assertEqual(r["history"].get("pubmed"), "2024-01-26")

    def test_language_and_country(self):
        r = self.by_id["12345678"]
        self.assertEqual(r["languages"], ["eng"])
        self.assertEqual(r["country"], "United States")

    def test_malformed_xml_raises_api_shape_error(self):
        """畸形 XML 必须抛 ApiShapeError，而不是静默返回空列表。

        静默返回 [] 时上层会认为"这一片确实没有文章"，
        于是一整批数据被安静丢弃，还标记为爬取成功。
        """
        with self.assertRaises(pc.ApiShapeError):
            pc.parse_pubmed_articles("<PubmedArticleSet><PubmedArticle>")

    def test_wrong_root_raises(self):
        with self.assertRaises(pc.ApiShapeError):
            pc.parse_pubmed_articles("<SomeOtherRoot><x/></SomeOtherRoot>")

    def test_empty_string_returns_empty_list(self):
        """空响应是"这批没有内容"，属于正常情况，不该抛异常。"""
        self.assertEqual(pc.parse_pubmed_articles(""), [])
        self.assertEqual(pc.parse_pubmed_articles("   \n  "), [])

    def test_single_article_root(self):
        """单篇时根节点可能是 PubmedArticle —— 也必须能解析。"""
        inner = PUBMED_XML.split("<PubmedArticle>")[1]
        single = "<PubmedArticle>" + inner.split("</PubmedArticle>")[0] + "</PubmedArticle>"
        recs = pc.parse_pubmed_articles(single)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["pmid"], "12345678")

    def test_article_without_pmid_skipped(self):
        """没有 PMID 的记录无法去重，必须跳过而不是产生空 ID 记录。"""
        xml = (
            "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
            "<Article><ArticleTitle>No PMID here</ArticleTitle></Article>"
            "</MedlineCitation></PubmedArticle></PubmedArticleSet>"
        )
        self.assertEqual(pc.parse_pubmed_articles(xml), [])


# ======================================================================================
# 日期辅助函数
# ======================================================================================


class TestParseUserDate(unittest.TestCase):
    """用户输入的日期容忍多种写法 —— 每种都是真实用户会敲出来的。"""

    def test_iso_dashed(self):
        self.assertEqual(pc.parse_user_date("2024-01-31"), _dt.date(2024, 1, 31))

    def test_slashed(self):
        self.assertEqual(pc.parse_user_date("2024/01/31"), _dt.date(2024, 1, 31))

    def test_chinese(self):
        self.assertEqual(pc.parse_user_date("2024年1月31日"), _dt.date(2024, 1, 31))

    def test_compact(self):
        self.assertEqual(pc.parse_user_date("20240131"), _dt.date(2024, 1, 31))

    def test_year_month_start_vs_end(self):
        """``2024-01``：start 补 1 号，end 补当月**最后一天**。

        用固定 31 会造出 2 月 31 日这种非法日期；
        用固定 28 又会漏掉 29/30/31 号的数据。
        """
        self.assertEqual(pc.parse_user_date("2024-01", "start"), _dt.date(2024, 1, 1))
        self.assertEqual(pc.parse_user_date("2024-01", "end"), _dt.date(2024, 1, 31))
        # 闰年二月必须补到 29
        self.assertEqual(pc.parse_user_date("2024-02", "end"), _dt.date(2024, 2, 29))
        self.assertEqual(pc.parse_user_date("2023-02", "end"), _dt.date(2023, 2, 28))

    def test_year_only_start_vs_end(self):
        self.assertEqual(pc.parse_user_date("2024", "start"), _dt.date(2024, 1, 1))
        self.assertEqual(pc.parse_user_date("2024", "end"), _dt.date(2024, 12, 31))

    def test_explicit_day_ignores_mode(self):
        """明确到了日，mode 就不该起作用。"""
        self.assertEqual(pc.parse_user_date("2024-03-15", "start"), _dt.date(2024, 3, 15))
        self.assertEqual(pc.parse_user_date("2024-03-15", "end"), _dt.date(2024, 3, 15))

    def test_datetime_and_date_objects(self):
        self.assertEqual(pc.parse_user_date(_dt.date(2024, 5, 1)), _dt.date(2024, 5, 1))
        self.assertEqual(
            pc.parse_user_date(_dt.datetime(2024, 5, 1, 10, 30)), _dt.date(2024, 5, 1)
        )

    def test_trailing_time_is_dropped(self):
        self.assertEqual(pc.parse_user_date("2024-01-31 12:00:00"), _dt.date(2024, 1, 31))

    def test_invalid_returns_none(self):
        """非法日期必须返回 None，绝不能抛异常 —— 用户手输的东西不可信。"""
        for bad in ("", None, "abc", "2024-13-01", "2024-02-30", "不是日期"):
            self.assertIsNone(pc.parse_user_date(bad), f"{bad!r} 应该返回 None")


class TestDateChunking(unittest.TestCase):
    def test_split_span(self):
        a, b = pc.split_span(_dt.date(2024, 1, 1), _dt.date(2024, 1, 10))
        self.assertEqual(a[0], _dt.date(2024, 1, 1))
        self.assertEqual(b[1], _dt.date(2024, 1, 10))
        # 两段必须不重不漏地覆盖原区间
        self.assertEqual(a[1] + _dt.timedelta(days=1), b[0])

    def test_split_span_single_day_returns_none(self):
        """只有一天时无法再分 —— 返回 None 让递归停下来。

        如果不返回 None，递归切分会无限进行下去（死循环 / 栈溢出）。
        """
        self.assertIsNone(pc.split_span(_dt.date(2024, 1, 1), _dt.date(2024, 1, 1)))
        self.assertIsNone(pc.split_span(_dt.date(2024, 1, 2), _dt.date(2024, 1, 1)))

    def test_year_chunks(self):
        chunks = pc.year_chunks(_dt.date(2022, 6, 1), _dt.date(2024, 3, 15))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], (_dt.date(2022, 6, 1), _dt.date(2022, 12, 31)))
        self.assertEqual(chunks[1], (_dt.date(2023, 1, 1), _dt.date(2023, 12, 31)))
        self.assertEqual(chunks[2], (_dt.date(2024, 1, 1), _dt.date(2024, 3, 15)))

    def test_year_chunks_covers_without_gaps(self):
        """切片必须严丝合缝：漏一天就永久丢那天的文献，且没有任何报错。"""
        start, end = _dt.date(2020, 3, 5), _dt.date(2023, 11, 20)
        chunks = pc.year_chunks(start, end)
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertEqual(prev[1] + _dt.timedelta(days=1), nxt[0])

    def test_year_chunks_single_year(self):
        start, end = _dt.date(2024, 2, 1), _dt.date(2024, 8, 1)
        self.assertEqual(pc.year_chunks(start, end), [(start, end)])

    def test_month_chunks(self):
        chunks = pc.month_chunks(_dt.date(2024, 1, 15), _dt.date(2024, 3, 10))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], (_dt.date(2024, 1, 15), _dt.date(2024, 1, 31)))
        self.assertEqual(chunks[1], (_dt.date(2024, 2, 1), _dt.date(2024, 2, 29)))
        self.assertEqual(chunks[2], (_dt.date(2024, 3, 1), _dt.date(2024, 3, 10)))

    def test_month_chunks_covers_without_gaps(self):
        start, end = _dt.date(2023, 11, 3), _dt.date(2024, 4, 17)
        chunks = pc.month_chunks(start, end)
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertEqual(prev[1] + _dt.timedelta(days=1), nxt[0])


class TestFormatRangeFor(unittest.TestCase):
    """⚠️ 本文件最重要的一组断言：日期格式的**故意不对称**。

    docs/DATA_SOURCE_NOTES.md 第 1.3 节实测：
      - 不良事件 receivedate、标签 effective_time  →  ``receivedate:[20200101+TO+20201231]``（纯数字）
      - 召回 report_date                            →  ``report_date:[2020-01-01+TO+2020-12-31]``（带短横线）

    如果"顺手统一"成一个格式，那么**一种端点的日期查询会全部返回 0 条**，
    而 openFDA 对此不会报错 —— 它只是查不到东西，表现为"这个时间段没有数据"。
    这种 bug 在数据量本来就少的时段极难察觉，是典型的静默数据缺失。
    """

    def test_receivedate_is_compact(self):
        got = pc.format_range_for(
            "receivedate", _dt.date(2024, 1, 1), _dt.date(2024, 12, 31)
        )
        self.assertEqual(got, "20240101+TO+20241231")
        self.assertNotIn("-", got, "不良事件必须用 YYYYMMDD，带短横线会查不到任何结果")

    def test_effective_time_is_compact(self):
        got = pc.format_range_for(
            "effective_time", _dt.date(2024, 1, 1), _dt.date(2024, 12, 31)
        )
        self.assertEqual(got, "20240101+TO+20241231")
        self.assertNotIn("-", got)

    def test_report_date_is_dashed(self):
        got = pc.format_range_for(
            "report_date", _dt.date(2024, 1, 1), _dt.date(2024, 12, 31)
        )
        self.assertEqual(got, "2024-01-01+TO+2024-12-31")

    def test_unknown_field_defaults_to_compact(self):
        self.assertEqual(
            pc.format_range_for("some_new_field", _dt.date(2024, 1, 1), _dt.date(2024, 1, 2)),
            "20240101+TO+20240102",
        )

    def test_config_table_matches_docs(self):
        """常量表本身也要锁住 —— 改错这张表 = 所有区间查询悄悄失效。"""
        self.assertEqual(pc.OPENFDA_DATE_FORMATS["receivedate"], "compact")
        self.assertEqual(pc.OPENFDA_DATE_FORMATS["effective_time"], "compact")
        self.assertEqual(pc.OPENFDA_DATE_FORMATS["report_date"], "dashed")

    def test_date_converters(self):
        d = _dt.date(2024, 3, 7)
        self.assertEqual(pc.date_to_compact(d), "20240307")
        self.assertEqual(pc.date_to_dashed(d), "2024-03-07")
        # PubMed mindate/maxdate 用斜杠 —— 第三种格式，同样不能统一
        self.assertEqual(pc.date_to_slash(d), "2024/03/07")


class TestDateRange(unittest.TestCase):
    def test_contains(self):
        r = pc.DateRange(_dt.date(2024, 1, 1), _dt.date(2024, 12, 31))
        self.assertTrue(r.contains(_dt.date(2024, 6, 1)))
        self.assertTrue(r.contains(_dt.date(2024, 1, 1)))   # 闭区间
        self.assertTrue(r.contains(_dt.date(2024, 12, 31)))
        self.assertFalse(r.contains(_dt.date(2023, 12, 31)))
        self.assertFalse(r.contains(None))

    def test_inactive_contains_everything(self):
        r = pc.DateRange()
        self.assertFalse(r.active())
        self.assertTrue(r.contains(_dt.date(1990, 1, 1)))
        self.assertEqual(r.describe(), "不限时间")

    def test_from_cfg(self):
        r = pc.DateRange.from_cfg({"date_from": "2024-01", "date_to": "2024"})
        self.assertEqual(r.start, _dt.date(2024, 1, 1))
        self.assertEqual(r.end, _dt.date(2024, 12, 31))


# ======================================================================================
# sanitize_component —— 文件名净化
# ======================================================================================


class TestSanitizeComponent(unittest.TestCase):
    """分类目录名直接来自上游数据（药品名/期刊名），必须净化。

    不净化的后果不是"报错"，而是 **崩溃或静默写错位置**：
      - 药品名里的 ``/`` 会凭空多出一层目录；
      - ``CON``/``LPT1`` 在 Windows 上根本无法创建，整个爬取中断；
      - 结尾的点/空格会让 Windows 悄悄改名，导致后续按名查找全部失败。
    """

    def test_illegal_windows_chars(self):
        got = pc.sanitize_component('a<b>c:d"e/f\\g|h?i*j')
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, got)
        self.assertEqual(got, "a_b_c_d_e_f_g_h_i_j")

    def test_control_chars(self):
        self.assertNotIn("\x00", pc.sanitize_component("a\x00b"))
        self.assertNotIn("\x1f", pc.sanitize_component("a\x1fb"))

    def test_reserved_device_names(self):
        """Windows 保留设备名 —— 不处理的话 create 目录直接失败。"""
        for name in ("CON", "PRN", "AUX", "NUL", "LPT1", "COM1", "lpt9", "con"):
            got = pc.sanitize_component(name)
            self.assertNotEqual(got.upper(), name.upper(), f"{name} 未被规避")
            self.assertTrue(got.startswith("_"), f"{name} 的规避方式应是加前缀: {got!r}")

    def test_trailing_dot_and_space_stripped(self):
        """Windows 会自动去掉结尾的点/空格，导致"写的名字"和"实际名字"不一致。"""
        self.assertEqual(pc.sanitize_component("aspirin..."), "aspirin")
        self.assertEqual(pc.sanitize_component("aspirin   "), "aspirin")
        self.assertEqual(pc.sanitize_component("aspirin . "), "aspirin")

    def test_empty_uses_fallback(self):
        self.assertEqual(pc.sanitize_component("", fallback="fb"), "fb")
        self.assertEqual(pc.sanitize_component("...", fallback="fb"), "fb")
        self.assertEqual(pc.sanitize_component(None, fallback="fb"), "fb")

    def test_empty_without_fallback(self):
        self.assertEqual(pc.sanitize_component(""), "unnamed")

    def test_max_len(self):
        got = pc.sanitize_component("x" * 200, max_len=20)
        self.assertEqual(len(got), 20)

    def test_max_len_does_not_leave_trailing_dot(self):
        got = pc.sanitize_component("a" * 19 + "." + "b" * 5, max_len=20)
        self.assertFalse(got.endswith("."))

    def test_normal_text_untouched(self):
        self.assertEqual(pc.sanitize_component("Ibuprofen Tablets 200mg"), "Ibuprofen Tablets 200mg")

    def test_cjk_preserved(self):
        self.assertEqual(pc.sanitize_component("阿司匹林"), "阿司匹林")


# ======================================================================================
# 显示宽度 / 数量级
# ======================================================================================


class TestDisplayHelpers(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(pc.format_size(0), "0 B")
        self.assertEqual(pc.format_size(512), "512 B")
        self.assertEqual(pc.format_size(1024), "1.0 KB")
        self.assertEqual(pc.format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(pc.format_size(1024 ** 3), "1.0 GB")

    def test_display_width_cjk(self):
        """中文字符占 2 列 —— 表格对齐全靠这个。"""
        self.assertEqual(pc.display_width("abc"), 3)
        self.assertEqual(pc.display_width("中文"), 4)
        self.assertEqual(pc.display_width("中a"), 3)

    def test_truncate_display_short_enough(self):
        self.assertEqual(pc.truncate_display("abc", 10), "abc")

    def test_truncate_display_ascii(self):
        got = pc.truncate_display("abcdefghij", 5)
        self.assertEqual(got, "abcd…")
        self.assertEqual(pc.display_width(got), 5)

    def test_truncate_display_never_splits_cjk(self):
        """⚠️ 绝不能把 CJK 字符截成半个。

        按"字符数"截断时，"阿司匹林"宽度 8，若按 5 个字符截就会得到
        宽度 5 的"半截"字符串 —— 在等宽终端里表格全部错位，
        在 GUI 里则可能显示成乱码方块。这里按**显示宽度**预算截断。
        """
        text = "阿司匹林肠溶片"
        got = pc.truncate_display(text, 7)
        # 截断点必须落在字符边界上：每个字符要么完整保留要么完全不要
        for ch in got.rstrip("…"):
            self.assertIn(ch, text)
        # 宽度不超预算，且没有半个字符
        self.assertLessEqual(pc.display_width(got), 7)
        self.assertTrue(got.endswith("…"))
        # 关键：不能出现宽度为奇数的 CJK 残留（那意味着切了半个字）
        body = got[:-1]
        self.assertEqual(pc.display_width(body) % 2, 0, f"CJK 被切成了半个: {got!r}")

    def test_truncate_display_exact_width(self):
        self.assertEqual(pc.truncate_display("abcd", 4), "abcd")

    def test_pad_display(self):
        self.assertEqual(pc.pad_display("中", 4), "中  ")
        self.assertEqual(pc.pad_display("中", 4, "right"), "  中")
        self.assertEqual(len(pc.pad_display("abcdef", 3)), 6)  # 超长不截断

    def test_magnitude_cn(self):
        self.assertEqual(pc.magnitude_cn(999), "999")
        self.assertEqual(pc.magnitude_cn(12345), "约 1.2 万")
        self.assertEqual(pc.magnitude_cn(123_456_789), "约 1.23 亿")
        self.assertEqual(pc.magnitude_cn(5, " 条"), "5 条")

    def test_magnitude_cn_bad_input(self):
        """统计数字来自上游，可能是字符串或缺失 —— 不能崩。"""
        self.assertEqual(pc.magnitude_cn("abc", " 条"), "— 条")
        self.assertEqual(pc.magnitude_cn(-1, " 条"), "— 条")

    def test_magnitude_bytes_and_seconds(self):
        self.assertEqual(pc.magnitude_bytes(2048), "2.0 KB")
        self.assertEqual(pc.magnitude_seconds(30), "30 秒")
        self.assertEqual(pc.magnitude_seconds(120), "2 分钟")
        self.assertEqual(pc.magnitude_seconds(7200), "2.0 小时")


# ======================================================================================
# 其他小工具
# ======================================================================================


class TestMiscHelpers(unittest.TestCase):
    def test_dedupe_keep_order(self):
        self.assertEqual(pc.dedupe_keep_order(["b", "a", "b", "c"]), ["b", "a", "c"])

    def test_parse_bool(self):
        for truthy in (True, "true", "1", "yes", "on", "Y", 1):
            self.assertTrue(pc.parse_bool(truthy), f"{truthy!r} 应为 True")
        for falsy in (False, "false", "0", "no", "off", "", None):
            self.assertFalse(pc.parse_bool(falsy), f"{falsy!r} 应为 False")

    def test_parse_bool_default(self):
        """None 时应返回 default —— 配置缺项靠它兜住。"""
        self.assertTrue(pc.parse_bool(None, True))
        self.assertFalse(pc.parse_bool(None, False))

    def test_sha1_short_stable(self):
        self.assertEqual(pc.sha1_short("abc"), pc.sha1_short("abc"))
        self.assertNotEqual(pc.sha1_short("abc"), pc.sha1_short("abd"))
        self.assertEqual(len(pc.sha1_short("abc")), 10)

    def test_jdump_compact(self):
        self.assertEqual(pc.jdump({"a": 1, "b": 2}), '{"a":1,"b":2}')

    def test_jdump_max_len(self):
        out = pc.jdump({"key": "x" * 100}, max_len=20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.endswith("…"))

    def test_jdump_non_serialisable(self):
        """不可序列化的对象必须回退到 str，不能抛异常。"""
        self.assertEqual(pc.jdump({1, 2}) != "", True)

    def test_safe_join(self):
        self.assertEqual(pc.safe_join("a", "b"), "a/b")
        self.assertEqual(pc.safe_join("a/", "/b"), "a/b")
        self.assertEqual(pc.safe_join("a", "", "b"), "a/b")

    def test_exception_hierarchy(self):
        """异常层次是上层错误处理的基础。

        EmptyResult 必须独立于其他错误 —— 上层靠它区分"查无结果"与"出错了"，
        这样才能对空结果不重试、对故障重试。如果 EmptyResult 变成 CrawlError
        之外的旁支，`except CrawlError` 就抓不到它，空结果会被当作未捕获异常。
        """
        for exc in (pc.HttpError, pc.RateLimited, pc.ApiShapeError, pc.EmptyResult):
            self.assertTrue(issubclass(exc, pc.CrawlError))
        self.assertNotIsInstance(pc.EmptyResult("x"), pc.ApiShapeError)

    def test_http_error_message(self):
        e = pc.HttpError(500, "https://example.com", b"boom")
        self.assertEqual(e.status, 500)
        self.assertIn("500", str(e))


class TestResolveOutputDir(unittest.TestCase):
    """output_dir 的解析规则。

    这里守的是一个真实踩过的坑：仓库里 config.json 曾写成作者机器上的绝对路径
    ``D:\\PharmaCrawler\\library``。别人克隆到别的盘/别的目录后，程序仍往
    ``D:\\PharmaCrawler\\library`` 写 —— 要么写到别人的项目里，要么因为目录不存在而报错。
    这就是"发布出去的包在作者机器上正常、在别人机器上不对"的典型成因。

    改成相对路径 ``library`` 后又暴露出第二个坑：相对路径若按**当前工作目录**解析，
    数据落在哪儿就取决于用户从哪敲的命令。双击 gui.bat 时 .bat 里的
    ``cd /d "%~dp0"`` 恰好把工作目录设成程序目录，看起来正常；
    但用 ``python D:\\path\\to\\pharma_crawler.py``、桌面快捷方式或计划任务启动时，
    工作目录是别处，数据就悄悄写到那个别处去了。

    正确规则：相对路径相对**程序所在目录**解析。
    """

    def setUp(self):
        self.program_dir = Path(r"D:\SomePortableFolder\PharmaCrawler")

    def test_relative_resolves_against_program_dir_not_cwd(self):
        """核心断言：相对路径拼到程序目录下，与当前工作目录无关。"""
        got = pc.resolve_output_dir("library", self.program_dir)
        self.assertEqual(got, self.program_dir / "library")
        self.assertTrue(got.is_absolute())

    def test_relative_with_subdir(self):
        self.assertEqual(
            pc.resolve_output_dir("data/out", self.program_dir),
            self.program_dir / "data" / "out",
        )

    def test_absolute_is_respected(self):
        """绝对路径是用户明确指定，必须原样保留 —— 不能"好心"拼到程序目录下。"""
        self.assertEqual(
            pc.resolve_output_dir(r"E:\PharmaData", self.program_dir),
            Path(r"E:\PharmaData"),
        )

    def test_empty_falls_back_to_library(self):
        for empty in ("", "   ", None):
            self.assertEqual(
                pc.resolve_output_dir(empty, self.program_dir),
                self.program_dir / "library",
            )

    def test_result_is_independent_of_cwd(self):
        """切换工作目录，解析结果必须完全不变。"""
        import os
        import tempfile

        before = pc.resolve_output_dir("library", self.program_dir)
        original = os.getcwd()
        # 必须切到一个**真实存在**的目录：program_dir 是虚构路径，不能 chdir 进去
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                after = pc.resolve_output_dir("library", self.program_dir)
            finally:
                os.chdir(original)
        self.assertEqual(before, after)

    def test_shipped_config_is_portable(self):
        """仓库里自带的 config.json 必须是相对路径。

        这条直接检查发布产物本身：只要有人把 output_dir 改回绝对路径，
        测试立刻失败 —— 这正是我们希望被拦住的回归。
        """
        import json

        cfg_path = Path(pc.__file__).resolve().parent / "config.json"
        if not cfg_path.exists():
            self.skipTest("找不到 config.json（可能未随包分发）")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        out = str(data.get("output_dir") or "")
        self.assertTrue(out, "config.json 必须有 output_dir")
        self.assertFalse(
            Path(out).is_absolute(),
            f"config.json 的 output_dir 是绝对路径 {out!r}；"
            f"这会让别人克隆到其他路径后写错位置，应改为相对路径如 'library'",
        )


if __name__ == "__main__":
    unittest.main()
