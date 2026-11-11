# Agent HUD UI 设计系统（提取版）

从 `Desktop\Agent HUD.lnk` 指向的已安装应用 **Agent HUD 0.6.0** 中提取。
所有颜色、字号、尺寸都来自**运行时 CSSOM** 与**官方开源仓库 tag v0.6.0** 的双向核对，
不是凭截图目测。

---

## 0. 取证链（先看这段，判断可信度）

| 环节 | 做法 | 结果 |
| --- | --- | --- |
| 快捷方式 | 解析 `.lnk` | 目标 `E:\ .stepclaw\Agent HUD\Agent HUD.exe`（注意 `E:\` 后有前导空格） |
| 应用形态 | 版本信息 + 数据目录 | `agenthud` / Agent HUD / **0.6.0**，未签名；`%LOCALAPPDATA%\app.agenthud.windows\EBWebView` → **Tauri + WebView2** |
| 运行时 | `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222` 重启后连 CDP | 抓到 2 个页面：HUD 悬浮层（`?surface=hud`）与设置窗口；样式表 `index-BW4Z611o.css` 86,560 字符 + `SettingsPanes-C6MBxagq.css` 2,445 字符 + sonner 注入 16,763 字符 |
| 源码 | `github.com/jazzenchen/agent-hud-open` @ `v0.6.0`（Apache-2.0，292★） | 前端在 `src/packages/ui`（shadcn 封装 + `global.css`）与 `src/packages/hud`（HUD 场景与数值 token） |
| 一致性校验 | 运行时 `:root` 计算值 vs `global.css` 声明 | 暗色 29 个、亮色 27 个 token **全部命中**；唯一"差异"是 `--settings-card-border` 的 `#ffffff11` 被浏览器归一化成 `#fff1`（等价） |
| 安装包 | 官方 Release v0.6.0 资产 `Agent-HUD-0.6.0-windows-x86_64-unsigned-setup.exe`（58,849,942 B） | 已定位并开始下载（GitHub 直连约 30 KB/s，未下载完）；**未执行安装包**，只做静态解包准备 |

> 二进制侧结论：`Agent HUD.exe` 里 `--color-` / `var(--` / `@font-face` 计数均为 0，只有 `brotli` 与 `tauri` 字样 —— 前端资源是 brotli 压缩内嵌的，**strings 提不出 CSS**，必须走 CDP 或源码。

---

## 1. 技术底座

- **Tauri 2**（Rust 壳）+ **WebView2**（实测内核 `Edg/157.0.4341.42`）
- **React + Vite**，无 Tailwind 配置文件 —— **Tailwind v4 CSS-first**（`@theme inline`、`@custom-variant dark`、`@source`、容器查询）
- **shadcn/ui**：`components.json` 中 `style: "new-york"`、`baseColor: "neutral"`、`cssVariables: true`、`iconLibrary: "lucide"`
- Radix UI 原语、`class-variance-authority`、`tailwind-merge`、`motion`、`sonner`（toast）
- 图标：**Lucide**（`lucide-react`），单色、13–16px

设计取向（推断，但证据很强）：**把 macOS 原生 App 观感搬到 Windows** —— SF Pro Text 优先字体栈、`#0a84ff` 系统蓝、212px 设置侧栏、13px 基准字号、`#30d158` 开关绿，全部是 Apple HIG 数值。

---

## 2. 颜色系统

### 2.1 语义 token（shadcn 命名，暗色优先）

来源：`src/packages/ui/src/global.css:36`（暗）/ `:69`（亮）

| token | 暗色（默认） | 亮色 | 用途 |
| --- | --- | --- | --- |
| `--background` | `#282828` | `#f5f5f7` | 窗口底色 |
| `--foreground` | `#f5f5f7` | `#1d1d1f` | 主文字 |
| `--card` | `#ffffff0d` | `#ffffff` | 卡片（半透明白 5%） |
| `--card-foreground` | `#f5f5f7` | `#1d1d1f` | 卡片文字 |
| `--popover` | `#303030` | `#ffffff` | 弹层底色 |
| `--popover-foreground` | `#f5f5f7` | `#1d1d1f` | 弹层文字 |
| `--primary` | `#0a84ff` | `#007aff` | 主色（系统蓝） |
| `--primary-foreground` | `white` | `white` | 主色上的文字 |
| `--secondary` | `#ffffff14` | `#0000000a` | 次级面（输入框/选择器） |
| `--muted` | `#ffffff14` | `#00000014` | 静默面（tabs 槽、滑轨） |
| `--muted-foreground` | `#98989d` | `#6e6e73` | 次要文字（Apple 灰） |
| `--accent` | `#ffffff14` | `#0000000a` | hover / 强调面 |
| `--destructive` | `#ff453a` | `#ff3b30` | 危险 |
| `--border` | `#ffffff1f` | `#0000000f` | 1px 描边 |
| `--input` | `#ffffff24` | `#0000001a` | 输入控件描边 |
| `--ring` | `#0a84ff` | `#007aff` | 焦点环 |
| `--radius` | `12px` | `12px` | 基准圆角 |

