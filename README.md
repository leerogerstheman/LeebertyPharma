# PharmaCrawler — FDA 药品数据 + PubMed 药学文献爬虫

> **纯 Python 标准库实现。** 输入检索式 → 抓取 openFDA 药品数据与 PubMed 文献 →
> 按物质名/期刊/检索词建立**硬链接分类**（不占额外空间）→ 生成 CSV / Excel / SQLite /
> Markdown / RIS / BibTeX / MEDLINE 索引 → 多维检索 + Material Design 3 图形界面。

界面遵循 [Material Design 3](https://m3.material.io/) 规范：基线配色方案（亮/暗双主题）、
圆角刻度、状态层（悬停 8% / 按下 10% 的精确叠加）、色调表面容器表达层级。

---

## 快速上手

**Windows（推荐，无需装 Python）**：解压后双击 `gui.bat`。

**网页版**：双击 `web.bat`（或 `python webui.py`），界面在浏览器里打开。

与 `gui.bat` 的区别是**它不需要 tkinter**——界面由标准库 `http.server` 提供。有些环境里
自带 Python 没编进 tkinter，`gui.bat` 会退回命令行模式，而网页版照常可用。后端完全是
同一套 `pharma_crawler.py`，检索结果、统计数字、导出文件与桌面版一致。

```powershell
python webui.py                     # 默认端口 8758，自动打开浏览器
python webui.py --port 9000 --no-browser
python webui.py --out D:\另一份数据   # 换数据目录（与命令行版 --out 同义）
```

只监听 `127.0.0.1`，不对外网开放。

**已装 Python**：`git clone` 本仓库后双击 `gui.bat`，或命令行：

```powershell
python pharma_crawler.py selftest                              # ① 自检（推荐先跑）
python pharma_crawler.py crawl --dataset label --search 'openfda.brand_name:"aspirin"'
python pharma_crawler.py crawl --term 'metformin[Title/Abstract] AND 2024:2024[PDAT]'
python pharma_crawler.py search metformin --scope abstract      # ④ 检索已下载的数据
python pharma_crawler.py gui                                    # 图形界面
```

**先申请 API key**（免费，配额差 120 倍）：

```powershell
python pharma_crawler.py auth set --openfda-key 你的KEY --pubmed-key 你的KEY --pubmed-email 你的邮箱
```

| | 无 key | 有 key |
| --- | --- | --- |
| openFDA | 240 次/分钟、**1,000 次/天** | 240 次/分钟、**120,000 次/天** |
| PubMed | **3 请求/秒** | 10 请求/秒 |

---

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **七大 FDA 数据源** | 说明书标签 / 药品召回 / 不良事件 / NDC 目录 / drugs@FDA 批准信息 / 药品短缺 / 橙皮书 |
| **PubMed 文献** | E-utilities 全流程：检索 → History server → EFetch 批量取全文元数据 |
| **联合爬取** | 一条命令同时抓 FDA 与 PubMed（"这药批了什么" + "别人研究了什么"） |
| **突破上限** | FDA 超 26,000 条自动切 `search_after` 游标；PubMed 超 9,999 条自动按**年→月→日**递归切分 |
| **进度与状态** | 进度条 + 实时日志 + **安全停止**（下次续跑）+ 段级断点续爬 |
| **多维检索** | 关键词（5 种范围）/ 来源 / 物质名 / 期刊 / 年份 / MeSH / 出版类型 / 摘要 / DOI |
| **七种导出** | CSV · 多工作表 Excel · SQLite（带索引） · Markdown · RIS · BibTeX · MEDLINE |
| **文献工具链** | RIS 可直接拖进 EndNote / Zotero / NoteExpress；BibTeX 可直接 `\cite` |
| **界面** | Material Design 3：亮/暗主题、胶囊按钮、卡片、纸片筛选、圆角对话框 |

**图形界面 4 个标签页**：爬取 / 数据检索 / 统计 / 设置。

### 界面截图

![图形界面](docs/gui_screenshot.png)

---

## ⚠️ 关于数据上限：三个必须知道的实测事实

这些数字**与官方文档不符**，均由本项目实测核对（详见 [`docs/DATA_SOURCE_NOTES.md`](docs/DATA_SOURCE_NOTES.md)）：

### 1. PubMed 的 `retmax` 上限是 **9,999**，不是 10,000

官方文档 NBK25499 至今仍写 "up to a maximum of 10,000 records"，但代码实际把 `retmax` 硬截断到 **9999**，
`retstart` 超过 **9998** 直接报错：

```
esearch.fcgi?db=pubmed&term=cancer&retmax=100000
→ {"count":"5714266","retmax":"9999",...}        ← 静默截断，不报错

esearch.fcgi?db=pubmed&term=cancer&retstart=10000
→ ERROR: 'retstart' cannot be larger than 9998   ← 显式报错
```

> **若按 10,000 切分，每片会静默少 1 条。** 这种 bug 能在生产环境潜伏数月。
> 本程序按 **9,999** 切分，并对 `retstart` 超限做了前置拦截。

### 2. openFDA 用 **HTTP 404** 表示"查无结果"

```
search=openfda.brand_name:"zzzznotadrug" → HTTP 404 {"error":{"code":"NOT_FOUND"}}
```

> **这不是故障。** 把 404 当错误重试的爬虫会在空查询上死循环、白白烧掉配额。
> 本程序把 404 明确识别为空结果（自检里专门验证这一条）。

### 3. openFDA 的 `openfda.*` 字段**永远是数组**

即使只有一个元素也是 `["TYLENOL"]` 而不是 `"TYLENOL"`。

> 直接索引会得到 `['TYLENOL']` 这种脏数据写进 CSV。本程序统一用 `first_of()` 解包。

### 4. 各端点的字段前缀不统一

`openfda.generic_name` 在 `label`/`event` 里有效，但在 **`ndc`/`shortages` 里查不到** ——
那两个端点的 `generic_name` 在**顶层**；`orangebook` 更是**完全没有 `openfda` 对象**。

> 写错字段名时查询语法完全合法，只会安静返回 0 条，用户根本无从判断原因。
> 本程序在命中 0 条时会自动去掉 `openfda.` 前缀重试，并把正确写法提示出来。

**其他实测要点**：

- openFDA `limit` 上限 **1000**、`skip` 上限 **25000**（可达窗口 26,000，不是"无上限"）。
- 端点名是 **`/drug/shortages.json`（复数）**，`/drug/shortage.json` 不存在。
- `count` 查询返回的 `meta` **没有** `results` 子对象 —— 解析时不能假设它存在。
- 召回日期用 `YYYY-MM-DD`，不良事件/标签用 `YYYYMMDD` —— **格式不统一，不能一概而论**。
- NCBI 的超限响应 `{"error":"API rate limit exceeded"}` **可能带 2xx 状态码回来**，
  **必须检查响应体**，只看 HTTP 状态会漏判、进而静默少收数据。
- openFDA 对药品类端点**没有任何打包下载**（`download.json` 里药品条目为零），只能爬 API。

---

## 常用命令速查

```powershell
# ---- FDA ----
python pharma_crawler.py crawl --dataset label --search 'openfda.generic_name:"metformin"'
python pharma_crawler.py crawl --dataset enforcement --search 'classification:"Class I"'
python pharma_crawler.py crawl --dataset event --search 'receivedate:[20230101+TO+20231231]'
python pharma_crawler.py crawl --dataset ndc --search 'generic_name:metformin'
python pharma_crawler.py crawl --all-datasets --search 'openfda.brand_name:"aspirin"'

# ---- PubMed ----
python pharma_crawler.py crawl --term 'aspirin[Title/Abstract]'
python pharma_crawler.py crawl --term '"Nature"[Journal] AND review[pt]' --date-from 2020-01-01
python pharma_crawler.py crawl --term 'metformin[Title/Abstract]' --reldate 30
python pharma_crawler.py crawl --term 'drug[AFFL]' --segment year

# ---- 联合爬取（药学研究的典型需求）----
python pharma_crawler.py crawl `
    --dataset drugsfda --search 'products.active_ingredients.name:metformin' `
    --term 'metformin[Title/Abstract] AND 2024:2024[PDAT]'

# ---- 先探量再决定 ----
python pharma_crawler.py crawl --dataset event --search 'receivedate:[20230101+TO+20231231]' --dry-run

# ---- 检索与维护 ----
python pharma_crawler.py search 阿司匹林 --scope all --show-abstract
python pharma_crawler.py search --mesh "Diabetes Mellitus, Type 2" --year 2023,2024
python pharma_crawler.py search metformin --has-doi --export 结果.xlsx
python pharma_crawler.py stats
python pharma_crawler.py reindex                    # 重建全部索引与导出
python pharma_crawler.py reset --what segments      # 清空断点状态（不清数据）
python pharma_crawler.py selftest                   # 自检
```

完整参数：`python pharma_crawler.py crawl --help`

---

## 数据目录结构

```
library/
  _records/records.jsonl     原始记录（每行一条，可 diff、可增量追加）
  _index/
    index.csv                全量 CSV
    catalog.xlsx             多工作表 Excel（全部 / 按来源 / 按数据集 / 统计）
    catalog.sqlite           带索引的 SQLite
    index.md                 Markdown 索引
    queries.csv              检索历史
  _export/
    pubmed.ris               文献导出（拖进 EndNote / Zotero 即用）
    pubmed.medline           MEDLINE 格式
    pubmed.bib               BibTeX（需在 config 里开 export_bibtex）
  _state/                    断点续爬状态（session / segments / pubmed_ids）
  by_substance/              按物质名的硬链接分类
  by_journal/                按期刊的硬链接分类
  by_query/                  按检索词的硬链接分类
```

原件永远只存一份（`_records`），分类目录里是**硬链接**，同一磁盘不额外占空间。

---

## 配置

`config.json` 内每项都带中文说明。常用项：

| 项 | 说明 |
| --- | --- |
| `openfda_api_key` / `pubmed_api_key` | API key（也可用 `auth set` 写到用户目录） |
| `requests_per_second` | 请求速率。**不要盲目调高**，上游会封 IP |
| `date_from` / `date_to` | 时间范围 |
| `segment_strategy` | `auto` / `year` / `month` / `none` |
| `export_*` | 各导出格式开关 |
| `gui_dark` | 启动时用深色主题 |
| `endpoints` | **上游改版时覆盖接口地址，无需改代码** |

### 关于 `output_dir`：整个文件夹可以随便搬

默认值是**相对路径** `library`，它相对**程序所在目录**解析（不是相对当前工作目录）。
所以你可以把整个 `PharmaCrawler` 文件夹挪到任何盘、任何路径，甚至直接拷给别人，
数据始终待在程序旁边，不会跑到别处去。

> ⚠️ 不要把它改成作者机器上的绝对路径。曾经这里写死过 `D:\PharmaCrawler\library`，
> 结果别人克隆到其他路径后，程序仍往那个位置写 —— 要么污染别人的项目，要么因目录不存在而报错。
> 这正是"在自己机器上一切正常、发出去就不对"的典型成因。
> 只有确实要固定到某处时才写绝对路径（如 `E:\PharmaData`）。
> `selftest` 会检查这一点，并顺带打印数据目录的**实际绝对路径**。

凭据优先级：`config.json` < 用户凭据库 `~/.pharma_crawler/credentials.json` < 环境变量 < 命令行。

**凭据等价于账号配额**：`config.json` 与 `credentials.json` 请勿外传或提交（已 gitignore）。

---

## 项目结构

```
pharma_crawler.py     核心：HTTP 客户端 · API 客户端 · 数据仓库 · 爬取引擎 · CLI
pharma_gui.py         图形界面（Material Design 3）
m3_theme.py           M3 设计令牌与配色运算
config.json           配置
docs/
  DATA_SOURCE_NOTES.md   ★ 接口实测事实（改版时先看这个）
  MATERIAL3_NOTES.md     M3 规范取值来源与 tkinter 落地要点
test_*.py             测试（375 项，全部离线）
*.bat                 启动器
```

---

## 设计取舍

**为什么坚持纯标准库？** 双击即用，不需要 `pip install`，打包体积小，
也不会因为某个依赖库停止维护而烂掉。代价是 Excel 导出、HTTP 客户端这些要自己写 ——
但它们各自只有一两百行，值得。

**为什么接口结构要做校验？** 上游改版是本项目最大的持续性风险。
没有校验的爬虫在接口变化后会"成功爬到 0 条"，然后安静地写一份空索引 ——
这比直接报错糟糕得多。本程序宁可大声失败。

**为什么日志里强调那些数字？** 因为它们与官方文档不符，而文档错了会让人
写出"看起来对、实际静默丢数据"的代码。这些结论都是实测得来，理由写在代码注释里。

---

## 已知限制

1. **紫皮书无打包下载**：FDA 只提供网页检索 + 一份 PDF，无法爬取。这是真实缺口，不假装支持。
2. **PMC 全文**：旧 `oa.fcgi` 接口已于 2026-08 下线，FTP 打包文件也被移除，
   现改用 S3 桶 `pmc-oa-opendata`。全文抓取默认关闭。
3. **不良事件数据量大**：FAERS 是百万级，建议先用 `--dry-run` 探量。
4. **PubMed 摘要可能受版权保护**，超合理使用范围的再分发需权利人许可。
5. **数据未经 FDA 校验，不得用于医疗决策。**
6. 超大规模语料（>10 万条），NCBI 官方建议改用本地 PubMed baseline 而非爬取。
7. Windows 完整支持；Linux/macOS 可用命令行模式。

---

## 测试

```powershell
python -m unittest discover -p "test_*.py"
```

375 项测试，**全部离线**（网络层被 mock），覆盖数据解析、API 客户端语义、
数据仓库与导出、断点续爬状态机。其中专门验证了几个反直觉的行为：
404 空结果语义、`retmax` 9999 截断、`openfda` 数组解包、日期格式不对称。

---

## 许可

**代码**以 MIT 许可发布，见 [LICENSE](LICENSE)。

**所抓取的数据**版权归各上游来源所有，MIT 许可**不覆盖**这些数据 ——
使用须遵守各自的条款，详见 [DATA_SOURCES_LICENSE.md](DATA_SOURCES_LICENSE.md)。

要点：

- openFDA 数据为公有领域（CC0 1.0）。建议署名：
  `Data provided by the U.S. Food and Drug Administration (https://open.fda.gov)`
- NLM 免责声明与版权声明须对使用者可见；不得使用 PMC/DailyMed 标识；不得暗示 NLM/NIH 背书。
- ⚠️ 遵守上游条款是**使用者自身的责任**。程序内置限流与退避机制以帮助合规，
  但不能替代你对条款的阅读。
