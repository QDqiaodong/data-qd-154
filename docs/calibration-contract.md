# 参考台阶校准契约 v1（signal-calibration）

在已发布的 `signal-waveform-fetch` 完整波形包之上，新增独立离线命令
`signal-calibration`：使用者只做了一次**参考台阶试验**（把仪器依次置于若干已知
参考值，记录升程与降程各段的实测响应），本命令据此为每个通道分别建立升程与
降程的校准曲线，并回答两件事：

1. **这次校准是否合格** —— 拟合残差与升降程差是否都在给定容差内；
2. **将来的一次读数能否唯一确定参考值** —— 把读数闭区间反解成曲线上全部可能
   的参考值范围，并区分无解、唯一和歧义。

它只读取真实包内的 `manifest.json` 与验真 `raw.bin`，按原端序、交错和
`raw*scale+offset` 语义流式解码；不连接设备、不读 `samples.ndjson`、不改动源包
或任何既有命令。仅用 Python 标准库。

## 1. 命令与退出码

```sh
signal-calibration --package DIR --steps STEPS.json --certificate CERT.json
```

- 合法试验与读数退出 `0`，**即使某个通道不合格**；不合格只体现在证书里。
- 输入/边界/引用错误、包验真失败或 I/O 失败一律退出 `2`（argparse 用法错误同
  为 `2`）。失败时**保留原有证书字节**，绝不留下半份结果。
- `CERT.json` 不得指向任何输入：不能是 STEPS 文件本身，不能位于 `--package`
  给出的包目录内（不能是 `manifest.json`/`raw.bin` 等），不能是符号链接或目录。
- 输出先用临时文件写满并 fsync，再原子 `os.replace` 到目标，之后 fsync 父目录。

## 2. STEPS.json

≤2 MiB 的 UTF-8 JSON 对象，顶层**恰含** `steps`、`readings`、
`residual_tolerance`、`hysteresis_tolerance`。所有对象拒绝重复键与额外字段；
`NaN`/`Infinity` 拒绝；布尔值不算数字。

### 2.1 数值字面量

`reference`、`low`、`high`、两项容差都是有限的 JSON 数值，允许指数形式
（如 `1.5e3`）。它们按**字面十进制值**精确解释（内部为 `Fraction`），绝不先转成
binary64。约束：

- 尾数数字最多 32 位（去掉符号和小数点后的数字个数）；
- 显式指数绝对值最多 308；
- 容差必须非负。

### 2.2 steps

数组，最多 1024 项，每项恰含：

| 字段 | 约束 |
| --- | --- |
| `start` | 非负整数绝对样本索引（拒绝小数与布尔） |
| `stop` | 非负整数，且 `stop > start` |
| `reference` | 有限 JSON 数值，精确按字面值 |
| `direction` | `up` 或 `down` |

结构约束：

- 按 `start` 排序后，半开样本段 `[start, stop)` 必须**无缝分割** `[0, points)`
  （`points` 取自包 manifest）：首段从 0 开始，后一段恰在前一段 `stop` 处开始，
  末段结束于 `points`；不得重叠、留空或越界。
- 至少 **3 个不同参考值**；每个参考值恰有一个 `up` 段和一个 `down` 段。

### 2.3 readings

数组，可为空（空数组），最多 4096 项，每项恰含：

| 字段 | 约束 |
| --- | --- |
| `id` | 唯一非空字符串标识符（无控制字符，≤128 字符） |
| `channel` | 包内**真实通道名** |
| `direction` | `up`、`down` 或 `unknown` |
| `low` | 有限 JSON 数值 |
| `high` | 有限 JSON 数值，且 `low <= high` |

读数是无方向的响应闭区间 `[low, high]`；`low == high` 是单点读数。

## 3. 实测均值（流式、精确）

- 按现有包契约验真：manifest 十二字段、`meta_sha256` 自洽指纹、`raw.bin` 长度与
  SHA256、端序/交错/标定语义、非有限原值或标定值拒绝。`manifest_sha256` 与
  `raw_sha256` 是 `manifest.json` 与 `raw.bin` **原始字节**的 SHA-256。
- 每项工程读数采用基线解码后的精确 binary64 值（`raw*scale + offset` 后按
  binary64 舍入）。
- 每通道、每台阶按**样本绝对索引**累计，不保留完整波形或完整通道。台阶可跨越
  解码块边界；按绝对索引归属，块边界不改变均值。
- 均值是这些 binary64 值的**精确有理数**平均：内部把每个值按
  `value * 2**1074` 转成整数（binary64 的最小子正规单位），累加后除以
  `样本数 * 2**1074`。与样本如何分块、以何种顺序解码无关。
- 台阶等权：均值只除以该段样本数，拟合时每个台阶权重相同，与段长无关。

## 4. 校准曲线

每个通道、每个方向分别拟合。参考节点是全体台阶参考值的**严格递增**去重列表
`x_0 < x_1 < ... < x_{k-1}`，两个方向共用同一组节点。

