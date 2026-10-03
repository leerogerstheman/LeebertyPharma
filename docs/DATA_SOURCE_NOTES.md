# 药学数据源接口说明（实测核对）

> 本文件记录 PharmaCrawler 依赖的**外部接口事实**。所有数字与行为都经过实测核对
> （官方文档与实测冲突时以实测为准，冲突处已标注）。
>
> 维护提示：上游一旦改版，先改 `config.json` 的 `endpoints` 覆盖项，再考虑改代码。

---

## 一、openFDA（美国 FDA 开放数据）

### 1.1 端点

| 名称 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| 药品说明书标签 | `/drug/label.json` | 可选 key | SPL 标签，2009 至今，**每周**更新 |
| 药品召回 | `/drug/enforcement.json` | 可选 key | 每周更新 |
| 不良事件 | `/drug/event.json` | 可选 key | FAERS，**百万级**，最大数据源 |
| NDC 目录 | `/drug/ndc.json` | 可选 key | **每日**更新 |
| drugs@FDA 批准信息 | `/drug/drugsfda.json` | 可选 key | 周一至周五每日更新，1939 至今 |
| **药品短缺** | `/drug/shortages.json` | 可选 key | ⚠️ **复数**，`/drug/shortage.json` 不存在 |
| 橙皮书 | `/drug/orangebook.json` | 可选 key | 每月更新，4.8 万条 |

- 基址 `https://api.fda.gov`，**仅 HTTPS**（HTTP 被拒）。
- 全部为 `GET`。
- ⚠️ **openFDA 对药品类端点没有任何打包下载**：`api.fda.gov/download.json` 里只有
  food / device / animalveterinary / tobacco / other / research / transparency，**药品条目为零**。
  所以只能走 API 爬取，没有批量捷径。

### 1.2 速率限制（精确）

| 条件 | 每分钟 | 每天 |
| --- | --- | --- |
| 无 API key | 240 / IP | **1,000 / IP** |
| 有 API key | 240 / key | **120,000 / key** |

- key 以查询参数传递，**必须放在第一位**：`?api_key=KEY&search=...`
- 也可用 `Authorization: Basic base64(key + ":")`。
- ⚠️ 未鉴权的 **1,000 次/天是硬约束**，正经爬取必须申请 key。
  免费申请：https://open.fda.gov/apis/authentication/

### 1.3 分页上限（实测）

| 参数 | 上限 | 实测证据 |
| --- | --- | --- |
| `limit` | **1000** | `limit=1001` → 400 `"Limit cannot exceed 1000 results for search requests."` |
| `skip` | **25000** | `skip=25001` → 400 `"Skip value must 25000 or less."` |
| `count` | 默认返回最高频的 1000 个值 | 文档 |

- 可达窗口 = `limit + skip` ≤ 26,000（文档所谓 "26,000" 是这个派生值，不是 cap）。
- ⚠️ `skip` 与 `search_after` **互斥**，绝不能同时传。

**突破上限的两条路：**

1. **`search_after` 游标（首选）**：响应头带
   `Link: <...&search_after=0%3D0000025c-...>; rel="next"`，
   首次查询**不带 skip**、带 `sort=field:asc`，然后一直跟随 `rel="next"` 直到该头消失，
   可滚动任意大小的结果集。⚠️ 游标 token 内嵌排序键，所以 **`sort` 字段必须唯一且稳定**
   （如 `receivedate` + `safetyreportid` 做 tiebreaker）。
2. **日期区间切分（回退）**：
   - 不良事件 `search=receivedate:[20200101+TO+20201231]`
   - 召回 `search=report_date:[2020-01-01+TO+2020-12-31]`（⚠️ **召回用短横线**，不良事件用纯数字）
   若某区间 `meta.results.total` > 26,000，继续二分。

### 1.4 查询语法

参数只有五个：`search`、`sort`、`count`、`limit`、`skip`。

