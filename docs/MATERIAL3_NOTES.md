# Material Design 3 实现参考（供 tkinter 使用）

> **取值来源说明**：`m3.material.io` 全站 JS 渲染，抓取只能拿到标题，无法取得数值。
> 因此本文件的数值取自 **一手来源** —— Google 在 AOSP/androidx 中生成的设计令牌文件：
> `PaletteTokens.kt`、`ColorLightTokens.kt`、`ColorDarkTokens.kt`、
> 各组件 `*Tokens.kt`、`StateTokens.kt`、`WindowSizeClass.kt`，
> 以及 MDC-Android 的 `dimens.xml`、`ElevationOverlayProvider.java`。
> 所有色值均由源 RGB 三元组计算得出，非转抄自博客。

---

## 一、颜色系统

### 1.1 基线亮色方案（37 个令牌）

| 令牌 | 色值 | 令牌 | 色值 |
| --- | --- | --- | --- |
| primary | `#6750A4` | outline | `#79747E` |
| on-primary | `#FFFFFF` | outline-variant | `#CAC4D0` |
| primary-container | `#EADDFF` | shadow | `#000000` |
| on-primary-container | `#21005D` | scrim | `#000000` |
| secondary | `#625B71` | inverse-surface | `#322F35` |
| on-secondary | `#FFFFFF` | inverse-on-surface | `#F5EFF7` |
| secondary-container | `#E8DEF8` | inverse-primary | `#D0BCFF` |
| on-secondary-container | `#1D192B` | surface-tint | `#6750A4` |
| tertiary | `#7D5260` | background | `#FEF7FF` |
| on-tertiary | `#FFFFFF` | on-background | `#1D1B20` |
| tertiary-container | `#FFD8E4` | surface | `#FEF7FF` |
| on-tertiary-container | `#31111D` | on-surface | `#1D1B20` |
| error | `#B3261E` | surface-variant | `#E7E0EC` |
| on-error | `#FFFFFF` | on-surface-variant | `#49454F` |
| error-container | `#F9DEDC` | surface-dim | `#DED8E1` |
| on-error-container | `#410E0B` | surface-bright | `#FEF7FF` |
| surface-container-lowest | `#FFFFFF` | surface-container-low | `#F7F2FA` |
| surface-container | `#F3EDF7` | surface-container-high | `#ECE6F0` |
| surface-container-highest | `#E6E0E9` | | |

### 1.2 基线暗色方案（37 个令牌）

| 令牌 | 色值 | 令牌 | 色值 |
| --- | --- | --- | --- |
| primary | `#D0BCFF` | outline | `#938F99` |
| on-primary | `#381E72` | outline-variant | `#49454F` |
| primary-container | `#4F378B` | shadow | `#000000` |
| on-primary-container | `#EADDFF` | scrim | `#000000` |
| secondary | `#CCC2DC` | inverse-surface | `#E6E0E9` |
| on-secondary | `#332D41` | inverse-on-surface | `#322F35` |
| secondary-container | `#4A4458` | inverse-primary | `#6750A4` |
| on-secondary-container | `#E8DEF8` | surface-tint | `#D0BCFF` |
| tertiary | `#EFB8C8` | background | `#141218` |
| on-tertiary | `#492532` | on-background | `#E6E0E9` |
| tertiary-container | `#633B48` | surface | `#141218` |
| on-tertiary-container | `#FFD8E4` | on-surface | `#E6E0E9` |
| error | `#F2B8B5` | surface-variant | `#49454F` |
| on-error | `#601410` | on-surface-variant | `#CAC4D0` |
| error-container | `#8C1D18` | surface-dim | `#141218` |
| on-error-container | `#F9DEDC` | surface-bright | `#3B383E` |
| surface-container-lowest | `#0F0D13` | surface-container-low | `#1D1B20` |
| surface-container | `#211F26` | surface-container-high | `#2B2930` |
| surface-container-highest | `#36343B` | | |

> ⚠️ **两处常见错误**（包括流传很广的 Figma kit 文档）：
> 亮色 `on-primary-container` 是 **`#21005D`**（Primary10），**不是** `#4F378A`；
> 暗色 `on-secondary` 是 **`#332D41`**（Secondary20），**不是** `#1D192B`。

### 1.3 色调板与角色映射

可用色调：**0、10、20、30、40、50、60、70、80、90、95、99、100**，
另有 M3 表面专用的非整十色调 4、6、12、17、22、24、87、92、94、96。

