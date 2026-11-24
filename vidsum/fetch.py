"""Turn a link, or pasted share text, into video.mp4 and meta.json in an item folder.

yt-dlp does the work for most sites (YouTube, Bilibili, TikTok, Instagram, X, Weibo,
Xiaohongshu share links, and the long tail it supports). One link gives one video: playlists
and channels stop after the first entry, live streams (yt-dlp's live flag, internet radio)
are skipped, and no download goes past MAX_BYTES. Two small fallbacks cover what yt-dlp
cannot do on its own:

- Douyin: yt-dlp's extractor needs signed requests. With the optional extra
  `vidsum-local[browser]`, a headless Chrome/Chromium opens the public video page and the
  page's own detail response gives the file address. No login, nothing stored.
- Vimeo: some vimeo.com pages want a login; the public player URL usually does not.

Only process videos you have the right to use, and follow each site's terms.
"""
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MEDIA_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi', '.flv', '.wmv', '.mpg', '.mpeg', '.ts', '.3gp',
             '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac'}
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/142.0.0.0 Safari/537.36')  # share-link redirects and Douyin's CDN answer browsers
# Plain file servers get an honest name: some (Cloudflare, 11-24) refuse a browser name that is not a browser.
FILE_UA = 'vidsum (+https://github.com/allenwu-blip/vidsum)'

# A link ends at whitespace, quotes, brackets, CJK text or full-width punctuation, so a URL can be
# pulled out of share text such as "7.94 复制打开抖音，看看【…】 https://v.douyin.com/abc/ 复制此链接…".
_URL = re.compile(r'https?://[^\s"\'<>‘-‟　-ヿ㐀-鿿가-힯＀-￯]+', re.I)
_TRAILING = '.,;:!?)]}>…'

PLATFORMS = {
    'douyin': ('douyin.com', 'iesdouyin.com'),
    'xiaohongshu': ('xiaohongshu.com', 'xhslink.com', 'rednote.com'),
    'bilibili': ('bilibili.com', 'b23.tv'),
    'youtube': ('youtube.com', 'youtu.be'),
    'tiktok': ('tiktok.com',),
    'instagram': ('instagram.com',),
    'x': ('x.com', 'twitter.com'),
    'weibo': ('weibo.com', 'weibo.cn'),
    'kuaishou': ('kuaishou.com', 'chenzhongtech.com', 'gifshow.com'),
    'vimeo': ('vimeo.com',),
}
# Share links that only redirect; yt-dlp does not know all of them, so they are resolved first.
SHORT_HOSTS = ('v.douyin.com', 'xhslink.com', 'b23.tv', 'v.kuaishou.com')
LOGIN_WORDS = ('cookie', 'log in', 'login', 'logged-in', 'sign in', 'authenticat', 'private', 'confirm your age',
               'not a bot', 'rate-limit', 'ip address is blocked')


class FetchError(RuntimeError):
    pass


class Final(FetchError):
    """Another route would not help, so fetch() stops here instead of handing the link to yt-dlp."""


class NotAVideo(Final):
    """The link works but points at something that is not a recorded video (a photo post, a profile, a live stream)."""


class TooBig(Final):
    """The download is over MAX_BYTES."""


MAX_BYTES = 4 * 1024 ** 3
OCTET = ('application/octet-stream', 'binary/octet-stream')
# Playlists (HLS, DASH) are short text files that point at the media; yt-dlp reads them and its live filter
# applies, so they never count as a media answer here even when sent as audio/mpegurl.
PLAYLIST_TYPES = ('mpegurl', 'dash+xml')
LIVE_AUDIO = 'this address is a live audio stream (internet radio); vidsum skips live streams'


# The only fields read from yt-dlp. They come back on one marked stdout line, so no info.json (which
# holds signed media URLs, request headers and per-format cookies) is ever written to disk.
META_FIELDS = ('id', 'title', 'fulltitle', 'uploader', 'channel', 'creator', 'channel_follower_count', 'timestamp',
               'upload_date', 'like_count', 'comment_count', 'repost_count', 'duration', 'webpage_url', 'filepath',
               'playlist_count')
META_MARK = 'VIDSUM_META '


