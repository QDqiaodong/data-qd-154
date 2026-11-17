# 连续采样 Welch 工作台数据契约 v1

在旧的一次性离线频谱（`signal-spectrum`）之外新增 SQLite 持久化工作台
`signal-spectrum-workbench`，提供 `init`、`apply`、`status` 三个子命令。
研究人员可以反复修订仍在 `seal` 之后的实时样本并查看 `live_psd`，封口后由
`sealed_psd` 给出不再改变的稳定结果。仅用 Python 标准库；只承诺单操作者与
进程终止（SIGKILL/崩溃/掉电前的正常磁盘语义除外，见下文）恢复。

## 1. 命令

```sh
signal-spectrum-workbench init   --db DB --config CONFIG --report REPORT
signal-spectrum-workbench apply  --db DB --source REQUEST
signal-spectrum-workbench status --db DB
```

- `init` 不覆盖任何已存在的数据库：DB 已存在时，先按库内**原配置**恢复待发布
  代报告，然后以退出码 2 拒绝覆盖；新 `--config`、新 `--report` 路径都不会替换
  库内持久化的旧值。
- 报告路径在 init 时规范化为绝对路径持久化，之后跨工作目录重启仍发布到该目标。
- DB、CONFIG/REQUEST、REPORT 不得指向同一文件（含硬链接同源）；报告目标是符号
  链接时拒绝。
- 校验/用法错误与发布失败一律退出码 2，成功退出 0。

## 2. CONFIG（init 输入，恰含五字段）

| 字段 | 约束 |
| --- | --- |
| `sample_rate` | 整数，1..10^9，拒绝 bool |
| `frame_size` (`N`) | 偶数，4..128 |
| `hop` (`H`) | 整数，1..N |
| `window` | `rectangular` 或 `hann`（旧工程同款对称窗） |
| `remove_mean` | 严格布尔 |

未知字段、缺字段、bool 冒充整数、非有限数一律拒绝。

## 3. REQUEST（apply 输入，恰含三字段）

| 字段 | 约束 |
| --- | --- |
| `request_id` | 非空白标识符，去空白后与原值相同，长度 ≤128，无控制字符 |
| `writes` | 数组，每批至多 20000 条，元素恰含 `index`、`value` |
| `seal` | 整数 0..100000，每批必须提供，初始为 0，单调不减 |

- `index` 为非负整数且 ≤100000；同批不得重复；数组排列不影响结果。
- `value` 为有限数值（沿用旧工程幅值限制 `|x| <= 10^9`，bool 拒绝）表示当前值
  替换；`null` 表示删除。删除一个从未写入的索引也是**已接受的写入**（留下
  tombstone）。
- 任何 `index < 旧 seal` 的写入都拒绝，且整批不生效（先整体校验再提交）。
- `seal` 同时冻结已有样本与缺口：封口后缺口窗永久跳过，迟到补样拒绝。

## 4. 状态层与前沿

- 样本按绝对索引保存有限数值或删除标记（tombstone）。
- `horizon = max(历次接受写入的最大索引 + 1, seal)`，只升不降；删除最高索引不
  使其回退。初始为 0。
- 窗口起点 `s = mH`（m≥0），仅列出满足 `s + N <= horizon` 的整窗；尾部不足 N
  的点不产生窗口。
- 窗口状态：
  - `complete`：窗内 N 个点此刻都持有有限数值；任一缺失（未写或 tombstone）即
    `gap`。缺口窗不补零、不进入任何 PSD 分母。
  - `sealed` 当且仅当 `s + N <= seal`，否则 `open`。
- 可修订样本被更正或删除时，所有覆盖该索引的重叠窗整体撤回并按当前状态重算；
  封口完整窗对 `sealed_psd` 的贡献此后不可改变。

## 5. 谱层

完整窗先按配置去除**该窗**均值，再乘旧定义的对称窗（矩形全 1；Hann 为
`0.5 - 0.5 cos(2πi/(N-1))`）。单侧功率谱密度：

```
PSD[k] = multiplicity * |DFT[k]|² / (sample_rate * sum(w²))
```

DC（k=0）与偶数 N 的 Nyquist（k=N/2）重数为 1，其余为 2。
`live_psd` 对全部完整窗逐频等权平均；`sealed_psd` 仅对已封口完整窗平均；没有
任何合格窗时对应值为 `null`。恒等核对（每窗成立）：

```
sum(PSD) * sample_rate / N == sum((centered*w)²) / sum(w²)
```

不得把旧工程的 `amplitude` 或 `windowed_energy` 直接当作 PSD。

