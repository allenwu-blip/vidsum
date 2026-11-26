"""vidsum: summarize or transcribe video files on your own machine.

  vidsum talk.mp4                      summary (default command: run)
  vidsum "https://youtu.be/…"          a link: downloaded with yt-dlp first
  vidsum transcribe talk.mp4 --format srt
  vidsum setup                         download the default models once
  vidsum mcp                           stdio MCP server
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import threading
import urllib.request

from . import __version__
from .i18n import T, ui, resolve, sections, heading, prompt as summary_prompt, boundary as boundary_text
from . import models
from .fetch import MEDIA_EXT, find_url, fetch as fetch_link

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {'run', 'transcribe', 'setup', 'mcp', '_visual', '_transcribe'}


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def save(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def run(cmd, **kw):
    return subprocess.run(list(map(str, cmd)), check=True, capture_output=True, text=True, **kw)


def tc(t):
    return f'{int(t)//60:02d}:{t%60:05.2f}'


def probe(video):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', video]).stdout)


def shown(path):
    """Paths under the working directory are printed relative to it."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def cache_dir():
    base = os.environ.get('VIDSUM_HOME') or os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'vidsum')
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_tools():
    missing = [tool for tool in ('ffmpeg', 'ffprobe') if not shutil.which(tool)]
    if missing:
        raise SystemExit('vidsum needs ' + ' and '.join(missing) + ' on PATH. Install FFmpeg: '
                         '`brew install ffmpeg` (macOS), `sudo apt install ffmpeg` (Debian/Ubuntu), '
                         '`winget install ffmpeg` (Windows).')


@contextlib.contextmanager
def builtin_lock(wait):
    """One heavy step at a time across vidsum processes (MCP calls, parallel shells)."""
    try:
        import fcntl
    except ImportError:  # Windows: cloud profile only, nothing heavy runs locally
        yield
        return
    with open(cache_dir() / 'heavy.lock', 'a+') as handle:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise RuntimeError('another vidsum run is still using the models')
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def heavy(args, tag):
    if not args.lock_script:
        with builtin_lock(args.lock_wait):
            yield
        return
    # The caller may put lock state in its writable workspace. Never edit lock scripts.
    env = os.environ.copy()
    env['HEAVY_LOCK'] = str(Path(args.lock_state).resolve())
    if args.global_lock_state:
        owner = Path(args.global_lock_state) / 'owner'
        deadline = time.time() + args.lock_wait
        while owner.exists():
            if time.time() > deadline:
                raise RuntimeError('全局重任务锁仍被占用：' + owner.read_text())
            time.sleep(2)
    who = f'vidsum-{os.getpid()}-{tag}'
    run([args.lock_script, 'acquire', who, args.lock_wait], env=env)
    try:
        yield
    finally:
        run([args.lock_script, 'release', who], env=env)


def wait_owned(proc, args):
    """Reap an owned PID; abort if a separate read-only global lock is claimed."""
    separate = args.global_lock_state and Path(args.global_lock_state).resolve() != Path(args.lock_state).resolve()
    while proc.poll() is None:
        if separate and (Path(args.global_lock_state)/'owner').exists():
            proc.terminate()
            try:proc.wait(timeout=20)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            raise RuntimeError('外部重任务取得全局锁，已按 PID 停止本模型；重跑可恢复')
        time.sleep(.25)
    return proc.returncode


def looks_like_path(text):
    text = text.strip()
    return (text.startswith(('/', '~', './', '../')) or bool(re.match(r'^[A-Za-z]:[\\/]', text))
            or (Path(text).suffix.lower() in MEDIA_EXT and '://' not in text))