### 2.2 产品级扩展 token

| token | 暗色 | 亮色 | 用途 |
| --- | --- | --- | --- |
| `--sidebar` | `#1f1f1f` | `#e8e8ea` | 设置侧栏底 |
| `--panel` | `#ffffff0d` | `#ffffff` | 面板 |
| `--dim` | `#98989d` | `#6e6e73` | 弱化文字 |
| `--mint` | `#32d583` | `#32d583` | 成功/额度语义词 |
| `--island-fill` | `#000` | `#f5f5f7` | 灵动岛/刘海面板底 |
| `--island-outline` | `#ffffff24` | `#0000001a` | 灵动岛描边 |
| `--meter-track` | `#ffffff24` | `#0000001a` | 用量条轨道 |
| `--segment-selected` | `#ffffff3d` | `#ffffff` | 分段控件选中块 |
| `--settings-card` | `#ffffff0d` | `#ffffff` | 设置卡片底 |
| `--settings-card-border` | `#ffffff11` | `#00000008` | 设置卡片描边（比 `--border` 更淡） |

### 2.3 透明度分层（核心手法）

暗色不是"一堆灰"，而是**同一种白 ink 按 alpha 分层**：`0d (5%) → 11 (7%) → 14 (8%) → 1a (10%) → 1f (12%) → 24 (14%) → 30 (19%) → 3d (24%) → 55 (33%)`。
好处是**换底色不用改组件**，半透明层会自动叠出正确层级；配合 `--island-fill: #000`，HUD 悬浮层能直接透出桌面。

选中文本：`::selection { background: #0a84ff55 }`（`global.css:141`）。

### 2.4 HUD 等级色 / 告警色

来源：`src/packages/hud/src/tokens.ts:40`（`SURFACE`）、`:86`（`LEVEL_FILL`）、`:95`（`LEVEL_SEE_THROUGH`）、`:127`（`ALERT_FILL`）

**等级色（fill：环/条/点）**

| level | 暗色 | 亮色 |
| --- | --- | --- |
| `ok` | `#3ddc84` | `#1f9d4c` |
| `warning` | `#ffd23f` | `#c08000` |
| `critical` | `#ff453a` | `#ff3b30` |

**等级色（text：数字/细字形）** —— 暗色沿用 fill；亮色压深到约 4.5:1：`ok #1e7e34` / `warning #946300` / `critical #d70015`；**透底模式**再提亮/加深一档（`#9df0c0` / `#ffe08a` / `#ffa39b`）。

**告警色调（alert tone）**

| tone | 暗色 fill | 亮色 fill | 透底（暗） |
| --- | --- | --- | --- |
| `turn` | `#82aaff` | `#2f62d0` | `#b8cfff` |
| `calm` | `#6cd8ac` | `#0f7b57` | `#a8ecd0` |
| `warning` | `#e9a16d` | `#a8521c` | `#f6c9a6` |

**权限卡配色**（`tokens.ts:165`）：Allow 实心 `#3ddc84` + 黑字；Deny 用 critical 的 14% / 30% 双层（fill + 边）；Always allow 用 ok 色描边 + ok 色文字。

**Agent 调色板（6 色循环）**（`panel/theme.ts:84`）

- 暗：`#c084fc` `#60a5fa` `#2dd4bf` `#fb923c` `#f472b6` `#a3e635`
- 亮：`#9333ea` `#2563eb` `#0d9488` `#ea580c` `#db2777` `#4d7c0f`

---

## 3. 字体与排版

```css
--font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
--font-mono: ui-monospace, "SF Mono", "Cascadia Code", monospace;
```