- **Primary**：10 `#21005D`、20 `#381E72`、30 `#4F378B`、40 `#6750A4`、80 `#D0BCFF`、90 `#EADDFF`
- **Neutral**：4 `#0F0D13`、6 `#141218`、10 `#1D1B20`、12 `#211F26`、17 `#2B2930`、
  22 `#36343B`、87 `#DED8E1`、90 `#E6E0E9`、92 `#ECE6F0`、94 `#F3EDF7`、96 `#F7F2FA`、98 `#FEF7FF`
- **Neutral-variant**：30 `#49454F`、50 `#79747E`、60 `#938F99`、80 `#CAC4D0`、90 `#E7E0EC`

**映射规则（亮色/暗色）**：

| 角色 | 亮 | 暗 | 角色 | 亮 | 暗 |
| --- | --- | --- | --- | --- | --- |
| primary | P40 | P80 | outline | NV50 | NV60 |
| on-primary | P100 | P20 | outline-variant | NV80 | NV30 |
| primary-container | P90 | P30 | surface / background | N98 | N6 |
| on-primary-container | P10 | P90 | on-surface / on-background | N10 | N90 |
| secondary | S40 | S80 | surface-variant | NV90 | NV30 |
| on-secondary | S100 | S20 | on-surface-variant | NV30 | NV80 |
| tertiary | T40 | T80 | inverse-surface | N20 | N90 |
| error | E40 | E80 | inverse-on-surface | N95 | N20 |
| error-container | E90 | E30 | inverse-primary | P80 | P40 |

表面容器：亮色 lowest=N100、low=N96、container=N94、high=N92、highest=N90、dim=N87、bright=N98；
暗色 lowest=N4、low=N10、container=N12、high=N17、highest=N22、dim=N6、bright=N24。

---

## 二、字体排印

字体 **Roboto**（400 Regular / 500 Medium），回退栈
`Roboto, "Helvetica Neue", Arial, sans-serif`；Windows 上若无 Roboto 退到 **Segoe UI**。

| 样式 | 字号 px | 行高 px | 字重 | 字距 px |
| --- | --- | --- | --- | --- |
| display-large | 57 | 64 | 400 | −0.25 |
| display-medium | 45 | 52 | 400 | 0 |
| display-small | 36 | 44 | 400 | 0 |
| headline-large | 32 | 40 | 400 | 0 |
| headline-medium | 28 | 36 | 400 | 0 |
| headline-small | 24 | 32 | 400 | 0 |
| title-large | 22 | 28 | 400 | 0 |
| title-medium | 16 | 24 | 500 | 0.15 |
| title-small | 14 | 20 | 500 | 0.1 |
| body-large | 16 | 24 | 400 | 0.5 |
| body-medium | 14 | 20 | 400 | 0.25 |
| body-small | 12 | 16 | 400 | 0.4 |
| label-large | 14 | 20 | 500 | 0.1 |
| label-medium | 12 | 16 | 500 | 0.5 |
| label-small | 11 | 16 | 500 | 0.5 |

> tkinter 无字距 API。≤0.5px 的字距视觉上可忽略，直接省去；字重 500 用
> `("Roboto Medium", size)` 或在不可用时仅对 label/title 角色退化为 bold。

---

## 三、圆角刻度

`none` 0 · `extra-small` 4 · `small` 8 · `medium` 12 · `large` 16 · `extra-large` 28 ·
`full` 9999/圆形。Expressive 另加 20、32、48。

组件用法：按钮 **full**；悬浮按钮 16；卡片 12；对话框 28；文本框 4（填充式仅上方两角）；
菜单 4；纸片 8；消息条 4；列表选中项 16；复选框 2；标签页指示条 3。

---

## 四、高度层级

实测层级：**0=0dp、1=1dp、2=3dp、3=6dp、4=8dp、5=12dp**
（2 是 3dp、3 是 6dp —— 这两点常被写错）。

旧版叠加公式出自 `ElevationOverlayProvider.java`：
`alpha = (4.5·ln(1+elevDp) + 2)/100`，叠加 `colorPrimary`。对应 1dp 5%、3dp 8%、
6dp 11%、8dp 12%、12dp 13.5%。常见的 0/5/8/11/12/14% 圆整表就是它的近似呈现。