- `search=field:term` — 单字段
- `search=a:x+AND+b:y` — **AND 必须是大写且带 `+`**
- `search=a:x+b:y` — 空格 = **OR**
- `sort=receivedate:desc` / `:asc`
- `count=field.exact` — **`.exact` 后缀按整句统计**而非分词，统计反应/药名时必须加
- 区间：`field:[a+TO+b]` 闭区间，`{}` 开区间
- 通配符 `*`；短语用 `%22` 包裹
- 不指定字段时搜索**所有字段**

### 1.5 `openfda` 嵌套对象

label / event / ndc / shortages / enforcement 记录里都有，实测键：
`application_number`、`brand_name`、`generic_name`、`manufacturer_name`、`product_ndc`、
`package_ndc`、`product_type`、`route`、`substance_name`、`rxcui`、`spl_id`、`spl_set_id`、
`is_original_packager`、`unii`、`nui`、`pharm_class_epc`、`pharm_class_cs`、`pharm_class_pe`、`pharm_class_moa`

⚠️ **所有值都是数组**（哪怕只有一个元素），取值一律 `[0]` 并做防御。

### 1.6 响应结构与错误

- 响应形如 `{"meta": {...}, "results": [...]}`。
- `meta` 含 `disclaimer`、`terms`、`license`、`last_updated`、`results.{skip,limit,total}`。
  ⚠️ **`count` 查询返回的 `meta` 没有 `results` 子对象**，解析时不能假设它存在。
- 错误对象：`{"error": {"code": "...", "message": "..."}}`
- ⚠️ **404 不一定是故障**：查无结果时返回
  **HTTP 404** `{"error":{"code":"NOT_FOUND","message":"No matches found!"}}`。
  **必须把 404 当作"空结果集"处理，绝不能重试** —— 否则空查询会无限重试。
  400 `BAD_REQUEST` 是 limit/skip 违规。
- CORS 全开（`Access-Control-Allow-Origin: *`）；无 UA 硬性要求，但限流按 IP，仍应设置可识别 UA。

### 1.7 使用条款

- 数据为**公有领域 CC0 1.0**，可商用，无需授权。
- 建议（非强制）署名：`Data provided by the U.S. Food and Drug Administration (https://open.fda.gov)`
- FDA 保留对**疑似绕过限流**者临时或永久封禁的权利，会监控用量。
- 数据**未经校验**，不得用于医疗决策。
- 更新频率差异大（周/日/月），应据此缓存，不要对日更数据一天之内反复爬。

### 1.8 常用字段

> ⚠️ **字段前缀在各端点之间不统一**（实测确认），这是最容易踩的坑：
> 写错字段名时查询语法完全合法，只会安静地返回 0 条，无从判断原因。
>
> | 端点 | `generic_name` 的位置 |
> | --- | --- |
> | `label` | `openfda.generic_name`（嵌套对象里） |
> | `event` | `openfda.generic_name` |
> | `enforcement` | `openfda.generic_name`，但也有顶层 `product_description` |
> | **`ndc`** | **顶层 `generic_name`**（`openfda.generic_name` 查不到） |
> | **`shortages`** | **顶层 `generic_name`** |
> | **`drugsfda`** | 顶层 `sponsor_name` + `products[].active_ingredients` |
> | **`orangebook`** | **完全没有 `openfda` 对象**，只有 `products[]` / `patents[]` / `approval_date` |
>
> 程序在命中 0 条时会自动去掉 `openfda.` 前缀重试，若去掉后能查到就提示正确写法。
> 实测：`ndc` 用 `openfda.generic_name:metformin` → 0 条；改用 `generic_name:metformin` → 741 条。

- 标签：`openfda.*`、`effective_time`(YYYYMMDD)、`set_id`、`id`、`version`
- 不良事件：`receivedate`、`safetyreportid`、`serious`、`patient.reaction.reactionmeddrapt`
- 召回：`report_date`、`classification`、`recalling_firm`、`product_description`、`reason_for_recall`
- NDC：`product_ndc`、`brand_name`、`generic_name`、`labeler_name`、`dosage_form`、`active_ingredients`
- drugs@FDA：`application_number`、`sponsor_name`、`products[].active_ingredients`
- 短缺：`package_ndc`、`generic_name`、`company_name`、`status`、`therapeutic_category`、`availability`
- 橙皮书：`application_number`、`product_number`、`approval_date`、`patents[]`、`products[].active_ingredients`