## 6. 报告（init 路径固定，确定性序列化）

JSON 键排序、UTF-8、无 NaN/Infinity、缩进 2、末尾换行。字段：

`schema_version`、`generation`、`request_id`（第 0 代为 `null`）、`config`、
`seal`、`horizon`、`windows`（按起点递增，每项含 `start`、`end`、
`status=complete|gap`、`sealed` 布尔）、`live_complete_count`、
`sealed_complete_count`、`sealed_gap_count`、`live_psd`、`sealed_psd`
（每个 PSD 项含 `index`、`frequency_hz`、`psd`；无窗为 `null`）。

- init 即生成并发布 `generation=0` 空报告。
- 之后每个**全新接受的 request_id** 使 generation 加一，即使写入结果与之前完全
  相同；同代报告内不会混合不同代状态。

## 7. 请求身份与重试

身份是请求文件**完整原始字节的 SHA256**（8 MB 上限、重复 JSON 键、`NaN` 常量
等沿用旧的严格读取规则），与 `request_id` 一起存入 `requests` 表：

- 同 `request_id` + 同 SHA 重试：识别为重放，**不加代、不重写报告**，无论其后
  是否已有更新的代。
- 同 `request_id` + 异字节：拒绝（退出 2）。
- 校验失败的新请求退出 2，不写样本、不写请求记录、不动前沿、不产生新报告代；
  无待发布代时现有报告字节保持不变。

## 8. 提交与发布协议（崩溃恢复的核心）

一条请求的生命周期：

1. 恢复阶段（每个命令最先执行，见下）。
2. 读取并严格校验请求；全部写入先在内存校验（含封口边界、重复键）。
3. **SQLite 单事务提交**：样本/tombstone 修订、seal/horizon、requests 身份行、
   新一代行、该代完整报告文本、`committed_generation` 一同可见。事务前任何失败
   都不生效。
4. 事务提交后，用库内存储的报告文本经同目录临时文件 + `os.replace` 原子替换报告
   文件，成功后才把 `published_generation` 推进到该代。

### 两个可见性时刻

- **数据库可见**：步骤 3 的事务提交完成时。此前崩溃 = 候选完全不存在，不会有
  半成品样本或代。
- **报告文件可见**：步骤 4 的 `os.replace` 完成时。它要么是旧代完整文件、要么
  是新代完整文件，不存在混合内容。

### 为什么未提交候选不会被发布

发布只从 `reports` 表读取**已提交代**的文本；未进入事务的候选没有任何持久化痕
迹，恢复逻辑也只认 `committed_generation`。

### 为什么旧代报告不会盖到新报告

`published_generation` 单调推进，恢复目标恒为
`committed_generation`（待发布的最高代）；重放请求显式跳过发布。即使进程在
replace 之后、确认之前被杀，下一次恢复最多把**当前最高代**再幂等写一遍，不会
回退文件内容。

### 源文件丢失后的恢复依据

每代报告的完整字节在提交事务内就已写入 SQLite `reports` 表，恢复只依赖 DB 而
不依赖原请求文件。任意命令（init/apply/status）启动时先检查
`committed_generation > published_generation`：若有待发布代，先重新发布该代并
推进确认标记，然后才处理新输入。因此校验新请求之前允许替换掉属于上一请求的旧
报告，这不算非法请求生效。

### 恢复失败时的诚实状态

恢复发布本身失败（例如报告目录不可写）：命令退出 2，`status` 仍输出可辨识的
`pending`（待发布代编号），`committed_generation` 与 `published_generation`
如实分列，绝不谎报成功。

### 测试故障钩子

实现提供两个仅用于测试的环境变量，模拟不可手工卡时序的终止点：

- `SIGSPECTRUM_WB_CRASH=after_commit`：事务提交后、文件替换前终止（退出码 10）。
- `SIGSPECTRUM_WB_CRASH=after_replace`：文件替换后、确认标记前终止（退出码 11）。

## 9. status 输出

JSON，含 `committed_generation`、`published_generation`、`pending`
（无待发布时为 `null`）、`seal`、`horizon`、`report_path`，以及当前 `config`。
恢复失败时同样输出该结构但退出 2。

## 10. 边界与非目标

- 单操作者；不处理并发写竞争，不承诺断电级磁盘持久性（SQLite WAL 的常规语义
  除外）。
- 不做插值、重采样、FFT 大规模加速；缺口不补零。
- 输入 JSON 上限 8MB，沿用旧的重复键/非有限数拒绝规则。
