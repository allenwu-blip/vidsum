# Benchmarks and model choice

All numbers come from one machine, an Apple M6 with 32 GB of unified memory, with the models already downloaded. Peak memory is MLX's own high-water mark (`mx.get_peak_memory`). Each model runs in its own process, one at a time.

## Small profile on the README clip

Clip: the first 45 seconds of "ScienceCasts: The Zero Gravity Coffee Cup" (Science@NASA, public domain, [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:ScienceCasts-_The_Zero_Gravity_Coffee_Cup.webm)), cut with `ffmpeg -t 45 -c copy`. Measured with `vidsum <clip>` from an installed copy (no uvx startup), under `nice -n 10` with `OMP_NUM_THREADS=4`, `HF_HUB_OFFLINE=1`, a fresh output folder for each run, and no other GPU job on the machine. Runs on 2026-11-23: 1-minute load average 1.7 to 3.8. Runs on 2026-11-24: 1-minute load average 3.3 to 4.4, with about 9 GB of swap in use.

We ran vidsum 0.3.0 with two versions of mlx-vlm on 2026-11-23: 0.7.12, the development environment, and 0.8.0, which `uvx vidsum-local` installed for 0.3.0 (a fresh environment built from its wheel). On 2026-11-24 we ran 0.4.0 in a fresh environment built from the 0.4.0 wheel, which brought mlx-vlm 0.8.1, the version in `requirements-lock.txt`. All three used mlx 0.32.4, mlx-whisper 0.4.3 and mlx-lm 0.32.2.

| What | 0.3.0, mlx-vlm 0.7.12 | 0.3.0, mlx-vlm 0.8.0 | 0.4.0, mlx-vlm 0.8.1 |
|---|---|---|---|
| Full summary with 8 key frames, 3 runs | median 100.5 s, range 98.4–103.6 s | median 95.9 s, range 92.6–105.1 s | median 116.0 s, range 111.0–121.1 s |
| Frame stage inside those runs | 55.3–55.5 s (8 key frames plus the contact sheet) | 54.3–55.3 s | 65.4–66.0 s |
| One key frame, 24 calls | mean 6.2 s, median 5.8 s, range 4.5–8.9 s | mean 6.1 s, median 5.8 s, range 4.4–8.7 s | mean 7.3 s, median 6.9 s, range 5.2–10.4 s |
| `--no-visual`, 1 run | 46.4 s | not run | 43.2 s |
| `transcribe --format srt`, 1 run | 3.3 s | not run | 3.5 s |
| Peak memory, frame stage | 7.18 GB (MLX) | 7.18 GB (MLX) | 7.18 GB (MLX) |

On this clip the two 0.3.0 runs are within run-to-run noise of each other. The 0.4.0 runs were about a fifth slower, almost all of it in the frame stage, which vidsum 0.4.0 did not change. The machine was busier that day (see the load and swap above), and with three runs each we cannot say how much of the gap is mlx-vlm 0.8.1 and how much is the machine. Only the small profile was run on 0.8.0 and 0.8.1; the large-profile numbers below are from 0.7.12. The GIF in the README is one more run on 0.8.0, this time typed as `uvx vidsum-local`: 106.8 s, key frames 5.0–8.6 s each, played back sped up.

An earlier full run on 2026-11-22, while a LoRA training job shared the GPU, took 315 s. A busy GPU can make vidsum about three times slower.

## Link downloads

Measured on 2026-11-24 with vidsum 0.4.0 and yt-dlp 2026.11.23, from a connection in the United States, one public video per site. The time is the download step only (link in, `video.mp4` and `meta.json` out), run through the same code `vidsum` uses; transcription and the summary are not included. End to end on 0.4.0, in the same session as the small-profile runs above: `transcribe <YouTube link> --format srt` took 5.0 s for a 19 s video, and a pasted Douyin share text to a Chinese summary with 8 key frames took 138.0 s for a 54 s video. "Height" is the height of the file that came down: vidsum asks for the best version whose shorter side is at most 720 pixels, so portrait videos arrive at 720x1280 when the site offers it.

