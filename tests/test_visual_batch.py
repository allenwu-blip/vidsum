"""A repeated work must use one text request and preserve each task's context."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from vidsum.cli import main

class VisualBatch(unittest.TestCase):
    def test_duplicate_reuses_inference_and_preserves_v1_and_context(self):
        root=Path(__file__).resolve().parents[1]/'.runtime';root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as work:
            out=Path(work);entries=[]
            for name,note in [('first','context A'),('second','context B')]:
                d=out/name;d.mkdir();entries.append({'id':name,'input':'cached'})
                meta={'label':name,'allen_context':note,'video_sha256':'same-video','duration_s':10}
                (d/'meta.json').write_text(json.dumps(meta))
                (d/'frames.json').write_text(json.dumps({'frames':[],'detected_scene_changes':0,'scene_threshold':.22}))
                (d/'asr.json').write_text(json.dumps({'segments':[],'status':'无口播'}))
                (d/'ocr.json').write_text('[]')
                for f,text in [('transcript.md','无口播'),('ocr.md','无文字'),('visual.md','## 联系表：整体视觉风格和抽样节奏\n猫位于中央'),('summary.md','previous '+name)]:
                    (d/f).write_text(text)
            manifest=out/'manifest.json';manifest.write_text(json.dumps(entries))
            body='\n'.join('## '+h+'\n\n[画面 00:01.00] 猫。' for h in ['这条讲什么','结构拆解','拍法和剪法','关键原话','数据','能学的点'])
            response={'choices':[{'message':{'content':body},'finish_reason':'stop'}]}
            argv=['vidsum','run','--manifest',str(manifest),'--output',str(out),'--stage','summarize','--model','text','--vision-model','vision','--asr-model','unused','--lock-script','unused','--force-summary']
            with patch.object(sys,'argv',argv),patch('vidsum.cli.heavy',side_effect=lambda *a:contextlib.nullcontext()),patch('vidsum.cli.server',return_value=contextlib.nullcontext('http://local')),patch('vidsum.cli.api',return_value=response) as api:
                main()
            self.assertEqual(api.call_count,1)
            for name,note in [('first','context A'),('second','context B')]:
                d=out/name
                self.assertEqual((d/'summary_v1.md').read_text(),'previous '+name)
                self.assertIn(note,(d/'summary.md').read_text())
                self.assertIn('[画面',(d/'summary.md').read_text())
            self.assertEqual(json.loads((out/'second/summary_reuse.json').read_text())['from'],'first')

    def test_missing_visual_blocks_entire_text_phase(self):
        root=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=root) as work:
            out=Path(work);entries=[]
            for name in ['done','pending']:
                d=out/name;d.mkdir();entries.append({'id':name,'input':'cached'})
                (d/'meta.json').write_text(json.dumps({'duration_s':10}))
                for filename in ['transcript.md','ocr.md','frames.json']:
                    (d/filename).write_text('{}')
                (d/'summary.md').write_text('keep previous')
            (out/'done/visual.md').write_text('finished')
            manifest=out/'manifest.json';manifest.write_text(json.dumps(entries))
            argv=['vidsum','run','--manifest',str(manifest),'--output',str(out),'--stage','summarize','--model','text','--vision-model','vision','--asr-model','unused','--lock-script','unused','--force-summary']
            with patch.object(sys,'argv',argv),patch('vidsum.cli.server') as server:
                with self.assertRaises(SystemExit) as stopped:main()
                self.assertEqual(stopped.exception.code,1)
                server.assert_not_called()
            self.assertEqual((out/'pending/summary.md').read_text(),'keep previous')
