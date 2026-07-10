# 报告与调参

## 报告怎么读

> **先分清两种报告。** `paperconan <dir>` 直接生成的 `audit/report.html` 是**确定性检测器的原始信号 / 人工复核工作台**——它按设计就含**大量 false positive**（共享对照、重绘坐标轴、单位换算、派生列、固定分母比值、四舍五入网格……多数命中都有完全良性的解释），而且**不代表任何结论**，不适合当作成品直接看或对外给出。
>
> **要得到一份正规、可读、经过判断的报告，请搭配 AI Agent + skill 使用**（见 [README › 快速开始](../README.md#快速开始推荐agent--skill)）：检测器只产出可复现的原始信号，Agent 在其上逐条判定（对照原表、图注、Methods，排除良性解释，再做对抗式复核），最后生成[判定后报告](#判定后-html-报告)。纯 CLI 拿不到这一步——判定本身需要一个会读上下文、会推理的 Agent 在环里。

`report.html`（分诊工作台）：顶部摘要 + "如何阅读本报告"说明 + 左侧 severity/detector/文件/关键词过滤 + finding 卡片 + last-digit histogram + cross-sheet 专段。启用 `--images` 后，所有登记资产保留在 `scan.json image_assets[]`；只有显式增加 `--image-diagnostics` 时，工作台才会显示可选的 `image_findings[]` 提示。提示不是完整复核清单，没有提示也不代表图像问题已解释。为便于分诊，误报偏多的 **low 级信号默认折叠**（左侧一键展开），cross-sheet 等重点信号始终可见。建议顺序：

1. 先看 `scan_errors` —— 解析失败或超大文件被跳过时，不能解读成"没问题"。
2. 先看跨 sheet / 跨文件重复，再看列关系，最后才看 within-column。
3. 对降级为 low 的 finding，核 `likely_benign` / `false_positive_context` / `prefilter_reason` 是否成立。
4. 打开原始表，按 evidence 的文件、sheet、行列复核。
5. 再读 figure legend 和 Methods，确认 shared control / 重复展示 / 单位换算 / 派生指标。

（若某张密集/高相关表触发了海量成对信号，报告会按 severity 保留每个 block 的前若干条并在顶部提示省略数量，可用 `PAPERCONAN_MAX_FINDINGS_PER_BLOCK` 调整，见 [命令行与库参考 › 内存 / 输出保护](cli.md#内存--输出保护)。）

`scan.json` 完整结构见 [`references/output-schema.md`](../skills/paperconan/references/output-schema.md)。

## 自适应图像复核

图像语义复核属于外部多模态 Agent 工作流，PaperConan 不配置模型 API、密钥或 provider
SDK，也不声称自主完成语义判断。标准顺序是：

1. 运行 `paperconan <input-dir> --images`；只有需要确定性辅助提示时才增加
   `--image-diagnostics`。
2. Agent 先确认自己能否打开本地图像，再读取每个 `image_assets` 记录。
3. 先看整图，理解面板标签、通道、处理步骤、共享对照、inset、图注和 Methods；小面板或
   未解决细节再使用原始像素裁剪。
4. 每个资产必须且只能记入 reviewed、unresolved、unreadable 或 deferred 中的一项。
5. Agent 可以在 `image_findings` 为空时用 `image_refs` 写入图像 finding；这类
   Agent-only finding 与数值 finding 放在同一个 `verdict.json findings[]`。

没有本地图像能力时，Agent 应写
`image_review.status: "unavailable_no_multimodal"`，说明图像语义复核未完成，并继续数值
复核。`image_review.status: "completed"` 表示覆盖记账完成，不表示每个图像问题都已解释。

## 误报控制：profiles 和 prefilter

检测器先产出原始 signal，`--profile` 再决定怎么处理常见误报。默认 `review`。

| profile | 行为 | 什么时候用 |
|---------|------|------------|
| `review` | 降级疑似误报，但保留可见 | 日常审计默认 |
| `forensic` | 不做降级，保留原始 severity | 怀疑默认过滤太保守、要复核原始信号 |
| `triage` | 同 `review` 判断，但把疑似误报隐藏 | 批量初筛、只想要最短清单 |

降级 / 隐藏的 finding 会带 `profile_action`（`kept` / `demoted` / `hidden`）、`false_positive_context`（如 `axis_or_scan_column`、`derived_or_unit_conversion`、`same_data_replot_or_duplicate_upload`、`omics_or_large_matrix_boundary_flood`）、`prefilter_reason` 和 `prefilter_flags`。

**重要：`review` 下的 low severity 可能是过滤器的意见，不是检测器原始判断。** 拿不准时重跑 `--profile forensic`。标签到检测器的反查表见 [`references/detectors.md`](../skills/paperconan/references/detectors.md)。

## 判定后 HTML 报告

默认的 `audit/report.html` 只展示确定性检测器输出；它不会替你判断论文。若你已经按 skill 的
[`references/adjudication-tiers.md`](../skills/paperconan/references/adjudication-tiers.md) 和
[`references/report-templates.md`](../skills/paperconan/references/report-templates.md) 写好了 `verdict.json`，可以再生成一份**判定后报告**：

```bash
paperconan report audit/scan.json --verdict verdict.json --out adjudication.html
```

流程是一套：**Agent 写判断 → `paperconan report` 渲染**。渲染器**对任何 verdict 都输出同一种高保真版式**（论文头 + Tier/impact/review 徽章 + 每条 finding 的独立卡片 + 紧跟其后的 evidence）——README 顶部那份示例报告就是这条命令的直接产物，没有任何私有管线。数值证据和图像证据出现在同一 finding 清单和同一 HTML 中，不另建图像报告。

`verdict.json` 的**主形态**是带 `findings` 数组的论文级对象（每条 finding 各带 `finding_ref` / `suspicion_tier` / `impact_scope` / `review_status` / `report_md`，图像 finding 还可带 `finding_type: "image"` 与 `image_refs`，论文级另有 `paper_conclusion` / `overall_impact` / `review_note` / `image_review`）；**单条 finding 只是"列了一条"**，同样富渲染，不再是旧版朴素排版。完整 schema 与例子见 [`references/adjudication-tiers.md`](../skills/paperconan/references/adjudication-tiers.md) › "Multiple Findings In One Paper" 和 [`references/report-templates.md`](../skills/paperconan/references/report-templates.md) › "Adaptive Numeric And Image Report"。旧的扁平 `report_md` + `finding_refs` 形态向后兼容，现在也会渲染成同样的高保真版式。适合单篇论文复核或批量审计后的归档。

图像 evidence 只会从 `scan.json image_assets[]` 登记且位于审计 artifact 根目录下的有界
preview 读取并内嵌；`verdict.json` 不能提供任意本机路径。报告中的预览用于复核定位，
Agent 的小区域判断仍应回到登记的原始像素资产。总内嵌预算由
`PAPERCONAN_MAX_IMAGE_EVIDENCE_MB` 控制。

注意：`paperconan report` 是本地、公开、无私有依赖的渲染器；不读取 Postgres、Blob、云端队列或任何
`recheck/` 私有缓存。真实论文 PDF、截图、主图等材料若要展示，应由使用者在自己的审计目录中合法保存并另行归档。
