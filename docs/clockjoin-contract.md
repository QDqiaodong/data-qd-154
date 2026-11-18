# 多仪器时钟拼接契约 v1（signal-clock-join）

在已发布的 `signal-waveform-fetch` 完整波形包之上，新增独立离线命令
`signal-clock-join`：以若干仪器间校时锚点为共同约束，把多台仪器各自留下的
完整波形包连接成一个**可追溯、共同时基**的事件拼接数据集。它只消费完整本地
包（`manifest.json` + 验真 `raw.bin`），不读 `samples.ndjson` 作为事实源，
不连接设备，不改动源包或既有命令行为。仅用 Python 标准库。

本命令解决的核心问题：各仪器的采样起点没有共同墙钟，只有若干跨仪器校时观测；
一个候选束里的每条关系单独看可能都可行，但整束未必存在**同一组**来源偏移同时
满足全部关系。工具用精确有理数在整束约束图上判定可行性，并对不可行的束给出
可机验的负环证据，而不是用各仪器独立误差区间的重叠来冒充共同可行。

## 1. 命令与退出码

```sh
signal-clock-join --source JOB.json --output DIR
```

- 成功退出 `0`，`DIR` 内发布 `report.json` 与 `events.ndjson`。
- 非法 JSON/边界/身份、参考或基础约束错误、包验真失败、I/O 失败一律退出 `2`
  （argparse 用法错误同为 2）。**束不相容是正常结果**，不阻止其他束产出。
- 全部源验真和计算完成后才整体发布：先把两文件写入 `DIR.staging`，全部成功
  后再原子 `os.replace` 为 `DIR`；失败清理暂存，绝不留下可被误认为成功的半份
  结果。已有输出目录（文件、目录或符号链接）拒绝覆盖。
- 输出目录不得为任何输入包目录本身，也不得是其祖先或后代（避免写坏源包所在
  路径）。
- 源包只读；单操作者离线批处理，不承诺并发写、进程终止续传或断电恢复。

## 2. JOB.json

≤8 MiB 的 UTF-8 JSON 对象，顶层**恰含** `reference`、`sources`、`anchors`、
`bundles`。所有对象拒绝重复键与未知字段；所有数值拒绝 `bool` 及
`NaN`/`Infinity`；`source_id`、`anchor_id`、`bundle_id`、`event_id`、
`relation_id` 沿用现有非空标识符约束（非空、无控制字符、≤128 字符）。
路径、`source_id` 及其余 ID 一律原样保留，不做去空白或改写。

### 2.1 sources

`1..32` 项，每项恰含 `source_id`、`package`：

| 字段 | 约束 |
| --- | --- |
| `source_id` | 非空标识符，全局唯一 |
| `package` | 非空路径字符串；相对路径按 **JOB 所在目录**解释 |

`reference` 必须等于其中一个 `source_id`；参考来源的偏移固定为 `0`。两个
`source_id` 允许指向同一个包，或指向 `capture_id` 相同的不同包——来源按
`source_id` 区分，**时钟变量与证据彼此独立**，绝不按 `capture_id` 合并。

### 2.2 anchors（基础校时锚点）

`0..512` 项，每项恰含 `anchor_id`、`left`、`right`、`lower_ns`、`upper_ns`。
`left`/`right` 端点恰含 `source_id`、`index`，`index` 是相应包范围内从 `0`
开始的整数（`0 <= index < points`）。每条锚点表达

```
lower_ns <= T(right) - T(left) <= upper_ns
```

`lower_ns`/`upper_ns` 是恰含 `num`、`den` 的有理数对象：`num` 为绝对值不超过
`10^18` 的整数，`den` 为 `1..10^9` 的整数。允许未约分，但**按有理数值判等**；
`lower_ns` 不得大于 `upper_ns`；不提供缺失/无限界形式。`anchor_id` 在基础锚点
中唯一。

### 2.3 bundles（候选束）

`1..128` 项，每项恰含 `bundle_id`、`events`、`relations`；`bundle_id` 全局
唯一。

- `events`：`1..64` 项，每项恰含 `event_id`、`source_id`、`channel`、`index`。
  `channel` 必须是该来源**真实通道名**，`event_id` 在本束内唯一，`index` 在
  相应包范围内。
- `relations`：`0..256` 项，每项恰含 `relation_id`、`left`、`right`、
  `lower_ns`、`upper_ns`。`left`/`right` 是本束 `event_id`，关系同样约束
  `T(right) - T(left)`，`relation_id` 在本束内唯一，上下界沿用锚点的有理数规则。

**所有**锚点端点、事件、引用、包与通道都要验证，即使某束最终不可能，也不能
忽略其坏引用（坏引用导致整体退出 2）。不同束彼此独立：一束的收紧绝不带入
另一束。

## 3. 时间模型与可行性

各来源的未知时钟偏移为 `x_s`，参考来源 `x_ref = 0`；样本时刻严格定义为

```
T(s, i) = x_s + 10^9 * i / sample_rate_s   [ns]
```

