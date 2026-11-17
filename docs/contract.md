# Bounded one-sided real-signal spectrum v1

离线采样分析者输入4..512个有限数值样本、正整数Hz sample_rate、rectangular/hann窗及布尔remove_mean。样本必须由调用方确保等间隔且无丢样；未提供时间戳，不自动插值。
可先去整段均值，再乘对称Hann或矩形窗，直接DFT。输出0..floor(N/2)频点；DC和偶数N的Nyquist不翻倍，其他单侧振幅翻倍，并除以N及coherent_gain=sum(window)/N。不是PSD或带宽噪声估计。
逐频能量按单侧重数×|DFT|²/N，核对窗后时域能量Parseval；振幅校正不应用于能量守恒。样例N8、2Hz单位正弦，peak bin2、振幅1、时域能量4。
浮点允许正常误差。窗会改变频泄漏、分辨率和峰值；不声称Hann校正后任意非整数频率峰值精确。无真实采集、重采样、FFT大规模加速或机械故障判定。