def fetch_external(command, text, d):
    """Run a user-supplied downloader: COMMAND <input> <item_dir>. It must leave meta.json and video.mp4 (or frames.json)."""
    result = subprocess.run(shlex.split(command) + [text, str(d)], capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError((message[-1] if message else f'fetch command exited {result.returncode}')[:400])
    if not (d / 'meta.json').exists() or not ((d / 'video.mp4').exists() or (d / 'frames.json').exists()):
        raise RuntimeError('fetch command did not leave meta.json and video.mp4 (or frames.json)')
    return json.loads((d / 'meta.json').read_text())


def frames(video, d, info):
    from PIL import Image, ImageDraw, ImageFont
    duration = float(info['format']['duration'])
    # The container can run longer than the last video frame (audio tail); seeking past it yields no frame.
    vdur = [float(s['duration']) for s in info.get('streams', []) if s.get('codec_type') == 'video' and s.get('duration')]
    if vdur and 0 < vdur[0] < duration: duration = vdur[0]
    fd = d/'frames'; fd.mkdir(exist_ok=True)
    # Analyse scene changes, then select strongest events spread across the timeline.
    r = run(['ffmpeg', '-hide_banner', '-i', video, '-an', '-vf', "scale=320:-2,select='gt(scene,0.22)',showinfo", '-fps_mode', 'vfr', '-f', 'null', '-'])
    scenes = [float(x) for x in re.findall(r'pts_time:([\d.]+)', r.stderr)]
    times = [0.0, min(2.9, max(0, duration-0.05))]
    if len(scenes) > 18:
        scenes = [scenes[round(i*(len(scenes)-1)/17)] for i in range(18)]
    times += scenes
    for t in [i*max(0, duration-0.08)/15 for i in range(16)]:
        if len(times) >= 24: break
        if all(abs(t-x) > max(0.25, duration/100) for x in times): times.append(t)
    times = sorted(set(round(t, 3) for t in times if 0 <= t < duration))[:24]
    items = []
    for i, t in enumerate(times):
        ok = False
        for back in (0, 0.5, 1.5):
            u = round(max(0.0, t-back), 3); name = f'{i+1:02d}_{u:08.3f}.jpg'
            (fd/name).unlink(missing_ok=True)  # a stale frame from an earlier run must not count as success
            try: run(['ffmpeg', '-v', 'error', '-y', '-ss', u, '-i', video, '-frames:v', '1', '-vf', "scale='min(960,iw)':-2", fd/name])
            except subprocess.CalledProcessError: continue
            if (fd/name).exists() and (fd/name).stat().st_size > 0: ok = True; break
        if not ok: continue
        items.append({'time_s': u, 'timecode': tc(u), 'file': name, 'scene_change': any(abs(u-s) < .01 for s in scenes)})
    if not items:
        raise RuntimeError('no frame could be extracted from the video stream')
    # Landscape and portrait thumbnails fit the same cell; no distortion.
    cols = 6 if len(items) > 16 else 4
    cw, ch = 390, 540
    sheet = Image.new('RGB', (cols*cw, ((len(items)+cols-1)//cols)*ch), '#161b22')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=22)
    for i, it in enumerate(items):
        im = Image.open(fd/it['file']); im.thumbnail((cw-12, ch-42))
        x = (i % cols)*cw; y = (i//cols)*ch
        sheet.paste(im, (x+(cw-im.width)//2, y+36))
        draw.text((x+12, y+6), it['timecode'], font=font, fill='white')
    sheet.thumbnail((2400, 2400)); sheet.save(d/'contact.jpg', quality=90)
    save(d/'frames.json', {'duration_s': duration, 'scene_threshold': .22, 'detected_scene_changes': len(re.findall(r'pts_time:', r.stderr)), 'frames': items})
    return items


def audio_only(d, info):
    save(d/'frames.json', {'media_type': 'audio', 'duration_s': float(info['format']['duration']), 'scene_threshold': None,
                           'detected_scene_changes': 0, 'frames': []})
    save(d/'ocr.json', [])
    (d/'ocr.md').write_text('# OCR\n\n-\n')


def ocr(d, items, lang='zh'):
    try:
        import Vision
        from Foundation import NSURL, NSDictionary
    except ImportError:
        save(d/'ocr.json', [{**it, 'lines': []} for it in items])
        (d/'ocr.md').write_text(T(lang, 'ocr_title') + '\n\n' + T(lang, 'ocr_unavailable') + '\n')
        return
    rows = []; md = [T(lang, 'ocr_title'), '', T(lang, 'ocr_note'), '']
    for it in items:
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(['zh-Hans', 'en-US'])
        req.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str((d/'frames'/it['file']).resolve())), NSDictionary.dictionary())
        ok, error = handler.performRequests_error_([req], None)
        if not ok: raise RuntimeError(str(error))
        lines = []
        for obs in req.results() or []:
            candidates = obs.topCandidates_(1)
            if candidates:
                c = candidates[0]; lines.append({'text': str(c.string()), 'confidence': float(c.confidence())})
        rows.append({**it, 'lines': lines})
        md += ['## '+it['timecode']+' · '+it['file'], '']+[x['text'] for x in lines]+['']
    save(d/'ocr.json', rows); (d/'ocr.md').write_text('\n'.join(md))


def ocr_text(d):
    try:
        return ' '.join(x['text'] for row in json.loads((d/'ocr.json').read_text()) for x in row['lines'])
    except (OSError, ValueError, KeyError):
        return ''


def asr_local(video, d, model, hint=None):
    import mlx_whisper
    import mlx.core as mx
    mx.set_cache_limit(256*1024*1024)
    wav = d/'audio.wav'
    try:
        run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-vn', '-ac', '1', '-ar', '16000', wav])
        events = classify_audio(d, wav)
        result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, language=hint or None, temperature=0,
            condition_on_previous_text=False, no_speech_threshold=.6, logprob_threshold=-1.0,
            hallucination_silence_threshold=2.0, word_timestamps=True, verbose=None)
    finally:
        wav.unlink(missing_ok=True)
    return result, events


def classify_audio(d, wav):
    from .audio import classify
    try:
        events = classify(wav)
    except Exception as ex:
        events = {'error': str(ex)}
    save(d/'audio_events.json', events)
    return events


def asr_cloud(video, d, hint=None):
    from . import cloud
    events = {}
    if sys.platform == 'darwin':
        wav = d/'audio.wav'
        try:
            run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-vn', '-ac', '1', '-ar', '16000', wav])
            events = classify_audio(d, wav)
        finally:
            wav.unlink(missing_ok=True)
    return cloud.transcribe(video, d/'.asr_chunks', language=hint), events


def srt_time(t, sep=','):
    ms = int(round(t*1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}'


def finish_transcript(d, result, events, duration, requested='auto'):
    from .audio import status as audio_status
    segs = [s for s in result.get('segments', []) if not (s.get('no_speech_prob', 0) > .6 and s.get('avg_logprob', 0) < -1)]
    # A high-confidence/no-speech decision is retained for audit; music hallucinations need review.
    code, segs = audio_status(segs, events, result.get('no_audio_stream', False))
    spoken = result.get('language') if code == 'speech' and segs else None
    lang = resolve(requested, spoken, ocr_text(d))
    status = T(lang, 'audio')[code]
    if code == 'singing':
        result['text'] = T(lang, 'lyrics_hidden')
        for segment in result.get('segments', []):
            segment['text'] = T(lang, 'lyrics_segment')
            segment.pop('words', None); segment.pop('tokens', None)
    save(d/'asr_raw.json', result)
    save(d/'asr.json', {'status': status, 'status_code': code, 'language': result.get('language'), 'output_language': lang,
                        'duration_s': duration, 'segments': segs})
    md = [T(lang, 'transcript_title'), '', status, '', T(lang, 'transcript_lang', language=result.get('language') or T(lang, 'none')), '']
    md += [f"- [{tc(s['start'])}–{tc(min(duration, s['end']))}] {s['text'].strip()}" for s in segs]
    (d/'transcript.md').write_text('\n'.join(md)+'\n')
    srt, vtt = [], ['WEBVTT', '']
    for i, s in enumerate(segs, 1):
        end = min(duration, s['end'])
        srt += [str(i), f"{srt_time(s['start'])} --> {srt_time(end)}", s['text'].strip(), '']
        vtt += [f"{srt_time(s['start'], '.')} --> {srt_time(end, '.')}", s['text'].strip(), '']
    (d/'transcript.srt').write_text('\n'.join(srt))
    (d/'transcript.vtt').write_text('\n'.join(vtt))
    (d/'transcript.txt').write_text('\n'.join(s['text'].strip() for s in segs)+'\n')
    return lang


def transcribe_worker(video, d, model, hint=None, requested='auto'):
    inf = probe(video)
    duration = float(inf['format']['duration'])
    if not any(s['codec_type'] == 'audio' for s in inf['streams']):
        result, events = {'segments': [], 'text': '', 'language': None, 'no_audio_stream': True}, {}
    else:
        result, events = asr_local(video, d, model, hint)
    finish_transcript(d, result, events, duration, requested)


def transcribe_item(video, d, args):
    if args.backend == 'cloud':
        inf = probe(video)
        duration = float(inf['format']['duration'])
        if not any(s['codec_type'] == 'audio' for s in inf['streams']):
            result, events = {'segments': [], 'text': '', 'language': None, 'no_audio_stream': True}, {}
        else:
            result, events = asr_cloud(video, d, args.asr_language)
        return finish_transcript(d, result, events, duration, args.language)
    with heavy(args, 'asr'):
        cmd = [sys.executable, '-m', 'vidsum.cli', '_transcribe', str(video), str(d), args.asr_model, '--language', args.language]
        if args.asr_language:
            cmd += ['--hint', args.asr_language]
        subprocess.run(cmd, check=True, stdout=sys.stderr)
    return json.loads((d/'asr.json').read_text()).get('output_language')


def api(url, payload=None, timeout=300, key=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response: return json.load(response)


@contextlib.contextmanager
def server(args):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    # MLX LM's /models scans the HF hub even for a local model. Ensure the
    # caller-selected cache exists without writing into the default home cache.
    from huggingface_hub.constants import HF_HUB_CACHE
    Path(HF_HUB_CACHE).mkdir(parents=True, exist_ok=True)
    log_file = open(Path(args.output)/'server.log', 'a')
    proc = subprocess.Popen([sys.executable, '-m', 'mlx_lm.server', '--model', args.model, '--host', '127.0.0.1', '--port', str(port), '--chat-template-args', '{"enable_thinking":false}', '--prompt-cache-size', '1', '--decode-concurrency', '1', '--prompt-concurrency', '1', '--prefill-step-size', '512'], stdout=log_file, stderr=subprocess.STDOUT)
    pidfile = Path(args.output)/'server.pid'; pidfile.write_text(str(proc.pid))
    base = f'http://127.0.0.1:{port}/v1'
    collision = []
    def watch():
        try: wait_owned(proc, args)
        except RuntimeError as ex: collision.append(str(ex))
    watcher = threading.Thread(target=watch, daemon=True); watcher.start()
    try:
        for _ in range(240):
            if proc.poll() is not None: raise RuntimeError('mlx_lm.server failed to start; see server.log')
            try:
                api(base+'/models', timeout=1); break
            except Exception: time.sleep(1)
        else: raise RuntimeError('mlx_lm.server did not start in time')
        yield base
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        watcher.join(timeout=2)
        pidfile.unlink(missing_ok=True); log_file.close()
        if collision: raise RuntimeError(collision[0])


def item_language(d, args):
    meta = json.loads((d/'meta.json').read_text()) if (d/'meta.json').exists() else {}
    if meta.get('output_language'):
        return meta['output_language']
    if (args.language or 'auto').lower() != 'auto':
        return args.language
    try:
        a = json.loads((d/'asr.json').read_text())
        if a.get('output_language'):
            return a['output_language']
        spoken = a.get('language') if a.get('segments') else None
    except (OSError, ValueError):
        spoken = None
    return resolve('auto', spoken, ocr_text(d))


def context_of(meta):
    return meta.get('context') or ''


def summary_head(meta, d, visual, lang):
    note = context_of(meta)
    head = '# '+meta.get('label', d.name)+'\n\n'
    if note: head += T(lang, 'context_heading')+'\n\n'+note+'\n\n'
    head += (T(lang, 'boundary_visual') if visual else T(lang, 'boundary_text')) + '\n\n'
    return head


def reuse_summary(original, d, args):
    meta = json.loads((d/'meta.json').read_text())
    lang = item_language(d, args)
    source = (original/'summary.md').read_text()
    body = source.split(T(lang, 'boundary_prefix'), 1)[1].split('\n\n', 1)[1]
    from .review import evidence_sections
    visual = wants_visual(args, d) and (d/'visual.md').exists()
    body = evidence_sections(body, d, use_visual=visual, lang=lang, template=args.template)
    (d/'summary.md').write_text(summary_head(meta, d, visual, lang)+body)
    shutil.copyfile(original/'summary_response.json', d/'summary_response.json')
    save(d/'summary_reuse.json', {'from': original.name, 'reason': 'identical video SHA256; local text inference run once'})


def wants_visual(args, d):
    if args.no_visual or not args.vision_model:
        return False
    try:
        return json.loads((d/'meta.json').read_text()).get('media_type') != 'audio'
    except (OSError, ValueError):
        return True


def ask(base, args, content, limit):
    """One chat request. Local mlx_lm.server takes sampler extras; a cloud API gets a plain request."""
    if args.backend == 'cloud':
        from . import cloud
        s = cloud.settings()
        return cloud.chat(base, s['key'], s['model'], content, max(limit, 4000), temperature=.15)
    return api(base+'/chat/completions', {'model': args.model, 'messages': [{'role': 'user', 'content': content}], 'max_tokens': limit,
               'temperature': .15, 'repetition_penalty': 1.12, 'chat_template_kwargs': {'enable_thinking': False}}, timeout=480)


def summarize(d, base, args):
    meta = json.loads((d/'meta.json').read_text())
    lang = item_language(d, args)
    template = args.template
    asr = json.loads((d/'asr.json').read_text())
    timeline = json.loads((d/'frames.json').read_text())
    visual = (d/'visual.md').read_text() if wants_visual(args, d) and (d/'visual.md').exists() else None
    review_path = d/'visual_review.json'
    if visual and review_path.exists():
        review = json.loads(review_path.read_text())
        if review.get('corrections') and '抽查校正' not in visual[:400]:
            visual = '抽查校正（优先于下面的模型原文，冲突时采用校正）：\n'+review['corrections']+'\n\n'+visual
    evidence = {'visual': visual, 'meta': {k: v for k, v in meta.items() if k not in ('context', 'focus')},
                'audio_status': asr['status'], 'transcript': (d/'transcript.md').read_text(), 'ocr': (d/'ocr.md').read_text(),
                'frame_timeline': timeline}
    kind = 'visual' if visual else 'text'
    if meta.get('media_type') == 'audio':
        kind = 'text'
    edge = boundary_text(lang, kind) if meta.get('media_type') != 'audio' else boundary_text(lang, 'audio')
    if meta.get('media_type') == 'note': edge += boundary_text(lang, 'images')
    focus = meta.get('focus') or args.focus
    if focus: edge += T(lang, 'focus_line', focus=focus)
    instructions = summary_prompt(template, lang, edge)
    if meta.get('media_type') == 'audio':
        # No picture: drop the on-screen section and the empty visual evidence so a small model is not confused.
        instructions = re.sub(r'^## ' + re.escape(heading(template, lang, 'shots')) + r'.*\n', '', instructions, flags=re.M)
        instructions = instructions.replace('Summarize this video', 'Summarize this recording', 1)
        evidence = {k: v for k, v in evidence.items() if k not in ('visual', 'frame_timeline', 'ocr')}
    content = instructions+json.dumps(evidence, ensure_ascii=False)
    def visible(response):
        raw = response['choices'][0]['message'].get('content') or ''
        text = re.sub(r'<think>.*?</think>', '', raw, flags=re.S)
        text = re.sub(r'<think>.*', '', text, flags=re.S).strip()
        loop = re.search(r'(.{8,}?)\1{3,}', text, flags=re.S)
        if loop: text = text[:loop.start()].rstrip()
        return text, response['choices'][0].get('finish_reason')
    names = sections(template, lang)
    filled = {'quotes', 'data'}
    if meta.get('media_type') == 'audio': filled.add('shots')
    needed = ['## '+name for r, name in names if r not in filled]
    about, last = heading(template, lang, 'about'), [n for r, n in names if r not in filled][-1]
    response = ask(base, args, content, 2400)
    body, reason = visible(response)
    if '## '+last not in body and '## '+about in body:
        follow = (f'下面这份摘要还没写完。只补 ## {last}：最多五条，每条一句，标推断，只谈视频创作，不重复字符。\n\n' if ui(lang) == 'zh' and template == 'creator'
                  else f'The summary below stopped early. Write only the missing "## {last}" section, at most five one-line bullets, without repeating text.\n\n') + body[:1800]
        response = ask(base, args, follow, 700)
        extra, _ = visible(response)
        if extra:
            body = body.rstrip()+'\n\n'+(extra if extra.lstrip().startswith('##') else '## '+last+'\n\n'+extra)
    if not all(h in body for h in needed):
        save(d/'summary_failed.json', {'finish_reason': reason, 'content': body})
        raise RuntimeError(T(lang, 'err_summary_truncated'))
    if ui(lang) != 'zh':
        body = re.sub(r'\[(?:visual|image|frame|picture)\s+(?=\d)', '[screen ', body, flags=re.I)
    from .review import evidence_sections
    body = evidence_sections(body, d, use_visual=bool(visual), lang=lang, template=template)
    head = summary_head(meta, d, visual, lang)
    (d/'summary.md').write_text(head+body+'\n')
    save(d/'summary_response.json', response)


def index(out, items, lang, template):
    about = heading(template, lang, 'about')
    lines = [T(lang, 'index_title'), '', T(lang, 'index_note'), '', T(lang, 'index_header'), '|---|---|---:|---:|---|---|']
    for i, x in enumerate(items, 1):
        d = out/x['id']; meta = json.loads((d/'meta.json').read_text()) if (d/'meta.json').exists() else {}
        msg = x.get('error', T(lang, 'index_pending'))
        summary = d/'summary.md'
        if summary.exists() and not x.get('error'):
            match = re.search(r'^## '+re.escape(about)+r'[^\n]*\n(.*?)(?=^## |\Z)', summary.read_text(), re.M | re.S)
            if match: msg = re.sub(r'\s+', ' ', match.group(1)).strip()[:150]
        target = x['id']+('/summary.md' if summary.exists() else '/ERROR.txt' if (d/'ERROR.txt').exists() else '/meta.json')
        title = str(meta.get('title') or x.get('label') or x['id']).replace('|', '/').replace('\n', ' ')[:80]
        duration = meta.get('duration_s'); duration = round(duration, 2) if duration is not None else '—'
        likes = meta.get('likes'); likes = T(lang, 'not_provided') if likes is None else likes
        lines.append(f"| {i} | [{title}]({target}) | {duration} | {likes} | {x['status']} | {msg.replace('|', '/').replace(chr(10), ' ')} |")
    (out/'index.md').write_text('\n'.join(lines)+'\n'); save(out/'batch_status.json', items)


def set_state(x, state, lang, error=None):
    x['state'] = state; x['status'] = T(lang, 'state')[state]
    if error is not None: x['error'] = error
    elif state != 'failed': x.pop('error', None)


def prepare_input(e, d, args, lang, cached_links):
    """Bring one input into d: copy a local file, run --fetch-command, or download a link (yt-dlp and fallbacks)."""
    text = e['input']
    video = d/'video.mp4'
    key = find_url(text)
    hit = cached_links.get(key) if key else None  # the same link exactly; ...?p=2 is a different video
    if hit is not None and hit != d:
        shutil.copyfile(hit/'video.mp4', video)
        return json.loads((hit/'meta.json').read_text())
    src = Path(text).expanduser()
    try:
        is_file = src.is_file()
    except OSError:  # pasted share text can be longer than any file name
        is_file = False
    if is_file:
        shutil.copyfile(src.resolve(), video)
        return dict(source='local', title=src.name, author=None, followers=None, published_at=None, likes=None, comments=None, collects=None, shares=None)
    if looks_like_path(text):
        raise RuntimeError(T(lang, 'err_missing_input'))
    no_cookies = getattr(args, 'no_cookies', False)
    if args.fetch_command and not no_cookies:
        meta = fetch_external(args.fetch_command, text, d)
    elif find_url(text) and no_cookies:
        meta = fetch_link(text, d, probe, cookies=None, cookies_from_browser=None, log=log, no_cookies=True)
    elif find_url(text):
        meta = fetch_link(text, d, probe, cookies=getattr(args, 'cookies', None),
                          cookies_from_browser=getattr(args, 'cookies_from_browser', None), log=log)
    else:
        raise RuntimeError(T(lang, 'err_no_fetcher'))
    if meta.get('link') and video.exists():
        cached_links[meta['link']] = d
    return meta


def configure(args, stage):
    """Pick the backend and make sure the model weights for this stage are on disk."""
    args.profile = args.profile or os.environ.get('VIDSUM_PROFILE') or models.DEFAULT_PROFILE
    if args.profile not in models.PROFILES:
        raise SystemExit(f'unknown profile {args.profile}; choose from {", ".join(models.PROFILES)}')
    args.backend = 'cloud' if args.profile == 'cloud' else 'mlx'
    if args.backend == 'cloud':
        from . import cloud
        cloud.require_key()
        args.model = args.model or 'cloud'
        args.asr_model = args.asr_model or 'cloud'
        if not args.no_visual and args.vision_model is None: args.vision_model = 'cloud'
        return
    explicit = all(getattr(args, k) for k in ('model', 'asr_model'))
    if not models.apple_silicon() and not explicit:
        raise SystemExit('Local models need an Apple Silicon Mac. On other machines use the cloud profile: '
                         '--profile cloud with VIDSUM_API_KEY set.')
    roles = {'prepare': ('asr',), 'visual': ('vision',), 'summarize': ('llm', 'vision'), 'all': ('asr', 'vision', 'llm'),
             'transcribe': ('asr',)}[stage]
    if args.no_visual:
        roles = tuple(r for r in roles if r != 'vision')
    overrides = {'asr': args.asr_model, 'llm': args.model, 'vision': args.vision_model or args.model, 'roles': roles}
    rows = models.plan(args.profile, overrides)
    try:
        paths = models.ensure(rows, allow_download=not args.no_download, log=log)
    except models.ModelsMissing as ex:
        raise SystemExit(str(ex))
    args.asr_model = paths.get('asr', args.asr_model)
    args.model = paths.get('llm', args.model)
    if not args.no_visual:
        args.vision_model = paths.get('vision', args.vision_model or args.model)


def add_common(p):
    p.add_argument('--output', default=os.environ.get('VIDSUM_OUTPUT', 'vidsum-output'), help='folder for results (default ./vidsum-output)')
    p.add_argument('--profile', default=None, help='small (default; measured peak 7.2 GB, 16 GB Macs untested), large (32 GB+ Mac) or cloud (OpenAI-compatible API, experimental)')
    p.add_argument('--asr-model', default=os.environ.get('VIDSUM_ASR_MODEL'), help='Whisper MLX folder or Hugging Face id')
    p.add_argument('--asr-language', default=os.environ.get('VIDSUM_ASR_LANGUAGE'), help='spoken language hint, e.g. en, zh (default: detect)')
    p.add_argument('--language', default=os.environ.get('VIDSUM_LANGUAGE', 'auto'), help='output language: auto (follow the video), en, zh, ja, ...')
    # These three also come from VIDSUM_COOKIES_FROM_BROWSER, VIDSUM_COOKIES and VIDSUM_FETCH_COMMAND; that happens after
    # parsing (apply_env_options), so --no-cookies can leave the variables unread.
    p.add_argument('--cookies-from-browser', default=None, metavar='BROWSER[:PROFILE]',
                   help='for links that need a signed-in session: read cookies from chrome, firefox, safari, edge, brave, ... (passed to yt-dlp)')
    p.add_argument('--cookies', default=None, metavar='FILE',
                   help='Netscape-format cookies.txt for links that need a signed-in session (passed to yt-dlp)')
    p.add_argument('--no-cookies', action='store_true',
                   help='never use cookies or a signed-in session: no cookies file, no browser cookies, the VIDSUM_COOKIES* and '
                        'VIDSUM_FETCH_COMMAND variables are not read, and yt-dlp is told not to load any cookies. '
                        'Links that need a login fail; download the video yourself and pass the file')
    p.add_argument('--fetch-command', default=None,
                   help='your own downloader instead of the built-in one: COMMAND <input> <item_dir>, must leave meta.json and video.mp4')
    p.add_argument('--no-download', action='store_true', help='never download model weights')
    p.add_argument('--lock-script', default=os.environ.get('VIDSUM_LOCK_SCRIPT'), help=argparse.SUPPRESS)
    p.add_argument('--lock-state', default=None, help=argparse.SUPPRESS)
    p.add_argument('--global-lock-state', default=os.environ.get('VIDSUM_GLOBAL_LOCK_STATE'), help=argparse.SUPPRESS)
    p.add_argument('--lock-wait', type=int, default=7200, help=argparse.SUPPRESS)


def parser():
    p = argparse.ArgumentParser(prog='vidsum', description='Summarize or transcribe video files and video links locally (Apple Silicon) or through an OpenAI-compatible API.',
                                epilog='Profiles:\n'+models.describe_profiles()+'\n\nExamples:\n  uvx vidsum-local talk.mp4\n  uvx vidsum-local "https://www.youtube.com/watch?v=jNQXAC9IVRw"\n  uvx vidsum-local transcribe talk.mp4 --format srt\n  uvx vidsum-local setup\n\nLinks are downloaded with yt-dlp; Douyin needs the [browser] extra. Only process videos you have the right to use.\nAfter `uv tool install vidsum-local` the shorter `vidsum` works too.',
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version='vidsum '+__version__)
    sp = p.add_subparsers(dest='command', required=True, metavar='{run,transcribe,setup,mcp}')
    v = sp.add_parser('_visual'); v.add_argument('model'); v.add_argument('directories', nargs='+'); v.add_argument('--frames', type=int, default=8)
    w = sp.add_parser('_transcribe'); w.add_argument('video'); w.add_argument('directory'); w.add_argument('model')
    w.add_argument('--hint'); w.add_argument('--language', default='auto')
    r = sp.add_parser('run', help='summarize videos (default command)', formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument('inputs', nargs='*', help='video or audio files, video links, or pasted share text'); r.add_argument('--manifest', type=Path, help='JSON list of {id, input, label, context, focus}')
    add_common(r)
    r.add_argument('--model', default=os.environ.get('VIDSUM_MODEL'), help='text model folder or Hugging Face id')
    r.add_argument('--vision-model', default=os.environ.get('VIDSUM_VISION_MODEL'), help='vision model (default: same as --model)')
    r.add_argument('--template', choices=['general', 'creator'], default=os.environ.get('VIDSUM_TEMPLATE', 'general'),
                   help='general: TL;DR, key points, timeline (default); creator: structure, shots and takeaways for video makers')
    r.add_argument('--focus', default=None, help='a question the summary should answer, e.g. "what does it say about pricing?"')
    r.add_argument('--visual-frames', type=int, choices=range(8, 13), default=8, metavar='8-12')
    r.add_argument('--no-visual', action='store_true', help='skip frame description (faster, text only)')
    r.add_argument('--force-summary', action='store_true', help='preserve old summary as summary_v1.md, then regenerate')
    r.add_argument('--stage', choices=['all', 'prepare', 'visual', 'summarize'], default='all')
    t = sp.add_parser('transcribe', help='transcript only (md, srt, vtt, txt, json)')
    t.add_argument('input', help='a file, a video link, or pasted share text'); add_common(t)
    t.add_argument('--format', choices=['md', 'srt', 'vtt', 'txt', 'json'], default='md', help='what to print to stdout')
    s = sp.add_parser('setup', help='check FFmpeg and download the models for a profile')
    s.add_argument('--profile', default=None); s.add_argument('--check', action='store_true', help='only report, download nothing')
    s.add_argument('--transcribe-only', action='store_true', help='only the speech model (1.6 GB)')
    s.add_argument('--no-cookies', action='store_true', help='accepted so every command can carry it; setup downloads no videos')
    sp.add_parser('mcp', help='run the MCP server on stdio')
    return p


def apply_env_options(p, args):
    """Fill the download options from the environment, unless --no-cookies says to leave all of them alone."""
    if args.no_cookies:
        given = [flag for flag, value in (('--cookies', args.cookies), ('--cookies-from-browser', args.cookies_from_browser),
                                          ('--fetch-command', args.fetch_command)) if value]
        if given:
            p.error('--no-cookies cannot be combined with ' + ', '.join(given))
        args.cookies = args.cookies_from_browser = args.fetch_command = None
        return
    for name, variable in (('cookies', 'VIDSUM_COOKIES'), ('cookies_from_browser', 'VIDSUM_COOKIES_FROM_BROWSER'),
                           ('fetch_command', 'VIDSUM_FETCH_COMMAND')):
        if getattr(args, name) is None:
            setattr(args, name, os.environ.get(variable))


def normalize_argv(argv):
    argv = list(argv)
    if argv and argv[0] not in COMMANDS and argv[0] not in ('-h', '--help', '--version'):
        argv.insert(0, 'run')
    return argv


def cmd_setup(args):
    check_tools()
    profile = args.profile or os.environ.get('VIDSUM_PROFILE') or models.DEFAULT_PROFILE
    print(f'vidsum {__version__}  profile: {profile}  ffmpeg: ok')
    if profile == 'cloud':
        from . import cloud
        s = cloud.settings()
        print(f'API base: {s["base"]}  chat model: {s["model"]}  speech model: {s["asr_model"]}  key: {"set" if s["key"] else "MISSING (set VIDSUM_API_KEY)"}')
        return 0 if s['key'] else 1
    if not models.apple_silicon():
        print('Local models need an Apple Silicon Mac. Use --profile cloud on this machine.')
        return 1
    roles = ('asr',) if args.transcribe_only else ('asr', 'llm')
    rows = models.plan(profile, {'roles': roles})
    todo = models.missing(rows)
    for role, repo, revision, size in rows:
        print(f'  {role:<4} {repo}  {size:.1f} GB  ' + ('needs download' if (role, repo, revision, size) in todo else 'ready'))
    print(f'Cache: {models.hf_cache()} (set HF_HOME to move it)')
    if args.check or not todo:
        return 0
    models.ensure(rows, allow_download=True, log=log)
    print('Ready.')
    return 0


def cmd_transcribe(args):
    check_tools()
    args.no_visual = True; args.vision_model = None; args.model = 'unused'
    configure(args, 'transcribe')
    lang = args.language
    args.lock_state = args.lock_state or str(cache_dir()/'heavy-state')
    out = Path(args.output).resolve(); out.mkdir(parents=True, exist_ok=True)
    d = out/(re.sub(r'[^A-Za-z0-9_-]+', '_', Path(args.input).stem)[:40].strip('_') + '_' + hashlib.sha256(args.input.encode()).hexdigest()[:8])
    d.mkdir(exist_ok=True)
    video = d/'video.mp4'
    if not video.exists():
        try:
            meta = prepare_input({'input': args.input}, d, args, ui(lang), {})
        except (RuntimeError, OSError) as ex:  # FetchError is a RuntimeError; show the reason, not a traceback
            log('failed: ' + str(ex))
            if not any(d.iterdir()): d.rmdir()
            return 1
        save(d/'meta.json', meta)
    transcribe_item(video, d, args)
    files = {'md': 'transcript.md', 'srt': 'transcript.srt', 'vtt': 'transcript.vtt', 'txt': 'transcript.txt', 'json': 'asr.json'}
    print((d/files[args.format]).read_text())
    log('Transcript files: ' + ', '.join(str(d/f) for f in files.values()))
    return 0


def cmd_run(p, args):
    check_tools()
    lang = ui(args.language if (args.language or 'auto').lower() != 'auto' else 'en')
    if args.stage == 'visual' and args.no_visual: p.error('--stage visual cannot be combined with --no-visual')
    args.output = str(Path(args.output).resolve()); out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    entries = json.loads(args.manifest.read_text()) if args.manifest else []
    entries += [{'input': s, 'id': f'{i+len(entries)+1:02d}_'+hashlib.sha256(s.encode()).hexdigest()[:10]} for i, s in enumerate(args.inputs)]
    if not entries: p.error('provide at least one input or a nonempty manifest')
    ids = [x['id'] for x in entries]
    if len(set(ids)) != len(ids): p.error('manifest ids must be unique')
    for e in entries:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', e['id']): p.error('manifest id must be a safe folder name: '+e['id'])
    configure(args, args.stage)
    args.lock_state = args.lock_state or str(cache_dir()/'heavy-state')
    results = []; ready = []; fingerprints = {}
    cached_links = {}
    for md in out.glob('*/meta.json'):
        try:
            cached = json.loads(md.read_text())
            if cached.get('link') and (md.parent/'video.mp4').is_file(): cached_links[cached['link']] = md.parent
        except (ValueError, OSError): pass
    for e in entries:
        d = out/e['id']; d.mkdir(exist_ok=True)
        x = {**e}; set_state(x, 'failed', lang); results.append(x)
        try:
            video = d/'video.mp4'
            if args.stage not in ('summarize', 'visual'):
                if (d/'meta.json').exists() and (video.exists() or (d/'frames.json').exists()): meta = json.loads((d/'meta.json').read_text())
                else: meta = prepare_input(e, d, args, lang, cached_links)
                label = e.get('label') or (' '.join(str(meta['title']).split())[:100] if meta.get('title') else None) or e['id']
                meta.update(label=label, context=e.get('context', ''))
                if e.get('focus'): meta['focus'] = e['focus']
                if meta.get('media_type') == 'note':
                    item_lang = resolve(args.language, None, '')
                    save(d/'meta.json', meta)
                    items = json.loads((d/'frames.json').read_text())['frames']
                    with heavy(args, 'note-ocr'): ocr(d, items, item_lang)
                    item_lang = resolve(args.language, None, ocr_text(d))
                    meta['output_language'] = item_lang; save(d/'meta.json', meta)
                    save(d/'asr.json', {'status': T(item_lang, 'audio')['images'], 'status_code': 'images', 'language': None, 'segments': []})
                    (d/'transcript.md').write_text(T(item_lang, 'transcript_title')+'\n\n'+T(item_lang, 'audio')['images']+'\n')
                    ready.append((d, x)); set_state(x, 'prepared', lang); index(out, results, lang, args.template); continue
                inf = probe(video); meta['duration_s'] = float(inf['format']['duration'])
                has_video = any(s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic', 0) == 0 for s in inf['streams'])
                if not has_video: meta['media_type'] = 'audio'
                with open(video, 'rb') as stream: meta['video_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
                save(d/'meta.json', meta)
                previous = fingerprints.get(meta['video_sha256'])
                if previous:
                    for name in ['frames.json', 'contact.jpg', 'ocr.md', 'ocr.json', 'transcript.md', 'transcript.srt', 'transcript.vtt', 'transcript.txt', 'asr.json', 'asr_raw.json', 'audio_events.json']:
                        if (previous/name).exists() and not (d/name).exists(): shutil.copyfile(previous/name, d/name)
                    if (previous/'frames').exists(): shutil.copytree(previous/'frames', d/'frames', dirs_exist_ok=True)
                log(T(lang, 'progress_frames', id=e['id']))
                if not (d/'ocr.md').exists():
                    if has_video:
                        with heavy(args, 'frames'): items = frames(video, d, inf); ocr(d, items, lang)
                    else: audio_only(d, inf)
                if not (d/'transcript.md').exists():
                    log(T(lang, 'progress_asr', id=e['id']))
                    transcribe_item(video, d, args)
                meta = json.loads((d/'meta.json').read_text())
                if (args.language or 'auto').lower() != 'auto': meta['output_language'] = args.language
                else:
                    meta.pop('output_language', None); save(d/'meta.json', meta)
                    meta['output_language'] = item_language(d, args)
                save(d/'meta.json', meta)
                fingerprints[meta['video_sha256']] = d
            for f in ['meta.json', 'transcript.md', 'frames.json', 'ocr.md']:
                if not (d/f).exists():
                    prior = (d/'ERROR.txt').read_text().strip() if (d/'ERROR.txt').exists() else ''
                    raise RuntimeError(T(lang, 'err_missing_artifact', name=f)+(T(lang, 'err_prior', prior=prior) if prior else ''))
            ready.append((d, x)); set_state(x, 'prepared', lang)
        except Exception as ex:
            set_state(x, 'failed', lang, str(ex)[:800]); (d/'ERROR.txt').write_text(x['error']+'\n'); log(e['id']+': '+x['error'])
        index(out, results, lang, args.template)
    visual_ready = [(d, x) for d, x in ready if wants_visual(args, d)]
    if args.stage in ('all', 'visual') and visual_ready:
        # One child owns the only loaded VLM; wait/reap it before the text server.
        unique = {}; aliases = []
        for d, x in visual_ready:
            meta = json.loads((d/'meta.json').read_text())
            fingerprint = meta.get('video_sha256') or str(d)
            if fingerprint in unique: aliases.append((d, unique[fingerprint]))
            else: unique[fingerprint] = d
        pending = [str(d) for d in unique.values() if not (d/'visual.md').exists()]
        if pending:
            try:
                if args.backend == 'cloud':
                    from .visual import cloud_worker
                    cloud_worker(pending, args.visual_frames)
                else:
                    with heavy(args, 'visual'):
                        proc = subprocess.Popen([sys.executable, '-m', 'vidsum.cli', '_visual', args.vision_model, *pending, '--frames', str(args.visual_frames)], stdout=sys.stderr)
                        pidfile = out/'visual.pid'; pidfile.write_text(str(proc.pid))
                        try:
                            wait_owned(proc, args)
                        finally:
                            if proc.poll() is None:
                                proc.terminate()
                                try: proc.wait(timeout=20)
                                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                            pidfile.unlink(missing_ok=True)
            except Exception as ex:
                for d, x in visual_ready:
                    if not (d/'visual.md').exists():
                        (d/'VISUAL_ERROR.txt').write_text(str(ex)+'\n')
        for d, original in aliases:
            for name in ['visual.md', 'visual.json', 'visual_review.json']:
                if (original/name).exists(): shutil.copyfile(original/name, d/name)
            if (d/'visual.md').exists(): save(d/'visual_reuse.json', {'from': original.name, 'inference_elapsed_s': 0, 'metrics_source': original.name+'/visual.json'})
        for d, x in visual_ready:
            if (d/'visual.md').exists(): set_state(x, 'visual_done', lang)
            else: set_state(x, 'failed', lang, T(lang, 'err_visual_incomplete'))
    if args.stage in ('all', 'summarize'):
        missing = [(d, x) for d, x in ready if wants_visual(args, d) and not (d/'visual.md').exists()]
        if missing:
            for d, x in missing:
                set_state(x, 'failed', lang, T(lang, 'err_visual_blocks_text'))
                (d/'ERROR.txt').write_text(x['error']+'\n')
            index(out, results, lang, args.template)
            sys.exit(1)
    if args.stage in ('all', 'summarize') and ready:
        try:
            summarized = {}
            if args.backend == 'cloud':
                from . import cloud
                text_server = contextlib.nullcontext(cloud.settings()['base'])
                gate = contextlib.nullcontext()
            else:
                text_server = server(args); gate = heavy(args, 'llm')
            with gate, text_server as base:
                for d, x in ready:
                    try:
                        if wants_visual(args, d) and not (d/'visual.md').exists():
                            raise RuntimeError(T(lang, 'err_visual_not_done'))
                        if args.force_summary and (d/'summary.md').exists():
                            if not (d/'summary_v1.md').exists(): (d/'summary.md').rename(d/'summary_v1.md')
                            else: (d/'summary.md').unlink()
                        if not (d/'summary.md').exists():
                            if (d/'summary_response.json').exists() and not (d/'summary_response_v1.json').exists():
                                shutil.copyfile(d/'summary_response.json', d/'summary_response_v1.json')
                            fingerprint = json.loads((d/'meta.json').read_text()).get('video_sha256')
                            if fingerprint and fingerprint in summarized: reuse_summary(summarized[fingerprint], d, args)
                            else: summarize(d, base, args)
                        fingerprint = json.loads((d/'meta.json').read_text()).get('video_sha256')
                        if fingerprint: summarized[fingerprint] = d
                        set_state(x, 'ok', lang); (d/'ERROR.txt').unlink(missing_ok=True); log(T(lang, 'progress_done', id=x['id']))
                    except Exception as ex:
                        set_state(x, 'failed', lang, str(ex)[:800]); (d/'ERROR.txt').write_text(x['error']+'\n')
                    index(out, results, lang, args.template)
        except Exception as ex:
            for d, x in ready:
                if x['state'] != 'ok':
                    set_state(x, 'failed', lang, str(ex)); (d/'ERROR.txt').write_text(str(ex)+'\n')
    index(out, results, lang, args.template)
    done = [x for x in results if x['state'] == 'ok']
    if len(entries) == 1 and done:
        print((out/done[0]['id']/'summary.md').read_text())
    for x in results:
        if x['state'] == 'ok': log('summary: '+shown(out/x['id']/'summary.md'))
        elif x['state'] == 'failed': log('failed: '+x['id']+': '+x.get('error', ''))
    log(f'{len(done)}/{len(results)} done. Index: {shown(out/"index.md")}')
    if any(x['state'] == 'failed' for x in results): sys.exit(1)


def main(argv=None):
    p = parser()
    args = p.parse_args(normalize_argv(sys.argv[1:] if argv is None else argv))
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if args.command in ('_visual', '_transcribe'):
        import logging
        logging.getLogger('torch.distributed.elastic.multiprocessing.redirects').setLevel(logging.ERROR)
    if args.command == '_visual':
        from .visual import worker
        worker(args.model, args.directories, args.frames); return
    if args.command == '_transcribe':
        transcribe_worker(Path(args.video), Path(args.directory), args.model, args.hint, args.language); return
    if args.command == 'setup': sys.exit(cmd_setup(args))
    if args.command == 'mcp':
        from .mcp_server import main as mcp_main
        mcp_main(); return
    apply_env_options(p, args)
    if args.command == 'transcribe': sys.exit(cmd_transcribe(args))
    cmd_run(p, args)


if __name__ == '__main__': main()