---

## 二、DailyMed（NLM）

- 基址 `https://dailymed.nlm.nih.gov/dailymed/services/`，**必须带版本段**，当前为 `v2`。
- 格式由扩展名决定：`.json` 或 `.xml`。**仅 GET，无需 API key。**
- 资源：`/applicationnumbers`、`/drugclasses`、`/drugnames`、`/ndcs`、`/rxcuis`、`/spls`、
  `/spls/{SETID}`、`/spls/{SETID}/history`、`/spls/{SETID}/media`、`/spls/{SETID}/ndcs`、
  `/spls/{SETID}/packaging`、`/uniis`
- **分页：`pagesize` 上限 100（默认 100），`page` 从 1 开始。**
  元数据返回 `total_elements`、`total_pages`、`next_page_url` —— 比 openFDA 友好得多。
- `/spls` 常用过滤：`drug_name`、`name_type`(g/b/both)、`ndc`、`rxcui`、`setid`、`labeler`、
  `manufacturer`、`boxed_warning`、`dea_schedule_code`、
  `published_date` + `published_date_comparison`(`lt/lte/gt/gte/eq`) —— 可做日期切分。
- 下载：`downloadzipfile.cfm?setId={setid}`、`downloadpdffile.cfm?setId={setid}`、
  `getFile.cfm?type=zip&setid={id}&version={n}`（后两者返回 `X-DAILYMED-LABEL-LAST-UPDATED` 头）。
- **无公开速率限制**，但仍应串行 + 小幅延时，保持礼貌。

---

## 三、drugs@FDA / 橙皮书 打包文件

| 数据集 | 直链 | 实测 | 更新 |
| --- | --- | --- | --- |
| drugs@FDA | `https://www.fda.gov/media/89850/download?attachment` | zip，6,092,427 B，文件名 `dafdata20261002.zip` | 周一至周五每天上午 |
| 橙皮书 | `https://www.fda.gov/media/76860/download?attachment` | zip，1,097,768 B，文件名 `EOBZIP_2026_08.zip` | 每月 |

- drugs@FDA 内含 **12 个 tab 分隔 `.txt`**：`ActionTypes_Lookup.txt`、`ApplicationDocs.txt`、
  `Applications.txt`、`ApplicationsDocsType_Lookup.txt`、`Join_Submission_ActionTypes_Lookup.txt`、
  `MarketingStatus.txt`、`MarketingStatus_Lookup.txt`、`Products.txt`、`SubmissionClass_Lookup.txt`、
  `SubmissionPropertyType.txt`、`Submissions.txt`、`TE.txt`
- 橙皮书内含 **3 个文件**，⚠️ **波浪号 `~` 分隔，不是 tab**：
  `products.txt`(7.5MB)、`patent.txt`(1.2MB)、`exclusivity.txt`(77KB)
- 橙皮书更推荐直接用 JSON API `/drug/orangebook.json`（48,761 条，每月更新），省去解包解析。
- **紫皮书无任何打包下载**，仅网页检索 + 一份 PDF 说明页（`https://www.fda.gov/media/182175/download`）。
  这是本项目中唯一无法爬取的来源，属真实缺口，不假装支持。

---

## 四、PubMed / NCBI E-utilities

### 4.1 端点

基址 **`https://eutils.ncbi.nlm.nih.gov/entrez/eutils/`**