令 `delta = 10^9*(i_R/sample_rate_R - i_L/sample_rate_L)`，一条约束
`lower <= x_R - x_L + delta <= upper` 展开为两条差分约束（边 `u->v` 权重 `w`
表示 `x_v <= x_u + w`）：

```
upper 侧：L -> R，权重 upper - delta
lower 侧：R -> L，权重 delta - lower
```

### 3.1 整束可行性（而不是逐关系可行）

一个候选束必须在**全部基础锚点 + 本束全部关系**下使用**同一组**来源偏移。
判定在联合约束图上做（基础边 + 本束关系边）：

- 联合图存在负环 `<==>` 无共同可行偏移 `==>` `impossible`；
- 联合图无负环 `==>` 存在共同可行偏移。

逐关系可行只是必要条件：验收案例中 `B-A=0`、`C-B=0`、`C-A=1` 三条各自可行
（分别对应偏移 (0,0)、(0,1)、(0,1) 等），但它们要求同一组 `(x_A,x_B,x_C)`
同时满足三者，联立即 `x_C-x_A=1` 与 `x_C-x_A=0` 矛盾，因此整束不可行。工具
必须判该束 `impossible`，绝不能把“每条关系都能单独排布”当成整束可行。

### 3.2 基础约束是硬错误

基础锚点自身必须可行（基础图无负环），且每个来源相对参考来源都有**有限**
上下界（基础图中任意两点可达，等价于任意来源到参考和参考到该来源都存在）。任
一不满足即整次输入错误：退出 `2` 且**不发布任何输出**。

### 3.3 最小偏移向量（为何能同时满足本束）

在“本束全部关系加入后”的加强可行域上定义最小向量。对可行系统，令
`dist(s)` 为联合图中从 `s` 到参考 `ref` 的最短距离，则

```
x_s = -dist(s, ref)
```

是**逐来源同时达到可行下界**的解，且每个来源的该分量不大于任何其他可行向量
的对应分量——它是可行域在逐坐标意义下的下确界（差约约束系统的最小解即相反符
号的最短距离解）。因此它不是“分别输出互不相容的单变量边界”，而是同一组满足
全部条件的向量。参考来源恒为 `0`。最小向量覆盖全部 `sources`：未出现在本束
events 或 relations 中的来源仍参与基础全图并输出其最小偏移。

### 3.4 guaranteed / conditional / impossible

- `guaranteed`：**仅基础锚点**允许的每一个偏移向量都已满足本束全部关系（关系
  对基础可行域冗余）。无关系的合法束为 `guaranteed`。
- `conditional`：存在共同可行偏移，但不满足上一前提（需要关系进一步收紧）。
- `impossible`：无共同可行偏移。

实现上，`guaranteed` 用基础图的最短距离判定：`x_R - x_L` 在基础可行域上的
最大值为 `dist(L,R)`、最小值为 `-dist(R,L)`；当且仅当两端极值都已落在
`[lower-delta, upper-delta]` 内，关系才被基础约束蕴含。

### 3.5 精确有理数

所有时间运算与可行性判定使用**精确有理数**（`fractions.Fraction`），归一化
分母为正，`num/den` 输出均约分；刚好相等的闭区间视为可行。脚本内部把所有权重
乘以共同的（正整数）分母，转为整数做 Bellman-Ford，全程无浮点容差。非整数
（如 1/3 ns）边界与“差 1/3 ns”的矛盾都按有理数精确判等。

## 4. 负环证据

不可行束输出可机验的负环，而不是文字诊断或虚构的闭包边。统一语义：

- 一条边表示 `x_to <= x_from + weight`；
- `cycle.edges` 按有向闭环顺序列出 `{kind, id, side, from, to, weight}`，其中
  `kind` 是 `anchor` 或 `relation`，`id` 用原始 `anchor_id`/`relation_id`，
  `side` 是 `lower` 或 `upper`，`weight` 为约分后的 `{num, den}`；
- 每条边都必须来自本束与基础锚点，端点首尾相接，且权重与真实约束一致（按上式
  由 `delta` 与原上下界重建）；
- `cycle.total_weight` 为逐边权重之和，必须**严格小于 0**；可行束 `cycle` 为
  `null`，`impossible` 束 `offsets` 为 `null`。

多条合法负环时任取一条；边的遍历顺序由来源与约束标识规范化，因此同一输入重
跑（包括数组重排）得到**确定**的同一证据。

## 5. 源包验真

只读每个包内的 `manifest.json` 与 `raw.bin`；两者为符号链接即拒绝。复用固定
基线的真实发布语义（`signal_spectrum.excursions.package`/`manifest`）：

- manifest 字节 ≤64 KiB，按 schema_version=1 的**十二字段真实发布包**识别，拒绝
  重复键、非有限数字、缺失/额外字段、非法 dtype/端序/通道配置；
- 用 manifest 自身的 `config`、`channels`、`raw_bytes`、`raw_sha256` 重建规范
  meta 对象并计算规范 SHA256，必须等于 `meta_sha256`；`raw_bytes` 必须等于
  `points*通道数*字宽`；
