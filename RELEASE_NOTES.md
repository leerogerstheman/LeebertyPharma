# 发布说明 v1.0.0

## 🎉 首个正式版本

**PharmaCrawler v1.0.0** —— FDA 药品数据 + PubMed 药学文献爬虫。
核心功能已完整可用，接口行为均经**实测核对**（不是照抄文档 —— 文档有多处是错的，见下）。

## 📦 下载与安装

**方式一：便携版（推荐，无需装 Python）**

下载 `PharmaCrawler-v1.0.0-portable-win64.zip` → **解压整个文件夹** → 双击 `START.bat`。

- 内置 Python 运行时，**不需要装 Python，也不需要 pip install**
- ⚠️ **请整个文件夹一起用**，不要把 `PharmaCrawler.exe` 单独拖出来 ——
  旁边的 `_internal\` 是程序的一部分，缺了就启动不了
- 数据存在文件夹内的 `library\`，整个文件夹可以随便挪到别的盘、别的路径
- 详细说明见包内 `使用说明.txt`

**方式二：从源码运行（跨平台）**

1. 下载本仓库源码（`Code` → `Download ZIP`，或 `git clone`）
2. 确认已装 **Python 3.9+**，且带 tkinter（官方安装包默认自带）
3. 双击 `gui.bat` 启动图形界面

> 纯标准库实现，**不需要 `pip install` 任何东西**。

**方式三：自己打包 exe**

```powershell
pip install pyinstaller
pyinstaller PharmaCrawler.spec
```

> 打包后记得把 `config.json` 复制到 exe 旁边，方便日后修改配置。

---

## ✅ 这个版本能做什么

**爬取**

- **七大 FDA 数据源**：说明书标签 / 药品召回 / 不良事件 / NDC 目录 / drugs@FDA 批准信息 / 药品短缺 / 橙皮书
- **PubMed 文献**：E-utilities 全流程（检索 → History server → EFetch 批量取元数据）
- **联合爬取**：一条命令同时抓 FDA 与 PubMed —— 药学研究的典型需求是"这药批了什么"加"别人研究了什么"
- 时间范围、每端点独立检索式、备注名、本次上限
- **干跑预估**：先看量级再决定下不下（`--dry-run`）
- **安全停止**：随时中断，已抓数据正常落盘，下次从断点续跑

**突破上游上限（本项目的核心难点）**

- FDA 超 26,000 条 → 自动切 `search_after` 游标滚动，可无限取
- PubMed 超 9,999 条 → 自动按 **年 → 月 → 日** 递归切分（实测 61,502 条命中切成 227 片）
- 段级断点续爬：已完成的时间片再次运行直接跳过，不重复请求

**数据管理**

- 7 种导出：CSV · 多工作表 Excel · SQLite（带索引） · Markdown · RIS · BibTeX · MEDLINE
- 硬链接分类目录（按物质名/期刊/检索词），同一磁盘**不额外占空间**
- 多维检索：关键词（5 种范围）/ 来源 / 物质名 / 期刊 / 年份 / MeSH / 出版类型 / 摘要 / DOI
- RIS 可直接拖进 EndNote / Zotero / NoteExpress；BibTeX 可直接 `\cite`

**图形界面（Material Design 3）**

- 4 个标签页：爬取 / 数据检索 / 统计 / 设置
- 亮色 / 暗色双主题，采用 M3 基线配色方案
- 胶囊按钮、卡片、纸片筛选、圆角对话框、线性进度条
- 状态层按 M3 精确比例实现（悬停 8%、按下 10% 的**叠加**，而非换色）

**可靠性**

- 接口结构校验 —— 上游改版时**大声报错**，绝不静默假成功
- 限流自适应 —— 命中限流自动指数退避，长期正常再逐步恢复
- 自检 `selftest` —— 一键验证本地环境 + 接口连通 + 结构未变（联网 11 项）
- 375 项离线测试

---

## 🔬 实测发现的文档错误（重要）

本项目所有接口行为都经实测核对。**官方文档有错，照抄会写出静默丢数据的代码**：

### 1. PubMed `retmax` 上限是 9,999，不是文档写的 10,000

```
请求 retmax=100000  →  返回 "retmax":"9999"          ← 静默截断，不报错
请求 retstart=10000 →  ERROR: cannot be larger than 9998  ← 显式报错
```

官方文档 NBK25499 至今仍写 "up to a maximum of 10,000 records"。
**按 10,000 切分，每片会静默少 1 条** —— 这种 bug 能在生产环境潜伏数月。

### 2. openFDA 用 HTTP 404 表示"查无结果"

```
search=openfda.brand_name:"zzzznotadrug" → HTTP 404 {"code":"NOT_FOUND"}
```

**这不是故障。** 把它当错误重试的爬虫会在空查询上死循环、白烧配额。

### 3. openFDA 的 `openfda.*` 字段永远是数组

即使只有一个元素也是 `["TYLENOL"]`。直接索引会得到 `['TYLENOL']` 这种脏数据进 CSV。

### 4. 各端点字段前缀不统一

`openfda.generic_name` 在 `label`/`event` 有效，但 **`ndc`/`shortages` 里查不到**
（它们的 `generic_name` 在顶层）；`orangebook` **完全没有 `openfda` 对象**。

实测：`ndc` 用 `openfda.generic_name:metformin` → **0 条**；改用 `generic_name:metformin` → **741 条**。

> 写错字段名时语法完全合法、只安静返回 0 条。本程序会自动去掉 `openfda.` 前缀重试，
> 并把正确写法提示出来。

### 5. 其他

| 事实 | 说明 |
| --- | --- |
| openFDA `limit` 上限 | **1000** |
| openFDA `skip` 上限 | **25000**（可达窗口 26,000） |
| 药品短缺端点 | 是 **`/drug/shortages.json`（复数）**，单数不存在 |
| `count` 查询的 `meta` | **没有** `results` 子对象 |
| 日期格式 | 召回用 `YYYY-MM-DD`，不良事件/标签用 `YYYYMMDD` —— **不统一** |
| NCBI 限流响应 | 可能带 **2xx** 状态码，**必须查响应体** |
| openFDA 药品打包下载 | **不存在**（`download.json` 里药品条目为零），只能爬 API |

---

## ⚠️ 已知限制（如实相告）

1. **紫皮书无法爬取** —— FDA 只提供网页检索 + 一份 PDF，无打包数据。这是真实缺口，不假装支持。
2. **PMC 全文** —— 旧 `oa.fcgi` 接口已于 2026-08 下线，FTP 打包文件一并移除，
   现改用 S3 桶 `pmc-oa-opendata`。全文抓取默认关闭。
3. **不良事件数据量为百万级** —— 建议先 `--dry-run` 探量。
4. **PubMed 摘要可能受版权保护** —— 超合理使用范围的再分发需权利人许可。
5. **数据未经 FDA 校验，不得用于医疗决策。**
6. **超大规模语料（>10 万条）** —— NCBI 官方建议改用本地 PubMed baseline，而非爬取。
7. Linux/macOS 可用命令行模式，图形界面以 Windows 为主。

---

## 🔑 强烈建议：先配 API key

配额差距是 **120 倍**：

| | 无 key | 有 key |
| --- | --- | --- |
| openFDA | 240 次/分钟、**1,000 次/天** | 240 次/分钟、**120,000 次/天** |
| PubMed | **3 请求/秒** | 10 请求/秒 |

```powershell
python pharma_crawler.py auth set --openfda-key 你的KEY --pubmed-key 你的KEY --pubmed-email 你的邮箱
```

- openFDA 申请：<https://open.fda.gov/apis/authentication/>
- NCBI 申请：<https://account.ncbi.nlm.nih.gov/> → Account settings → API Key Management

> 凭据写入用户目录 `~/.pharma_crawler/`，**不会写进项目文件夹**（项目目录常被同步/分享）。
> NCBI 另要求把 tool + email 注册到 `eutils@ncbi.nlm.nih.gov` —— 只填在请求里是不够的，
> 被封 IP 后只有注册过才能解封。

---

## 😰 遇到问题？

提 [Issue](https://github.com/leerogerstheman/LeebertyPharma/issues)，最好附上：

- 做了什么、完整命令行
- `python pharma_crawler.py selftest` 的输出
- 报错截断或日志

---

## 🔭 后续计划（不承诺时间）

- DailyMed 接入（SPL 说明书全文）
- PMC 全文抓取（走 S3 桶）
- 检索式构造器（GUI 里下拉选字段，不用记语法）
- 定时增量爬取
- 界面中英双语

---

## 🐛 发布后修正的两个缺陷

v1.0.0 的初版源码存在两个**便携性**问题 —— 都属于"作者机器上一切正常、
发出去就不对"的类型，已修复并补了回归测试。

### 1. 数据目录写死在作者机器上

`config.json` 里原本写的是绝对路径 `D:\PharmaCrawler\library`。
别人克隆到别的盘或别的目录后，程序仍往那个位置写：要么污染别人的项目，
要么因为目录不存在而报错。

改为相对路径 `library` 后，又暴露出第二个更隐蔽的问题：

### 2. 相对路径按"当前工作目录"解析，而不是程序目录

原实现直接 `Path(cfg["output_dir"])`。双击 `gui.bat` 时，
`.bat` 里的 `cd /d "%~dp0"` 恰好把工作目录设成了程序目录，**看起来完全正常**；
但改用 `python D:\path\to\pharma_crawler.py`、桌面快捷方式或计划任务启动时，
工作目录是别处，数据就悄悄写到那个别处去了。

**修复**：新增 `resolve_output_dir()` —— 相对路径一律相对**程序所在目录**解析，
绝对路径原样保留。`selftest` 现在会打印数据目录的**实际绝对路径**，
并检查配置是否被写成了绝对路径。

### 3. 打包成 exe 后数据落进 `_internal\`

构建便携版时发现：PyInstaller 把代码放进 `_internal\`，
而程序目录取自 `__file__`，于是数据写进 `_internal\library\` ——
用户在资源管理器里根本看不到，删掉 `_internal\` 还会把数据一起删掉。

**修复**：冻结时改用 `sys.executable` 所在目录（即 exe 旁边）。
配置文件也在 exe 旁边留一份，用户可以直接编辑。

> 验证方式：把 ZIP 解压到随机目录，从 `C:\Windows` 启动，
> 确认数据落在解压目录内、`C:\Windows` 下无任何残留。

---

## v1.0.0（2026-10-03）

首个版本。

**核心**

- 七大 FDA 数据源 + PubMed E-utilities，可单源也可联合爬取
- 突破上游上限：FDA 走 `search_after` 游标，PubMed 走年→月→日递归切分
- 段级断点续爬 + 安全停止
- 7 种导出格式、硬链接分类、多维检索

**界面**

- Material Design 3 图形界面（亮/暗双主题、胶囊按钮、卡片、纸片、圆角对话框）
- 4 个标签页 + 实时进度与日志

**可靠性**

- 接口结构校验（改版时大声报错，不静默假成功）
- 限流自适应（指数退避 + 逐步恢复）
- `selftest` 自检（联网 11 项）
- 375 项离线测试

**文档**

- `docs/DATA_SOURCE_NOTES.md` —— 接口实测事实（改版时先看这个）
- `docs/MATERIAL3_NOTES.md` —— M3 取值来源与 tkinter 落地要点