| 工具 | 文件 | 关键参数 | 说明 |
| --- | --- | --- | --- |
| ESearch | `esearch.fcgi` | `db`,`term`,`retmode`,`retmax`,`retstart`,`usehistory`,`WebEnv`,`query_key`,`sort`,`field`,`datetype`,`mindate`,`maxdate`,`reldate`,`rettype` | 查询 → UID 列表；`rettype=count` 只取计数 |
| EFetch | `efetch.fcgi` | `db`,`id` 或 (`WebEnv`+`query_key`),`retmode`,`rettype`,`retstart`,`retmax` | 取完整记录，主力 |
| ESummary | `esummary.fcgi` | `db`,`id`,`retmode` | DocSum，JSON 干净稳定 |
| ELink | `elink.fcgi` | `dbfrom`,`db`,`id`,`cmd`,`linkname` | `pubmed_pmc` 可做 PMID→PMCID |
| EInfo | `einfo.fcgi` | `db` | 字段元数据，可编程枚举字段 |
| EPost | `epost.fcgi` | `db`,`id` | UID 列表 → WebEnv/query_key |
| ECitMatch | `ecitmatch.cgi` | `db=pubmed`,`rettype=xml`,`bdata` | ⚠️ 是 **`.cgi`** 不是 `.fcgi`；引文串 → PMID |

### 4.2 ⚠️ 数字上限（本文件最重要的一节）

| 项 | 文档说法 | **实测真相** | 证据 |
| --- | --- | --- | --- |
| `retmax` | 10000 | **9999（硬截断，不报错）** | `retmax=100000` 返回 `"retmax":"9999"` |
| `retstart` | 10000 | **9998（超了直接报错）** | `retstart=10000` → `"ERROR":"...'retstart' cannot be larger than 9998. For PubMed, ESearch can only retrieve the first 9,999 records..."` |

```
实测 1：
esearch.fcgi?db=pubmed&term=cancer&retmax=100000&retmode=json
→ {"count":"5714266","retmax":"9999","retstart":"0",...}      ← 静默截断

实测 2：
esearch.fcgi?db=pubmed&term=cancer&retstart=10000&retmax=10
→ ERROR: 'retstart' cannot be larger than 9998                ← 显式报错

实测 3：
esearch.fcgi?db=pubmed&term=...&retstart=9998&retmax=10
→ 只返回 1 条，warninglist: "Restrictions achieved. start and count adjusted to 9998, 1"
```

> **结论：分段单元是 9,999 条，不是 10,000。偏移量 0…9998。**
> 若按 10,000 切分，每片会**静默少 1 条** —— 这种 bug 会在生产环境潜伏数月。
> 官方文档 NBK25499 至今仍写 "up to a maximum of 10,000 records"，**以代码行为为准**。

### 4.3 History Server：突破 9,999 的正确姿势

1. **建集**：`esearch.fcgi?db=pubmed&term=…&usehistory=y&retmax=0`
   返回 `count`、`querykey`（如 `"1"`）、`webenv`（如 `"MCID_6ac11eeb0c30af7c880636e4"`）。
2. **取数**：`efetch.fcgi?db=pubmed&query_key=1&WebEnv=MCID_…&retstart=0&retmax=500&retmode=xml`
   已实测可按 WebEnv 正确分页取全文。

⚠️ **关键区别**：9,999/9998 的天花板管的是 **ESearch 的 ID 列举**，不是 **EFetch 的 WebEnv 迭代**。
所以可靠模式是：让**每个切片** < 9,999，存入 History，再用 EFetch 以 200–500 一批把它抽干。
多个集只有共享同一个 `WebEnv` 才能合并；后续调用必须带 `WebEnv=`，
用 `term=%23<key>+AND+…` 可对已有集再查询。

### 4.4 EFetch 输出格式

| `retmode` | `rettype` | 结果 | 实测 |
| --- | --- | --- | --- |
| `xml` | 默认 | 完整 PubmedArticleSet XML | ✅ |
| `xml` | `medline` | MedlineCitation 形式的 XML | ✅ |
| `text` | `medline` | MEDLINE 标签文本（`PMID-`/`TI  -`/`AB  -`/`MH  -`） | ✅ |
| `text` | `abstract` | 格式化引文 + 摘要，末尾带 DOI/PMCID/PMID | ✅ |

- `retmode=html` **已于 2012-02 移除**，传了会静默回落到 DB 默认值。
- `id` 支持逗号分隔**且不含空格**（`id=352,25125,234`）。

