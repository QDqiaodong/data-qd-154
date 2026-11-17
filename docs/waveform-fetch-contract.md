# 可追溯原始波形采集契约 v1（signal-waveform-fetch）

离线频谱使用者不再只能接受别人准备好的本地样本：本命令通过真实 HTTP 从测量
设备取回**一次完整的原始采集**，并把元数据、全部原始字节与逐点标定值作为一个
内容可验真的数据包发布。它与 Welch 工作台完全隔离：不读写工作台数据库，不产生
writes、seal、PSD 或请求代际状态。仅使用 Python 标准库。

## 1. 命令与退出码

```sh
signal-waveform-fetch --url BASE --output DIR [--resume] \
                      [--channel C --analysis-count M]
signal-waveform-device   # 离线、真实 TCP/HTTP 的设备模拟器
```

- 成功退出 `0`；任何协议、校验、预算或发布失败退出 `2`（崩溃注入点除外，见
  第 8 节）。不存在“部分成功”的发布。
- 默认**不**导出 analysis。`--channel` 与 `--analysis-count` 必须同时给出：
  `C` 是从 0 开始的合法通道下标；`M` 为 4..512 且不超过 `points`；导出该通道
  前 M 个标定值，`sample_rate` 取自本次元数据，`window=rectangular`、
  `remove_mean=false`，结构与旧 `signal-spectrum` 输入完全一致，旧 DFT 可直接
  消费。发布前会真实跑一遍旧 DFT，不能被旧 DFT 接受的超幅值（`|x|>10^9`）只在
  启用 analysis 时拒绝发布；基本数据包只要求标定值有限。
- 已成功发布的目录（含 `manifest.json`）拒绝覆盖。暂存目录由输出路径确定：
  `DIR.staging`。未加 `--resume` 时若暂存已存在则拒绝复用。

## 2. 最小设备 HTTP 协议（模拟器与客户端共同遵守）

所有控制响应均为 JSON，单响应上限 64 KiB，必须带 `Content-Length`，禁止分块
传输编码；重复 JSON 键、未知字段、`NaN`/`Infinity` 一律拒绝。除显式布尔字段外，
布尔值不得冒充数值。

| 方法与路径 | 语义 |
| --- | --- |
| `GET /capabilities` | JSON 恰含 `max_range_bytes`（1..65536 整数）与 `lease`（布尔） |
| `GET /capture/meta` | 当前元数据 + 强 `ETag` 响应头 |
| `GET /capture/data` | 当前原始载荷；须带单一区间 `Range: bytes=a-b` 与 `If-Match` |
| `POST /leases` | 仅 lease 设备；201 返回 `{"token": 标识符}`，原子固定此刻捕获 |
| `GET /leases/T/meta`、`GET /leases/T/data` | 与当前采集相同的元数据/范围接口，作用于不可变快照 |
| `DELETE /leases/T` | 释放保留槽，可重复调用（幂等，含未知 token） |

范围与条件读取规则：

- 匹配时返回 `206`，带完整 `Content-Range: bytes a-b/总字节数` 与 `ETag`；
  `If-Match` 不匹配返回 `412`；lease 过期返回 `410`。
- 设备忽略 Range 而回 `200`、`Content-Range` 起止/总长错误、截断、超长、
  缺失/弱/冲突 ETag，都不得被当作完整块提交。
- token 不含斜杠或控制字符（单一路径段）。
- 客户端不得假设 lease 寿命覆盖整次采集；正常结束与可处理失败时都尽力
  `DELETE` 释放，且不依赖被终止进程的 `finally`。

### 2.1 meta 字段（恰含八个）

| 字段 | 约束 |
| --- | --- |
| `capture_id` | 非空标识符；相同 id 换配置仍是不同身份 |
| `sample_rate` | 1..10^9 整数，拒绝 bool |
| `points` | 至少 4 的整数 |
| `dtype` | `int16` / `uint16` / `float32` |
| `byte_order` | `little` / `big` |
| `channels` | 1..16 个 `{name,scale,offset}`；name 唯一非空；scale/offset 有限数 |
| `raw_bytes` | 必须等于 `points*channels*字宽`，且 ≤ 256 MiB |
| `sha256` | 完整原始载荷的 64 位十六进制摘要 |

### 2.2 强 ETag 联合身份

