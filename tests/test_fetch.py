"""Link input: share-text parsing, short links, yt-dlp plumbing, fallbacks and routing. No network."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from vidsum import cli, fetch

DOUYIN_SHARE = ('7.94 复制打开抖音，看看【某某的作品】功夫课怎么教？# 太极 # 出海 '
                'https://v.douyin.com/AbC-12_xy/ x@F.Ub 09/12 kpQ:/')
XHS_SHARE = ('【机器手怎么做 - 某某 | 小红书 - 你的生活兴趣社区】 😆 a1B2c3 😆 '
             'https://www.xiaohongshu.com/discovery/item/64b000000000000000000001?source=webshare&xsec_token=AB1=&xsec_source=pc_share')


class ShareText(unittest.TestCase):
    def test_finds_link_inside_chinese_share_text(self):
        self.assertEqual(fetch.find_url(DOUYIN_SHARE), 'https://v.douyin.com/AbC-12_xy/')
        self.assertTrue(fetch.find_url(XHS_SHARE).endswith('xsec_source=pc_share'))
        self.assertEqual(fetch.find_url('【标题】https://b23.tv/AbCd12，快来看'), 'https://b23.tv/AbCd12')
        self.assertEqual(fetch.find_url('see https://youtu.be/jNQXAC9IVRw.'), 'https://youtu.be/jNQXAC9IVRw')
        self.assertIsNone(fetch.find_url('some share text'))
        self.assertIsNone(fetch.find_url(''))

    def test_platforms(self):
        cases = {'https://v.douyin.com/x/': 'douyin', 'https://www.iesdouyin.com/share/video/1/': 'douyin',
                 'https://xhslink.com/a/x': 'xiaohongshu', 'https://www.rednote.com/explore/ab': 'xiaohongshu',
                 'https://m.bilibili.com/video/BV1': 'bilibili', 'https://youtu.be/x': 'youtube',
                 'https://www.tiktok.com/@a/video/1': 'tiktok', 'https://x.com/a/status/1': 'x',
                 'https://twitter.com/a/status/1': 'x', 'https://weibo.com/tv/show/1': 'weibo',
                 'https://vimeo.com/1': 'vimeo', 'https://example.org/v': 'web', 'https://notdouyin.com/': 'web'}
        for url, kind in cases.items():
            self.assertEqual(fetch.platform_of(url), kind, url)

    def test_normalize(self):
        self.assertEqual(fetch.normalize('https://www.iesdouyin.com/share/video/7300000000000000001/?from_ssr=1&region=US'),
                         'https://www.douyin.com/video/7300000000000000001')
        self.assertEqual(fetch.normalize('https://www.douyin.com/discover?modal_id=7300000000000000001'),
                         'https://www.douyin.com/video/7300000000000000001')
        self.assertEqual(fetch.normalize('https://www.iesdouyin.com/share/note/7300000000000000001/'),
                         'https://www.douyin.com/note/7300000000000000001')
        self.assertEqual(fetch.normalize('https://www.rednote.com/explore/64b000000000000000000001?xsec_token=A'),
                         'https://www.xiaohongshu.com/explore/64b000000000000000000001?xsec_token=A')
        self.assertEqual(fetch.normalize('https://www.youtube.com/watch?v=x'), 'https://www.youtube.com/watch?v=x')

    def test_direct_media(self):
        self.assertTrue(fetch.is_direct_media('https://example.org/files/clip.MP4?sig=1'))
        self.assertFalse(fetch.is_direct_media('https://www.youtube.com/watch?v=x'))


class FakeResponse:
    def __init__(self, location):
        self.headers = {'Location': location} if location else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ShortLinks(unittest.TestCase):
    def test_follows_share_redirects_only(self):
        hops = {'https://v.douyin.com/AbC/': '/redirect', 'https://v.douyin.com/redirect':
                'https://www.iesdouyin.com/share/video/7300000000000000001/?region=US'}
        seen = []

        class Opener:
            def open(self, request, timeout=None):
                seen.append(request.full_url)
                return FakeResponse(hops.get(request.full_url))
        out = fetch.resolve_short('https://v.douyin.com/AbC/', opener=Opener())
        self.assertEqual(out, 'https://www.iesdouyin.com/share/video/7300000000000000001/?region=US')
        self.assertEqual(seen, ['https://v.douyin.com/AbC/', 'https://v.douyin.com/redirect'])
        self.assertEqual(fetch.resolve_short('https://www.youtube.com/watch?v=x', opener=Opener()), 'https://www.youtube.com/watch?v=x')

    def test_redirect_reported_as_http_error(self):
        class Opener:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, 302, 'Found', {'Location': 'https://www.xiaohongshu.com/explore/ab?xsec_token=T'}, io.BytesIO())
        self.assertEqual(fetch.resolve_short('https://xhslink.com/a/x', opener=Opener()), 'https://www.xiaohongshu.com/explore/ab?xsec_token=T')


class YtDlpPlumbing(unittest.TestCase):
    def test_command_flags(self):
        d = Path('/tmp/item')
        cmd = fetch.ytdlp_command('https://www.bilibili.com/video/BV1', d, cookies='/tmp/copy/cookies.txt', cookies_from_browser='firefox:work')
        self.assertEqual(cmd[1:3], ['-m', 'yt_dlp'])
        self.assertIn('--ignore-config', cmd); self.assertIn('--no-playlist', cmd)
        self.assertEqual(cmd[cmd.index('--playlist-items') + 1], '1')
        self.assertEqual(cmd[cmd.index('--match-filters') + 1], '!is_live')
        self.assertEqual(cmd[cmd.index('-S') + 1], 'res:720')
        self.assertTrue(cmd[cmd.index('--print') + 1].startswith('after_move:' + fetch.META_MARK + '%(.{'))
        self.assertNotIn('--write-info-json', cmd)
        self.assertEqual(cmd[cmd.index('--cookies-from-browser') + 1], 'firefox:work')
        self.assertEqual(cmd[cmd.index('--cookies') + 1], '/tmp/copy/cookies.txt')
        self.assertEqual(cmd[cmd.index('--http-chunk-size') + 1], '10M')
        self.assertEqual(cmd[-2:], ['--', 'https://www.bilibili.com/video/BV1'])
        plain = fetch.ytdlp_command('https://youtu.be/x', d)
        self.assertNotIn('--cookies', plain); self.assertNotIn('--cookies-from-browser', plain)

    def fake_ytdlp(self, work, body):
        """Patch target for ytdlp_command: runs body with argv = [item_dir, cookies_path_or_empty]."""
        script = Path(work) / 'fake_ytdlp.py'
        script.write_text(textwrap.dedent(body))
        return lambda url, d, cookies=None, *a, **k: [sys.executable, str(script), str(d), str(cookies or '')]

    def test_run_reads_printed_meta_and_writes_nothing_else(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            command = self.fake_ytdlp(work, f'''
                import json, sys
                from pathlib import Path
                d = Path(sys.argv[1])
                print('[download] 100% of 1.00MiB', flush=True)
                (d / 'video.webm').write_bytes(b'media')
                print({fetch.META_MARK!r} + json.dumps({{'title': 'T', 'filepath': str(d / 'video.webm')}}))
            ''')
            lines = []
            with patch.object(fetch, 'ytdlp_command', command):
                info = fetch.run_ytdlp('https://youtu.be/x', d, log=lines.append)
            self.assertEqual(info['title'], 'T')
            self.assertEqual(sorted(p.name for p in d.iterdir()), ['video.mp4'])
            self.assertEqual(lines, ['[download] 100% of 1.00MiB'])

    def test_cookie_file_is_a_private_copy(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            mine = Path(work) / 'cookies.txt'; mine.write_text('# Netscape HTTP Cookie File\n')
            seen = Path(work) / 'seen.txt'
            command = self.fake_ytdlp(work, f'''
                import json, sys
                from pathlib import Path
                d, cookies = Path(sys.argv[1]), Path(sys.argv[2])
                Path({str(seen)!r}).write_text(str(cookies))
                cookies.write_text(cookies.read_text() + 'written back by yt-dlp\\n')
                (d / 'video.mp4').write_bytes(b'media')
                print({fetch.META_MARK!r} + json.dumps({{'filepath': str(d / 'video.mp4')}}))
            ''')
            with patch.object(fetch, 'ytdlp_command', command):
                fetch.run_ytdlp('https://youtu.be/x', d, cookies=str(mine), log=lambda line: None)
            copy = Path(seen.read_text())
            self.assertNotEqual(copy, mine); self.assertNotEqual(copy.parent, d)
            self.assertFalse(copy.exists())
            self.assertEqual(mine.read_text(), '# Netscape HTTP Cookie File\n')

    def test_cookie_options_checked(self):
        with tempfile.TemporaryDirectory() as work:
            mine = Path(work) / 'c.txt'; mine.write_text('x')
            with self.assertRaisesRegex(fetch.FetchError, 'not both'):
                fetch.check_cookie_options(str(mine), 'chrome')
            with self.assertRaisesRegex(fetch.FetchError, 'cookies file not found'):
                fetch.check_cookie_options(str(Path(work) / 'missing.txt'))
            fetch.check_cookie_options(str(mine)); fetch.check_cookie_options(None, 'chrome')

    def test_interrupt_stops_ytdlp(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            pid_file = Path(work) / 'pid'
            command = self.fake_ytdlp(work, f'''
                import os, time
                from pathlib import Path
                Path({str(pid_file)!r}).write_text(str(os.getpid()))
                print('[download]   1.0% of 1.00GiB', flush=True)
                time.sleep(60)
            ''')

            def interrupted(line):
                raise KeyboardInterrupt
            started = time.time()
            with patch.object(fetch, 'ytdlp_command', command), self.assertRaises(KeyboardInterrupt):
                fetch.run_ytdlp('https://youtu.be/x', d, log=interrupted)
            self.assertLess(time.time() - started, 30)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_byte_cap_stops_an_endless_download(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            pid_file = Path(work) / 'pid'
            command = self.fake_ytdlp(work, f'''
                import os, sys, time
                from pathlib import Path
                Path({str(pid_file)!r}).write_text(str(os.getpid()))
                with open(Path(sys.argv[1]) / 'video.mp3.part', 'wb') as out:
                    while True:
                        out.write(b'x' * 1024); out.flush(); time.sleep(0.01)
            ''')
            started = time.time()
            with patch.object(fetch, 'ytdlp_command', command), patch.object(fetch, 'WATCH_EVERY_S', 0.1):
                with self.assertRaisesRegex(fetch.TooBig, 'passed 0 GB and was stopped'):
                    fetch.run_ytdlp('http://radio.example/stream', d, log=lambda line: None, max_bytes=8192)
            self.assertLess(time.time() - started, 20)
            self.assertEqual(list(d.iterdir()), [])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_byte_cap_ignores_finished_parts_during_a_merge(self):
        # bv*+ba: the finished video and audio parts plus the merged output sit on disk together, about twice
        # the final size; none of them is a .part file, so a merge under the cap must not be stopped.
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            command = self.fake_ytdlp(work, f'''
                import json, os, sys, time
                from pathlib import Path
                d = Path(sys.argv[1])
                (d / 'video.f0.mp4').write_bytes(b'v' * 6000); (d / 'video.f1.m4a').write_bytes(b'a' * 3000)
                with open(d / 'video.temp.mp4', 'wb') as out:
                    for _ in range(9):
                        out.write(b'm' * 1000); out.flush(); time.sleep(0.05)
                os.replace(d / 'video.temp.mp4', d / 'video.mp4')
                (d / 'video.f0.mp4').unlink(); (d / 'video.f1.m4a').unlink()
                print({fetch.META_MARK!r} + json.dumps({{'title': 'merged', 'filepath': str(d / 'video.mp4')}}))
            ''')
            with patch.object(fetch, 'ytdlp_command', command), patch.object(fetch, 'WATCH_EVERY_S', 0.02):
                info = fetch.run_ytdlp('https://youtu.be/x', d, log=lambda line: None, max_bytes=12000)
            self.assertEqual(info['title'], 'merged')
            self.assertEqual((d / 'video.mp4').stat().st_size, 9000)

    def test_nothing_downloaded(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            command = self.fake_ytdlp(work, "print('[download] Finished downloading playlist: x')")
            with patch.object(fetch, 'ytdlp_command', command):
                with self.assertRaisesRegex(fetch.FetchError, 'live stream'):
                    fetch.run_ytdlp('https://www.youtube.com/@someone/live', d, log=lambda line: None)

    def test_playlist_link_says_first_entry(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            command = self.fake_ytdlp(work, f'''
                import json, sys
                from pathlib import Path
                d = Path(sys.argv[1]); (d / 'video.mp4').write_bytes(b'media')
                print({fetch.META_MARK!r} + json.dumps({{'filepath': str(d / 'video.mp4'), 'playlist_count': 5}}))
            ''')
            lines = []
            with patch.object(fetch, 'ytdlp_command', command):
                fetch.run_ytdlp('https://www.youtube.com/@someone/videos', d, log=lines.append)
            self.assertTrue(any('took the first one' in line for line in lines))

    def test_run_failure_reports_error_line(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work) / 'item'; d.mkdir()
            command = self.fake_ytdlp(work, '''
                import sys
                print('[vimeo] 1: Downloading webpage')
                print('ERROR: [vimeo] 1: The web client only works when logged-in. Use --cookies')
                sys.exit(1)
            ''')
            with patch.object(fetch, 'ytdlp_command', command):
                with self.assertRaisesRegex(fetch.FetchError, 'only works when logged-in'):
                    fetch.run_ytdlp('https://vimeo.com/1', d, log=lambda line: None)

    def test_meta_from_info(self):
        info = {'title': 'Me at the zoo', 'uploader': 'jawed', 'channel_follower_count': 10, 'timestamp': 1114214400,
                'like_count': 5, 'comment_count': 2, 'repost_count': None, 'duration': 19, 'webpage_url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw'}
        meta = fetch.meta_from_info(info, 'https://youtu.be/jNQXAC9IVRw', 'youtube')
        self.assertEqual(meta['source'], 'youtube'); self.assertEqual(meta['author'], 'jawed')
        self.assertEqual(meta['published_at'], '2005-04-23T00:00:00Z')
        self.assertEqual(meta['link'], 'https://youtu.be/jNQXAC9IVRw')
        self.assertTrue(meta['fetched_with'].startswith('yt-dlp '))
        self.assertEqual(fetch.meta_from_info({'upload_date': '20240102'}, 'l', 'web')['published_at'], '2024-01-02')


class Routing(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); self.d = Path(self.work.name)
        self.probe = lambda path: None

    def tearDown(self):
        self.work.cleanup()

    def test_youtube_goes_to_ytdlp(self):
        calls = []

        def fake(url, d, cookies=None, cookies_from_browser=None, log=None):
            calls.append((url, cookies, cookies_from_browser)); (d / 'video.mp4').write_bytes(b'x'); return {'title': 't'}
        with patch.object(fetch, 'run_ytdlp', fake):
            meta = fetch.fetch('watch this https://www.youtube.com/watch?v=x', self.d, self.probe, cookies_from_browser='chrome')
        self.assertEqual(calls, [('https://www.youtube.com/watch?v=x', None, 'chrome')])
        self.assertEqual(meta['source'], 'youtube')

    def test_login_error_gets_cookie_hint(self):
        def fake(*a, **k):
            raise fetch.FetchError('ERROR: [instagram] x: Requested content is not available, rate-limit reached or login required')
        with patch.object(fetch, 'run_ytdlp', fake):
            with self.assertRaisesRegex(fetch.FetchError, '--cookies-from-browser chrome'):
                fetch.fetch('https://www.instagram.com/reel/x/', self.d, self.probe, log=lambda line: None)

    def test_douyin_uses_browser_route_first(self):
        def fake_douyin(url, link, d, probe):
            (d / 'video.mp4').write_bytes(b'x'); return {'source': 'douyin', 'link': link, 'url': url}
        with patch.object(fetch, 'resolve_short', return_value='https://www.iesdouyin.com/share/video/7300000000000000001/'), \
             patch.object(fetch, 'browser_available', return_value=True), patch.object(fetch, 'douyin_fetch', fake_douyin), \
             patch.object(fetch, 'run_ytdlp', side_effect=AssertionError('yt-dlp should not run')):
            meta = fetch.fetch(DOUYIN_SHARE, self.d, self.probe)
        self.assertEqual(meta['url'], 'https://www.douyin.com/video/7300000000000000001')
        self.assertEqual(meta['link'], 'https://v.douyin.com/AbC-12_xy/')

    def test_douyin_without_browser_extra_says_how(self):
        with patch.object(fetch, 'resolve_short', return_value='https://www.douyin.com/video/7300000000000000001'), \
             patch.object(fetch, 'browser_available', return_value=False), \
             patch.object(fetch, 'run_ytdlp', side_effect=fetch.FetchError('ERROR: [Douyin] Fresh cookies (not necessarily logged in) are needed')):
            with self.assertRaisesRegex(fetch.FetchError, r'vidsum-local\[browser\]'):
                fetch.fetch('https://v.douyin.com/x/', self.d, self.probe, log=lambda line: None)

    def test_vimeo_retries_player_url(self):
        urls = []

        def fake(url, d, *a, **k):
            urls.append(url)
            if 'player.' not in url:
                raise fetch.FetchError('ERROR: [vimeo] 1: The web client only works when logged-in')
            (d / 'video.mp4').write_bytes(b'x'); return {'title': 'v'}
        with patch.object(fetch, 'run_ytdlp', fake):
            meta = fetch.fetch('https://vimeo.com/123456789', self.d, self.probe, log=lambda line: None)
        self.assertEqual(urls, ['https://vimeo.com/123456789', 'https://player.vimeo.com/video/123456789'])
        self.assertEqual(meta['source'], 'vimeo')

    def test_vimeo_player_retry_does_not_hide_a_final_error(self):
        calls = []

        def fake(url, d, *a, **k):
            calls.append(url)
            if len(calls) == 1:
                raise fetch.FetchError('ERROR: [vimeo] 1: The web client only works when logged-in')
            raise fetch.TooBig('the download passed 4 GB and was stopped')
        with patch.object(fetch, 'run_ytdlp', fake):
            with self.assertRaises(fetch.TooBig):
                fetch.fetch('https://vimeo.com/123456789', self.d, self.probe, log=lambda line: None)
        self.assertEqual(len(calls), 2)

    def test_page_ending_in_webm_falls_back_to_ytdlp(self):
        def not_media(url, d, probe):
            raise fetch.FetchError('URL is not a media file (Content-Type text/html)')

        def fake(url, d, *a, **k):
            (d / 'video.mp4').write_bytes(b'x'); return {'title': 'wiki'}
        with patch.object(fetch, 'download_direct', not_media), patch.object(fetch, 'run_ytdlp', fake), \
             patch.object(fetch, 'check_web_stream', lambda url: None):
            meta = fetch.fetch('https://commons.wikimedia.org/wiki/File:Clip.webm', self.d, self.probe)
        self.assertEqual(meta['title'], 'wiki')


class DouyinAndDirect(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); self.d = Path(self.work.name)
        self.probe = lambda path: None

    def tearDown(self):
        self.work.cleanup()

    def test_profile_link_is_not_a_video(self):
        with patch.object(fetch, 'resolve_short', return_value='https://www.iesdouyin.com/share/user/MS4wLjABAAAA'), \
             patch.object(fetch, 'browser_available', return_value=True), \
             patch.object(fetch, 'run_ytdlp', side_effect=AssertionError('yt-dlp should not run')):
            with self.assertRaisesRegex(fetch.NotAVideo, 'not a Douyin video link'):
                fetch.fetch('https://v.douyin.com/x/', self.d, self.probe, log=lambda line: None)

    def test_photo_post_stops_without_fallback(self):
        with patch.object(fetch, 'resolve_short', return_value='https://www.douyin.com/note/7300000000000000001'), \
             patch.object(fetch, 'browser_available', return_value=True), \
             patch.object(fetch, 'douyin_detail', return_value={'images': [{}], 'video': {}}), \
             patch.object(fetch, 'run_ytdlp', side_effect=AssertionError('yt-dlp should not run')):
            with self.assertRaisesRegex(fetch.NotAVideo, 'photo post'):
                fetch.fetch('https://v.douyin.com/x/', self.d, self.probe, log=lambda line: None)

    def test_browser_reason_reaches_the_final_error(self):
        with patch.object(fetch, 'resolve_short', return_value='https://www.douyin.com/video/7300000000000000001'), \
             patch.object(fetch, 'browser_available', return_value=True), \
             patch.object(fetch, 'douyin_fetch', side_effect=fetch.FetchError('no browser for the Douyin fallback: install Google Chrome')), \
             patch.object(fetch, 'run_ytdlp', side_effect=fetch.FetchError('ERROR: [Douyin] Fresh cookies (not necessarily logged in) are needed')):
            with self.assertRaises(fetch.FetchError) as caught:
                fetch.fetch('https://v.douyin.com/x/', self.d, self.probe, log=lambda line: None)
        self.assertIn('install Google Chrome', str(caught.exception))
        self.assertNotIn('--cookies-from-browser', str(caught.exception))

    @unittest.skipUnless(fetch.browser_available(), 'playwright not installed')
    def test_browser_crash_becomes_fetch_error(self):
        from playwright.sync_api import Error as PlaywrightError
        with patch.object(fetch, '_douyin_detail', side_effect=PlaywrightError('Target page, context or browser has been closed')):
            with self.assertRaisesRegex(fetch.FetchError, 'headless browser failed: Target page'):
                fetch.douyin_detail('https://www.douyin.com/video/7300000000000000001')

    def test_direct_link_network_error(self):
        with patch.object(fetch, 'download_file', side_effect=urllib.error.URLError('timed out')):
            with self.assertRaisesRegex(fetch.FetchError, 'timed out'):
                fetch.download_direct('https://example.org/clip.mp4', self.d, self.probe)

    def test_short_link_network_error(self):
        class Opener:
            def open(self, request, timeout=None):
                raise urllib.error.URLError('nodename nor servname provided')
        with self.assertRaisesRegex(fetch.FetchError, 'could not open the share link'):
            fetch.resolve_short('https://b23.tv/AbCd12', opener=Opener())


class Streams(unittest.TestCase):
    """A direct link to something that never ends (internet radio) must not download forever."""

    def serve(self, headers, body=b'x' * 4096, repeat=1):
        import http.server, threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    for _ in range(repeat):
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def test_no_length_is_refused_without_fallback(self):
        base = self.serve({'Content-Type': 'audio/mpeg'}, repeat=50)
        with tempfile.TemporaryDirectory() as work, \
             patch.object(fetch, 'run_ytdlp', side_effect=AssertionError('yt-dlp should not run')):
            with self.assertRaisesRegex(fetch.NotAVideo, 'live stream'):
                fetch.fetch(base + '/radio.mp3', Path(work), lambda path: None, log=lambda line: None)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_icy_header_is_refused(self):
        base = self.serve({'Content-Type': 'audio/mpeg', 'icy-name': 'Radio', 'Content-Length': '4096'})
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(fetch.NotAVideo, 'live audio stream'):
                fetch.download_file(base + '/live.aac', Path(work) / 'video.mp4')

    def test_size_cap(self):
        base = self.serve({'Content-Type': 'video/mp4', 'Content-Length': str(10 * 4096)}, repeat=10)
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(fetch.FetchError, 'vidsum stops at'):
                fetch.download_file(base + '/big.mp4', Path(work) / 'video.mp4', max_bytes=8192)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_normal_file(self):
        base = self.serve({'Content-Type': 'video/mp4', 'Content-Length': '4096'})
        with tempfile.TemporaryDirectory() as work:
            self.assertEqual(fetch.download_file(base + '/clip.mp4', Path(work) / 'video.mp4'), 'video/mp4')
            self.assertEqual((Path(work) / 'video.mp4').stat().st_size, 4096)

    def serve_raw(self, reply):
        """A server that answers every connection with these bytes, then keeps sending (an old SHOUTcast radio)."""
        import socketserver, threading

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.recv(4096)
                    self.request.sendall(reply)
                    for _ in range(50):
                        self.request.sendall(b'x' * 4096)
                except OSError:
                    pass
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def run_fetch(self, url, ytdlp_runs=True):
        """fetch() with the real download code; yt-dlp is replaced and records whether it was reached."""
        reached = []

        def fake(url, d, *a, **k):
            reached.append(url); (d / 'video.mp4').write_bytes(b'x'); return {'title': 'from yt-dlp'}
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        d = Path(work.name)
        try:
            with patch.object(fetch, 'run_ytdlp', fake):
                return fetch.fetch(url, d, lambda path: None, log=lambda line: None), reached, d
        finally:
            self.assertEqual(bool(reached), ytdlp_runs)

    def test_page_at_a_webm_address_without_length_goes_to_ytdlp(self):
        # a wiki file page: HTML, no Content-Length, address ends in .webm (the round-3 regression)
        base = self.serve({'Content-Type': 'text/html; charset=utf-8'}, body=b'<html>' * 100)
        meta, reached, d = self.run_fetch(base + '/wiki/File:Clip.webm')
        self.assertEqual(meta['title'], 'from yt-dlp')
        self.assertEqual(sorted(p.name for p in d.iterdir()), ['video.mp4'])

    def test_web_page_without_suffix_goes_to_ytdlp(self):
        base = self.serve({'Content-Type': 'text/html'}, body=b'<html>' * 100)
        self.assertEqual(self.run_fetch(base + '/watch/123')[0]['title'], 'from yt-dlp')

    def test_header_check_failure_still_goes_to_ytdlp(self):
        import socket
        with socket.socket() as sock:  # a port nobody listens on: the check fails, yt-dlp decides as before
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        self.assertEqual(self.run_fetch(f'http://127.0.0.1:{port}/page')[0]['title'], 'from yt-dlp')

    def test_radio_without_suffix_is_refused_before_ytdlp(self):
        base = self.serve({'Content-Type': 'audio/mpeg'}, repeat=50)
        with self.assertRaisesRegex(fetch.NotAVideo, r"live stream.*curl -L -o clip\.mp4 'http://127\.0\.0\.1:\d+/stream'"):
            self.run_fetch(base + '/stream', ytdlp_runs=False)

    def test_radio_with_icy_headers_and_no_suffix_is_refused(self):
        base = self.serve({'Content-Type': 'text/plain', 'icy-metaint': '16000'}, repeat=50)
        with self.assertRaisesRegex(fetch.NotAVideo, 'live audio stream'):
            self.run_fetch(base + '/live', ytdlp_runs=False)

    def test_shoutcast_status_line_is_one_message(self):
        base = self.serve_raw(b'ICY 200 OK\r\nicy-name: Radio\r\ncontent-type: audio/mpeg\r\n\r\n')
        for path in ('/radio.mp3', '/live'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(fetch.NotAVideo, 'live audio stream'):
                    self.run_fetch(base + path, ytdlp_runs=False)

    def test_over_the_cap_stops_without_fallback(self):
        base = self.serve({'Content-Type': 'video/mp4', 'Content-Length': str(5 * 1024 ** 3)})
        with self.assertRaisesRegex(fetch.TooBig, '5.0 GB; vidsum stops at 4 GB'):
            self.run_fetch(base + '/huge.mp4', ytdlp_runs=False)
        with self.assertRaisesRegex(fetch.TooBig, '5.0 GB'):
            self.run_fetch(base + '/download?id=1', ytdlp_runs=False)

    def test_ogg_live_stream_is_refused(self):
        base = self.serve({'Content-Type': 'application/ogg'}, repeat=50)
        for path in ('/live.ogg', '/live'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(fetch.NotAVideo, 'live stream'):
                    self.run_fetch(base + path, ytdlp_runs=False)

    def test_octet_stream_without_length_and_suffix_is_refused(self):
        base = self.serve({'Content-Type': 'application/octet-stream'}, repeat=50)
        with self.assertRaisesRegex(fetch.NotAVideo, 'live stream'):
            self.run_fetch(base + '/stream', ytdlp_runs=False)

    def test_playlists_without_length_go_to_ytdlp(self):
        # an HLS or DASH manifest is a short text file; yt-dlp reads it and skips live ones with its filter
        for ctype in ('audio/mpegurl', 'application/vnd.apple.mpegurl', 'application/x-mpegURL', 'application/dash+xml'):
            with self.subTest(ctype=ctype):
                base = self.serve({'Content-Type': ctype}, body=b'#EXTM3U\n')
                self.assertEqual(self.run_fetch(base + '/hls/index.m3u8')[0]['title'], 'from yt-dlp')

    def test_direct_link_that_is_a_web_page(self):
        base = self.serve({'Content-Type': 'text/html', 'Content-Length': '4096'})
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(fetch.FetchError, 'not a media file') as caught:
                fetch.download_direct(base + '/clip.mp4', Path(work), lambda path: None)
            self.assertNotIsInstance(caught.exception, fetch.Final)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_douyin_cdn_without_length_is_accepted_up_to_the_cap(self):
        base = self.serve({'Content-Type': 'application/x-unknown'}, repeat=3)
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / 'video.mp4'
            fetch.download_file(base + '/v/abc', target, known_video=True)
            self.assertEqual(target.stat().st_size, 3 * 4096)
            target.unlink()
            with self.assertRaises(fetch.TooBig):
                fetch.download_file(base + '/v/abc', target, known_video=True, max_bytes=5000)
            self.assertEqual(list(Path(work).iterdir()), [])


class McpTimeout(unittest.TestCase):
    def test_timeout_stops_the_cli(self):
        import subprocess
        from vidsum import mcp_server
        with self.assertRaises(subprocess.TimeoutExpired):
            mcp_server._run(['--help'], timeout=0.001)


class CliWiring(unittest.TestCase):
    def test_share_text_reaches_fetch_with_cookie_options(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work)
            seen = {}

            def fake(text, item, probe, cookies=None, cookies_from_browser=None, log=None):
                seen.update(text=text, cookies=cookies, browser=cookies_from_browser)
                (item / 'video.mp4').write_bytes(b'x')
                return {'source': 'douyin', 'link': 'https://v.douyin.com/AbC-12_xy/', 'title': 't'}
            args = SimpleNamespace(fetch_command=None, cookies='c.txt', cookies_from_browser='safari')
            links = {}
            with patch.object(cli, 'fetch_link', fake):
                meta = cli.prepare_input({'input': DOUYIN_SHARE}, d, args, 'zh', links)
            self.assertEqual(seen, {'text': DOUYIN_SHARE, 'cookies': 'c.txt', 'browser': 'safari'})
            self.assertEqual(meta['source'], 'douyin')
            self.assertIn('https://v.douyin.com/AbC-12_xy/', links)

    def test_cache_needs_the_same_link(self):
        with tempfile.TemporaryDirectory() as work:
            part1 = Path(work) / 'part1'; part1.mkdir()
            (part1 / 'video.mp4').write_bytes(b'part1'); (part1 / 'meta.json').write_text('{"title": "p1"}')
            d = Path(work) / 'item'; d.mkdir()
            links = {'https://www.bilibili.com/video/BV1xx': part1}
            args = SimpleNamespace(fetch_command=None, cookies=None, cookies_from_browser=None)

            def fake(text, item, probe, **k):
                (item / 'video.mp4').write_bytes(b'part2'); return {'link': fetch.find_url(text), 'title': 'p2'}
            with patch.object(cli, 'fetch_link', fake):
                meta = cli.prepare_input({'input': 'https://www.bilibili.com/video/BV1xx?p=2'}, d, args, 'en', links)
            self.assertEqual(meta['title'], 'p2')
            d2 = Path(work) / 'item2'; d2.mkdir()
            with patch.object(cli, 'fetch_link', side_effect=AssertionError('cached')):
                meta = cli.prepare_input({'input': '看这个 https://www.bilibili.com/video/BV1xx 。'}, d2, args, 'en', links)
            self.assertEqual(meta['title'], 'p1'); self.assertEqual((d2 / 'video.mp4').read_bytes(), b'part1')

    def test_long_share_text_is_not_treated_as_a_path(self):
        text = '很长的分享文字' * 200 + ' https://v.douyin.com/AbC-12_xy/'
        with tempfile.TemporaryDirectory() as work:
            args = SimpleNamespace(fetch_command=None, cookies=None, cookies_from_browser=None)

            def fake(t, item, probe, **k):
                (item / 'video.mp4').write_bytes(b'x'); return {'link': fetch.find_url(t)}
            with patch.object(cli, 'fetch_link', fake):
                meta = cli.prepare_input({'input': text}, Path(work), args, 'zh', {})
        self.assertEqual(meta['link'], 'https://v.douyin.com/AbC-12_xy/')

    def test_transcribe_link_failure_is_one_line(self):
        with tempfile.TemporaryDirectory() as work:
            args = SimpleNamespace(input='https://www.instagram.com/reel/x/', output=work, language='en', lock_state=None,
                                   format='srt', fetch_command=None, cookies=None, cookies_from_browser=None)
            err = io.StringIO()
            with patch.object(cli, 'check_tools'), patch.object(cli, 'configure'), \
                 patch.object(cli, 'fetch_link', side_effect=fetch.FetchError('could not download x: login required')), \
                 patch('sys.stderr', err):
                self.assertEqual(cli.cmd_transcribe(args), 1)
            self.assertIn('failed: could not download x: login required', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_cookie_flags_parse(self):
        args = cli.parser().parse_args(cli.normalize_argv(['https://youtu.be/x', '--cookies-from-browser', 'chrome', '--cookies', 'c.txt']))
        self.assertEqual((args.command, args.inputs, args.cookies_from_browser, args.cookies), ('run', ['https://youtu.be/x'], 'chrome', 'c.txt'))
        args = cli.parser().parse_args(['transcribe', 'https://youtu.be/x', '--cookies-from-browser', 'firefox'])
        self.assertEqual(args.cookies_from_browser, 'firefox')


class NoCookies(unittest.TestCase):
    """--no-cookies: no cookies file, no browser cookies, no VIDSUM_COOKIES* or VIDSUM_FETCH_COMMAND; the default is unchanged."""
    ENV = {'VIDSUM_COOKIES': '/nowhere/cookies.txt', 'VIDSUM_COOKIES_FROM_BROWSER': 'chrome', 'VIDSUM_FETCH_COMMAND': 'my-downloader'}

    def parsed(self, argv):
        p = cli.parser()
        args = p.parse_args(cli.normalize_argv(argv))
        cli.apply_env_options(p, args)
        return args

    def test_switch_leaves_the_environment_unread(self):
        with patch.dict(os.environ, self.ENV):
            for argv in (['https://youtu.be/x', '--no-cookies'], ['transcribe', 'https://youtu.be/x', '--no-cookies']):
                args = self.parsed(argv)
                self.assertTrue(args.no_cookies)
                self.assertEqual((args.cookies, args.cookies_from_browser, args.fetch_command), (None, None, None))

    def test_without_switch_the_environment_still_applies(self):
        with patch.dict(os.environ, self.ENV):
            args = self.parsed(['https://youtu.be/x'])
            self.assertFalse(args.no_cookies)
            self.assertEqual((args.cookies, args.cookies_from_browser, args.fetch_command),
                             ('/nowhere/cookies.txt', 'chrome', 'my-downloader'))
            args = self.parsed(['transcribe', 'https://youtu.be/x', '--cookies-from-browser', 'firefox'])
            self.assertEqual(args.cookies_from_browser, 'firefox')  # a flag still wins over the variable
        with patch.dict(os.environ, {}, clear=True):
            args = self.parsed(['https://youtu.be/x'])
            self.assertEqual((args.cookies, args.cookies_from_browser, args.fetch_command), (None, None, None))

    def test_switch_refuses_cookie_flags(self):
        for extra in (['--cookies', 'c.txt'], ['--cookies-from-browser', 'chrome'], ['--fetch-command', 'dl']):
            with patch('sys.stderr', io.StringIO()) as err, self.assertRaises(SystemExit):
                self.parsed(['https://youtu.be/x', '--no-cookies', *extra])
            self.assertIn('--no-cookies cannot be combined with ' + extra[0], err.getvalue())

    def test_main_applies_the_switch(self):
        seen = {}
        with patch.dict(os.environ, self.ENV), patch.object(cli, 'cmd_transcribe', lambda args: seen.update(vars(args)) or 0):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['transcribe', 'https://youtu.be/x', '--no-cookies'])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual((seen['cookies'], seen['cookies_from_browser'], seen['fetch_command']), (None, None, None))

    def test_ytdlp_command_loads_no_cookies(self):
        cmd = fetch.ytdlp_command('https://youtu.be/x', Path('/tmp/item'), cookies='/tmp/c.txt',
                                  cookies_from_browser='chrome', no_cookies=True)
        self.assertIn('--no-cookies', cmd); self.assertIn('--no-cookies-from-browser', cmd); self.assertIn('--ignore-config', cmd)
        self.assertNotIn('--cookies', cmd); self.assertNotIn('--cookies-from-browser', cmd); self.assertNotIn('/tmp/c.txt', cmd)
        self.assertEqual(cmd[-2:], ['--', 'https://youtu.be/x'])

    def test_run_ytdlp_gets_no_cookies(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work); seen = []

            def fake_command(url, item, *a, **k):
                seen.append((a, k)); return [sys.executable, '-c', 'pass']
            # the stand-in downloads nothing, so run_ytdlp ends with "downloaded nothing"; what it was asked to run matters
            with patch.object(fetch, 'ytdlp_command', fake_command), self.assertRaisesRegex(fetch.FetchError, 'downloaded nothing'):
                fetch.run_ytdlp('https://youtu.be/x', d, cookies='/nowhere/c.txt', cookies_from_browser='chrome',
                                log=lambda line: None, no_cookies=True)
        self.assertEqual(seen, [((), {'no_cookies': True})])

    def test_fetch_passes_nothing_and_hints_at_a_file(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work); calls = []

            def fake(url, item, *a, **k):
                calls.append((a, k)); (item / 'video.mp4').write_bytes(b'x'); return {'title': 't'}
            with patch.object(fetch, 'run_ytdlp', fake):
                fetch.fetch('https://www.youtube.com/watch?v=x', d, lambda path: None, cookies='/nowhere/c.txt',
                            cookies_from_browser='chrome', log=lambda line: None, no_cookies=True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], ()); self.assertTrue(calls[0][1]['no_cookies'])

            def login(*a, **k):
                raise fetch.FetchError('ERROR: [instagram] x: login required. Use --cookies')
            with patch.object(fetch, 'run_ytdlp', login):
                with self.assertRaises(fetch.FetchError) as caught:
                    fetch.fetch('https://www.instagram.com/reel/x/', d, lambda path: None, log=lambda line: None, no_cookies=True)
            message = str(caught.exception)
            self.assertIn('pass the file', message); self.assertNotIn('--cookies-from-browser', message)

    def test_prepare_input_skips_cookies_and_custom_downloader(self):
        with tempfile.TemporaryDirectory() as work:
            d = Path(work); seen = {}

            def fake(text, item, probe, **k):
                seen.update(k); (item / 'video.mp4').write_bytes(b'x'); return {'link': fetch.find_url(text), 'title': 't'}
            args = SimpleNamespace(fetch_command='dl', cookies='c.txt', cookies_from_browser='safari', no_cookies=True)
            with patch.object(cli, 'fetch_link', fake), patch.object(cli, 'fetch_external', side_effect=AssertionError('custom')):
                cli.prepare_input({'input': DOUYIN_SHARE}, d, args, 'zh', {})
        self.assertEqual((seen['cookies'], seen['cookies_from_browser'], seen['no_cookies']), (None, None, True))


class PublicClaims(unittest.TestCase):
    """The docs, the skill and the MCP descriptions must not say links are unsupported."""
    ROOT = Path(__file__).resolve().parents[1]

    def test_no_stale_no_scraping_lines(self):
        files = ['README.md', 'skills/vidsum/SKILL.md', 'vidsum/mcp_server.py', 'vidsum/cli.py', 'server.json',
                 '.claude-plugin/plugin.json', '.claude-plugin/marketplace.json', 'pyproject.toml']
        stale = ['does not scrape', 'No platform scraping', 'must be downloaded by the user first', 'not a YouTube/TikTok page',
                 '不抓抖音', 'download platform links first', 'Download platform links yourself first']
        for name in files:
            text = (self.ROOT / name).read_text()
            for phrase in stale:
                self.assertFalse(phrase in text, f'{name}: {phrase}')

    def test_plugin_launcher_is_pinned(self):
        # The Anthropic plugin directory blocks an unpinned uvx launcher; the pin must follow each release.
        from vidsum import __version__
        args = json.loads((self.ROOT / '.claude-plugin/plugin.json').read_text())['mcpServers']['vidsum']['args']
        self.assertEqual(args, ['--from', f'vidsum-local[browser]=={__version__}', 'vidsum-local', 'mcp'])

    def test_mcp_descriptions_name_links(self):
        from vidsum import mcp_server
        self.assertIn('Douyin', mcp_server.summarize_video.__doc__)
        self.assertIn('share text', mcp_server.transcribe_video.__doc__)
        self.assertTrue(mcp_server.READ_ONLY.open_world_hint)


if __name__ == '__main__':
    unittest.main()
