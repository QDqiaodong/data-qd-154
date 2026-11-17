import math,cmath

def spectrum(samples,rate,window,remove_mean):
    n=len(samples);mean=math.fsum(samples)/n;centered=[x-mean if remove_mean else x for x in samples]
    weights=[1.0]*n if window=='rectangular' else [0.5-0.5*math.cos(2*math.pi*i/(n-1)) for i in range(n)]
    values=[x*w for x,w in zip(centered,weights)];gain=math.fsum(weights)/n;bins=[];full_energy=0.0
    for k in range(n//2+1):
        coefficient=sum((v*cmath.exp(-2j*math.pi*k*i/n) for i,v in enumerate(values)),0j)
        multiplicity=1 if k==0 or n%2==0 and k==n//2 else 2
        amplitude=multiplicity*abs(coefficient)/(n*gain);energy=multiplicity*abs(coefficient)**2/n;full_energy+=energy
        bins.append({'index':k,'frequency_hz':k*rate/n,'real':coefficient.real,'imaginary':coefficient.imag,'amplitude':amplitude,'windowed_energy':energy})
    time_energy=math.fsum(v*v for v in values);peak=max(bins,key=lambda b:(b['amplitude'],-b['index']))
    return {'sample_count':n,'input_mean':mean,'coherent_gain':gain,'bins':bins,'peak_bin':peak['index'],'windowed_time_energy':time_energy,'windowed_frequency_energy':full_energy,'parseval_residual':full_energy-time_energy}
