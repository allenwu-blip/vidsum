"""An external lock claim must terminate only this tool's owned child."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from vidsum.cli import wait_owned

class ProcessLock(unittest.TestCase):
    def test_external_claim_stops_owned_pid(self):
        root=Path(__file__).resolve().parents[1]/'.runtime';root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as work:
            base=Path(work);shared=base/'shared';shared.mkdir();(shared/'owner').write_text('another-task')
            args=SimpleNamespace(global_lock_state=str(shared),lock_state=str(base/'local'))
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            try:
                with self.assertRaisesRegex(RuntimeError,'按 PID 停止'):
                    wait_owned(child,args)
                self.assertIsNotNone(child.poll())
                self.assertEqual((shared/'owner').read_text(),'another-task')
            finally:
                if child.poll() is None:child.terminate();child.wait()

    def test_shared_own_lock_does_not_stop_child(self):
        root=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=root) as work:
            base=Path(work);(base/'owner').write_text('our-task')
            args=SimpleNamespace(global_lock_state=str(base),lock_state=str(base))
            child=subprocess.Popen([sys.executable,'-c','pass'])
            self.assertEqual(wait_owned(child,args),0)