### 4.5 PubMed XML 需抽取字段

根 `PubmedArticleSet` → `PubmedArticle` → (`MedlineCitation`, `PubmedData`)。

| 目标 | 路径 | 备注 |
| --- | --- | --- |
| PMID | `MedlineCitation/PMID` | 带 `Version` 属性 |
| 标题 | `Article/ArticleTitle` | 可能含内联标记 |
| 摘要 | `Article/Abstract/AbstractText` | **可重复**；`@Label`(BACKGROUND/METHODS/…)、`@NlmCategory` |
| 作者 | `Article/AuthorList/Author` | `LastName`/`ForeName`/`Initials`；团体作者用 `CollectiveName` |
| 单位 | `Author/AffiliationInfo/Affiliation` | 可重复 |
| 期刊 | `Article/Journal/Title` + `ISOAbbreviation` | `ISSN/@IssnType`；`JournalIssue/Volume`、`Issue` |
| 日期 | `Article/Journal/JournalIssue/PubDate` | `Year`/`Month`/`Day`，**或**自由文本 `MedlineDate`（如 "2020 Jan-Feb"） |
| 页码 | `Article/Pagination/StartPage`\|`EndPage`\|`MedlinePgn` | |
| **DOI** | `Article/ELocationID[@EIdType='doi']` **和** `PubmedData/ArticleIdList/ArticleId[@IdType='doi']` | ⚠️ **两处独立**，都要读 |
| PMCID | `PubmedData/ArticleIdList/ArticleId[@IdType='pmc']` | 值形如 `PMC7745181`（带前缀） |
| MeSH | `MedlineCitation/MeshHeadingList/MeshHeading` | `DescriptorName(@UI,@MajorTopicYN)` + `QualifierName(@UI,@MajorTopicYN)` |
| 关键词 | `MedlineCitation/KeywordList[@Owner='NOTNLM']/Keyword` | |
| 出版类型 | `Article/PublicationTypeList/PublicationType` | 带 `@UI` |
| 历史日期 | `PubmedData/History/PubMedPubDate[@PubStatus=…]` | `pubmed`/`medline`/`entrez`/`pmc-release`/`received`/`revised`/`accepted` |

### 4.6 速率限制与礼仪

官方原文：

> "NCBI recommends that users post no more than three URL requests per second and limit
> large jobs to either weekends or between **9:00 PM and 5:00 AM Eastern time** during weekdays."

> "Without an API key, any site (IP address) posting more than 3 requests per second to the
> E-utilities will receive an error message. By including an API key, a site can post up to
> **10 requests per second** by default."

- 大批量窗口是 **ET 21:00–05:00**，不只是"晚上 9 点后"。
- 限流：无 key **3 rps**，有 key **10 rps**（>100 rps 仅限上述非工作时段）。
- ⚠️ 超限响应体为 `{"error":"API rate limit exceeded","count":"11"}`，
  **可能带 2xx 状态码返回**！所以**必须检查响应体**，不能只看 HTTP 状态。
  与 HTTP 429 同等对待：指数退避 + 抖动，仅重试幂等 GET。
- **无任何官方日配额**，不要臆造一个。
- API key：登录 NCBI → 用户名菜单 → Account settings → API Key Management → Create。
  以 `&api_key=ABCD123` 传递。**重新生成会使旧 key 立即失效。**
- `tool=` 与 `email=` 每请求非必须，但**合规必须**：只有把二者**注册**到
  `eutils@ncbi.nlm.nih.gov` 才能解封 IP。官方明说"仅在请求里提供 tool 和 email 是不够的"。
  仍应每次请求都带上。
- 参数除 `WebEnv` 外**一律小写**；空格用 `+`；`"`→`%22`；`#`→`%23`；长查询用 POST。

### 4.7 字段标签与日期格式

