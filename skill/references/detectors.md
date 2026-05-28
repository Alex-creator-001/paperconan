# paperconan detectors reference

每个检测器：**原理** · **典型命中** · **常见误报**。Agent 在向用户解读 finding 之前应该 skim 一遍对应条目。

---

## 跨列关系类 (block-level relation detectors)

### `identical_column`
- **原理**：同一 block 内两列每一行数值完全一致（atol=1e-9）。
- **典型命中**：作者用同一列数据填了两次声称独立的列。
- **常见误报**：极少。如果两列 header 写的都是同一指标（如同一对照组在两张图重复使用），可能合理。

### `constant_offset`
- **原理**：col_b - col_a 在所有行上为同一非零常数。
- **典型命中**：col_b 是 col_a 加了 k 后捏造出来的"实验组"。
- **常见误报**：测量受到固定偏置（如温度补偿）— 但通常文章里会说明。

### `constant_ratio`
- **原理**：col_b / col_a 在所有行上为同一比例（非 1）。
- **典型命中**：col_b 是 col_a 乘了 k 倍后伪造的"处理组"。
- **常见误报**：单位换算（mg → ng × 1000）；剂量梯度时间轴。

### `sum_constant`
- **原理**：col_a + col_b 在所有行上为同一常数 K。
- **典型命中**：百分比对（前/后 = 100）；两组互补造数。
- **常见误报**：真实互补关系如分配比例（合理共存）。

### `exact_linear`
- **原理**：col_b = slope × col_a + intercept，残差 ~0，r > 0.99，且非 identical/offset/ratio。
- **典型命中**：用线性公式从一列推出另一列。
- **常见误报**：物理学/化学上确有严格线性关系的量（吸光度 vs 浓度的标准曲线）。

### `small_diff_set`
- **原理**：col_b - col_a 只取 2-6 个离散值。
- **典型命中**：作者从一组 base 数据派生小幅度扰动得到"独立"实验。
- **常见误报**：定量分级 / 离散刻度测量。

### `many_equal_pairs`
- **原理**：两列 ≥ 50% 行 byte-identical，但不是完全相同（有少量手改痕迹）。
- **典型命中**："9/10 完全一致只改 1 格" 的造假指纹。
- **常见误报**：肿瘤长宽常常相近但本来就独立测量 — 看 figure legend。

---

## 单列模式类 (within-column detectors)

### `arithmetic_progression`
- **原理**：整列等差（diff 恒定，且非 0）。
- **典型命中**：理论 / 模拟生成的对照组被误标为实验组（1, 2, 3, … 整数）。
- **常见误报**：剂量梯度、时间轴、index 列。Agent 看到这条要先确认列名。

### `within_col_value_duplication`
- **原理**：同列内某个具体数值重复出现 ≥ 一半的行数（且不是全相同）。
- **典型命中**："0.208975 在 8 个独立实验里出现 8 次" 的造假。
- **常见误报**：检出限以下截断（LOD）；零计数。

### `within_col_decimal_repetition`
- **原理**：同列中 ≥ 2/3 数值末两位完全一致（如 `.25` / `.75`）。
- **典型命中**：编造数字时不自觉地写同样的小数尾。
- **常见误报**：细胞计数 / 4 视野平均，会天然落在 0.25 步长。

### `rounded_to_half_or_int`
- **原理**：整列 ≥ 70% 末位是 0 或 5。
- **典型命中**：人工随手凑数。
- **常见误报**：量表测量、Likert scale、按 0.5 刻度记录。

### `missing_last_digits`
- **原理**：≥ 20 个数据中，某些末位数字（如 3, 7）从未出现。
- **典型命中**：编造者倾向于写"漂亮"的尾数（避免 3 / 7）。
- **常见误报**：极少。本检测器只在样本量充足时触发。

### `identical_after_rounding`
- **原理**：≥ 4 个 cell 共享同一 1 位小数舍入值，但精确值 ≥ 3 种不同。
- **典型命中**：先写概数再"反向"补全精度的伪精确数据。
- **常见误报**：测量天然在某区间聚集。

---

## 整 sheet 末位/末两位类 (sheet-level digit detectors)

### `last_digit_chi_square`
- **原理**：整 sheet 数值末位数字（1-9）做 χ² 均匀性检验，flag p < 1e-6。
- **典型命中**：编造者末位偏好特定数字（5、0、2 等）。
- **常见误报**：测量受刻度量化（仪器精度有限），并非造假。
- **解读时**：必须配合 `top` 字段看哪个末位被偏向了 — 给用户具体证据。

### `repeated_two_decimal_endings`
- **原理**：整 sheet 末两位高度集中（top 末两位占比 > 5%）。
- **典型命中**：批量编造数字的指纹。
- **常见误报**：单位换算 / 公式派生导致天然出现 `.00` / `.50`。

---

## 跨 sheet 类 (cross-sheet detectors) — **最高优先级**

### `cross_sheet_position_identical`
- **原理**：同一 xlsx 文件的两张 sheet 在 ≥ 15% 同位置上数值 bit-identical（≥3 位小数）。
- **典型命中**：作者复制一整张 sheet 然后改了少量值充当"独立"实验。
- **常见误报**：合理的共享对照组（但 source data 应该明确标注）。
- **怎么解读**：这是 paperconan 最强的信号 — 通常意味着 sheet 之间确实有派生关系。

### `cross_sheet_value_overlap`
- **原理**：同一文件两张 sheet 共享 ≥ 40% 的小数值（不要求位置匹配）。
- **典型命中**：池化 + 重新洗牌伪造独立实验。
- **常见误报**：共享样本量集合 / 同一仪器输出范围。

---

## 在 evidence 里高亮的列怎么对照

每条 finding 的 `evidence.highlight_cols` 是 0-based 绝对列下标（不是 block 内偏移）。配合 `evidence.col_offset` 推断出 evidence 表里的相对位置：

```
local_idx = abs_col - evidence.col_offset
```

HTML 报告已经处理好高亮渲染 — 这段信息是给 agent 想直接引用具体单元格时用的。
