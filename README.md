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

## 当前能力和样例
# Bounded one-sided real-signal spectrum v1

离线采样分析者输入4..512个有限数值样本、正整数Hz sample_rate、rectangular/hann窗及布尔remove_mean。样本必须由调用方确保等间隔且无丢样；未提供时间戳，不自动插值。
可先去整段均值，再乘对称Hann或矩形窗，直接DFT。输出0..floor(N/2)频点；DC和偶数N的Nyquist不翻倍，其他单侧振幅翻倍，并除以N及coherent_gain=sum(window)/N。不是PSD或带宽噪声估计。
逐频能量按单侧重数×|DFT|²/N，核对窗后时域能量Parseval；振幅校正不应用于能量守恒。样例N8、2Hz单位正弦，peak bin2、振幅1、时域能量4。
浮点允许正常误差。窗会改变频泄漏、分辨率和峰值；不声称Hann校正后任意非整数频率峰值精确。无真实采集、重采样、FFT大规模加速或机械故障判定。
