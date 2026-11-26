# vidsum (skill-only plugin)

This folder is the version of vidsum for the Claude plugin directory. It holds one agent skill and nothing else: no hooks, no scripts. The skill teaches Claude to run the vidsum command-line tool in your terminal and to read its results. This version never uses cookies or a signed-in session.

vidsum summarizes or transcribes a video or audio file on your Mac, or a video link (YouTube, Bilibili, Douyin, Xiaohongshu, TikTok, Instagram, X, Weibo, Vimeo and other sites yt-dlp supports, including pasted share text). You get a timestamped transcript, SRT/VTT subtitles, notes on sampled key frames, the text shown on screen, and a short summary whose key points cite timecodes such as `[audio 01:23]`, `[screen 00:40]` and `[OCR 00:12]`. The quotes section is copied from the transcript by code, not written by the model.

## What you need

- An Apple Silicon Mac for the local models (measured peak memory 7.2 GB; 16 GB Macs should work but are untested).
- uv: follow the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
- FFmpeg: follow the [FFmpeg download page](https://ffmpeg.org/download.html).
- About 7.6 GB of disk for the default models. The skill tells Claude to ask you before it starts that download.

The tool itself is the [vidsum-local](https://pypi.org/project/vidsum-local/) package on PyPI, version 0.4.1. Claude runs it with uv when the skill is used, with `--no-cookies` on every command.

## Example prompts

1. Summarize ~/Movies/keynote.mp4 and list the key quotes with timecodes.
2. Transcribe https://www.youtube.com/watch?v=jNQXAC9IVRw and give me an SRT file.
3. Summarize https://commons.wikimedia.org/wiki/File:ScienceCasts-_The_Zero_Gravity_Coffee_Cup.webm and tell me which points come from what is said and which from what is shown.
4. Break down the first three seconds of this short video for a creator: (a local file or pasted share text).

## Privacy: what it reads, sends, fetches and keeps

- It reads only the video file or link you give it.
- This version does not read browser cookies and never uses a signed-in session. Every command runs with `--no-cookies`, which also leaves vidsum's cookie environment variables unread and tells yt-dlp not to load any cookies. Videos that need a login can't be downloaded: download them yourself and use the local file.
- uv installs vidsum and its Python packages from PyPI. The one-time setup downloads model weights from Hugging Face.
- A link is downloaded from the site it points to (yt-dlp; for Douyin, a headless Chrome that starts from a fresh, empty profile).
- With the default local profiles nothing else leaves your Mac. Results go to `./vidsum-output/` or a folder you choose. There is no telemetry and no vidsum server.
- An experimental cloud profile, for machines without Apple Silicon, sends audio, frames and text to an OpenAI-compatible API that you configure. It is off unless you choose it.
- What it keeps: the results in the output folder, the model weights in the Hugging Face cache (`~/.cache/huggingface/hub`), the packages in uv's cache, and small lock files in `~/.cache/vidsum`. All of it stays on your Mac until you delete it.

Only process videos you have the right to use, and follow each site's terms.

## Source

Source, full documentation and benchmarks: https://github.com/allenwu-blip/vidsum

## License

MIT. See [LICENSE](LICENSE).
