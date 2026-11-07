# Dependencies / 第三方组件

Dependencies are installed separately, not vendored. Project MIT covers only original VIDSUM code. Model weights, videos and generated results are excluded.

- [mlx-whisper / mlx-examples](https://github.com/ml-explore/mlx-examples): MIT.
- [mlx-vlm](https://github.com/Neywa-Labs/mlx-vlm): MIT; installed separately, not vendored.
- [mlx-lm](https://github.com/ml-explore/mlx-lm), [mlx](https://github.com/ml-explore/mlx): MIT.
- [Pillow](https://github.com/python-pillow/Pillow): MIT-CMU / HPND; see installed LICENSE files.
- [Playwright Python](https://github.com/microsoft/playwright-python): Apache-2.0. User-installed Chrome has its own terms; not bundled.
- [PyObjC](https://github.com/ronaldoussoren/pyobjc): MIT; Vision, SoundAnalysis and Quartz are system Apple frameworks and not redistributed.
- [FFmpeg](https://ffmpeg.org/legal.html): LGPL/GPL depending on the external binary build; not bundled.
- Whisper / Qwen model licenses belong to their upstream model cards; no model download or redistribution is part of this repository.

See `.venv/.../dist-info` license files for transitive dependencies. `requirements-lock.txt` records the run environment. No code from candidate summarizer repositories or the user's original dy_fetch.py is vendored.
