"""stdio MCP server with two tools: summarize_video and transcribe_video.

The pipeline runs in a child process with its output captured, so nothing but JSON-RPC
reaches stdout. Calls are handled one at a time; heavy steps also take vidsum's file lock.

Environment: VIDSUM_PROFILE (small | large | cloud), VIDSUM_MCP_OUTPUT (where results go,
default ~/.cache/vidsum/mcp), VIDSUM_COOKIES_FROM_BROWSER or VIDSUM_COOKIES for links that need a
signed-in session, plus the cloud settings described in vidsum/cloud.py.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, models

MAX_CHARS = 60000
_one_at_a_time = threading.Lock()

mcp = MCPServer(
    'vidsum',
    title='vidsum: local video summaries',
    version=__version__,
    website_url='https://github.com/allenwu-blip/vidsum',
    instructions=(
        'Use summarize_video when the user wants to know what a video or recording says or shows: a summary, key points, '
        'a timeline, notes from a lecture, meeting, tutorial or talk. Use transcribe_video when they want the words: a transcript, '
        'subtitles (SRT/VTT) or exact quotes. Both take a file path on this computer, a video link (YouTube, Bilibili, TikTok, '
        'Instagram, X, Weibo, Vimeo, Xiaohongshu, Douyin and other sites yt-dlp supports) or pasted share text that contains a link. '
        'Links are downloaded first (one video per link; Douyin needs the server started with the browser extra). '
        'With the default local profiles the models then run on this machine and nothing is uploaded; '
        'VIDSUM_PROFILE=cloud sends audio and frames to the API you set. A summary takes about 2 minutes for a 45 s clip on an Apple M6, '
        'longer for long videos; a transcript is faster. Only use videos the user has the right to process.'),
)


def _cache():
    base = os.environ.get('VIDSUM_MCP_OUTPUT') or os.path.join(
        os.environ.get('VIDSUM_HOME') or os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'vidsum'), 'mcp')
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _not_ready(roles):
    """Return a message if model weights still need downloading (a tool call would time out)."""
    profile = os.environ.get('VIDSUM_PROFILE') or models.DEFAULT_PROFILE
    if profile == 'cloud':
        from . import cloud
        return None if cloud.settings()['key'] else 'The cloud profile is selected but VIDSUM_API_KEY is not set in the MCP server environment.'
    if not models.apple_silicon():
        return ('Local models need an Apple Silicon Mac. Set VIDSUM_PROFILE=cloud and VIDSUM_API_KEY '
                '(any OpenAI-compatible API) in this MCP server\'s environment.')
    overrides = {'roles': roles, 'asr': os.environ.get('VIDSUM_ASR_MODEL'), 'llm': os.environ.get('VIDSUM_MODEL'),
                 'vision': os.environ.get('VIDSUM_VISION_MODEL')}
    todo = models.missing(models.plan(profile, overrides))
    if not todo:
        return None
    size = sum(s or 0 for *_, s in todo)
    flag = ' --transcribe-only' if roles == ('asr',) else ''
    return (f'vidsum has not downloaded its models yet (about {size:.1f} GB, one time, stored in {models.hf_cache()}). '
            f'Ask the user first, then run this in a terminal and call the tool again when it finishes:\n'
            f'  "{sys.executable}" -m vidsum.cli setup{flag}')


def _run(args, timeout):
    with _one_at_a_time:
        started = time.monotonic()
        proc = subprocess.Popen([sys.executable, '-m', 'vidsum.cli', *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            # SIGTERM first: the CLI turns it into KeyboardInterrupt and stops its own children (yt-dlp, model
            # workers) and removes the temporary cookie copy. SIGKILL only if it has not exited after 15 s.
            proc.terminate()
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise
        result = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    return result, time.monotonic() - started


def _folder(path, kind):
    digest = hashlib.sha256(f'{path}|{kind}'.encode()).hexdigest()[:10]
    return _cache() / f'{kind}_{digest}'


def _clip(text, where):
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f'\n\n[cut at {MAX_CHARS} characters; full text: {where}]'


def _failure(result):
    lines = [line for line in (result.stderr or '').splitlines() if line.strip()]
    return 'vidsum failed: ' + ' | '.join(lines[-4:])[:1500]


def summarize(path, language='auto', focus='', describe_frames=True):
    note = _not_ready(('asr', 'llm'))
    if note:
        return note
    out = _folder(path, 'summary')
    args = ['run', path, '--output', str(out), '--language', language or 'auto']
    if focus:
        args += ['--focus', focus]
    if not describe_frames:
        args.append('--no-visual')
    result, seconds = _run(args, timeout=4 * 3600)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(_failure(result))
    return _clip(result.stdout.strip(), str(out)) + f'\n\n---\nvidsum {__version__}, {seconds:.0f} s. Evidence files (frames, transcript, OCR): {out}'


def transcribe(path, language='', format='md'):
    note = _not_ready(('asr',))
    if note:
        return note
    out = _folder(path, 'transcript')
    args = ['transcribe', path, '--output', str(out), '--format', format or 'md']
    if language:
        args += ['--asr-language', language]
    result, seconds = _run(args, timeout=4 * 3600)
    if result.returncode != 0:
        raise RuntimeError(_failure(result))
    return _clip(result.stdout.strip(), str(out)) + f'\n\n---\nvidsum {__version__}, {seconds:.0f} s. Also saved as .md, .srt, .vtt, .txt and .json in {out}'


# openWorldHint: links are downloaded from the web, and the cloud profile sends audio and frames to an API.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
async def summarize_video(path: str, language: str = 'auto', focus: str = '', describe_frames: bool = True) -> str:
    """Summarize a video (or audio) file: what it is about, key points with timestamps, a timeline, what is shown on screen, and verbatim quotes.

    Use this when the user asks to summarize, watch, explain, review or take notes on a video, recording, lecture,
    meeting, talk, tutorial or screen recording: a file on this computer (mp4, mov, mkv, webm, mp3, m4a, wav, ...) or a
    link to a video page (YouTube, Bilibili, Douyin, Xiaohongshu, TikTok, Instagram, X, Weibo, Vimeo, ...; Douyin
    needs the server started with the [browser] extra). One link gives one video; live streams are skipped.
    It transcribes the speech, samples key frames, reads on-screen text and writes a cited Markdown summary.
    Links are downloaded with yt-dlp first. With the default local profiles nothing is uploaded; VIDSUM_PROFILE=cloud
    sends audio and frames to the API you set.
    Takes about 2 minutes for a 45 s clip on an Apple M6, plus download time, longer for long videos.

    Args:
        path: absolute path to a file, a video link, or pasted share text that contains a link
            (e.g. a Douyin or Xiaohongshu share message). Sites that need a login use the server's cookie settings.
        language: language of the summary, e.g. "en", "zh", "ja"; "auto" follows the spoken language.
        focus: optional question the summary should answer, e.g. "what does the speaker say about pricing?".
        describe_frames: false skips looking at the picture (faster, text only).
    """
    return await asyncio.to_thread(summarize, path, language, focus, describe_frames)


@mcp.tool(annotations=READ_ONLY)
async def transcribe_video(path: str, language: str = '', format: str = 'md') -> str:
    """Transcribe the speech in a video or audio file into timestamped text, or into SRT / VTT subtitles.

    Use this when the user wants a transcript, captions or subtitles, or the exact words someone said in a video or
    recording, on this computer or behind a video link. Faster than summarize_video because it skips frames and the language model.
    Links are downloaded first. With the default local profiles it runs Whisper large-v3-turbo on Apple Silicon and
    nothing is uploaded; VIDSUM_PROFILE=cloud sends the audio to the API you set.

    Args:
        path: absolute path to a file, a video link, or pasted share text that contains a link.
        language: spoken-language hint such as "en" or "zh"; empty means detect automatically.
        format: "md" (timestamped lines, default), "srt", "vtt", "txt" or "json".
    """
    return await asyncio.to_thread(transcribe, path, language, format)


def main():
    mcp.run('stdio')


if __name__ == '__main__':
    main()
