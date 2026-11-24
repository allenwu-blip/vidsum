import os
import unittest
from unittest import mock
from vidsum.visual import choose_frames, progress_width, render_visual, short_line, text_width

class VisualEvidence(unittest.TestCase):
    def test_scene_preference_and_endpoints(self):
        items=[dict(time_s=i,file=f'{i}.jpg',scene_change=i in (2,3,8,12,13,16)) for i in range(20)]
        selected=choose_frames({'frames':items},8)
        self.assertEqual([r['time_s'] for r in selected],[0,2,3,8,12,13,16,19])

    def test_uniform_fallback_and_small_notes(self):
        items=[dict(time_s=i,scene_change=False) for i in range(24)]
        selected=choose_frames({'frames':items},8)
        self.assertEqual(len(selected),8)
        self.assertEqual(selected[0],items[0]);self.assertEqual(selected[-1],items[-1])
        self.assertEqual(choose_frames({'frames':items[:3]},8),items[:3])
        self.assertLessEqual(max(b['time_s']-a['time_s'] for a,b in zip(selected,selected[1:])),5)

    def test_report_preserves_source_and_timecode(self):
        data=dict(model='local-model',frames=[dict(timecode='00:03.20',file='frame.jpg',description='可见一只猫')],contact=dict(description='联系表观察'),elapsed_s=2,peak_rss_gb=3,peak_mlx_gb=2)
        text=render_visual(data)
        self.assertIn('00:03.20',text);self.assertIn('可见一只猫',text);self.assertIn('单帧不能证实运镜',text)


    def test_progress_line_cuts_at_a_word(self):
        text='A large, complex space station structure with prominent solar arrays in orbit above Earth.'
        line=short_line(text)
        self.assertTrue(line.endswith('…')); self.assertLessEqual(len(line),61)
        self.assertEqual(line,'A large, complex space station structure with prominent…')
        self.assertTrue(text.startswith(line[:-1]))
        self.assertEqual(short_line('A cup of coffee.'),'A cup of coffee.')
        cjk='一个宇航员漂浮在空间站舱内手里拿着一个透明的塑料袋'*4
        self.assertEqual(short_line(cjk),cjk[:30]+'…')  # CJK takes two columns each: 60 columns is 30 characters
        self.assertEqual(text_width(short_line(cjk)),61)

    def test_progress_line_fits_the_terminal(self):
        head='01_1b1d0c2cf1: frame 4/8 00:17.98 (8 s) '
        english='A large, complex space station structure with prominent solar arrays in orbit above Earth.'
        cjk='一个宇航员漂浮在空间站舱内，手里拿着一个透明的塑料袋，背后的面板上写着 HATCH。'*2
        def width_at(cols, tty=True):
            with mock.patch('vidsum.visual.stdout_is_terminal', return_value=tty), mock.patch('vidsum.visual.shutil.get_terminal_size', return_value=os.terminal_size((cols, 24))):
                return progress_width(head)
        self.assertEqual(width_at(104), 60)   # the demo terminal: same cut as before
        self.assertEqual(short_line(english, width_at(104)), 'A large, complex space station structure with prominent…')
        self.assertEqual(width_at(80), 80-text_width(head)-2)
        self.assertEqual(width_at(20), 16)
        self.assertEqual(width_at(80, tty=False), 60)  # logs and MCP clients do not wrap
        for cols in (80, 104, 120):
            for text in (english, cjk):
                self.assertLess(text_width(head+short_line(text, width_at(cols))), cols)


class DescribeLoop(unittest.TestCase):
    def test_progress_line_and_report_with_fake_model(self):
        import contextlib, io, json, tempfile
        from pathlib import Path
        from vidsum.visual import describe_directories
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)/'01_x'; (d/'frames').mkdir(parents=True)
            frames = [dict(time_s=i*3.0, timecode=f'00:0{i*3}.00', file=f'{i}.jpg', scene_change=False) for i in range(3)]
            for f in frames: (d/'frames'/f['file']).write_bytes(b'x')
            (d/'contact.jpg').write_bytes(b'x')
            (d/'frames.json').write_text(json.dumps({'frames': frames}))
            (d/'meta.json').write_text(json.dumps({'output_language': 'en', 'media_type': 'video'}))
            seen = []
            def describe(paths, prompt):
                seen.append(len(paths)); return {'description': '*   **Subject:** A cup of coffee.\n- Action: none', 'elapsed_s': 1.0, 'generation_tokens': 5, 'finish_reason': 'stop'}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                describe_directories(describe, [d], 8, 'org/model', 0.0, {'reset': lambda: None, 'peak': lambda: 0.0})
            self.assertIn('frame 1/3', out.getvalue()); self.assertIn('A cup of coffee.', out.getvalue())
            self.assertNotIn('**', out.getvalue())
            self.assertEqual(seen, [1, 2, 2, 1])  # each frame is compared with the one before, then the contact sheet
            self.assertIn('A cup of coffee', (d/'visual.md').read_text())
            self.assertEqual(json.loads((d/'visual.json').read_text())['model'], 'org/model')