Windows 上会回落到 **Segoe UI**（推断：栈里前两项在 Windows 不存在；未做字体回退的逐字比对），macOS 上是 SF Pro Text。**无 Web Font**（`@font-face` 计数 0），零字体加载成本。

| 层级 | 字号 | 字重 | 行高 | 场景 |
| --- | --- | --- | --- | --- |
| 页面标题 | 20px | 600 | 26px | 统计页大标题 |
| 区块标题 | 16px | 600 | 24px | 面板标题 |
| 正文/主体 | **13px** | 400 | 18px | 全局基准（`body`） |
| 控件文字 | 12px | 500（按钮）/ 400（tab） | 16.5px | 按钮、输入、下拉、tab |
| 密集信息 | 11px | 400 | 15px | 账号行、脚注、notice |
| 徽标 | 10px | 500 | — | badge |

细节：`-webkit-font-smoothing: antialiased`、`text-rendering: optimizeLegibility`、`font-synthesis: none`；数字统一 `font-variant-numeric: tabular-nums`（`panel/theme.ts` 的 `tabular` 常量），保证 token 计数不跳动。字重只用 **400 / 500 / 600 / 700** 四档。

---

## 4. 间距、尺寸与密度

### 4.1 实际使用的间距

实测 gap 频次：`8px (59)` → `6px (42)` → `2px (23)` → `16px (18)` → `12px (11)` → `5/10/7px`。
主节奏是 **2 / 6 / 8 / 12 / 16**，不是整 4 的倍数表；6px 与 8px 承担大部分微间距。

### 4.2 控件高度（同一套"紧凑 macOS"标尺）

| 控件 | 尺寸 |
| --- | --- |
| Button（默认） | 高 28，圆角 6，字号 12，图标按钮 28×28 |
| Input | 高 28，圆角 6 |
| Select trigger | 高 27，最小宽 114，圆角 6 |
| Tabs 容器 | 高 28，内边距 2，gap 1，圆角 7 |
| Tabs 触发器 | 高 24，左右 12，圆角 5 |
| Badge | 高 20，圆角 5，字号 10 |
| Switch | 34×20，内边距 2，滑块 16×16 |
| Slider | 轨道 4，滑块 13×13 |

### 4.3 结构尺寸

| 元素 | 数值 | 来源 |
| --- | --- | --- |
| 设置窗口 | 960×720（实测视口） | 运行时 |
| 设置侧栏 | 212px 宽 | 运行时 DOM |
| HUD 悬浮面板 | 540px 宽 | `hud-defaults.json` `panelWidth` |
| 面板内容区 | 504px（540 − 2×18 外边距） | 运行时 DOM |
| 刘海面板 | 200×24 | `notchWidth` / `notchHeight` |
| Simple 模式圆环 | 22px 直径，环宽 1.5 | `simpleSize` / `ringWidth` |
| 统计页工具栏 | 44px 高，左右 22px | `global.css` `.statistics-toolbar` |

### 4.4 HUD 面板间距（`panel/theme.ts:15` `SOURCE_PANEL`）

`inset 6 · marker 8 · rowGap 12 · nameWidth 152 · valueWidth 62 · detailWidth 96 · section 10 · block 10 · divided 8 · blockParts 8 · rowPadding 4 · sessionRows 6 · heading 13px · account 11px · row 13px · detail 12px · headingHeight 18 · footerSpacing 10 · footerTop 6 · footerButton 28 · compactChartHeight 40`

### 4.5 三档密度（`panel/theme.ts:74`）

| 密度 | section | block | divided | row | session |
| --- | --- | --- | --- | --- | --- |
| comfortable | 14 | 11 | 11 | 6 | 8 |
| normal（默认） | 10 | 8 | 8 | 4 | 6 |
| compact | 6 | 5 | 5 | 2 | 4 |

字号与图表高度**不随密度变化** —— 只压缩留白。

---

## 5. 圆角、描边、阴影

**圆角阶梯**：`5 (badge/tab/chip) → 6 (button/input/select) → 7 (tabs 容器) → 8 (popover/tooltip) → 10 (alert/stats card) → 12 (card/panel/--radius)`。

**描边**：统一 1px，颜色全部是 alpha token；设置卡用更淡的 `--settings-card-border` 与 `--border` 区分层级。**没有毛玻璃**：`backdrop-filter` 在样式表里只有 Tailwind 变量清单，没有实际声明。

