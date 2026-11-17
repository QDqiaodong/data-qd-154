import math
from .common import require,number,rows
from .calculation import spectrum

def analyze(data):
    require(data,'sample_rate window remove_mean samples');rate=number(data['sample_rate'],1,10**9)
    if data['window'] not in ('rectangular','hann') or type(data['remove_mean']) is not bool:raise ValueError('invalid window or remove_mean')
    samples=rows(data['samples'],512)
    if len(samples)<4:raise ValueError('need 4..512 equally spaced samples')
    for x in samples:
        if type(x) not in (int,float) or not math.isfinite(x) or abs(x)>10**9:raise ValueError('invalid sample')
    return spectrum(samples,rate,data['window'],data['remove_mean'])
