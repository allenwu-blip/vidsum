"""Model profiles and first-run downloads.

Weights go to the Hugging Face cache (default ~/.cache/huggingface/hub; set HF_HOME or
HF_HUB_CACHE to move it). Explicit local paths are used as they are and never trigger a download.
"""
import os
from pathlib import Path
import platform
import shutil
import sys

# Revisions are pinned so a first run downloads exactly what was tested.
WHISPER = ('mlx-community/whisper-large-v3-turbo', 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb', 1.61)
PROFILES = {
    'small': {
        'about': 'Apple Silicon; measured peak 7.2 GB, 16 GB Macs should work but are untested. Qwen3.5 9B (4-bit) describes frames and writes the summary.',
        'asr': WHISPER,
        'llm': ('mlx-community/Qwen3.5-9B-MLX-4bit', '938d8919941c6e7efd3c7150eff7fe9d12afa631', 5.98),
    },
    'large': {
        'about': '32 GB Apple Silicon and up. Qwen3.6 35B-A3B (4-bit), better detail, about 22 GB peak.',
        'asr': WHISPER,
        'llm': ('lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit', '0c4a20a6437ae5985ddc9eb1a3f122ee6c151c3b', 20.43),
    },
    'cloud': {
        'about': 'Any OpenAI-compatible API. Needs VIDSUM_API_KEY; frames, audio and text are sent to that API.',
    },
}
DEFAULT_PROFILE = 'small'


class ModelsMissing(RuntimeError):
    """Raised when weights are not on disk and downloading is not allowed."""


def apple_silicon():
    return sys.platform == 'darwin' and platform.machine() == 'arm64'


def hf_cache():
    from huggingface_hub.constants import HF_HUB_CACHE
    return Path(HF_HUB_CACHE)


def _cached(repo, revision):
    from huggingface_hub import snapshot_download
    try:
        return snapshot_download(repo, revision=revision, local_files_only=True)
    except Exception:
        return None


def _is_local(value):
    """Anything that is not an "org/name" Hugging Face id is used exactly as given."""
    import re
    if not value:
        return False
    if Path(value).expanduser().exists() or value.startswith(('/', '.', '~')):
        return True
    return not re.fullmatch(r'[\w.-]+/[\w.-]+', value)


def plan(profile, overrides):
    """List (role, value, revision, size_gb) for every model this run needs."""
    spec = PROFILES[profile]
    rows = []
    for role in overrides.get('roles', ('asr', 'llm', 'vision')):
        given = overrides.get(role)
        key = 'llm' if role == 'vision' else role
        if given:
            rows.append((role, given, None, None))
        elif key in spec:
            repo, revision, size = spec[key]
            rows.append((role, repo, revision, size))
    return rows


def missing(rows):
    """Rows whose weights are not on disk yet."""
    out = []
    for role, value, revision, size in rows:
        if _is_local(value):
            continue
        if _cached(value, revision) is None:
            out.append((role, value, revision, size))
    return out


def ensure(rows, allow_download=True, log=print):
    """Return {role: local path}. Downloads missing weights unless that is not allowed."""
    from huggingface_hub import snapshot_download
    todo = list({(v, r): (role, v, r, size) for role, v, r, size in missing(rows)}.values())
    offline = os.environ.get('HF_HUB_OFFLINE') == '1'
    if todo and (not allow_download or offline):
        total = sum(s or 0 for *_, s in todo)
        names = ', '.join(v for _, v, _, _ in todo)
        raise ModelsMissing(f'Model weights not downloaded yet: {names} (about {total:.1f} GB). '
                            f'Run `uvx vidsum-local setup` once (`vidsum setup` if you installed it with `uv tool install`), '
                            f'or point --model/--asr-model at local folders.')
    if todo:
        total = sum(s or 0 for *_, s in todo)
        free = shutil.disk_usage(hf_cache().parent if hf_cache().parent.exists() else Path.home()).free / 1e9
        log(f'First run: downloading {total:.1f} GB of model weights to {hf_cache()} '
            f'(free space {free:.0f} GB; set HF_HOME to put them elsewhere).')
        if free < total + 2:
            raise RuntimeError(f'Not enough disk space for {total:.1f} GB of weights ({free:.0f} GB free).')
    paths = {}
    for role, value, revision, size in rows:
        if _is_local(value):
            paths[role] = str(Path(value).expanduser())
            continue
        found = _cached(value, revision)
        if found is None:
            log(f'  {value}' + (f' ({size:.1f} GB)' if size else ''))
            found = snapshot_download(value, revision=revision)
        paths[role] = found
    return paths


def describe_profiles():
    lines = []
    for name, spec in PROFILES.items():
        size = sum(spec[k][2] for k in ('asr', 'llm') if k in spec)
        lines.append(f'  {name:<6} {spec["about"]}' + (f' Download: {size:.1f} GB.' if size else ''))
    return '\n'.join(lines)