- 节点值 `y_i` 是该方向在该参考值台阶上的实测均值。
- 拟合值 `f_i` 取**非递减**序列，使 `Σ (y_i - f_i)^2` 最小（PAVA/等权保序
  回归，按精确有理数实现）。节点间按参考值**线性插值**。
- 残差判据：`max |y_i - f_i|`（两方向所有节点上取最大）。
- 升降程差判据：同节点 `max |f^up_i - f^down_i|`。
- 通道合格当且仅当**两项都不超过**各自容差（`value <= tolerance`，按有理数精确
  比较）。任何正间隙都不会被 epsilon 抹平；恰好等于容差算合格。

## 5. 读数反解

对一个读数闭区间 `[low, high]`，反解是曲线定义域内**全部**满足
`low <= curve(x) <= high` 的参考值 `x` 的集合：

- **不外推**：只在节点域 `[x_0, x_{k-1}]` 内求解；定义域外的读数得空集。
- 曲线是分段线性且（按定义）非递减，但仍可能含**平台**：水平段上响应相同的
  整段参考值都算解，平台两端都保留。
- 方向已知时只用该方向的曲线；`direction = unknown` 时取两条曲线反解结果的
  并集。
- 合并规则：只合并**相交或端点相接**的闭区间；严格正的空隙必须保留，不得填补，
  不得丢掉平台的任一端。
- 分类：空列表为 `outside`；单个零长度区间为 `unique`；其余（多区间，或单个
  正长度区间）为 `ambiguous`。通道不合格时照常输出这些反解诊断。

## 6. 证书

`CERT.json` 是单个 JSON 对象（`sort_keys=True`、缩进 2、末尾换行）：

```
{
  "manifest_sha256": "...",           # manifest.json 原字节 SHA-256
  "raw_sha256": "...",                # raw.bin 原字节 SHA-256
  "channels": [ ... ],                # 按包中通道顺序
  "readings": [ ... ]                 # 按 id 升序
}
```

`channels` 每项：

| 字段 | 含义 |
| --- | --- |
| `channel` | 通道名 |
| `qualified` | 两项容差是否都满足 |
| `residual_max` | 两方向节点最大绝对拟合残差 |
| `hysteresis_max` | 同节点升降程最大绝对响应差 |
| `up` / `down` | 该方向节点数组，参考值升序 |

每个节点对象恰含 `reference`（参考值）、`measured_mean`（实测均值）、`fitted`
（拟合值），均为约分后的 `{"num": 整数, "den": 正整数}`。

`readings` 每项恰含：

| 字段 | 含义 |
| --- | --- |
| `id` | 读数 id |
| `channel` | 通道名 |
| `classification` | `outside` / `unique` / `ambiguous` |
| `intervals` | 起点升序、互不相接的闭区间数组 `[{"low": {...}, "high": {...}}, ...]` |

所有计算所得数值都以约分 `{num, den}` 表示；`den` 恒为正。调整 STEPS 中
`steps`/`readings` 数组顺序不改变证书字节（通道顺序由包 manifest 决定，读数
顺序由 `id` 决定，参考值顺序由数值决定）。

## 7. 验收索引

- 参考节点 0、1、2、3，升程均值 0、4、2、6，降程 3、4、6、8，台阶不等长（含
  降程段更长的情形）。升程拟合为 0、3、3、6；`direction=unknown` 的读数 `[3,3]`
  反解为 `[0,0]` 与 `[1,2]`，`[3,4]` 反解为 `[0,7/3]`（
  `tests/test_calibration.py::AcceptanceScenario`）。
- 恰好等容差判合格、任一项略超即不合格，且两项独立：
  `::ToleranceBoundary`。
- 曲线域外不外推、平台保留两端、跨两曲线保留空隙：
  `::DomainAndGaps`。
- 跨解码块边界的台阶按绝对索引精确归属：`::BlockBoundary`、
  `::ChunkSplitting`。
- 指定方向的单点反解：`::DomainAndGaps::test_directed_single_point_inversions`。
- 坏 payload 摘要、坏 manifest 指纹、非有限载荷、符号链接 manifest 均退出 2 且
  保留旧证书：`::PackageVerification`。
- 真实格式包、非单位 scale/offset、独立 naive 解码逐段核对均值：
  `::NonUnitCalibration`。
- 数组重排字节不变：`::ReorderInvariance`；输入结构/字面量边界拒绝：
  `::TrialValidation`。
- 独立暴力枚举核对保序拟合最优、空隙合并、分类：
  `::IndependentMath`。

## 8. 与既有命令的兼容

新增独立子包 `signal_spectrum.calibration`，不改变离线 DFT、Welch 工作台、波形
采集、越限检索与时钟拼接命令的接口或输出。包验真与流式解码复用固定基线代码
（`signal_spectrum.excursions.package`、`manifest`），不复制一套可能与真实
schema 漂移的弱校验。
