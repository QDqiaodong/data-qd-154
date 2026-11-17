# 越限检索档案契约 v1（signal-excursions）

在已发布的 `signal-waveform-fetch` 完整波形包之上，新增独立离线命令
`signal-excursions`：`build` 把一组已验真波形包构建成一个**不可变、自足、可长期
离线查询**的越限检索档案；`query` 按来源、通道、半开样本范围与阈值，返回测量值
连续超限的极大样本段。仅用 Python 标准库；不连接设备，不读取 `samples.ndjson`
作为事实源，不修改源包、既有工作台或任何现有命令行为。

## 1. 命令与退出码

```sh
signal-excursions build --sources SOURCES.json --archive DIR
signal-excursions query  --archive DIR --source ID --channel NAME \
    --start A --stop B --op ge|lt --threshold T --limit K
```

- 成功退出 `0`；非法输入、源校验失败、档案损坏/版本不支持或 I/O 失败一律退出
  `2`（argparse 用法错误同为 2）。不存在“部分成功”的发布。
- `DIR` 已存在（文件、目录或符号链接）拒绝覆盖；暂存目录 `DIR.staging` 已存在
  时拒绝复用。所有源验真、索引完整提交后，才把暂存目录原子 `os.replace` 为
  `DIR`；失败清理暂存，绝不发布可查询的半成品（进程被强杀可能留下可辨识的
  `DIR.staging` 残余，它永远不会被当作档案）。

## 2. SOURCES.json

恰含一个键 `sources` 的 JSON 对象，数组 1..32 项；每项恰含：

| 字段 | 约束 |
| --- | --- |
| `source_id` | 现有非空标识符约束（非空、无控制字符、≤128 字符），全局唯一 |
| `package` | 非空路径字符串；相对路径按 SOURCES 文件所在目录解释 |

拒绝重复 JSON 键、非有限数字与未定义字段；文件 ≤1 MiB。档案以用户给定的
`source_id` 为键，**不**用可能重复的 `capture_id` 合并来源；所有源包的
`raw_bytes` 合计 ≤1 GiB，每包沿用基线 256 MiB 上限。

## 3. 源包验真（build）

只读取每个包内的 `manifest.json` 与 `raw.bin`；两者为符号链接即拒绝。

- manifest 字节 ≤64 KiB；按**固定基线真实发布的 schema_version=1 包字段**识别
  （恰含 `schema_version`、`source_url`、`capabilities`、`consistency_channel`、
  `etag`、`capture_id`、`config`、`channels`、`raw_bytes`、`raw_sha256`、
  `meta_sha256`、`analysis` 十二项），不是把 manifest 直接当远端 meta 对象。
  拒绝重复键、非有限数字、缺失/额外字段、非法 dtype/端序/通道配置。
- 一致性：用 manifest 自身的 `config`、`channels`、`raw_bytes`、`raw_sha256`
  重建规范化 meta 对象并计算规范 SHA256，必须等于 `meta_sha256`；
  `raw_bytes` 必须等于 `points*通道数*字宽`。
- `raw.bin` 流式读取一遍：累计长度必须等于 `raw_bytes`，全量 SHA256 必须等于
  `raw_sha256`，否则不得发布档案。按现有端序、point-major 通道交错与
  `value = raw*scale + offset` 语义逐点标定；非有限原值或标定值整批拒绝。
- 即使 `samples.ndjson` 存在也绝不作为索引事实源；索引只来自上述验真字节。

## 4. 档案格式（DIR）

```
DIR/
    catalog.db                 SQLite：meta / sources / channels / nodes
    values/<source_seq>_<channel_index>.bin
                               逐点标定值快照：little-endian float64，
                               每通道恰好 points 个，不量化、不降低精度
```

- `meta`：`archive_version=1`、`leaf_size=256`、`fanout=64`、`source_count`。
  query 只接受 `archive_version=1`，其余报“版本不支持”退出 2。
- `sources`：`source_seq`（SOURCES 顺序）、`source_id`（唯一）、`capture_id`、
  `sample_rate`、`points`、`dtype`、`byte_order`、`raw_bytes`、`raw_sha256`、
  `manifest_sha256`（原 manifest 文件字节 SHA256，供结果追溯）。
- `channels`：通道名（源内唯一）、`scale`/`offset`（证据）、`top_level`、
  `values_file`、`values_sha256`。
- `nodes`：持久区间摘要索引。0 级为叶段，每段最多 256 个连续样本；每个父节点
  汇总最多 64 个顺序子节点。每节点保存**自身的**绝对样本范围
  `[start, stop)`、`max_value`、`max_index`（最大值最早出现位置）、
  `min_value`——节点是真实区间摘要，不是不含样本范围的装饰目录。