def find_url(text):
    """The first http(s) link in text, without trailing punctuation; None if there is none."""
    match = _URL.search(text or '')
    return match.group(0).rstrip(_TRAILING) if match else None


def host_of(url):
    return (urllib.parse.urlparse(url).hostname or '').lower()


def platform_of(url):
    host = host_of(url)
    for name, hosts in PLATFORMS.items():
        if any(host == h or host.endswith('.' + h) for h in hosts):
            return name
    return 'web'


def is_direct_media(url):
    return Path(urllib.parse.urlparse(url).path).suffix.lower() in MEDIA_EXT


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_short(url, opener=None, hops=5):
    """Follow redirects of share short links (v.douyin.com, xhslink.com, b23.tv, ...) to the real page."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(hops):
        if host_of(url) not in SHORT_HOSTS:
            return url
        request = urllib.request.Request(url, headers={'User-Agent': UA})
        try:
            with opener.open(request, timeout=20) as response:
                location = response.headers.get('Location')
        except urllib.error.HTTPError as ex:
            location = ex.headers.get('Location') if 300 <= ex.code < 400 else None
            if not location:
                raise FetchError(f'share link returned HTTP {ex.code}: {url}')
        except (urllib.error.URLError, OSError) as ex:
            raise FetchError(f'could not open the share link {url}: {getattr(ex, "reason", ex)}')
        if not location:
            return url
        url = urllib.parse.urljoin(url, location)
    return url


def normalize(url):
    """Map share-page variants to the address yt-dlp and the fallbacks understand."""
    kind = platform_of(url)
    parts = urllib.parse.urlparse(url)
    if kind == 'douyin':
        match = re.search(r'(?:/(video|note|slides)/|modal_id=|vid=)(\d{15,})', url)
        if match:
            return f'https://www.douyin.com/{"note" if match.group(1) in ("note", "slides") else "video"}/{match.group(2)}'
    if kind == 'xiaohongshu' and parts.hostname and not parts.hostname.endswith('xhslink.com'):
        # yt-dlp only matches www.xiaohongshu.com; rednote.com is the same site.
        return urllib.parse.urlunparse(parts._replace(netloc='www.xiaohongshu.com', scheme='https'))
    return url


def needs_login(message):
    text = (message or '').lower()
    return any(word in text for word in LOGIN_WORDS)


def _ytdlp_version():
    try:
        from importlib.metadata import version
        return version('yt-dlp')
    except Exception:
        return 'unknown'


def ytdlp_command(url, d, cookies=None, cookies_from_browser=None, max_res=720):
    """cookies here is the path yt-dlp may rewrite on exit, so callers pass a private copy (run_ytdlp does)."""
    cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config',
           # one link, one recorded video: a playlist or channel stops after its first entry, live streams are skipped
           '--no-playlist', '--playlist-items', '1', '--match-filters', '!is_live',
           # best video+audio whose shorter side is at most max_res (so portrait 720x1280 counts as 720p)
           '-f', 'bv*+ba/b', '-S', f'res:{max_res}', '--merge-output-format', 'mp4',
           '-o', str(d / 'video.%(ext)s'), '--no-mtime', '--no-simulate',
           '--print', 'after_move:' + META_MARK + '%(.{' + ','.join(META_FIELDS) + '})j',
           # --print makes yt-dlp quiet; keep its progress lines
           '--progress', '--newline', '--progress-delta', '5',
           '--retries', '3', '--socket-timeout', '30',
           # Some CDNs (Bilibili's, 11-24) refuse requests without a Range header; chunked download sends one.
           '--http-chunk-size', '10M']
    if cookies:
        cmd += ['--cookies', str(cookies)]
    if cookies_from_browser:
        cmd += ['--cookies-from-browser', cookies_from_browser]
    return cmd + ['--', url]


def check_cookie_options(cookies=None, cookies_from_browser=None):
    """Give yt-dlp one source of cookies. With both, it would save the whole browser jar into the file."""
    if cookies and cookies_from_browser:
        raise FetchError('use either --cookies or --cookies-from-browser, not both '
                         '(for the MCP server: VIDSUM_COOKIES or VIDSUM_COOKIES_FROM_BROWSER)')
    if cookies and not Path(cookies).expanduser().is_file():
        raise FetchError(f'cookies file not found: {cookies} (give the full path)')


def _clean_item(d):
    for path in d.glob('video.*'):
        if path.name != 'video.mp4' or path.stat().st_size == 0:
            path.unlink(missing_ok=True)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


WATCH_EVERY_S = 2


def _watch_size(proc, d, max_bytes, over, done):
    """Backstop for an endless stream that got past the header checks: stop yt-dlp once its files pass max_bytes."""
    while not done.wait(WATCH_EVERY_S) and proc.poll() is None:
        size = 0
        # Only files still being written: during a merge the finished video and audio parts and the merged
        # output sit side by side, about twice the final size, and none of them is a .part file.
        for path in d.glob('video.*'):
            if not (path.name.endswith('.part') or '.part-Frag' in path.name):
                continue
            try:
                size += path.stat().st_size
            except OSError:  # yt-dlp renamed or removed it between glob and stat
                pass
        if size > max_bytes:
            over.append(size)
            proc.terminate()
            return


def run_ytdlp(url, d, cookies=None, cookies_from_browser=None, log=None, max_bytes=MAX_BYTES):
    """Download with yt-dlp in a child process. Progress goes to the log; returns the META_FIELDS it printed."""
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    check_cookie_options(cookies, cookies_from_browser)
    _clean_item(d)
    info, tail, over = {}, [], []
    with tempfile.TemporaryDirectory(prefix='vidsum-cookies-') as private:
        cookie_copy = None
        if cookies:  # yt-dlp writes its cookie jar back on exit; let it write to a copy, never the user's file
            cookie_copy = Path(private) / 'cookies.txt'
            shutil.copyfile(Path(cookies).expanduser(), cookie_copy)
            cookie_copy.chmod(0o600)
        proc = subprocess.Popen(ytdlp_command(url, d, cookie_copy, cookies_from_browser), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors='replace')
        # --max-filesize only works when the size is known up front, so the cap is enforced by watching the files
        done = threading.Event()
        watcher = threading.Thread(target=_watch_size, args=(proc, d, max_bytes, over, done), daemon=True)
        watcher.start()
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith(META_MARK):
                    try:
                        info = json.loads(line[len(META_MARK):])
                    except ValueError:
                        pass
                elif line:
                    log(line)
                    tail = (tail + [line])[-20:]
            proc.wait()
        finally:  # Ctrl-C, SIGTERM (the MCP server sends it on a timeout), or a log error: never leave yt-dlp running
            _stop(proc)
            proc.stdout.close()
            done.set()
            watcher.join(5)
    if over:
        _clean_item(d)
        (d / 'video.mp4').unlink(missing_ok=True)
        raise TooBig(f'the download passed {max_bytes / 1024 ** 3:.0f} GB and was stopped '
                     '(a live stream, or a very long video)')
    if proc.returncode != 0:
        errors = [line for line in tail if line.startswith('ERROR')]
        raise FetchError((errors[-1] if errors else (tail[-1] if tail else f'yt-dlp exited {proc.returncode}'))[:400])
    path = Path(info['filepath']) if info.get('filepath') else None
    if not (path and path.parent == d and path.is_file() and path.stat().st_size > 0):
        files = [p for p in sorted(d.glob('video.*')) if p.suffix not in ('.part', '.ytdl') and p.stat().st_size > 0]
        path = files[0] if files else None
    if path is None:
        _clean_item(d)
        raise FetchError('yt-dlp downloaded nothing: the link may be a live stream, an empty playlist or channel, '
                         'or a post without a video')
    if path.name != 'video.mp4':
        path.replace(d / 'video.mp4')
    if (info.get('playlist_count') or 0) > 1:
        log(f'this link lists {info["playlist_count"]} videos; vidsum took the first one')
    return info


def _iso(timestamp=None, day=None):
    if timestamp:
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(timestamp)))
    if day and re.fullmatch(r'\d{8}', str(day)):
        return f'{day[:4]}-{day[4:6]}-{day[6:]}'
    return None


def meta_from_info(info, link, kind):
    return dict(source=kind, link=link, url=info.get('webpage_url') or link,
                title=info.get('title') or info.get('fulltitle'),
                author=info.get('uploader') or info.get('channel') or info.get('creator'),
                followers=info.get('channel_follower_count'),
                published_at=_iso(info.get('timestamp'), info.get('upload_date')),
                likes=info.get('like_count'), comments=info.get('comment_count'), collects=None,
                shares=info.get('repost_count'), reported_duration_s=info.get('duration'),
                fetched_with='yt-dlp ' + _ytdlp_version())


def _media_type(headers):
    return (headers.get('Content-Type') or '').split(';')[0].strip().lower()


def _is_media(ctype):
    """Audio or video bytes (not a web page, not a playlist)."""
    if any(word in ctype for word in PLAYLIST_TYPES):
        return False
    return ctype.startswith(('video/', 'audio/')) or ctype == 'application/ogg' or ctype in OCTET


def _live_message(url):
    return ('the server sends this without a size, which usually means a live stream; vidsum skips live streams. '
            f"If it is an ordinary file, download it first (curl -L -o clip.mp4 '{url}') and pass the path")


def _is_radio(headers):
    return any(name.lower().startswith('icy-') for name in headers.keys())


def _check_media_headers(url, headers, max_bytes, need_length=True):
    """Refuse a media response that would never end (icy- radio headers, or no Content-Length) or is over the cap."""
    if _is_radio(headers):
        raise NotAVideo(LIVE_AUDIO)
    length = headers.get('Content-Length') or ''
    if not length.isdigit():
        if need_length:
            raise NotAVideo(_live_message(url))
        return
    if int(length) > max_bytes:
        raise TooBig(f'the file is {int(length) / 1024 ** 3:.1f} GB; vidsum stops at {max_bytes / 1024 ** 3:.0f} GB')


def _open(request, timeout):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except http.client.BadStatusLine as ex:  # old SHOUTcast servers answer "ICY 200 OK" instead of "HTTP/1.x 200"
        if str(ex.line).lstrip().upper().startswith('ICY'):
            raise NotAVideo(LIVE_AUDIO)
        raise


def download_file(url, target, headers=None, timeout=60, max_bytes=MAX_BYTES, known_video=False):
    """Fetch one media file; returns its Content-Type.

    A response that is not audio/video (a web page whose address ends in .mp4 or .webm) raises a plain
    FetchError, so the caller can let yt-dlp try. A media response that would never end (internet radio)
    raises NotAVideo before anything is written. known_video is for addresses taken from a site's own
    video details (Douyin's CDN): any type and a missing size are accepted, and only the byte cap applies.
    """
    request = urllib.request.Request(url, headers={'User-Agent': FILE_UA, **(headers or {})})
    part = target.with_name(target.name + '.part')
    try:
        with _open(request, timeout) as response:
            ctype = _media_type(response.headers)
            if not (not ctype or _is_media(ctype) or known_video or _is_radio(response.headers)):
                raise FetchError(f'URL is not a media file (Content-Type {ctype})')
            _check_media_headers(url, response.headers, max_bytes, need_length=not known_video)
            copied = 0
            with open(part, 'wb') as out:
                while chunk := response.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise TooBig(f'the download passed {max_bytes / 1024 ** 3:.0f} GB and was stopped')
                    out.write(chunk)
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)
    return ctype


def download_direct(url, d, probe):
    """A direct link to a media file (…/clip.mp4)."""
    try:
        download_file(url, d / 'video.part.mp4')
    except FetchError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as ex:
        raise FetchError(f'could not download {url}: {getattr(ex, "reason", None) or ex}')
    try:
        probe(d / 'video.part.mp4')
    except Exception:
        (d / 'video.part.mp4').unlink(missing_ok=True)
        raise FetchError('the downloaded file is not playable media')
    (d / 'video.part.mp4').replace(d / 'video.mp4')
    name = Path(urllib.parse.urlparse(url).path).name or url
    return dict(source='url', link=url, title=name, author=None, followers=None, published_at=None, likes=None,
                comments=None, collects=None, shares=None)


def check_web_stream(url, timeout=15, max_bytes=MAX_BYTES):
    """Before yt-dlp gets a plain web address: one GET, headers only. An endless audio/video answer (internet
    radio at /live or /stream) is refused here; yt-dlp would record it until the caller's timeout. If this
    request fails, or the answer is a web page or a finite file, yt-dlp decides as before."""
    request = urllib.request.Request(url, headers={'User-Agent': FILE_UA})
    try:
        with _open(request, timeout) as response:
            ctype = _media_type(response.headers)
            if _is_media(ctype) or _is_radio(response.headers):
                _check_media_headers(url, response.headers, max_bytes)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        pass


def douyin_detail(page_url, attempts=4, wait_s=15):
    """Open the public Douyin page in a headless browser and return the page's own aweme detail JSON."""
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        raise FetchError('Douyin needs the browser extra: run `uvx --from "vidsum-local[browser]" vidsum-local <link>`, '
                         'or start the MCP server with `uvx --from "vidsum-local[browser]" vidsum-local mcp` '
                         '(uses your installed Google Chrome; without Chrome, also run '
                         '`uvx --from "vidsum-local[browser]" playwright install chromium`)')
    match = re.search(r'/(?:video|note)/(\d+)', page_url)
    if not match:
        raise NotAVideo(f'not a Douyin video link: {page_url}')
    aweme_id = match.group(1)
    try:
        return _douyin_detail(sync_playwright, PlaywrightError, page_url, aweme_id, attempts, wait_s)
    except PlaywrightError as ex:  # a crashed or missing browser is an error of this route, not of the link
        raise FetchError('the headless browser failed: ' + (str(ex).strip().splitlines() or ['unknown error'])[0][:200])


def _douyin_detail(sync_playwright, PlaywrightError, page_url, aweme_id, attempts, wait_s):
    with sync_playwright() as p:
        browser, problems = None, []
        for options in ({'channel': 'chrome'}, {}):
            try:
                browser = p.chromium.launch(headless=True, **options)
                break
            except PlaywrightError as ex:
                problems.append(str(ex).splitlines()[0])
        if browser is None:
            raise FetchError('no browser for the Douyin fallback: install Google Chrome, or run '
                             '`uvx --from "vidsum-local[browser]" playwright install chromium` (' + '; '.join(problems)[:300] + ')')
        found, answered = {}, {}

        def receive(response):
            if '/aweme/v1/web/aweme/detail' in response.url and 'detail' not in found:
                try:
                    detail = json.loads(response.body()).get('aweme_detail') or {}
                except Exception:
                    detail = {}
                if str(detail.get('aweme_id')) == aweme_id:
                    found['detail'] = detail
                else:
                    answered['empty'] = True  # this load got an empty answer; the next reload usually does not
        try:
            # One browser profile for all attempts: its first load sets Douyin's cookies and often gets an
            # empty answer, reloads in the same profile then get the details (11-24: 5 of 5 reloads,
            # against 1 of 5 loads in fresh profiles).
            context = browser.new_context(locale='zh-CN')
            try:
                for _ in range(attempts):
                    answered.clear()
                    page = context.new_page()
                    page.on('response', receive)
                    try:
                        page.goto(page_url, wait_until='domcontentloaded', timeout=45000)
                        for _ in range(wait_s):
                            if 'detail' in found or answered:
                                break
                            page.wait_for_timeout(1000)
                    except PlaywrightError:
                        pass
                    finally:
                        page.close()
                    if 'detail' in found:
                        break
            finally:
                context.close()
        finally:
            browser.close()
    if 'detail' not in found:
        raise FetchError('the Douyin page did not return the video details (removed, private, or a verification page)')
    return found['detail']


def douyin_fetch(page_url, link, d, probe):
    detail = douyin_detail(page_url)
    video = detail.get('video') or {}
    urls = (video.get('play_addr') or {}).get('url_list') or []
    if detail.get('images'):  # a photo post; its play_addr, if any, is only the background music
        raise NotAVideo('this Douyin post is a photo post, not a video')
    if not urls:
        raise FetchError('the Douyin details have no playable address')
    problems = []
    for url in urls:
        try:
            download_file(url, d / 'video.mp4', headers={'User-Agent': UA, 'Referer': 'https://www.douyin.com/'},
                          timeout=120, known_video=True)
            probe(d / 'video.mp4')
            break
        except Final:  # the other addresses serve the same file
            (d / 'video.mp4').unlink(missing_ok=True)
            raise
        except Exception as ex:
            (d / 'video.mp4').unlink(missing_ok=True)
            problems.append(str(ex)[:120])
    else:
        raise FetchError('every Douyin download address failed: ' + '; '.join(problems[-2:]))
    stats, author = detail.get('statistics') or {}, detail.get('author') or {}
    return dict(source='douyin', link=link, url=page_url, title=detail.get('desc'), author=author.get('nickname'),
                followers=author.get('follower_count'), published_at=_iso(detail.get('create_time')),
                likes=stats.get('digg_count'), comments=stats.get('comment_count'), collects=stats.get('collect_count'),
                shares=stats.get('share_count'), reported_duration_s=(video.get('duration') or 0) / 1000 or None,
                fetched_with='headless browser (public page)')


def vimeo_player(url):
    match = re.match(r'https?://(?:www\.)?vimeo\.com/(\d+)(?:/([0-9a-f]+))?', url)
    if not match:
        return None
    return f'https://player.vimeo.com/video/{match.group(1)}' + (f'?h={match.group(2)}' if match.group(2) else '')


def fetch(text, d, probe, cookies=None, cookies_from_browser=None, log=None):
    """Download the video a link or share text points to into d. Returns meta for meta.json."""
    link = find_url(text)
    if not link:
        raise FetchError('no link found in the input')
    check_cookie_options(cookies, cookies_from_browser)
    if is_direct_media(link):
        try:
            return download_direct(link, d, probe)
        except Final:
            _clean_item(d)
            raise
        except FetchError:
            _clean_item(d)  # a page whose address ends in .mp4/.webm (e.g. a wiki file page): let yt-dlp try
    url = normalize(resolve_short(link))
    kind = platform_of(url)
    if kind == 'web':  # site extractors flag live videos for --match-filters; a bare radio address has no flag
        check_web_stream(url)
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    browser_problem = None
    if kind == 'douyin' and browser_available():
        # yt-dlp's Douyin extractor cannot sign its requests, so the browser route goes first.
        try:
            meta = douyin_fetch(url, link, d, probe)
            probe(d / 'video.mp4')
            return meta
        except Final:
            _clean_item(d)
            raise
        except FetchError as ex:
            _clean_item(d)
            browser_problem = str(ex).rstrip(' .')
            log(f'browser route failed ({browser_problem}); trying yt-dlp')
    try:
        meta = meta_from_info(run_ytdlp(url, d, cookies, cookies_from_browser, log), link, kind)
    except Final:
        _clean_item(d)
        raise
    except FetchError as first:
        _clean_item(d)
        meta = None
        if kind == 'vimeo' and vimeo_player(url):
            log('trying the public Vimeo player address')
            try:
                meta = meta_from_info(run_ytdlp(vimeo_player(url), d, cookies, cookies_from_browser, log), link, kind)
            except Final:
                _clean_item(d)
                raise
            except FetchError:
                _clean_item(d)
        if meta is None:
            reason, hint = str(first).rstrip(' .'), ''
            if browser_problem:  # the browser route is the one that normally works for Douyin; say why it did not
                reason = f'{browser_problem} (headless browser); yt-dlp: {reason}'
            elif kind == 'douyin':
                hint = (' Douyin needs the browser extra: run `uvx --from "vidsum-local[browser]" vidsum-local <link>`, '
                        'or start the MCP server with `uvx --from "vidsum-local[browser]" vidsum-local mcp` '
                        '(uses Google Chrome if installed).')
            elif needs_login(str(first)) and not (cookies or cookies_from_browser):
                hint = (' This site wants a signed-in browser session: add --cookies-from-browser chrome '
                        '(or firefox, safari, edge) or --cookies cookies.txt.')
            raise FetchError(f'could not download {url}: {reason}.{hint}')
    probe(d / 'video.mp4')
    return meta


def browser_available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False