**当前做法（应采用）**：叠加色已在 MDC 1.11.0-alpha02+ 移除，改用**色调表面容器**：
+1 用 `surface-container-low`，+2 用 `surface-container`，+3/+4 用 `-high`，
+5 用 `-highest`，凹陷用 `-lowest`，模态背后用 `surface-dim`。

**tkinter 落地**：tkinter 没有真实投影，**不要用深色矩形假装阴影**。
靠堆叠上述表面色调 + 1px `outline-variant` 描边来模拟层级。
遮罩 = 黑色 32% 透明（`#52000000`）。

---

## 五、组件规格

**状态层（精确）**：悬停 **8%** · 聚焦 **10%** · 按下 **10%** · 拖拽 **16%** ——
以**内容色的半透明叠加**实现，**不是**替换填充色。
（早期版本聚焦/按下为 12%，当前是 10%。）禁用态：容器 10% 不透明，内容 38%。

| 组件 | 关键规格 |
| --- | --- |
| 按钮 | 高 40、圆角 20（胶囊）、左右内边距 24（文字按钮 12）、最小宽 64、图标 18、标签 Label Large |
| 文本框 | 高 56、圆角 4（填充式仅上两角）、描边 1dp → **聚焦变 2dp primary**、标签上浮并缩到 12px |
| 卡片 | 圆角 12、内边距 16。elevated 用 `surface-container-low`，filled 用 `surface-container-highest`，outlined 为 1dp `outline-variant` |
| 纸片 | 高 32、圆角 8、图标 18；筛选/输入选中态用 `secondary-container` |
| 标签页 | 纯文字 48 / 带图标 64；指示条 **高 3、圆角 3** |
| 列表 | 56/72/88；内边距 16；选中 = `secondary-container`、圆角 16 |
| 对话框 | 圆角 28、`surface-container-high`、层级 3（6dp）、图标 24 |
| 导航栏 | 侧栏 80/96；抽屉 360，指示器 336×56；底栏 64 |
| 顶栏 | 高 64、`surface`、标题 Title Large |
| 进度 | 线性高 4；环形 40 外径、描边 4；间隙 4 + 停止点 |
| 消息条 | 高 48、圆角 4、`inverse-surface` |
| 开关 | 轨道 52×32，滑块 24（关 16、按 28），光晕 40 |
| 复选框 | 18×18、圆角 2、光晕 40 · 单选框 外环 20、描边 2、圆点 10 |
| 滑块 | Expressive 为 16 轨 + 4×44 条形手柄；经典基线为 4 轨 + 20 圆形手柄 —— 择一 |
| 提示 | 纯文本高 24 圆角 4；富文本圆角 12 · 分隔线 1dp `outline-variant` |

---

## 六、间距与布局

4dp 基准单位，多数间距用 8dp。常用值：4、8、12、16、24、32、48、64。

**断点（实测自 `WindowSizeClass.kt`）**：

| 宽度类 | dp |
| --- | --- |
| compact | 0 – 599 |
| medium | 600 – 839 |
| expanded | 840 – 1199 |
| large | 1200 – 1599 |
| extra-large | ≥ 1600 |

高度类：compact <480、medium 480–899、expanded ≥900。

令牌三层命名：`md.ref.palette.primary40` → `md.sys.color.primary` → `md.comp.filled-button.container.color`。

---

## 七、无法从官方来源核实的事项

1. 对话框 **最小 280 / 最大 560dp**、消息条 **最小 344dp** —— 属 M3 网页规范值，
   未出现在所核对的令牌文件中。
2. 顶栏 medium/large 展开高度（112/152dp）—— 仅见于网页规范。
3. 各尺寸类的布局**边距**（16/24）与分栏数 —— 仅见于网页规范。
4. **字距**在 tkinter 无对应 API。
5. **Material Symbols 图标字形** —— tkinter 无矢量字形渲染器，
   只能用内嵌 PNG、位图字体或 Unicode 符号。这是平台限制，不是规范缺口。

> ⚠️ **最大的落地限制**：tkinter **无法原生绘制圆角**。
> 本文所有圆角（卡片 12、对话框 28、胶囊按钮 20）都必须靠
> `Canvas` + `create_polygon`/`create_arc` 平滑样条自绘圆角矩形，
> 或预渲染 9-slice PNG 作 `PhotoImage` 边框。
> ttk 的 `clam`/`alt` 主题**无法**仅靠样式产出这些形状 ——
> 必须先把一个统一的圆角矩形绘制助手做好，它是整个主题的地基。
