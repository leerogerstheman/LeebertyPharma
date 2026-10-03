# 数据来源声明

本程序（PharmaCrawler）的**代码**以 MIT 许可发布，见 [LICENSE](LICENSE)。

但**所抓取的数据**版权归各上游来源所有，使用须遵守各自的条款 ——
MIT 许可**不覆盖**这些数据。以下声明来自各来源的官方条款。

---

## openFDA（美国食品药品监督管理局）

- 数据为**公有领域**，采用 **CC0 1.0** 许可，可商用，无需授权。
- 署名**建议但非强制**。建议引用格式：

  ```
  Data provided by the U.S. Food and Drug Administration (https://open.fda.gov)
  ```

- ⚠️ FDA 明确说明：**该数据未经校验，不得用于医疗决策。**
- FDA 保留对**疑似超出或绕过限流**的使用者临时或永久限制访问的权利，且会监控用量。
- 条款：<https://open.fda.gov/terms/>

---

## PubMed / NCBI E-utilities（美国国家医学图书馆）

- NLM 的**免责声明与版权声明必须对产品使用者可见**
  （<https://www.ncbi.nlm.nih.gov/About/disclaimer.html>）。
- ⚠️ **PubMed 摘要可能受版权保护**，超出合理使用范围的再分发需取得权利人许可。
  NLM 不就法律问题提供意见。
- ⚠️ NCBI 明文规定：**"NCBI does not allow scripting against our web pages."**
  只能通过 E-utilities / FTP / S3 访问，**不得爬取网页**，违者可能被封 IP。
- 速率要求：无 API key 每秒不超过 **3** 次请求；有 key 每秒 **10** 次。
  大批量任务建议安排在**周末**或**工作日的 ET 21:00–05:00**。
- 建议在请求中携带可识别的 `tool` 与 `email`，并把二者**注册**到
  `eutils@ncbi.nlm.nih.gov` —— 官方明确说明：仅在请求里带上是不够的，
  只有注册过才能在封 IP 后解封。
- 超大规模语料，NCBI 官方建议改用**本地 PubMed baseline** 而非在线爬取：
  <https://www.nlm.nih.gov/databases/download/pubmed_medline.html>

---

## DailyMed（美国国家医学图书馆）

- 需**致谢 NLM 为数据来源**。
- ⚠️ **不得使用 PMC / DailyMed 的商标或标识**。
- ⚠️ **不得暗示 NLM 或 NIH 的背书**。
- 只再分发许可允许再分发的数据；若数据已过期，需明确声明。

---

## PMC 开放获取（PubMed Central）

- 旧 `oa.fcgi` 接口已于 **2026-08** 下线，FTP 打包文件同步移除；
  现改用 S3 桶 `pmc-oa-opendata`（见 [docs/DATA_SOURCE_NOTES.md](docs/DATA_SOURCE_NOTES.md) 第 4.8 节）。
- 适用与上条 DailyMed 相同的 NLM 条款：致谢 NLM、不得使用标识、不得暗示背书、
  只再分发许可允许的数据。

---

## 使用者的责任

使用本程序即表示你已知悉：**遵守各上游数据源的使用条款是使用者自身的责任**，
而非本程序作者的责任。程序内置了限流与退避机制以帮助合规，
但它不能替代你对上述条款的阅读与遵守。
