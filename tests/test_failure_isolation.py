"""The batch must report every missing input, not abort at the first failure."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class FailureIsolation(unittest.TestCase):
    def test_two_failures_are_indexed(self):
        root=Path(__file__).resolve().parents[1]/'.runtime'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as work:
            d=Path(work)
            result=subprocess.run([sys.executable,'-m','vidsum.cli','run',str(d/'missing-one.mp4'),str(d/'missing-two.mp4'),
                '--output',str(d/'out'),'--stage','prepare','--model','unused','--asr-model','unused','--lock-script','unused'],capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            rows=json.loads((d/'out/batch_status.json').read_text())
            self.assertEqual(len(rows),2)
            self.assertTrue(all(x['status']=='失败' and '输入文件不存在' in x['error'] for x in rows))
            self.assertTrue(all((d/'out'/x['id']/'ERROR.txt').exists() for x in rows))

    def test_manifest_cannot_escape_output(self):
        root=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=root) as work:
            d=Path(work);m=d/'batch.json'
            m.write_text(json.dumps([{'id':'../escaped','input':'absent'}]))
            result=subprocess.run([sys.executable,'-m','vidsum.cli','run','--manifest',str(m),'--output',str(d/'out'),
                '--model','unused','--asr-model','unused','--lock-script','unused'],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse((d/'escaped').exists())

if __name__=='__main__':unittest.main()
