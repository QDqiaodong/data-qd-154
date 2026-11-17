
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,math
from pathlib import Path
from fractions import Fraction
from signal_spectrum.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_sine(self):
        r=analyze(sample());self.assertEqual(r['peak_bin'],2);self.assertAlmostEqual(r['bins'][2]['amplitude'],1);self.assertAlmostEqual(r['windowed_time_energy'],4);self.assertAlmostEqual(r['parseval_residual'],0)
    def test_dc_not_doubled(self):
        d=sample();d['remove_mean']=False;d['samples']=[3]*8;r=analyze(d);self.assertEqual(r['peak_bin'],0);self.assertAlmostEqual(r['bins'][0]['amplitude'],3)
    def test_nyquist_not_doubled(self):
        d=sample();d['samples']=[1,-1]*4;r=analyze(d);self.assertEqual(r['peak_bin'],4);self.assertAlmostEqual(r['bins'][4]['amplitude'],1)
    def test_hann_gain_energy(self):
        d=sample();d['window']='hann';r=analyze(d);self.assertAlmostEqual(r['coherent_gain'],7/16);self.assertAlmostEqual(r['parseval_residual'],0,places=10)
    def test_odd_length_parseval(self):
        d=sample();d['samples']=[1,2,3,4,5];r=analyze(d);self.assertEqual(len(r['bins']),3);self.assertAlmostEqual(r['parseval_residual'],0)
    def test_nonfinite(self):
        d=sample();d['samples'][0]=float('nan');self.bad(d)
    def test_bool_rejected(self):
        d=sample();d['samples'][0]=True;self.bad(d)
    def test_bound(self):
        d=sample();d['samples']=[0]*513;self.bad(d)

class Command(unittest.TestCase):
    def call(self,src,out):
        return subprocess.run([sys.executable,'-m','signal_spectrum.cli','--source',str(src),'--output',str(out)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
