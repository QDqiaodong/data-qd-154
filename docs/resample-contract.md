# 多通道波形重采样契约 v1（signal-resample）

在已发布的 `signal-waveform-fetch` 完整波形包之上，新增独立离线命令
`signal-resample`：把一个**完成态多通道波形数据集**重采样到新的共同采样时间网格，
产出逐通道的派生数据集，供需要把不同采样设备的数据放到同一时间轴上的离线分析
流程使用。仅用 Python 标准库；不发起网络请求，不读 `samples.ndjson` 作为事实源，
不改写输入，也不改变任何既有命令（DFT、Welch、采集、越限、时钟拼接、校准）的
接口与输出。

## 1. 命令与退出码

```sh
signal-resample --input DATASET --target-rate HZ --dest DIR
```

- 成功退出 `0`，`DIR` 内恰含 `samples.f64le` 与 `lineage.json` 两个文件，并在
  stdout 打印一行 JSON 摘要。
- 非法输入、源包验真失败、目标输出越界、I/O 失败一律退出 `2`（argparse 用法
  错误同为 `2`）。不存在部分成功：失败时 `DIR` 不出现。
- `TARGET-RATE` 为十进制正整数 Hz，范围 `1..10^9`；`bool`、小数、非数字都拒绝。
- `DIR` 已存在（文件、目录或符号链接）拒绝覆盖；`DIR.staging` 已存在时拒绝
  复用（残留需人工清理）。`DIR` 不得是输入数据集本身、其后代或祖先。
- 暂存目录固定为 `DIR.staging`，与 `DIR` 同一父目录，因此最终 `os.replace`
  不会跨文件系统。全部写入、fsync 与摘要复核完成后才整体切换；失败清理本次
  暂存内容，输入字节保持不变。

## 2. 输入数据集验真

输入必须是**固定基线实际发布的完整波形包**：`manifest.json` + `raw.bin`。
复用仓库已有的 schema_version=1 包 manifest 解析、`meta_sha256` 复核、
raw 长度/SHA256 校验与 point-major 通道解码/标定语义（`excursions.manifest`
与 `excursions.package`）：

- `manifest.json` 字节 ≤64 KiB，恰含十二个发布字段，拒绝符号链接、重复 JSON
  键、`NaN`/`Infinity`、额外或缺失字段、非法 dtype/端序/通道配置；
- 用 manifest 自身的 `config`/`channels`/`raw_bytes`/`raw_sha256` 重建规范
  meta 并计算规范 SHA256，必须等于 `meta_sha256`；
- `raw.bin` 不是符号链接，流式读取一遍：长度等于 `raw_bytes`，全量 SHA256
  等于 `raw_sha256`，不完整交错帧整批拒绝；
- 按现有 `raw*scale + offset` 的 binary64 语义解码，非有限原值或标定值整批
  拒绝。`samples.ndjson` 从不读取。

## 3. 时间网格与支持条件

令源采样率 `Fs`、目标采样率 `Ft`，`g=gcd(Fs,Ft)`、`p=Ft/g`、`q=Fs/g`；输出时间
点固定为**全局** `t=m/Ft` 秒（`m` 为非负整数），其源坐标为 `x_m = m*Fs/Ft =
m*q/p`。所有 `m` 与相位直接由整数比确定，**不使用逐步浮点累加**：

- 输出点 `m` 的核窗口为整数源索引 `j` 且 `|j - x_m| <= R`；
- 仅当完整窗口落在 `[0, points-1]` 内才输出，即闭区间条件
  `R <= m*q/p <= points-1-R`：
  首个 `m = ceil(R*p/q)`，末个 `m = floor((points-1-R)*p/q)`；
- 无有效输出点（`points < 2R+1` 或区间为空）时拒绝转换；
- 目标输出点数最多 `5,000,000`，输出文件（`output_points*通道数*8`）最多
  `640 MiB`，越界在解码前拒绝。

`lineage.json` 记录首个 `m`、末个 `m`、输出点数与逐点源坐标范围
（`source_coordinate_range.first/last`，精确约分有理数）以及时间映射
（`t_m = m/Ft`，步长 `1/Ft`）。

## 4. 滤波核

每通道独立应用对称有限长窗化 sinc 低通核，固定半径 `R=16` 个源样本：

