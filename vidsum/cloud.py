"""OpenAI-compatible API backend: transcription, frame description and the summary.

Settings (environment):
  VIDSUM_API_KEY        API key (falls back to OPENAI_API_KEY)
  VIDSUM_API_BASE       base URL, default https://api.openai.com/v1
  VIDSUM_CLOUD_MODEL    chat model that accepts images, default gpt-6-luna
  VIDSUM_ASR_API_BASE / VIDSUM_ASR_API_KEY / VIDSUM_CLOUD_ASR_MODEL
                        speech-to-text endpoint, defaults to the same API and whisper-1
                        (whisper-1 returns segment timestamps)
"""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from . import __version__

USER_AGENT = f'vidsum/{__version__}'
MAX_UPLOAD = 24 * 1024 * 1024


def settings():
    key = os.environ.get('VIDSUM_API_KEY') or os.environ.get('OPENAI_API_KEY')
    base = (os.environ.get('VIDSUM_API_BASE') or 'https://api.openai.com/v1').rstrip('/')
    return {
        'key': key,
        'base': base,
        'model': os.environ.get('VIDSUM_CLOUD_MODEL') or 'gpt-6-luna',
        'asr_key': os.environ.get('VIDSUM_ASR_API_KEY') or key,
        'asr_base': (os.environ.get('VIDSUM_ASR_API_BASE') or base).rstrip('/'),
        'asr_model': os.environ.get('VIDSUM_CLOUD_ASR_MODEL') or 'whisper-1',
    }


def require_key():
    s = settings()
    if not s['key']:
        raise RuntimeError('Cloud profile needs an API key: set VIDSUM_API_KEY (or OPENAI_API_KEY). '
                           'Use VIDSUM_API_BASE for any other OpenAI-compatible service.')
    return s


def _request(url, key, body, content_type, timeout):
    headers = {'Content-Type': content_type, 'User-Agent': USER_AGENT}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', 'replace')[:600]
            if error.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 3)
                continue
            raise CloudError(error.code, detail) from None


class CloudError(RuntimeError):
    def __init__(self, code, detail):
        super().__init__(f'API error {code}: {detail}')
        self.code, self.detail = code, detail


def chat(base, key, model, content, limit, temperature=.15, extra=None, timeout=480):
    """POST /chat/completions. Adapts to servers that reject max_tokens or temperature."""
    payload = {'model': model, 'messages': [{'role': 'user', 'content': content}], 'max_tokens': limit,
               'temperature': temperature, **(extra or {})}
    for _ in range(4):
        try:
            return _request(base + '/chat/completions', key, json.dumps(payload).encode(), 'application/json', timeout)
        except CloudError as error:
            text = error.detail.lower()
            if error.code != 400:
                raise
            if 'max_tokens' in text and 'max_tokens' in payload:
                payload['max_completion_tokens'] = payload.pop('max_tokens')
            elif 'temperature' in text and 'temperature' in payload:
                payload.pop('temperature')
            elif extra and any(k in text for k in extra):
                for k in extra:
                    payload.pop(k, None)
            else:
                raise
    raise RuntimeError('API kept rejecting the request parameters')


def image_part(path, longest=1008):
    from PIL import Image
    with Image.open(path) as source:
        im = source.convert('RGB')
        im.thumbnail((longest, longest))
        buffer = io.BytesIO()
        im.save(buffer, format='JPEG', quality=88)
    return {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}}


def describe(paths, prompt, max_tokens=480):
    s = require_key()
    content = [{'type': 'text', 'text': prompt}] + [image_part(p) for p in paths]
    started = time.monotonic()
    response = chat(s['base'], s['key'], s['model'], content, max_tokens * 2, temperature=0)
    text = (response['choices'][0]['message'].get('content') or '').strip()
    if not text:
        raise RuntimeError('vision API returned an empty answer')
    return {'description': text, 'elapsed_s': round(time.monotonic() - started, 3),
            'generation_tokens': (response.get('usage') or {}).get('completion_tokens'),
            'finish_reason': response['choices'][0].get('finish_reason')}


def _multipart(fields, file_field, filename, data, mime):
    boundary = 'vidsum' + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: {mime}\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def _audio_chunks(video, work, seconds=600):
    """Mono 16 kHz 32 kbps MP3 pieces, each well under the 25 MB upload limit."""
    pattern = work / 'chunk_%03d.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-vn', '-ac', '1', '-ar', '16000', '-b:a', '32k',
                    '-f', 'segment', '-segment_time', str(seconds), '-reset_timestamps', '1', str(pattern)],
                   check=True, capture_output=True)
    return sorted(work.glob('chunk_*.mp3'))


def transcribe(video, work, language=None, seconds=600):
    """Return a Whisper-shaped dict: text, language, segments[{start, end, text}]."""
    s = require_key()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    segments, texts, detected = [], [], None
    try:
        for index, chunk in enumerate(_audio_chunks(video, work, seconds)):
            data = chunk.read_bytes()
            if len(data) > MAX_UPLOAD:
                raise RuntimeError('audio chunk too large for the API upload limit')
            fields = [('model', s['asr_model']), ('response_format', 'verbose_json'), ('timestamp_granularities[]', 'segment')]
            if language:
                fields.append(('language', language))
            body, ctype = _multipart(fields, 'file', chunk.name, data, 'audio/mpeg')
            try:
                result = _request(s['asr_base'] + '/audio/transcriptions', s['asr_key'], body, ctype, 600)
            except CloudError as error:
                if error.code != 400:
                    raise
                # Models without timestamps (for example gpt-transcribe) only accept json.
                fields = [f for f in fields if f[0] not in ('response_format', 'timestamp_granularities[]')]
                fields.append(('response_format', 'json'))
                body, ctype = _multipart(fields, 'file', chunk.name, data, 'audio/mpeg')
                result = _request(s['asr_base'] + '/audio/transcriptions', s['asr_key'], body, ctype, 600)
            offset = index * seconds
            detected = detected or result.get('language') or (result.get('languages') or [None])[0]
            texts.append(result.get('text', ''))
            rows = result.get('segments')
            if rows:
                for row in rows:
                    segments.append({'start': offset + float(row['start']), 'end': offset + float(row['end']),
                                     'text': row.get('text', ''), 'avg_logprob': row.get('avg_logprob', 0),
                                     'no_speech_prob': row.get('no_speech_prob', 0)})
            elif result.get('text', '').strip():
                segments.append({'start': float(offset), 'end': float(offset + seconds), 'text': result['text'],
                                 'untimed': True})
    finally:
        for chunk in work.glob('chunk_*.mp3'):
            chunk.unlink(missing_ok=True)
    return {'text': ' '.join(t.strip() for t in texts if t), 'language': _code(detected), 'segments': segments,
            'backend': 'cloud:' + s['asr_model']}


def _code(language):
    names = {'english': 'en', 'chinese': 'zh', 'japanese': 'ja', 'korean': 'ko', 'spanish': 'es', 'french': 'fr',
             'german': 'de', 'portuguese': 'pt', 'italian': 'it', 'russian': 'ru'}
    if not language:
        return None
    if isinstance(language, dict):
        language = language.get('language') or language.get('code')
    return names.get(str(language).lower(), str(language).lower())
