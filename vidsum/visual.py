"""Offline, serial MLX vision worker. Runs in a separate, PID-owned process."""
import gc
import json
import os
import re
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from .i18n import T, language_name, ui


def text_width(text):
    """Terminal columns the text takes: wide and full-width characters (CJK) take two, combining marks none."""
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def short_line(text, limit=60):
    """One line of at most `limit` terminal columns plus an ellipsis, cut at a word boundary when there is one."""
    text = ' '.join(text.split())
    if text_width(text) <= limit:
        return text
    used = end = 0
    for c in text:
        used += text_width(c)
        if used > limit:
            break
        end += 1
    space = text.rfind(' ', 0, end + 1)  # a space right after the limit still counts as a word boundary
    cut = text[:space] if space > 0 and text_width(text[:space]) >= limit // 2 else text[:end]
    return cut.rstrip(' ,;:-') + '…'


def stdout_is_terminal():
    try:
        return os.isatty(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):  # replaced by a buffer, as in `unittest -b`
        return False


def progress_width(prefix, limit=60):
    """Columns left for the description after `prefix`, so a progress line does not wrap in the terminal.

    At most `limit`. When stdout is not a terminal (a log file, an MCP client) nothing wraps, so `limit` is used.
    One column is kept for the ellipsis and one spare, for terminals that draw the ellipsis two columns wide."""
    if not stdout_is_terminal():
        return limit
    cols = shutil.get_terminal_size().columns
    return max(16, min(limit, cols - text_width(prefix) - 2))


def choose_frames(timeline, count=8):
    """Keep endpoints; prefer scene events spread through the whole clip."""
    items = sorted(timeline['frames'], key=lambda x: x['time_s'])
    if len(items) <= count:
        return items
    selected = [items[0], items[-1]]
    candidates = [x for x in items[1:-1] if x.get('scene_change')]
    while len(selected) < count and candidates:
        best = max(candidates, key=lambda x: min(abs(x['time_s']-s['time_s']) for s in selected))
        selected.append(best); candidates.remove(best)
    candidates = [x for x in items if x not in selected]
    while len(selected) < count and candidates:
        best = max(candidates, key=lambda x: min(abs(x['time_s']-s['time_s']) for s in selected))
        selected.append(best); candidates.remove(best)
    return sorted(selected, key=lambda x: x['time_s'])


def render_visual(data, lang='zh'):
    lines = [T(lang, 'visual_title'), '', T(lang, 'visual_model', model=data['model']), '',
             T(lang, 'visual_caveat'), '']
    for row in data['frames']:
        lines += [f"## {row['timecode']} · {row['file']}", '', row['description'], '']
    lines += [T(lang, 'visual_contact'), '', data['contact']['description'], '',
              T(lang, 'visual_metrics', elapsed=data['elapsed_s'], rss=data['peak_rss_gb'], mlx=data['peak_mlx_gb']), '']
    return '\n'.join(lines)


def output_language(d):
    try:
        return json.loads((Path(d)/'meta.json').read_text()).get('output_language') or 'zh'
    except (OSError, ValueError):
        return 'zh'


def local_describer(model_path, max_tokens=480):
    """Load one MLX-VLM model; return (describe, metrics, close)."""
    import mlx.core as mx
    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    from PIL import Image
    mx.set_cache_limit(256 * 1024 * 1024)
    started = time.monotonic()
    model, processor = load(model_path)
    config = load_config(model_path)
    load_s = time.monotonic() - started

    def describe(paths, prompt):
        images = []
        for path in paths:
            with Image.open(path) as source:
                im = source.convert('RGB')
                im.thumbnail((1008, 1008))
                images.append(im.copy())
        formatted = apply_chat_template(processor, config, prompt, num_images=len(images), enable_thinking=False)
        t = time.monotonic()
        result = generate(model, processor, formatted, image=images, max_tokens=max_tokens,
                          temperature=0.0, verbose=False)
        if result.finish_reason == 'length':
            result = generate(model, processor, formatted, image=images, max_tokens=max_tokens*2,
                              temperature=0.0, verbose=False)
            if result.finish_reason == 'length':raise RuntimeError('视觉回答截断，需增大 token 上限')
        text = result.text
        if not text.strip() or '</think>' not in text and text.startswith('<think>'):
            raise RuntimeError('视觉输出为空或只有思考内容')
        import re
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
        mx.clear_cache(); gc.collect()
        return {'description': text, 'elapsed_s': round(time.monotonic()-t, 3), 'generation_tokens':result.generation_tokens, 'finish_reason':result.finish_reason}

    metrics = {'reset': mx.reset_peak_memory, 'peak': lambda: mx.get_peak_memory()/1e9}
    def close():
        nonlocal model, processor
        del model, processor
        mx.clear_cache(); gc.collect()
    return describe, metrics, load_s, close