常用标签（经 `einfo.fcgi?db=pubmed` 核对）：
`[All Fields]`、`[Title]`、`[Title/Abstract]`、`[MeSH Terms]`、`[MeSH Major Topic]`、
`[MeSH Subheading]`、`[Author]`、`[Author - Corporate]`、`[Journal]`、`[Affiliation]`、
`[Publication Type]`、`[Date - Publication]`、`[Date - Entry]`、`[Date - Create]`、
`[Date - Completion]`、`[Date - Modification]`、`[Date - MeSH]`、`[Volume]`、`[Issue]`、
`[Pagination]`、`[Language]`、`[EC/RN Number]`、`[Pharmacological Action]`、`[Grants and Funding]`、
`[Substance Name]`、`[Text Word]`、`[UID]`

- 布尔运算符 **AND/OR/NOT 必须大写且两侧有空格**。
- 日期：`datetype=pdat|mdat|edat`，`mindate`/`maxdate` **必须成对出现**，
  格式 **`YYYY/MM/DD`（斜杠）**，也接受 `YYYY` 与 `YYYY/MM`。
- 实测 `"Nature"[Journal] AND 2023[PDAT] AND review[pt]` 会被翻译成
  `"Nature"[Journal] AND 2023/01/01:2023/12/31[Date - Publication] AND "review"[Publication Type]`。

### 4.8 ⚠️ PMC 开放获取：近期重大变更

- **`https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi` 已 404**（`PMC13900`、`PMC7745181`
  在 `www` 与 `pmc` 两个域名下均实测 404）。**不要基于它写代码。**
- **2026-08-24 那一周，NCBI 移除了 FTP 与旧 Cloud Service 上所有 PMC Article Dataset 文件**：
  `oa_package/*.tar.gz`、`oa_file_list.csv`、`oa_comm`/`oa_noncomm` 目录划分**全部不复存在**。
- 替代方案：**S3 桶 `pmc-oa-opendata`**，区域 `us-east-1`，全世界可读，无需账号：
  - HTTPS 基址 `https://pmc-oa-opendata.s3.amazonaws.com/`
  - 匿名 CLI 加 `--no-sign-request`
  - 结构说明见 `https://pmc-oa-opendata.s3.amazonaws.com/README.txt`
- **不再有 baseline / incremental 打包**。每条文章是独立对象，前缀为**逐版本**形式
  （如 `PMC12805672.1/`，内含 JATS `.xml`、`.txt`、`.json`，许可允许时还有 `.pdf`/媒体）。
  `oa_comm`/`oa_noncomm` 的许可划分已取消，改为**逐文章** `license_code`。
- S3 清单：`inventory-reports/pmc-oa-opendata/metadata/`，每日 CSV(.gz)，**仅保留 30 天**。
- 官方 eSearch-S3 流程：`esearch.fcgi?db=pmc&term=<q>+AND+(open_access[Filter]+OR+author_manuscript[Filter])`
  → idlist → 前缀 `PMC<id>.` → `list-objects-v2` → 下载。日期过滤用 `pmcrdat`。
- PMC 交叉引用文件仍在：`https://ftp.ncbi.nlm.nih.gov/pub/pmc/PMC-ids.csv.gz`
- 计量许可过滤器：`cc0_license`、`cc_by_license`、`cc_by-sa_license`、`cc_by-nd_license`、
  `cc_by-nc*_license`。

### 4.9 引文导出：实测可用的只有一种

| 格式 | 方式 | 状态 |
| --- | --- | --- |
| MEDLINE | `https://pubmed.ncbi.nlm.nih.gov/?term=<ids>&format=pubmed&size=10` | ✅ 实测可用 |
| MEDLINE/XML | `efetch.fcgi?db=pubmed&id=<ids>&rettype=medline&retmode=text\|xml` | ✅ 推荐 |
| RIS | PubMed 仅有网页端 `.nbib` 下载 | ⚠️ **无可用 `format=ris` 查询参数** |
| CSL JSON | — | ❌ 无公开端点，需自 ESummary JSON 或 EFetch XML 转换 |

⚠️ `format=ris`、`format=medline`、`format=csl` 一律返回
*"Invalid value for parameter format. Select a valid choice."*，**只有 `format=pubmed` 被接受**。

### 4.10 使用条款

