"""A caller-owned fetcher must be read-only, and its heavy entrypoint must never run."""
import json
from pathlib import Path
import tempfile
import unittest
from vidsum.douyin import fetch_with_existing

class ExternalFetcher(unittest.TestCase):
    def test_source_stays_read_only_and_only_capture_runs(self):
        root=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=root) as work:
            base=Path(work);source=base/'source';source.mkdir();output=base/'output';output.mkdir()
            script=source/'fetch.py'
            script.write_text('''import json
from pathlib import Path
def capture_all(pairs,out):
    name,link=pairs[0];d=out/name;d.mkdir(exist_ok=True)
    info={'aweme_id':'123','desc':'Example','author':'Author','author_followers':12,'likes':34,'comments':5,'collects':6,'shares':7,'duration_s':8,'link':link}
    (d/'info.json').write_text(json.dumps({'info':info,'detail':{'create_time':1700000000,'video':{'url_list':['private-expiring-url']}}}))
    return [name]
def heavy(*args):
    raise RuntimeError('heavy must not run')
if __name__=='__main__':
    raise RuntimeError('main must not run')
''')
            before=script.read_bytes();metadata=script.stat()
            result=fetch_with_existing(script,'https://v.douyin.com/example/',output/'item')
            self.assertEqual((result['likes'],result['followers'],result['shares']),(34,12,7))
            self.assertEqual(before,script.read_bytes())
            self.assertEqual(metadata.st_mtime_ns,script.stat().st_mtime_ns)
            self.assertFalse((source/'__pycache__').exists())
            self.assertNotIn('private-expiring-url',(output/'item/info.json').read_text())

if __name__=='__main__':unittest.main()
