"""Public-facing behaviour: default command, model resolution, subtitles, fetch hook, English template."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from vidsum import cli, models


def tone(path, seconds=2, video=True):
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}']
    if video:
        cmd += ['-f', 'lavfi', '-i', f'testsrc=size=320x240:rate=10:duration={seconds}', '-shortest']
    subprocess.run(cmd + [str(path)], check=True)


class DefaultCommand(unittest.TestCase):
    def test_bare_file_means_run(self):
        self.assertEqual(cli.normalize_argv(['talk.mp4']), ['run', 'talk.mp4'])
        self.assertEqual(cli.normalize_argv(['--profile', 'cloud', 'a.mp4']), ['run', '--profile', 'cloud', 'a.mp4'])
        for argv in (['setup'], ['transcribe', 'a.mp4'], ['mcp'], ['--version'], ['-h']):
            self.assertEqual(cli.normalize_argv(argv), argv)


class ModelResolution(unittest.TestCase):
    def test_explicit_names_are_not_downloaded(self):
        rows = models.plan('small', {'roles': ('asr', 'llm'), 'asr': 'unused', 'llm': '/models/text'})
        self.assertEqual(models.missing(rows), [])
        self.assertEqual(models.ensure(rows, allow_download=False), {'asr': 'unused', 'llm': '/models/text'})

    def test_profile_rows_are_pinned(self):
        rows = models.plan('small', {'roles': ('asr', 'llm')})
        self.assertTrue(all(len(revision) == 40 for _, _, revision, _ in rows))
        self.assertEqual(models.plan('cloud', {'roles': ('asr', 'llm')}), [])


class Subtitles(unittest.TestCase):
    def test_srt_vtt_and_language(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work)
            result = {'language': 'en', 'segments': [{'start': 0.0, 'end': 2.5, 'text': ' Hello there.'},
                                                     {'start': 61.2, 'end': 63.0, 'text': ' One minute in.'}]}
            lang = cli.finish_transcript(d, result, {}, 70.0, 'auto')
            self.assertEqual(lang, 'en')
            self.assertIn('00:01:01,200 --> 00:01:03,000\nOne minute in.', (d/'transcript.srt').read_text())
            self.assertTrue((d/'transcript.vtt').read_text().startswith('WEBVTT'))
            self.assertIn('[01:01.20–01:03.00] One minute in.', (d/'transcript.md').read_text())
            self.assertEqual(json.loads((d/'asr.json').read_text())['status_code'], 'speech')


class FetchHook(unittest.TestCase):
    def test_external_downloader_contract(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work); sample = base/'sample.mp4'; tone(sample)
            script = base/'fetch.py'
            script.write_text('import json,shutil,sys\nfrom pathlib import Path\n'
                              f'd=Path(sys.argv[2]);shutil.copy({str(sample)!r},d/"video.mp4")\n'
                              '(d/"meta.json").write_text(json.dumps({"source":"mine","link":sys.argv[1],"title":"t"}))\n')
            d = base/'item'; d.mkdir()
            args = SimpleNamespace(fetch_command=f'"{sys.executable}" "{script}"')
            links = {}
            meta = cli.prepare_input({'input': 'my-share-text https://example.org/x'}, d, args, 'en', links)
            self.assertEqual(meta['source'], 'mine'); self.assertTrue((d/'video.mp4').exists())
            self.assertIn('https://example.org/x', next(iter(links)) if links else 'https://example.org/x')

    def test_missing_path_and_page_links(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work)
            args = SimpleNamespace(fetch_command=None)
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                cli.prepare_input({'input': '/nope/clip.mp4'}, d, args, 'en', {})
            with self.assertRaisesRegex(RuntimeError, 'not a local file'):
                cli.prepare_input({'input': 'some share text'}, d, args, 'en', {})


class EnglishTemplate(unittest.TestCase):
    def test_general_summary_without_platform_data(self):
        with tempfile.TemporaryDirectory() as work:
            out = Path(work); d = out/'talk'; d.mkdir()
            (d/'meta.json').write_text(json.dumps({'label': 'talk', 'context': 'private note', 'duration_s': 30, 'source': 'local'}))
            (d/'frames.json').write_text(json.dumps({'frames': [], 'detected_scene_changes': 0, 'scene_threshold': .22}))
            (d/'asr.json').write_text(json.dumps({'segments': [{'start': 1, 'end': 4, 'text': 'We ship on Friday.'}], 'status': 'speech detected', 'language': 'en'}))
            (d/'ocr.json').write_text('[]')
            (d/'transcript.md').write_text('# Transcript\n\n- [00:01.00–00:04.00] We ship on Friday, after the review.\n')
            (d/'ocr.md').write_text('-'); (d/'visual.md').write_text('frames')
            body = '## TL;DR\n\nA release update [audio 00:01].\n\n## Key points\n\n- ships Friday [audio 00:01]\n\n## Timeline\n\n00:00–00:30 update\n\n## On screen\n\nA slide [screen 00:02].\n\n## Key quotes\n'
            response = {'choices': [{'message': {'content': body}, 'finish_reason': 'stop'}]}
            manifest = out/'m.json'; manifest.write_text(json.dumps([{'id': 'talk', 'input': 'cached'}]))
            argv = ['vidsum', 'run', '--manifest', str(manifest), '--output', str(out), '--stage', 'summarize', '--model', 'text', '--asr-model', 'unused']
            with patch.object(sys, 'argv', argv), patch('vidsum.cli.heavy', side_effect=lambda *a: contextlib.nullcontext()), \
                 patch('vidsum.cli.server', return_value=contextlib.nullcontext('http://local')), patch('vidsum.cli.api', return_value=response) as api:
                cli.main()
            text = (d/'summary.md').read_text()
            prompt = api.call_args[0][1]['messages'][0]['content']
            self.assertNotIn('private note', prompt)
            self.assertIn('private note', text)
            for h in ['## TL;DR', '## Key points', '## Timeline', '## On screen', '## Key quotes', '“We ship on Friday, after the review.”']:
                self.assertIn(h, text)
            self.assertNotIn('## Data', text)


if __name__ == '__main__':
    unittest.main()