- ⚠️ **"NCBI does not allow scripting against our web pages."**
  **只能走 E-utilities / FTP / S3，不得爬网页**，违者可能被封 IP。
- 注册 `tool` + `email`；携带可识别 User-Agent。
- NLM 免责声明与版权声明必须对产品使用者可见。
- ⚠️ **PubMed 摘要可能受版权保护**，超出合理使用范围的再分发需权利人许可。
- 超大规模语料，NCBI 官方建议改用**本地 PubMed baseline**
  （https://www.nlm.nih.gov/databases/download/pubmed_medline.html），可完全绕开 9,999 上限。
- PMC 条款：需致谢 NLM 为来源；**不得使用 PMC 商标/标识**；不得暗示 NLM/NIH 背书；
  只再分发许可允许的数据；数据过期需声明。

### 4.11 推荐分段策略

```
1. 探量   esearch(rettype=count) → N
2. 若 N ≤ 9999:
     esearch(usehistory=y, retmax=0) → WebEnv, query_key
     EFetch 循环 retstart = 0,500,1000… （批 200–500）
3. 若 N > 9999: 切片，不要硬翻页
     a. 日期切片 — datetype=pdat，按年取 mindate/maxdate，
        某片仍 > 9999 就递归到月、再到日
     b. 字段分区 — [PT]、[MH]、[Journal]、[Language]
     c. 入库日期切片 — EDAT/CRDT，适合增量爬取
     每个切片必须严格 < 9999
4. 每片：esearch(usehistory=y, retmax=0) → WebEnv+query_key → EFetch 分批
5. 按 PMID 去重（切片会重叠，用 seen 集合或数据库索引）
6. 限流 3 rps（无 key）/ 10 rps（有 key）；对
   {"error":"API rate limit exceeded"} 与 HTTP 429 退避
7. 按切片持久化"最后成功的 maxdate/EDAT" → 可断点续爬、可增量
8. PMC 全文：以 S3 清单 CSV 作 PMCID 来源（无 1 万上限），再走 eSearch-S3 流程
```

⚠️ PubMed 在持续增长，**按年切片不是长期稳定的**：
要把 **年 → 月 → 日** 的递归从第一天就内置，不要硬编码按年的常量。

---

## 五、来源

- openFDA：[API basics](https://open.fda.gov/apis/) · [Authentication](https://open.fda.gov/apis/authentication/) ·
  [Query params](https://open.fda.gov/apis/query-parameters/) · [Query syntax](https://open.fda.gov/apis/query-syntax/) ·
  [Paging](https://open.fda.gov/apis/paging/) · [Downloads](https://open.fda.gov/apis/downloads/) ·
  [Terms](https://open.fda.gov/terms/) · [Shortages](https://open.fda.gov/apis/drug/drugshortages/) ·
  [Orange Book](https://open.fda.gov/apis/drug/orangebook/)
- DailyMed：[Web services](https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm) ·
  [spls](https://dailymed.nlm.nih.gov/dailymed/webservices-help/v2/spls_api.cfm) ·
  [drugnames](https://dailymed.nlm.nih.gov/dailymed/webservices-help/v2/drugnames_api.cfm)
- FDA 打包文件：[drugs@FDA data files](https://www.fda.gov/drugs/drug-approvals-and-databases/drugsfda-data-files) ·
  [Orange Book data files](https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files)
- NCBI：[Usage policy NBK25497](https://www.ncbi.nlm.nih.gov/books/NBK25497/) ·
  [Parameters NBK25499](https://www.ncbi.nlm.nih.gov/books/NBK25499/) ·
  [API key KA-05317](https://support.nlm.nih.gov/kbArticle/?pn=KA-05317) ·
  [Bulk access KA-05510](https://support.nlm.nih.gov/kbArticle/?pn=KA-05510) ·
  [PMC FTP](https://pmc.ncbi.nlm.nih.gov/tools/ftp/) · [PMC on AWS](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/) ·
  [S3 README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt) ·
  [PubMed baseline](https://www.nlm.nih.gov/databases/download/pubmed_medline.html)
