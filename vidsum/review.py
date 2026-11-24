"""Deterministic evidence sections: do not let an LLM invent visual facts or quotes."""
import json
from pathlib import Path
import re
from .i18n import T, sections, heading, ui

PLATFORM_FIELDS = ['author', 'followers', 'published_at', 'likes', 'comments', 'collects', 'shares']


def normalize_headings(body, names):
    for name in names:
        body = re.sub(r'^## ' + re.escape(name) + r'[^\n]*', '## ' + name, body, flags=re.M)
    return body


def replace_section(body, name, content):
    pattern = r'^## ' + re.escape(name) + r'[ \t]*(?:\n|\Z).*?(?=^## |\Z)'
    replacement = '## ' + name + '\n\n' + content.strip() + '\n\n'
    if re.search(pattern, body, re.M | re.S):
        return re.sub(pattern, lambda _: replacement, body, flags=re.M | re.S)
    return body + '\n' + replacement


def drop_section(body, name):
    return re.sub(r'^## ' + re.escape(name) + r'[ \t]*(?:\n|\Z).*?(?=^## |\Z)', '', body, flags=re.M | re.S)


def spread(items, n=3):
    if len(items) <= n:
        return items
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def evidence_sections(body, d: Path, use_visual=True, lang='zh', template='creator'):
    names = [name for _, name in sections(template, lang)]
    role = lambda r: heading(template, lang, r)
    body = normalize_headings(body, names)
    editorial = d / 'editorial_review.json'
    overrides = json.loads(editorial.read_text()) if editorial.exists() else {}

    def select_quotes(items, key):
        patterns = overrides.get(key)
        if patterns is None:
            return spread(items)
        selected = []
        for pattern in patterns:
            found = next((item for item in items if re.search(pattern, item[1]) and item not in selected), None)
            if found:
                selected.append(found)
        return selected[:3]

    meta = json.loads((d / 'meta.json').read_text())
    timeline = json.loads((d / 'frames.json').read_text())
    a = json.loads((d / 'asr.json').read_text())
    ocr = json.loads((d / 'ocr.json').read_text())
    meta['analysis'] = {'scene_change_candidates': timeline['detected_scene_changes'], 'sampled_frames': len(timeline['frames']),
                        'scene_threshold': timeline['scene_threshold'], 'method': 'FFmpeg pixel scene scores + uniform sampling; not verified cuts'}
    characters = sum(len(re.sub(r'\s+', '', seg['text'])) for seg in a['segments'])
    meta['analysis']['transcript_characters'] = characters
    meta['analysis']['characters_per_second'] = round(characters / meta['duration_s'], 2) if meta.get('duration_s') else None
    (d / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')

    shots = T(lang, 'shots_text_only')
    shots += T(lang, 'shots_scene_counts', candidates=meta['analysis']['scene_change_candidates'], sampled=meta['analysis']['sampled_frames'])
    if use_visual and (d / 'visual.md').exists():
        found = re.search(r'^## ' + re.escape(role('shots')) + r'\n(.*?)(?=^## |\Z)', body, re.M | re.S)
        shots = (found.group(1).strip() if found else T(lang, 'shots_visual_fallback')) + T(lang, 'shots_visual_note')
    if use_visual and (d / 'visual_review.json').exists():
        review = json.loads((d / 'visual_review.json').read_text())
        if review.get('corrections'):
            label = T(lang, 'review_contact') if str(review.get('review_scope', '')).startswith('contact_only') else T(lang, 'review_frames')
            shots = T(lang, 'review_note', label=label, corrections=review['corrections']) + '\n\n' + shots
    if meta.get('media_type') != 'audio':
        body = replace_section(body, role('shots'), shots)
    else:
        body = drop_section(body, role('shots'))

    transcript = (d / 'transcript.md').read_text()
    quotes = []
    for line in transcript.splitlines():
        m = re.match(r'- \[([^]]+)\] (.+)', line)
        cut = ui(lang) != 'zh' and m and m.group(2).rstrip().endswith(('-', '—', '...', '…'))  # sentence cut off mid-word
        if m and not cut and 6 <= len(m.group(2).strip()) <= 180 and not any(x in m.group(2) for x in ['明镜与点点栏目', 'Amara.org', '字幕志愿者', '字幕由']):
            quotes.append((m.group(1), m.group(2)))
    qo, qc = T(lang, 'quote_open'), T(lang, 'quote_close')
    q = [T(lang, 'quotes_audio'), '', T(lang, 'quotes_audio_note', status=a['status']), '']
    for t, text in select_quotes(quotes, 'audio_quote_patterns'):
        q.append(f'- [{t}] {qo}{text}{qc}')
    if not quotes:
        q.append(T(lang, 'quotes_none_audio'))
    candidates = []
    for row in ocr:
        if ui(lang) == 'zh':
            # Chinese videos: pure-ASCII lines are almost always handles and watermarks.
            junk = lambda t: re.fullmatch(r'[@©\s]*[A-Za-z0-9_ .]+', t)
        else:
            junk = lambda t: re.match(r'^\s*[@©]', t) or re.fullmatch(r'\S+\.(?:com|net|org|cn|tv)\S*', t) or ' ' not in t.strip()
        lines = [x['text'] for x in row['lines'] if x.get('confidence', 0) >= .5 and 6 <= len(x['text']) <= 180
                 and not junk(x['text'])]
        if lines:
            candidates.append((row['timecode'], max(lines, key=len)))
    if meta.get('media_type') != 'audio':
        q += ['', T(lang, 'quotes_ocr'), '']
        # Editorial patterns may select any original OCR line, not just the longest.
        if 'ocr_quote_patterns' in overrides:
            candidates = [(row['timecode'], line['text']) for row in ocr for line in row['lines'] if 4 <= len(line['text']) <= 180]
        chosen = select_quotes(candidates, 'ocr_quote_patterns')
        for t, text in chosen:
            q.append(f'- [{t}] {qo}{text}{qc}')
        if not chosen:
            q.append(T(lang, 'quotes_none_ocr'))
    body = replace_section(body, role('quotes'), '\n'.join(q))

    has_platform_data = any(meta.get(k) is not None for k in PLATFORM_FIELDS)
    if template == 'creator' or has_platform_data:
        table = list(T(lang, 'data_header'))
        for key, label in T(lang, 'data_labels'):
            value = meta.get(key)
            table.append('| ' + label + ' | ' + (T(lang, 'not_provided') if value is None else str(value).replace('|', '/')) + ' |')
        table += ['', T(lang, 'data_note')]
        body = replace_section(body, role('data'), '\n'.join(table))
    else:
        body = drop_section(body, role('data'))

    model_roles = [r for r in ('about', 'structure', 'takeaways')]
    if editorial.exists() and not (use_visual and (d / 'visual.md').exists()):
        for r in model_roles:
            key = role(r)
            if key in overrides:
                body = replace_section(body, key, overrides[key])
    if use_visual and (d / 'visual_review.json').exists():
        review = json.loads((d / 'visual_review.json').read_text())
        allowed = [role(r) for r in model_roles]
        for name, content in review.get('summary_overrides', {}).items():
            if name in allowed:
                body = replace_section(body, name, content)
    return body.rstrip() + '\n'