def worker(model_path, directories, count=8, max_tokens=480):
    describe, metrics, load_s, close = local_describer(model_path, max_tokens)
    try:
        parts = Path(model_path).parts
        # HF cache paths end in models--org--name/snapshots/<revision>; report org/name instead of the hash.
        name = next((x[8:].replace('--', '/') for x in reversed(parts) if x.startswith('models--')), Path(model_path).name)
        describe_directories(describe, directories, count, name, load_s, metrics)
    finally:
        close()


def cloud_worker(directories, count=8, max_tokens=480):
    from . import cloud
    model = cloud.settings()['model']
    describe_directories(lambda paths, prompt: cloud.describe(paths, prompt, max_tokens), directories, count,
                         'cloud:' + model, 0.0, {'reset': lambda: None, 'peak': lambda: 0.0})


def describe_directories(describe, directories, count, model_name, load_s, metrics):
    for directory in directories:
        d = Path(directory)
        if (d/'visual.md').exists():
            continue
        start = time.monotonic(); metrics['reset']()
        lang = output_language(d)
        # ru_maxrss is lifetime-wide; sample current RSS for a per-video peak.
        rss = [0]; stop = threading.Event()
        def monitor():
            while not stop.is_set():
                try:
                    value = subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())], text=True)
                    rss[0] = max(rss[0], int(value.strip())*1024)
                except (ValueError, subprocess.SubprocessError):
                    pass
                stop.wait(.25)
        sampler = threading.Thread(target=monitor, daemon=True); sampler.start()
        data = {'model': model_name, 'pid': os.getpid(), 'load_s': load_s, 'schema_version':2, 'frames': []}
        try:
            timeline=json.loads((d/'frames.json').read_text())
            selected = choose_frames(timeline, count)
            partial=d/'visual.partial.json'
            if partial.exists():
                old=json.loads(partial.read_text())
                prefix=old.get('frames',[])
                if old.get('schema_version')==2 and old.get('model')==data['model'] and [r['file'] for r in prefix]==[r['file'] for r in selected[:len(prefix)]]:
                    data['frames']=prefix
                    data['prior_elapsed_s']=old.get('elapsed_s',0)
                    data['prior_peak_mlx_gb']=old.get('peak_mlx_gb',0)
                    data['prior_peak_rss_gb']=old.get('peak_rss_gb',0)
            previous=d/'frames'/data['frames'][-1]['file'] if data['frames'] else None
            for frame in selected[len(data['frames']):]:
                paths = ([previous] if previous else []) + [d/'frames'/frame['file']]
                prompt = T(lang, 'frame_prompt', language=language_name(lang))
                row = {**frame, **describe(paths, prompt)}
                data['frames'].append(row); previous = paths[-1]
                data.update(elapsed_s=round(time.monotonic()-start+data.get('prior_elapsed_s',0),3),peak_mlx_gb=max(metrics['peak'](),data.get('prior_peak_mlx_gb',0)),peak_rss_gb=max(rss[0]/1e9,data.get('prior_peak_rss_gb',0)))
                (d/'visual.partial.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
                first = next((l for l in (re.sub(r'\*\*|__', '', x).strip(' -*#') for x in row['description'].splitlines()) if l), '')
                first = re.sub(r'^(?:Subject|人物/主体|主体|画面主体)\s*[:：]\s*', '', first)
                head = f"{d.name}: frame {len(data['frames'])}/{len(selected)} {frame['timecode']} ({row['elapsed_s']:.0f} s) "
                print(head + short_line(first, progress_width(head)), flush=True)
            contact_prompt = T(lang, 'contact_prompt', language=language_name(lang))
            if timeline.get('media_type')=='note':
                contact_prompt=T(lang, 'contact_prompt_images', language=language_name(lang))
            data['contact'] = describe([d/'contact.jpg'], contact_prompt)
            data['elapsed_s'] = round(time.monotonic()-start+data.get('prior_elapsed_s',0), 3)
            data['peak_mlx_gb'] = max(metrics['peak'](),data.get('prior_peak_mlx_gb',0))
            data['peak_rss_gb'] = max(rss[0]/1e9,data.get('prior_peak_rss_gb',0))
            data['lifetime_peak_rss_gb'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1e9
            (d/'visual.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
            (d/'visual.md').write_text(render_visual(data, lang))
            (d/'visual.partial.json').unlink(missing_ok=True)
            (d/'VISUAL_ERROR.txt').unlink(missing_ok=True)
        except Exception as ex:
            (d/'VISUAL_ERROR.txt').write_text(str(ex)+'\n')
            print(d.name, '视觉失败:' if ui(lang) == 'zh' else 'frames failed:', str(ex), flush=True)
        finally:
            stop.set(); sampler.join(timeout=2)