- `raw.bin` 流式读取一遍：累计长度必须等于 `raw_bytes`，全量 SHA256 必须等于
  `raw_sha256`；按现有端序、point-major 通道交错与 `value = raw*scale + offset`
  语义逐点标定；**即使未被候选事件引用**，非有限原值或标定值也整批拒绝。
- 单包沿用基线 256 MiB 上限，全部源 `raw_bytes` 合计 ≤1 GiB。

`report.json` 中每个来源的 `manifest_sha256` 是**输入 manifest 原字节**的
SHA256，不是重新序列化的摘要。

## 6. 输出

`report.json` 顶层恰含 `schema_version`（`1`）、`reference`、`sources`、
`bundles`：

- `sources` 按 `source_id` 排序，每项记录 `source_id`、`capture_id`、
  `sample_rate`、`points`、`raw_sha256`、`manifest_sha256`；
- `bundles` 按 `bundle_id` 排序，每项恰含 `bundle_id`、`status`、`offsets`、
  `cycle`。`offsets` 是按 `source_id` 排序的 `{source_id, offset_ns}` 数组或
  `null`；`cycle` 是上述 `{edges, total_weight}` 对象或 `null`；`status` 仅
  `guaranteed`/`conditional`/`impossible`。

`events.ndjson` 只含**可行束**的事件，每行恰含 `bundle_id`、`event_id`、
`source_id`、`channel`、`index`、`time_ns`、`value`，按 `bundle_id`、
`event_id` 排序；`time_ns` 由本束最小偏移向量计算，`value` 为与现有标定语义
一致的有限 binary64 值。原始样本不做插值、重采样或阈值筛选。同一输入 JSON 的
数组重排不改变任何输出字节（顺序完全由 `source_id`/`bundle_id`/`event_id` 与
约束标识决定）。不写墙钟时间或临时路径。

## 7. 资源预算与测量方法

- 最多 32 来源、512 基础锚点、128 候选束，每束 ≤64 事件、≤256 关系；原始合计
  ≤1 GiB；峰值 RSS ≤192 MiB，**不得整包或整通道载入内存**。
- 每个源 `raw.bin` 只经固定 1 MiB 读缓冲流式解码一遍；内存只保留被可行束引用
  的事件样本值（≤128*64 个）与单块标定缓冲，绝不整包或整通道常驻。
- 测量方法（`tests/test_clockjoin.py::ResourceBound`）：三个约 80 MiB 的 float32
  包（合计 240 MiB，显著大于 192 MiB 上限，测试断言语料确实超过上限）由 CLI 子
  进程流式处理。为保证读数归属该命令而非早先更重的子进程，用一个全新驱动进程只
  启动这一个 CLI 子进程，再用 `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`
  （Linux 单位 KiB，对已回收子进程取峰值）读出其峰值。本机实测峰值约 44 MiB
  （含解释器），远低于 192 MiB 上限。不用小数据测试冒充资源证据。

## 8. 验收覆盖索引

- 三来源 A/B/C：基础锚点令 `B-A`、`C-A` 位于 `[0,10]` ns。束要求
  `B-A=0`、`C-B=0`、`C-A=1`（逐条可行、整束 `impossible`，负环总权重为
  `-1`）；束要求 `B-A=5`、`C-B=2`（`conditional`，`A=0,B=5,C=7`）。
  `tests/test_clockjoin.py::AcceptanceScenario`。
- 无关系或仅要求 `B-A∈[0,10]` 为 `guaranteed`；给基础增加 `B-A=5` 后含
  `B-A=5,C-B=2` 的束仍给 `B=5,C=7`，且前一个不相容束不能污染它。同上。
- 负环由**独立检查器**按原输入重建各侧边权并验证闭合/负和，不允许仅断言
  `cycle` 非空（`rebuild_cycle_edges`）。
- 不同 `sample_rate` 造成的 1/3 ns 级非整数边界、刚好相等与差 1/3 ns 的矛盾、
  冗余及反向锚点、多条约束共同收紧偏移、无界/矛盾基础锚点、同 `capture_id` 不
  同 `source_id`、数组重排字节不变：
  `tests/test_clockjoin.py::RationalBoundaries`、`::StructuralCases`。
- 用独立小规模枚举核对整束可行性、最小偏移与 `guaranteed` 分类：
  `tests/test_clockjoin.py::IndependentEnumeration`。
- 真实发布 manifest（不同 `sample_rate`、同名通道、非单位 scale/offset、大小端
  多通道原始数据）逐行核对 `time_ns`/`value`；raw 摘要错、meta 指纹错、未引用位
  置的非有限 float32、坏事件引用均拒绝，已有成功输出保持原字节：
  `tests/test_clockjoin.py::PackageVerification`。

## 9. 与既有命令的兼容

新增独立子包 `signal_spectrum.clockjoin`，不改变离线 DFT、Welch 工作台、波形采集
与越限检索命令的接口或输出。包验真/流式解码/安全目录发布复用固定基线代码
（`signal_spectrum.excursions.package`、`manifest`、`sources` 语义），不复制一套
可能与真实 schema 漂移的弱校验。