**阴影**（克制，仅用于浮起感）：

| 用途 | 值 |
| --- | --- |
| Tab 选中块 | `0 1px 2px #0002` |
| Select trigger | `0 1px 1px #0001` |
| Switch 滑块 | `0 1px 3px #0004` |
| Slider 滑块 | `0 1px 3px #0006` |
| Popover / Dropdown / Tooltip | `0 8px 28px #0003` |
| 卡片 / 按钮 | `none` |

> 设计取向：**层次靠透明度叠色和 1px 边，不靠重阴影**。

---

## 6. 组件规格（24 个 `data-slot`）

清单：`accordion-content/trigger`、`alert/alert-description`、`badge`、`button`、`card/card-content`、`dropdown-menu-content/item`、`input`、`select-content/item/trigger`、`separator`、`slider-range/thumb/track`、`switch/switch-thumb`、`tabs/tabs-list/tabs-trigger`、`tooltip-content`。

| 组件 | 关键规格 | 状态/变体 |
| --- | --- | --- |
| Button | 28px 高 / 6px 圆角 / 12px 500 / gap 6 / 无阴影 | CVA：`default destructive outline secondary ghost link`；尺寸 `xs sm default lg icon icon-xs icon-sm icon-lg` |
| Card | 12px 圆角 / 1px `--border` / `--card` 底 / content 左右 14 | 无 header 内边距（gap 0，padding 0） |
| Badge | 20px 高 / 5px 圆角 / 10px 500（覆盖 shadcn 的 `rounded-full`） | `default secondary destructive outline ghost link` |
| Tabs | 槽 `--muted` + 2px 内边距；选中块 `--segment-selected` + `0 1px 2px #0002` | `data-state="active"` |
| Select | 27px / 114px 最小宽 / `--secondary` 底 + `--input` 边 | 内容 8px 圆角 + 28px 阴影 |
| Input | 28px / 6px / `--secondary` 底 | 焦点环 `--ring`，offset 2 |
| Switch | 34×20 / 关 `#ffffff30`（亮色 `#00000024`）/ 开 `#30d158` | 滑块 16px 白 + 阴影 |
| Slider | 轨道 4px `--muted`，进度 `--primary`，滑块 13px | — |
| Alert | 10px 圆角 / 14px 内边距 / 12px 字 / 上下 14 外边距 | 描述行高 1.5 |
| Accordion | trigger 上下 12px / 13px 500 | — |
| Separator | 1px `--border` | — |

焦点态统一：`outline-color: var(--ring); outline-offset: 2px`，组件层再加 `ring-[3px] ring-ring/50`。

---

## 7. 动效

**时长档位**：`140ms`（hover 背景）→ `180ms`（进度收尾）→ `240ms`（内容淡入）→ `360/420ms`（卡片入场）→ `520/900ms`（SVG 绘制/柱状生长）→ `1.6s`（骨架波）。

**缓动**：主入场 `cubic-bezier(0.2, 0.7, 0.2, 1)`；数据洗刷 `cubic-bezier(0.12, 0.6, 0.3, 1)`；其余 `ease-out`。

**关键帧**：`stats-reveal`（淡入 + 上移 4px）、`stats-wave`（骨架点波动）、`session-art-rise/draw/run`（会话示意图：柱升起、线描出、泳道跑出）、`session-pulse`、`session-fade-in`、`loading-state-in/spin`。

**无障碍**：`prefers-reduced-motion: reduce` 把动画/过渡压到 `0.01ms`（`global.css:521`、`:582`、`:698`）。

**HUD 光晕参数**（`hud-defaults.json`）：`glowStyle blur`、`glowEffect breathe`、`breathSeconds 3`、`idleBreathSeconds 7`、`breathAmplitude 0.6`、`glowRange 14`、`glowBlur 8`、`glowBrightness 0.9`、`glowGridPitch 10`、`glowOutwardOnly true`。

**交互延迟**：hover 展开 `400ms`、收起 `200ms`、消息淡出 `5s`。

---

## 8. 布局范式