| Site | Result | Cookies | Time | Height | Notes |
|---|---|---|---|---|---|
| Douyin, pasted share text with a short link | OK | no | 8.4, 9.2, 16.9 s (3 runs) | 1080 | headless browser route (`[browser]` extra); 54 s video |
| Douyin, short link | OK | no | 17.1, 18.1, 19.2 s (3 runs) | 1080 | headless browser route; 320 s video |
| Xiaohongshu, link with `xsec_token` | OK | no | 3.6 s | 1280 | one run earlier that day failed; a bare note link without the token needs cookies |
| Xiaohongshu, same link with `--cookies` | OK | yes | 10.0 s | 1280 | |
| Bilibili | OK | no | 3.6 s | 720 | 90 s video |
| YouTube | OK | no | 2.2 s | 240 | 19 s video, only offered at 240p |
| TikTok | OK | no | 1.0 s | 960 | 19 s portrait video; two other test posts answered "IP blocked" |
| Instagram reel | OK | no | 1.5 s | 1280 | 5 s video |
| X | OK | no | 1.8 s | 900 | 30 s video |
| Weibo | OK | no | 5.8 s | 720 | 76 s video |
| Vimeo | OK | no | 2.6 s | 720 | via the player URL; DRM-protected videos cannot be downloaded |
| Wikimedia Commons file page | OK | no | 2.3 s | 720 | 241 s video |
| Kuaishou | not supported | | | | yt-dlp has no extractor for it |

Douyin needs signed requests that yt-dlp cannot make without fresh cookies, so with the `[browser]` extra vidsum opens the public page in a headless Chrome or Chromium and reads the video address from the page's own data request. The first load in a new browser profile often gets an empty answer; reloading in the same profile then gets the details. In a side test the same day, 5 of 5 reloads in one profile got the details, against 1 of 5 loads in fresh profiles, so vidsum loads the page up to four times in one profile. Sites change often, so treat this table as a snapshot.

## Large profile, frame stage

Measured on 2026-11-07 with mlx-vlm 0.7.12. Qwen3.6 35B-A3B (4-bit) described key frames for 12 videos (13 jobs; two jobs shared one video). Each video got 8 key frames plus one look at the contact sheet: 96 frame descriptions and 12 contact-sheet calls in total. Images were at most 1008 px on the long side, at most two per call, because each key frame is compared with the one before it.

| | Result |
|---|---|
| Key frame, mean | 5.25 s |
| Key frame, median | 4.32 s |
| Key frame, range | 2.61–65.21 s (the slowest is the first call, which includes warm-up) |
| Contact sheet, mean | 4.30 s |
| Peak memory, frame stage | 21.954 GB (MLX); sampled process RSS peaked at 2.035 GB, which leaves out most Metal memory |
| First model load | 5.68 s |

The test videos were private, so they are not published. The text stage used the same weights in a separate process. We have not run the large profile on a machine with less than 32 GB and do not claim it fits.

A hand check of the frame notes for 3 of these videos covered 168 items: 109 correct, 29 wrong, 30 uncertain. That is 79.0% of the items that could be decided, or 64.9% of all items. It is a small sample, not an accuracy guarantee; the frames and OCR stay next to every summary so you can check.

## Why a new tool

Before writing vidsum we looked at these projects (2026-11-07):

| Project | License | Why we did not use it |
|---|---|---|
| [Video_summarization_mlx](https://github.com/Peter-obi/Video_summarization_mlx) | MIT | Closest match and MLX-native, but transcript only: no frames or OCR, and old pinned dependencies |
| [BiliSum](https://github.com/lycohana/BiliSum) | MIT | Handles local video, but needs Node and several workspaces, and its vision step calls a configurable API |
| [WhisperX](https://github.com/m-bain/whisperX) | BSD-2-Clause | A transcription and alignment component built on PyTorch and CTranslate2, with no MLX path |
| [mlx-examples/whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) | MIT | vidsum uses its published package, mlx-whisper |

vidsum reuses published packages (mlx-whisper, mlx-lm, mlx-vlm, Pillow, PyObjC for Apple Vision) and copies no code from these projects. FFmpeg is an external command and is not shipped. Model weights are not shipped either; each model has its own license, listed in [THIRD_PARTY.md](https://github.com/allenwu-blip/vidsum/blob/main/THIRD_PARTY.md).