- 截止频率 `fc = 0.5*min(1, Ft/Fs)` cycles/input-sample；
- `d = j - x_m`，仅使用 `|d| <= R` 的整数输入样本 `j`；
- `sinc(x)=sin(pi*x)/(pi*x)`，`sinc(0)=1`；
- Blackman 窗 `w(d)=0.42+0.5*cos(pi*d/R)+0.08*cos(2*pi*d/R)`；
- 未归一化权重 `h(d)=2*fc*sinc(2*fc*d)*w(d)`；
- 每个输出值为 `sum(input[j]*h(d)) / sum(h(d))`，逐点归一化以保持常量信号；
  分母为零或任何输出非有限则整次转换失败。

权重只依赖精确分数相位 `phase=(m*q) mod p`。实现按需求值：小 `p` 时复用
固定预算（2048 项）的相位权重缓存；预算用尽或 `p` 极大时直接逐点计算，绝不按
`p` 无界分配。两条路径给出相同的输出定义和摘要，且相位始终是整数，不用近似
浮点相位换取缓存命中。

## 5. 输出格式

`samples.f64le`：point-major、通道顺序不变的 little-endian IEEE-754 float64
标定值，共 `output_points * 通道数` 个。值是源 `raw*scale+offset` 已标定的结果，
消费者**不得再次套用**该变换（见 `lineage.json.calibration`）。

`lineage.json`：UTF-8、键排序稳定、缩进 2、末尾换行，不含临时路径或墙钟时间；
相同输入与参数产生逐字节相同的文件与样本字节。使用独立的派生波形 schema，
不冒充原采集 manifest，也不要求旧波形命令接受派生目录。至少包含：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | `1` |
| `kind` | `derived-waveform` |
| `source_capture_id` | 源包 `capture_id` |
| `source_manifest_sha256` | 源 manifest 文件字节 SHA256 |
| `source_raw_sha256` | 源 `raw.bin` SHA256 |
| `source_points` | 源样本点数 |
| `source_sample_rate` / `target_sample_rate` | `Fs` / `Ft`（Hz） |
| `ratio_p` / `ratio_q` / `ratio_gcd` | 约分比与 `gcd` |
| `kernel` | 窗、半径、抽头数、截止频率、归一化与公式 |
| `first_output_index` / `last_output_index` | 首/末全局 `m` |
| `output_points` | 输出点数 |
| `output_channels` | 按源顺序的通道名 |
| `output_bytes` | `samples.f64le` 字节数 |
| `output_sha256` | `samples.f64le` 的 SHA256 |

另有 `sample_layout`、`calibration`、`source_coordinate_range`、`time_mapping`、
`support` 说明布局、标定状态、逐点源坐标范围与支持条件。

## 6. 流式与资源边界

- 输入按帧流式解码；每通道只保留产生下一输出点所需的有界邻域（长度
  `2R+1` 加一个解码块），以及一个定长输出块（最多 8192 点）后立即写入文件。
  滤波状态随通道数与 `2R+1` 邻域有界，不随载荷或点数增长。
- 不保留全量输出或全量 polyphase 表；相位缓存上限 2048 项（<2 MiB）。
- 峰值常驻内存预算 96 MiB。`tests/test_resample.py::ResourceBound` 用 240 MiB
  的合成 float32 载荷（显著大于预算）在子进程中实测峰值 RSS；本机实测约
  23 MiB（含解释器）。
- 全部输出写完后重新读取 `samples.f64le` 复核 `output_sha256`，暂存目录内容
  核对恰为两文件后，才 `os.replace` 切换为 `DIR`。已有目标拒绝覆盖，符号链接
  目标拒绝，注入输出写入或摘要阶段故障时不留有效 `lineage.json` 或完成态目录
  （测试钩子 `SIGSPECTRUM_RS_FAIL=after_samples|after_lineage`）。

## 7. 验收覆盖

`tests/test_resample.py` 覆盖：常量多通道输入在目标高于/低于/等于源采样率时的
精确增益、输出长度与首个 `m`；整数与非整数比（含 44,100→16,000 Hz）与独立小规模
`Fraction` 参考实现逐点一致（≤1e-12）；48,000→16,000 Hz 的 3,000 Hz 通带幅度
误差 ≤0.02、18,000 Hz 高频 RMS < 输入 RMS 的 0.01；两端支持不足不产生输出且边界
严格等于闭区间条件；跨解码块的多通道帧不串位；深度抽取（`q=100,000`）跨块正确；
坏摘要、重复键、非法目标率、点数/大小超限、非有限原值/标定值均退出 2 且目标不
出现、输入字节不变；`output_sha256` 逐字节核对、已存在目标拒绝覆盖、注入 I/O
故障无完成态目录；以及超预算载荷的子进程峰值 RSS 测量。
