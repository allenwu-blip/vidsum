# Third-party components

vidsum's own code is MIT. Dependencies are installed by your package manager, not vendored. Model weights are downloaded from Hugging Face on first use and are covered by their own licenses. Videos and generated results are never part of this repository.

Python packages:

- [mlx](https://github.com/ml-explore/mlx), [mlx-lm](https://github.com/ml-explore/mlx-lm), [mlx-whisper](https://github.com/ml-explore/mlx-examples): MIT.
- [mlx-vlm](https://github.com/Neywa-Labs/mlx-vlm): MIT.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): MIT.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): Unlicense. [yt-dlp-ejs](https://github.com/yt-dlp/ejs): Unlicense AND MIT AND ISC. Installed with its `default`, `curl-cffi` and `deno` extras: [curl_cffi](https://github.com/lexiforest/curl_cffi) (MIT), [Deno](https://github.com/denoland/deno) (MIT) as the JavaScript runtime yt-dlp uses for YouTube, and from `default`: brotli (MIT), certifi (MPL-2.0), [mutagen](https://github.com/quodlibet/mutagen) (GPL-2.0-or-later), pycryptodomex (BSD and public domain), requests (Apache-2.0), urllib3 (MIT) and websockets (BSD-3-Clause). vidsum does not include or modify any of them; pip or uv installs them as separate packages. vidsum never imports mutagen; yt-dlp uses it to write tags into media files.
- [Playwright for Python](https://github.com/microsoft/playwright-python): Apache-2.0. Only with the optional `browser` extra, for Douyin; it drives your installed Google Chrome or Playwright's Chromium.
- [huggingface_hub](https://github.com/huggingface/huggingface_hub): Apache-2.0.
- [Pillow](https://github.com/python-pillow/Pillow): MIT-CMU (HPND).
- [PyObjC](https://github.com/ronaldoussoren/pyobjc): MIT. Vision, SoundAnalysis and Quartz are Apple system frameworks and are not redistributed.

External programs:

- [FFmpeg](https://ffmpeg.org/legal.html): LGPL or GPL depending on your build. Not bundled.

Default model weights (downloaded by `uvx vidsum-local setup`):

- [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo): converted from OpenAI Whisper, MIT.
- [mlx-community/Qwen3.5-9B-MLX-4bit](https://huggingface.co/mlx-community/Qwen3.5-9B-MLX-4bit): Apache-2.0.
- [lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit](https://huggingface.co/lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit) (`--profile large`): Apache-2.0.

The demo clip in the README is "ScienceCasts: The Zero Gravity Coffee Cup" by Science@NASA, public domain, from [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:ScienceCasts-_The_Zero_Gravity_Coffee_Cup.webm).

`requirements-lock.txt` is the fresh environment that the 0.4.0 wheel resolved on 2026-11-24 (mlx-vlm 0.8.1, yt-dlp 2026.11.23), without the optional `browser` extra. See [benchmarks](https://github.com/allenwu-blip/vidsum/blob/main/docs/benchmarks.md) for which versions each measurement used.
