# data-qd-154 · 离线信号频谱分析

Bounded real-signal DFT with window gain and energy evidence。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/signal-spectrum --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/signal_spectrum/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 连续采样 Welch 工作台（signal-spectrum-workbench）

旧离线命令保持不变；另提供 SQLite 持久化工作台，支持现场采样更正、封口与崩溃恢复，
仅用标准库。数据契约见 [工作台契约](docs/workbench-contract.md)。

```sh
.venv/bin/signal-spectrum-workbench init  --db run/wb.db --config fixtures/workbench/config.json --report run/wb-report.json
.venv/bin/signal-spectrum-workbench apply --db run/wb.db --source fixtures/workbench/request-01.json
.venv/bin/signal-spectrum-workbench apply --db run/wb.db --source fixtures/workbench/request-02.json
.venv/bin/signal-spectrum-workbench status --db run/wb.db
```

- 样本按非负绝对索引保存数值或删除标记（`null`）；`seal` 单调推进并冻结样本与缺口，
  封口后的写入整批拒绝、整批回滚；`horizon` 是写入高水位与 seal 的最大值且不回退。
- 窗起点 `s=mH`，仅列 `s+N<=horizon` 的整窗，逐窗给出 `complete/gap` 与 `sealed/open`；
  缺口窗不补零、不进平均。`live_psd` 平均全部完整窗，`sealed_psd` 只平均封口完整窗，
  无合格窗时为 `null`。
- 每个全新接受的请求使 `generation` 加一；请求身份是源文件完整字节 SHA256，同 ID 同字节
  重试不加代，同 ID 异字节拒绝。校验失败退出 2 且不动样本/前沿/报告。
- 接受请求先在单个 SQLite 事务内提交（含该代完整报告文本），再原子替换报告文件并确认
  `published_generation`。提交与替换之间、替换与确认之间的进程终止都由下一条命令自动
  恢复，恢复只依赖数据库、不依赖原请求文件；`status` 分列 `committed_generation`、
  `published_generation` 与 `pending`，恢复失败时如实报待发布并退出 2。
- 可运行的故障注入与恢复/重试演示：`PYTHONPATH=src python3 examples/workbench_recovery_demo.py`
  （测试钩子 `SIGSPECTRUM_WB_CRASH=after_commit|after_replace`）。

## HTTP 原始波形采集（signal-waveform-fetch）

新增独立子包 `signal_spectrum.waveform`：通过真实 HTTP 把测量设备的一次完整原始采集
保存为可追溯数据包，不读写 Welch 工作台，不产生 writes、seal、PSD 或请求代际状态。
协议、一致性通道、检查点与故障注入见 [波形采集契约](docs/waveform-fetch-contract.md)。

```sh
# 终端 1：离线、真实 TCP/HTTP 的设备模拟器（也支持 --no-lease 的仅 ETag 设备）
PYTHONPATH=src python3 -m signal_spectrum.waveform.device_cli --port 18080 --points 64
# 终端 2：采集并发布一个完整验真的数据包
PYTHONPATH=src python3 -m signal_spectrum.waveform.cli \
  --url http://127.0.0.1:18080 --output run/wave-pkg
# 可选：导出能被旧 signal-spectrum 直接消费的 analysis.json（前 M 个标定值）
PYTHONPATH=src python3 -m signal_spectrum.waveform.cli \
  --url http://127.0.0.1:18080 --output run/wave-pkg-analysis \
  --channel 0 --analysis-count 4
# 进程终止后用同一输出路径续传
PYTHONPATH=src python3 -m signal_spectrum.waveform.cli \
  --url http://127.0.0.1:18080 --output run/wave-pkg --resume
```

- 两种可证一致的通道：仅 ETag 设备对每块做 `If-Match` 条件读取并在收尾再核 meta；
  lease 设备用 `POST /leases` 固定不可变快照，过期（410）则整体重启，绝不拼接新身份。
- 每块严格核对 206/Content-Range/ETag/字节数，流式哈希并比对设备宣告的全量 SHA256；
  弱 ETag、200 忽略 Range、错区间、截断、超长、身份冲突都不能发布成功包。
- 解码支持 int16/uint16/float32 与大小端、point-major 多通道交错与逐点标定；工作缓冲
  上限 1 MiB，不把完整载荷或完整通道数组装入内存；原始数据上限 256 MiB。
- 暂存目录 `DIR.staging` 与 fsync 检查点绑定 URL、完整 meta、ETag、偏移与前缀 SHA，
  支持 `--resume`；篡改已承诺前缀直接报错，已发布目录拒绝覆盖，失败只保留可辨识暂存。
- 预算参数（`SIGSPECTRUM_WF_BLOCK_RETRIES`、`SIGSPECTRUM_WF_RESTARTS`、
  `SIGSPECTRUM_WF_TIMEOUT`）与崩溃钩子（`SIGSPECTRUM_WF_CRASH`）公开且有限，不存在
  无限重试。