ETag 必须是 HTTP **强**实体标签（拒绝 `W/"..."`）。设备保证它联合标识完整
meta、原始字节与标定：三者任一变化都更换 ETag，即使 `capture_id` 复用；相同
ETag 下 meta 必须一致，meta 与 data 响应都携带该联合身份。客户端额外做两层
本地绑定：

1. 会话把 ETag 与本地计算的“meta 指纹 + 原始 SHA”联合摘要绑定，重校验时若
   设备在同一 ETag 下给出不同 meta/标定，判身份冲突。
2. 每块流式哈希、全量 SHA 与设备宣告的 `sha256` 比对，截获同 ETag 下被篡改的
   字节。

## 3. 两种一致性通道（不是只按 lease 布尔发一组请求）

会话先协商 `max_range_bytes` 与 lease 能力，再选择通道；选定后履行该通道的
**全部**身份义务。lease 永远不是采集前提：仅 ETag 设备与 lease 设备都必须能
成功采集。

### 3.1 `etag` 通道（ETag-only 设备，或显式 `--channel-mode etag`）

- 每个块都带当前强 ETag 做 `If-Match` 条件读取；412 表示身份已变。
- 全部字节下载完成后**再次**条件读取 meta：ETag 与 meta 必须逐字段等于会话
  身份，证明传输期间设备没有轮换捕获。
- 最后比对全量 SHA。

### 3.2 `lease` 通道（设备支持且未被强制 etag 时默认）

- `POST /leases` 原子固定当前捕获，之后所有 meta/data 读取都走
  `/leases/T/...` 并带 `If-Match`；快照在有效期内不可变。
- 任一读返回 410（过期）即令整个候选失效：**禁止**把新 lease 的后半段接到旧
  前缀，只能在重启预算内以新身份从零采集。
- 正常发布后与可处理异常时尽力 `DELETE` 原 lease。

### 3.3 身份变化的统一处理

412、410、ETag/meta/布局变化都触发同一动作：丢弃旧身份的全部字节前缀、半样本
与派生值（删除并重建 `raw.bin` 与派生文件），按新元数据重新解释，重启计数
`restarts_used` 加一。全次重启是有限预算（默认 3，可用
`SIGSPECTRUM_WF_RESTARTS` 收紧，不能设为无限）。预算耗尽即有界失败，保留可辨识
暂存目录。

## 4. 分块、缓冲与内存预算

- 单块指一个完整 Range 响应。客户端块大小取
  `min(设备 max_range_bytes, 256 KiB, 剩余字节)`，配合 64 KiB 读缓冲，工作集
  远低于 1 MiB（不含 Python 运行时）。
- 原始载荷上限 256 MiB；通道 1..16；points ≥ 4。任何阶段都不把完整载荷或完整
  通道数组装入内存：JSON 控制响应 ≤64 KiB 且按 `Content-Length` 限读，原始字节
  经固定缓冲流入临时文件，NDJSON 逐点写行。
- 连接超时（默认 10s，`SIGSPECTRUM_WF_TIMEOUT`）、单块重试
  （默认 4，`SIGSPECTRUM_WF_BLOCK_RETRIES`）、全次重启（默认 3）都是公开且有限
  的预算参数。断流/截断只重试**同一区间**，未完整验证的字节绝不进入承诺前缀；
  网络流可以在二进制样本中间断开。

## 5. 解码与标定

- 支持 int16/uint16/float32 与 little/big；多通道按点交错（point-major），同一
  点依 channels 顺序排列等字宽样本。
- 逐通道 `value = raw*scale + offset`；拒绝非有限原值与非有限标定结果。
- 数据总长必须等于 `points*channels*字宽`，不足整帧的尾部拒绝。
- `samples.ndjson` 每行恰含 `index` 与 `values`：index 从 0 连续到 points-1，
  values 按通道顺序记录全部标定值。
- 允许先完整下载并验真原始字节，再流式解码发布，不要求边下载边发布。

## 6. 暂存、检查点与 `--resume`

暂存目录 `DIR.staging/`：

| 文件 | 含义 |
| --- | --- |
| `raw.bin` | 已逐块承诺的原始前缀 |
| `_download.tmp` | 当前未承诺块，任何重启都删除，绝不计入前缀 |
| `checkpoint.json` | 持久检查点（临时文件 + fsync + 原子替换 + 目录 fsync） |
| `samples.ndjson` / `analysis.json` / `manifest.json` | 发布阶段派生品，重启时先清除 |