- 档案自足：build 成功后源包移走或改动不影响查询；query 不访问源包或网络。
  快照/索引一旦发布不可变，没有任何更新命令。

## 5. query 语义

- 命中谓词：`ge` 为 `v >= t`，`lt` 为 `v < t`；阈值相等计入 `ge`、排除于 `lt`。
  比较基于与现有解码器一致的有限 binary64 标定值；`t` 必须有限。
- 范围 `0 <= A < B <= 该来源 points`，`K` 为 1..10000 整数（布尔不能冒充整数）。
- 返回范围 `[A,B)` 内部全部**极大连续**命中段 `[start,stop)`：跨叶段/摘要节点
  的相邻命中合并；范围边界可截断原始越限段。段按起点升序。
- 每段返回 `peak_value`（该段最大值，`lt` 段同样是最大值）与 `peak_index`
  （最大值最早出现位置）；区间与 `peak_index` 均为源包绝对样本索引，不按查询
  起点归零，不作时间插值。
- `--limit K` 返回前 K 个**完整**极大段并给 `has_more`：不在第 K 段内部截断，
  也不把第 K+1 段误并入。无段时 `runs=[]`、`has_more=false`。
- 返回 JSON：`source_id`、`capture_id`、`channel`、`sample_rate`、`start`、
  `stop`、`op`、`threshold`、`limit`、`raw_sha256`、`manifest_sha256`、`runs`、
  `has_more`、`node_reads`、`leaf_reads`、`value_reads`。

## 6. 索引使用与访问计数

- 遍历自顶向下：一个完整落入查询范围的索引区间，若摘要判定**全命中**
  （`ge`：`min_value >= t`；`lt`：`max_value < t`）则直接贡献整段，不读其底层
  逐点值；若判定**全不命中**（`ge`：`max_value < t`；`lt`：`min_value >= t`）
  则整段剪枝。部分相交的节点不套用全段峰值，只能向下展开；边界裁剪与摘要不能
  判定的叶段才读取逐点值，且只读相交子范围。
- 禁止先装入全部索引或顺序扫描整个通道再声称使用索引；除结果前 K 段外不保留
  完整通道或全部索引节点。
- 计数来自统一存储访问层（`ArchiveReader`），按实际取得记录，与 SQLite 页缓存
  /操作系统缓存无关，不根据结果或理论复杂度补填：
  - `node_reads`：实际取得的持久摘要记录条数（同节点重复取得也累计）；用于
    定位来源/通道的常量目录读取不计入。
  - `leaf_reads`：实际取得逐点值的叶段次数。
  - `value_reads`：从快照取出的样本值个数（非 read 调用次数）；单次 query
    不超过 `256 × leaf_reads`。

## 7. 资源预算与测量方法

- 最多 32 个源包、原始字节合计 ≤1 GiB；build 与 query 的峰值 RSS 均 ≤192 MiB。
- 测量方法（`tests/test_excursions_cli.py::ResourceBound`）：用单个 216 MiB
  原始包（54,000,000 个 float32，显著大于 RSS 上限）构建档案；build 与 query
  各自在独立子进程运行，经 `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`
  （Linux 单位 KiB，对已回收子进程取峰值）在前后采样。本机实测：build/query
  峰值约 64 MiB（含解释器），远低于 192 MiB 上限。
- build 流式工作集：1 MiB 读缓冲 + 每通道 ≤8192 值标定缓冲 + 每级 ≤64 条摘要
  聚合；query 工作集：遍历栈（每层 ≤64 条）+ 单叶 ≤256 值 + 前 K 段结果。

## 8. 验收覆盖索引

- 语义：越限段横跨两个叶段、两个摘要节点与查询左边界；阈值相等计入 `ge`、
  排除 `lt`；截断后段、最早并列峰值、K 段分页与 `has_more`；范围只切入段中间、
  整个通道相等、负阈值、首尾点命中、多来源同名通道、少于/恰/多于 K 段、
  不存在的来源/通道——全部与独立逐点朴素预期一致。
- 索引：1,048,576 点通道仅 500000..500009 为 10：全范围 `ge 5` 只返回
  `[500000,500010)` 且 `value_reads=256 ≤1024`、`node_reads=129 ≤512`；
  全范围 `ge 0` 只返回整个通道且 `value_reads=0`；全范围 `lt 0` 无段且
  `value_reads=0`。
- 建档：真实波形包 manifest 与大小端多通道 raw、非单位标定、相同
  `capture_id` 不同 `source_id`、原始 SHA 冲突拒绝、建成后删除源包结果不变；
  `samples.ndjson` 与 raw 矛盾时仍以 raw 为准。
- 读值计数：先 build 再启动独立 query 进程，计数来自持久访问层；
  `value_reads ≤ 256 × leaf_reads` 对每个用例断言。
- 资源：见第 7 节方法与结果。