- 离线演示（真实 TCP/HTTP，无 mock）：
  `PYTHONPATH=src python3 examples/waveform_fetch_demo.py`。

## 越限检索档案（signal-excursions）

新增独立子包 `signal_spectrum.excursions`：把一组已发布的完整波形包构建成不可变、
自足的越限检索档案，长期离线查询测量值连续超限的极大样本段。不连接设备、不以
`samples.ndjson` 为事实源、不改动源包与既有工作台。档案格式、索引与计数语义见
[越限检索档案契约](docs/excursions-contract.md)。

```sh
# SOURCES.json：{"sources": [{"source_id": "s1", "package": "run/wave-pkg"}]}
PYTHONPATH=src python3 -m signal_spectrum.excursions.cli build \
  --sources SOURCES.json --archive run/exc-archive
PYTHONPATH=src python3 -m signal_spectrum.excursions.cli query \
  --archive run/exc-archive --source s1 --channel 0 \
  --start 0 --stop 64 --op ge --threshold 0.5 --limit 10
```

- build 严格验真 schema_version=1 的 manifest（含 `meta_sha256` 自洽指纹）与
  raw.bin 的长度/SHA256，按现有端序、交错与 scale/offset 语义流式标定为
  binary64 快照；叶段 ≤256 样本、扇出 64 的持久区间摘要存于 SQLite。
- query 只用档案（源包可删）：返回 `[start,stop)` 内全部极大连续命中段及
  `peak_value`/`peak_index`（绝对样本索引），`--limit K` 给前 K 个完整段与
  `has_more`；摘要可判定的整段不读逐点值，输出实际
  `node_reads`/`leaf_reads`/`value_reads`。
- 最多 32 个源包、合计 ≤1 GiB；build 与 query 峰值 RSS ≤192 MiB；成功退出 0，
  非法输入、源校验、档案损坏/版本不支持或 I/O 失败退出 2。

## 多仪器时钟拼接（signal-clock-join）

新增独立子包 `signal_spectrum.clockjoin`：把多个已发布的完整波形包与仪器间
校时锚点连接成可追溯的事件拼接数据集。各仪器采样起点无共同墙钟，工具用**精确
有理数**在“全部基础锚点 + 本束全部关系”的联合约束图上判定整束是否使用同一组
来源偏移；不以各仪器独立误差区间的重叠冒充共同可行。不读 `samples.ndjson`、
不连接设备、不改动源包与既有命令。契约见
[时钟拼接契约](docs/clockjoin-contract.md)。

```sh
# JOB.json：reference/sources/anchors/bundles（相对 package 路径按 JOB 目录解释）
signal-clock-join --source JOB.json --output run/joined
# 离线可运行示例（临时目录内造三包并发布）
PYTHONPATH=src python3 examples/clock_join_demo.py
```

- 每个候选束在**同一组**来源偏移下判定；逐关系可行不等于整束可行。不可行束
  输出**可机验负环**（真实闭环、逐边权重与原始约束一致、总权重严格为负）；
  可行束输出逐来源同时达到可行下界的最小偏移向量，并区分 `guaranteed`
  （基础锚点已蕴含全部关系）与 `conditional`。
- 基础锚点矛盾或任一来源相对参考无有限上下界属整次输入错误（退出 2、不发布）；
  束不相容是正常结果，不阻止其他束产出。
- 复用固定基线包验真与流式解码（manifest 十二字段、`meta_sha256` 自洽指纹、
  raw 长度/SHA256、端序/交错/scale-offset），manifest ≤64 KiB、单包 ≤256 MiB、
  合计 ≤1 GiB；`report.json`/`events.ndjson` 全部验证完成后才原子发布，已有输出
  目录拒绝覆盖。最多 32 来源、512 锚点、128 束，峰值 RSS ≤192 MiB。

## 当前能力和样例
# Bounded one-sided real-signal spectrum v1

离线采样分析者输入4..512个有限数值样本、正整数Hz sample_rate、rectangular/hann窗及布尔remove_mean。样本必须由调用方确保等间隔且无丢样；未提供时间戳，不自动插值。
可先去整段均值，再乘对称Hann或矩形窗，直接DFT。输出0..floor(N/2)频点；DC和偶数N的Nyquist不翻倍，其他单侧振幅翻倍，并除以N及coherent_gain=sum(window)/N。不是PSD或带宽噪声估计。
逐频能量按单侧重数×|DFT|²/N，核对窗后时域能量Parseval；振幅校正不应用于能量守恒。样例N8、2Hz单位正弦，peak bin2、振幅1、时域能量4。
浮点允许正常误差。窗会改变频泄漏、分辨率和峰值；不声称Hann校正后任意非整数频率峰值精确。无真实采集、重采样、FFT大规模加速或机械故障判定。