检查点绑定：设备 URL、完整 meta、强 ETag、一致性通道、lease token、已提交偏移、
前缀 SHA256、已用重启次数与 analysis 参数。恢复流程：

1. 读取并严格校验检查点字段；URL、analysis 参数不一致直接失败。
2. 重新哈希本地已提交前缀验证 SHA；**本地前缀被篡改必须报错**，不能伪装成远端
   变化。
3. 截断 raw.bin 到已提交偏移：raw 落盘与检查点更新之间被终止产生的尾部是未承诺
   字节，不得被误认为已完成块。
4. 重新协商能力并核远端身份：etag 通道再读当前 meta；lease 通道用原 token 读
   快照 meta。原 lease 已失效则按第 3.3 节整体重启，不得拼接新身份。
5. 同身份重连只从本地已完整校验的块边界继续，不跳过服务端已发送但未确认的字节。

只承诺进程终止恢复（SIGKILL/崩溃），不承诺断电或并发采集。

## 7. 发布与 manifest

仅在全量 SHA、逐点解码、标定有限性（及启用时的旧 DFT 验收）全部通过后，才把
`raw.bin`、`samples.ndjson`、`manifest.json`（及可选 `analysis.json`）作为新目录
原子 `os.replace` 发布；检查点与临时文件不进入发布目录。manifest 记录来源 URL、
capabilities、一致性通道、强 ETag、采集 ID、配置、标定、实际字节数、验证使用的
原始 SHA 与 meta 指纹。**不写入任何墙钟时间**，使同内容目录可逐字节比较。发布是
一次性的：目标已发布则拒绝覆盖；失败只保留 `DIR.staging` 可辨识状态。

## 8. 测试注入点（公开、有限、仅用于测试）

环境变量：

- `SIGSPECTRUM_WF_BLOCK_RETRIES`：单块重试预算（非负整数）。
- `SIGSPECTRUM_WF_RESTARTS`：全次身份重启预算（非负整数）。
- `SIGSPECTRUM_WF_TIMEOUT`：socket 超时秒数。
- `SIGSPECTRUM_WF_CRASH=after_raw_append|after_checkpoint`：分别在“块字节已落盘
  但检查点未更新”与“检查点已更新”后以退出码 12 终止进程，用于续传验收。

模拟器控制 API（`DeviceServer`，真实 HTTP 处理线程内生效）：`set_capture`、
`swap_after_data_requests(capture, n, keep_etag=...)`、
`add_fault` / `add_repeated_fault`（`ignore_range`、`bad_content_range`、
`excess_bytes`、`missing_etag`、`weak_etag`、`conflicting_etag`、`truncate`、
`tamper_body`）、`expire_leases` / `expire_leases_after_data_requests`、
`auto_alternate`、`set_fragment` 以及完整 `request_log`。所有测试客户端均通过
真实 socket 发 HTTP 请求，不以 mock 返回值替代协议联调，也不需要外网或账号。

## 9. 验收覆盖索引

- 会话：ETag-only 与 lease 两类设备成功采集；同 capture id 换配置后从零采集新
  身份；lease 过期（传输中与恢复时）行为；断流后同身份续传；设备持续变动的有界
  失败；请求日志证明未混合身份。
- 解码：双通道有符号/无符号整数、大小端 float32 的已知字节逐点标定；奇数长度网络
  碎片跨字宽/跨通道边界；拒绝 NaN、长度乘积不符、全文件 SHA 不符。
- 联动：首身份双通道 little-endian int16，传输两块后换成同 id 单通道 big-endian
  float32 且改 scale；恢复后 raw、manifest、analysis 只描述新布局（C=0、M=4），
  并核对 samples.ndjson 全部值；默认不导出 analysis 另有独立成功案例。
- 续传：两个崩溃点分别终止后重启；未承诺尾部不被误认完成、已承诺块不重复不跳字节；
  篡改已承诺前缀拒绝续传；最终数据与无中断采集一致。
- 恶意/错误响应：200 忽略 Range、错误 Content-Range、过量字节、缺失/弱/冲突 ETag、
  相同 ETag 改标定/字节，均不得发布且保留已有成功目录。