1. **设置窗口 = 侧栏 + 内容**：`212px` 侧栏（30px 行高图标导航），内容区独立滚动。
2. **统计页 = 12 列网格 + 容器查询**：`@container stats`，≥1040px 时趋势图占 8 列 / Agent 占比占 4 列；<1000px 时 5 个数字块变 3+2；<600px 变两列。**断点写在容器上，不写视口上**。
3. **Agent 卡片**：`repeat(auto-fill, minmax(340px, 1fr))`，`gap 14`。
4. **HUD 悬浮层**：整页透明（`.native-hud-document` 强制 `background: transparent`），只有 540px 面板 + 顶部条，其余透传鼠标。

---

## 9. 复用到自己的项目

最小可用路径（Tailwind v4 + shadcn）：

1. 复制 `src/packages/ui/src/global.css` 到你的 `app.css`，保留 `@import "tailwindcss"` 与 `@theme inline` 段。
2. 在你的根节点写 `<html class="dark" data-theme="dark">`，或只保留 `:root` 暗色块（这套设计暗色才是默认态）。
3. 组件直接用 `src/packages/ui/src/ui/*.tsx`（24 个 slot），依赖 `radix-ui`、`class-variance-authority`、`tailwind-merge`、`lucide-react`。
4. HUD/看板类界面再引入 `src/packages/hud/src/tokens.ts`（等级色、告警色、权限卡、see-through 规则）与 `panel/theme.ts`（`SOURCE_PANEL`、`densityMetrics`、`agentColor`）。
5. 不要在 Tailwind 配置里重复定义颜色 —— v4 下 `@theme inline` 已经把 CSS 变量映射成 `bg-background` 这类工具类。

移植时最容易踩的三点：

- 半透明 token 在**浅色底**上几乎不可见（`#ffffff0d` 叠白 = 无变化），必须同时带亮色块。
- HUD 透底需要 `seeThroughGreys()` 那套"灰变墨色 tint"的替换逻辑，不能直接把 `#98989d` 用到透明底上。
- 字号别整体放大：这套节奏是按 13px 基准 + 28px 控件高度调的，改成 14/32 会立刻失去 macOS 密度感。

---

## 10. 边界与未完成项

- **安装包未解包完成**：官方 Release 资产 `Agent-HUD-0.6.0-windows-x86_64-unsigned-setup.exe`（58,849,942 B）在本机网络下约 28–30 KB/s，且多次被 CDN 中途掐断（已下 15.5 MB 后主动停止）。**未执行、未安装**。已安装 `Agent HUD.exe` 的 SHA256 为 `A8570C0191543D22410557BA33B1D847DA5AE7EA48A0B41BF964C71D71FFA7C8`（40,396,800 B），可用于与解包后 payload 比对。

  续传（可反复重跑，`-C -` 断点续传）：

  ```powershell
  curl.exe -L --retry 8 --retry-delay 2 --retry-all-errors -C - `
    -o "C:\temp\hud\installer\Agent-HUD-0.6.0-windows-x86_64-unsigned-setup.exe" `
    "https://github.com/jazzenchen/agent-hud-open/releases/download/v0.6.0/Agent-HUD-0.6.0-windows-x86_64-unsigned-setup.exe"
  # 下完后先核 SHA256SUMS，再静态解包（不运行安装器）：
  # & "E:\ .stepclaw\lingxi-desktop\resources\7za.exe" x <setup.exe> -o<out>
  ```
- **未测亮色主题的实际观感**：亮色 token 齐全，但本机 `settings.json` 为 `appearance: system` 且系统处于暗色，实测截图只有暗色。
- **未覆盖 iOS/watchOS 端**：仓库里还有 Swift 端的 `Theme+Palettes.swift`，其 token 被移植成 `global.css` 注释所述来源；本次只提取 Windows 桌面端可见部分。
- **提取时重启过 HUD 两次**（一次开 CDP、一次恢复）。当前已恢复为无调试端口运行，`settings.json` / `presentation.json` 未被改写。

---

## 11. 文件索引

| 文件 | 作用 |
| --- | --- |
| `tokens.json` | 机器可读 token（颜色/字体/间距/圆角/动效/组件），可直接喂给设计工具或代码生成 |
| `preview.html` | token 与组件实物预览页（用本文件的数值重绘） |
| `preview.png` | 预览页的 headless 渲染截图 |

原始证据（本机临时目录，非交付物）：

- `C:\temp\hud\dump-*.json`：两个页面的 CSSOM、计算样式、DOM 矩形
- `C:\temp\hud\all.css`：运行时样式表拼接
- `C:\temp\hud\src\*`：从 GitHub API 拉取的源码文件（base64 解码）
