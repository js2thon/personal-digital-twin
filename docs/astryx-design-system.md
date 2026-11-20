# Astryx 设计系统集成说明

> 参考：https://github.com/meta/astryx （Meta 开源设计系统，Beta，Apache-2.0）
> 版本：`@astryxdesign/core@0.7.11` / `@astryxdesign/theme-neutral@0.7.11` / `@astryxdesign/cli@0.7.11`

## 一、集成策略

本控制台是**零构建的静态 HTML**（`web/index.html`），由 FastAPI 直接挂载。若强行引入 React 19 组件运行时，会引入构建链、破坏已验证的六个功能页，收益与风险不匹配。因此采用分层落地：

| 层级 | 落地内容 | 状态 |
|---|---|---|
| L1 Foundations（本次重点） | 语义色 `light-dark()`、间距/圆角/字阶令牌、WCAG 4.5:1 对比度、统一 focus ring、elevation shadow、reduced-motion、响应式 | ✅ 已重构 `web/index.html` 全量 CSS |
| L2 主题与品牌 | `theme-neutral` 令牌值、明暗主题切换（跟随系统 + 手动覆盖 + localStorage 持久化） | ✅ 已实现 |
| L3 CLI 与规范 | `@astryxdesign/cli` 安装，`docs tokens` 核实令牌，AGENTS 约定 | ✅ 已安装（见 `web/astryx/`） |
| L4 组件级 | React 19 + `@astryxdesign/core` 150+ 组件（Button/Card/Table/Badge/AppShell...） | 🔜 预留，见「迁移路径」 |

## 二、采用的 Astryx 规范（令牌真实取自 `theme-neutral/dist/theme.css`）

### 语义色（Foundations / color）
Astryx 用 CSS `light-dark()` 实现自动明暗切换，无需 JS 判断：

```
--color-text-primary   light-dark(#000000, #ffffff)
--color-text-secondary light-dark(#474747, #c6c6c6)
--color-accent         light-dark(#1b1b1b, #f1f1f1)
--color-border         light-dark(#00000014, #ffffff1A)
--color-text-success   light-dark(#2f7d33, #90ca90)   ← 已达 4.5:1
--color-text-warning   light-dark(#836700, #eec448)
--color-text-error     light-dark(#c0262c, #ffaea7)
```

控制台映射到 `--ax-*` 前缀，并保留旧变量名（`--accent` 等）作为兼容层，避免改动全部 JS。

### 间距阶梯（spacing，0.25rem 基）
`--spacing-1`=4px ... `--spacing-8`=32px。所有 padding/gap 改用令牌，消除硬编码像素。

### 圆角阶梯（radius，4dp 基）
`--radius-inner`=4px / `--radius-element`=8px / `--radius-container`=12px / `--radius-full`=9999px（chip、主题按钮）。

### 字阶（typography，14px × 1.2^step 几何比例）
`--font-size-sm`=12px / `base`=14px / `lg`=17px / `xl`=20px / `2xl`=24px；字体栈采用 Astryx 的 `Figtree, -apple-system, ...` 并前置中文回退。

### 无障碍（Astryx 明确要求 WCAG 1.4.3）
- 状态徽章改用语义色对（success/warning/error 前景 + muted 背景），亮度对比达标
- 全局 `:focus-visible` 统一双层 focus ring
- `@media (prefers-reduced-motion: reduce)` 关闭所有过渡动效
- 表格行 hover、按钮 disabled/active 反馈

## 三、本次设计优化的具体改动

1. **明暗双主题**：系统跟随 + header「◐ 主题」按钮手动切换 + localStorage 记忆
2. **头部粘性导航**：`position: sticky`，长表格滚动时 tab 常驻
3. **层级提升**：卡片加 `--shadow-low` elevation，去掉原先扁平的纯边框
4. **控件统一**：输入框继承主题色/placeholder 色，按钮统一 disabled/active/过渡动效
5. **危险操作可视化**：`.danger-btn`（删除、批量拒绝）用 error 语义色描边
6. **响应式**：≤760px 时统计栅格 4→2 列、tabs 换行
7. **表格可读性**：列头 `white-space: nowrap`、行 hover 高亮、垂直居中

## 四、组件级迁移路径（L4，未来）

`web/astryx/` 已安装 React 19 + core + CLI + Vite。需要组件化时：

```bash
cd web/astryx
npm run astryx -- component --list         # 查看 150+ 组件
npm run astryx -- component Table          # 查 Table 的 props/最佳实践
npm run astryx -- template dashboard       # 输出 dashboard 页面骨架
npm run astryx -- docs tokens              # 令牌完整参考
npm run astryx -- init                     # 把组件索引写入 AGENTS.md
```

建议按「表格 → 徽章 → 按钮 → 卡片 → AppShell」渐进替换，令牌层已经对齐，替换样式成本极低。
注意：`@astryxdesign/cli` 需用 scoped 名称调用（unscoped `astryx` 会解析到无关包）。

## 五、依赖

```bash
cd web/astryx
npm install @astryxdesign/core @astryxdesign/theme-neutral @stylexjs/stylex react react-dom
npm install -D @astryxdesign/cli vite @vitejs/plugin-react
```

> `web/astryx/node_modules` 已在 `.gitignore` 中排除。静态控制台 `web/index.html` 不依赖 node_modules，可直接由 FastAPI 提供服务。
