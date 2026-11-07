import unittest
from vidsum.visual import choose_frames, render_visual

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
