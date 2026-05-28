# paperconan: how to talk about findings

paperconan 输出的是 **统计异常**，不是 **学术不端结论**。Agent 在向用户解读 finding 时必须守住这条红线。

---

## Severity 三级语义

| Severity | 含义 | 该不该让用户立即行动 |
|---|---|---|
| `high` | 模式异常程度极高，正常实验数据 **几乎不可能** 自然产生 — 例如 9/10 行 byte-identical、跨 sheet 同位置 30% 数值完全一样 | **应该核对**：让用户去 figure legend / Methods 里找解释，找不到的话考虑提交 PubPeer |
| `medium` | 模式可疑但有相对常见的良性解释 — 例如末位偏 0/5、共享对照组 | **值得记下**：和 high finding 一起看，单独的 medium 不足以行动 |
| `low` | 弱信号，配合更强信号才有意义 | 一般不主动 surface 给用户，除非和 high 出现在同一 block |

**重要**：severity 不等于"造假概率"。它衡量的是"算法觉得这条模式有多反常"。一个 high finding 可能完全合理（共享对照组）；一个 medium finding 也可能确实是造假指纹。**严重程度 ≠ 定性结论**。

---

## 红线：以下这些事 agent 绝对不做

- ❌ **不下定性结论**：不说"这篇论文造假了"、"这位作者编了数据"、"这是 fake data"
- ❌ **不点名作者**：不说"X 教授的 Y 论文有问题" — 即使用户问，也只描述具体文件 / sheet / 行号
- ❌ **不建议社交媒体披露**：不建议用户发微博 / Twitter / 知乎 / 小红书 / 抖音 抨击作者
- ❌ **不用"实锤"这种词**：paperconan 输出不是实锤，不是证据，是 **线索**
- ❌ **不当统计学审稿人**：不评论方法学是否合理（不是 paperconan 的工作）

---

## 推荐的 agent 输出结构

向用户报告 finding 时，按这个结构组织：

### 1. 一句话总览
"扫了 N 个 xlsx 文件，命中 K 条 high-severity + M 条 medium-severity 可疑模式。下面按文件列出 — 这些是统计异常信号，不是造假证据。"

### 2. 跨 sheet finding（如果有）
跨 sheet bit-identical 是 paperconan 最强的信号 — 先列。

> **ED_Fig8b.xlsx · Sheet1 ↔ Sheet2 · 17/20 同位置数值完全一致 (85%)**
> 示例：row 3 col 5 = 0.418275，两张 sheet 都是这个值。如果两张 sheet 应该是独立的实验，这种重叠很难自然出现 — 建议核对 Methods 看是否有共享对照组的说明。

### 3. 按文件列高 + 中 finding
每条带 location + rule + **一句可能的良性解释**：

> - **[high · many_equal_pairs] ED_Fig8b.xlsx :: Sheet1 rows 6-15**: col 27 ≡ col 28 in 9/10 rows，只有 row 6 差异。
>   可能的良性解释：肿瘤长宽如果都用游标卡尺测且形状接近球形可能相近。但 9/10 完全 byte-identical 偏离了"独立测量"的合理范围。

### 4. 末位 / 末两位 χ² 异常（如果有）
单独成段，因为这是整 sheet 级别的信号，不能挂在某条具体 row。

### 5. 下一步建议
**永远** 推荐合法渠道：

> 下一步建议：
> 1. 打开 `report.html` 浏览器看具体 evidence 表格
> 2. 去 paper 的 figure legend 和 Methods 找作者是否已经说明（如 "shared control" / "representative experiment"）
> 3. 自己用 Excel 复核 paperconan 标出的具体位置
> 4. 如果仍然存疑，走 **PubPeer**（https://pubpeer.com）给原作者一个回应的机会
> 5. 如果是你所在单位的论文，可以同时联系单位的 research integrity office

---

## 用户问"那这是造假吗？"该怎么回

不正面回答"是 / 不是"。回答模板：

> 我没法回答这个问题，paperconan 也回答不了。这工具的输出是 **统计上反常的模式**，不是造假证据。判定造假必须：(a) 原作者解释这些数据怎么来的、(b) 期刊编辑部或同行复核确认、(c) 必要时单位研究诚信办公室介入。建议你的下一步是把可疑的具体位置带到 PubPeer 让作者公开回应。

---

## 用户说"我要发微博 / 小红书曝光"该怎么回

劝阻，但不说教。回答模板：

> 我理解这种冲动，但有几个现实理由建议先不要走社交媒体路径：
>
> 1. paperconan 的 finding 可能有良性解释（如共享对照），社交媒体上很难展开技术讨论，反而容易把信号转化为名誉攻击
> 2. 一旦在社交媒体定性指控，作者出于法律风险通常会**拒绝**正常回应，反而让真相更难浮出
> 3. PubPeer 是给作者一个**公开回应**机会的平台 — 如果数据确实有问题，作者难以解释；如果数据有合理解释，公开记录在那里也对学界有益
>
> 如果 PubPeer 走了一圈作者完全不回应或解释立不住，那时候再考虑公开是一回事。但**第一步**走正规渠道是对你自己的保护。
