# VIDSUM

Apple Silicon 本地视频总结：MLX Whisper 转写、FFmpeg 场景帧、macOS Vision OCR、MLX-VLM 看画面，再由本地千问 35B 合成带来源的摘要。支持本地视频、公开抖音视频和可取得公开图片的图文作品。

## 安装

需要 macOS Apple Silicon、Python 3.11+、FFmpeg/ffprobe；抖音公开下载需要本机 Chrome。安装版本见 requirements-lock.txt，模型权重单独准备，不随代码分发。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install -e . --no-deps
export VIDSUM_MODEL="/path/to/Qwen-35B-A3B-MLX-4bit"
export VIDSUM_VISION_MODEL="/path/to/local/Qwen-VL-MLX-4bit"
export VIDSUM_ASR_MODEL="/path/to/whisper-mlx"
export VIDSUM_LOCK_SCRIPT="/path/to/heavy_lock.sh"
export VIDSUM_GLOBAL_LOCK_STATE="/path/to/shared/heavy"
.venv/bin/vidsum run ./clip.mp4 --output ./results \
  --lock-state "$VIDSUM_GLOBAL_LOCK_STATE"
```

模型通过参数或环境变量传入。视觉模型须既有 `vision_config`，又有视觉塔权重，并能由锁定版本的 `mlx-vlm` 实际加载和推理；单有配置字段不足以证明可用。优先检查已有模型；另选模型时建议约 8B 的千问 VL MLX 4bit，检查下载体积、上游许可和实测准确率。模型下载是显式准备步骤，流水线不会自动下载；离线推理可设 `HF_HUB_OFFLINE=1`。本轮现有 35B 也含视觉塔，因此允许视觉、文本两个阶段复用同一磁盘权重，进程分别加载。

32GB Mac 可运行本轮已有 35B 4bit，但需要关闭其他占内存的模型并严格串行；更小视觉模型的需求取决于图像分辨率、图像张数和上下文，8B 4bit 建议至少 16GB，仍应在本机测峰值。35B 视觉与文字模型不能同时驻留。实测内存、速度见 [选型.md](选型.md)。

## 开关与分阶段

- `--vision-model /path/to/model` 或 `VIDSUM_VISION_MODEL` 开启看图；`--no-visual` 关闭。未配模型时明确标“画面没看，只看了字”。
- `--visual-frames 8` 选择 8–12 张关键帧，保留首尾，优先取分散的场景候选，不足用均匀帧补齐。原始取帧最多 24 张，contact.jpg 覆盖完整采样。
- `--stage prepare` 仅下载、提帧、OCR、转写；`--stage visual` 从产物恢复并跑完全部看图；`--stage summarize` 只启动文字模型合成；默认 `--stage all` 按这三个阶段依次运行。
- `--force-summary` 重写摘要前将旧 summary.md 保存为 summary_v1.md；已有 v1 不覆盖。原始旧模型回答另存 summary_response_v1.json。

看图独立子进程只加载一次视觉模型，逐帧输入当前图和上个时间点的图，描述主体、动作场景、景别构图、文字、风格及变化；再单独看 contact.jpg。视觉进程退出并回收 PID 后才启动文字服务，结束时只按本工具创建的 PID 停止。重复视频按 SHA256 复用视觉结果。已核对的 visual_review.json 会优先校正视觉原文，并用于重建拍法段落，原始 visual.json 保留。失败条目隔离，配置视觉后缺 visual.md 不会算摘要成功。图文图片按原序号列出，无视频时间码，不虚构时长或音轨。

重任务先 acquire、结束 release。`heavy_lock.sh` 须支持 `HEAVY_LOCK`。锁状态与其他重任务使用同一目录时可跨工具互斥。若只允许在工作区写入，可将实际锁放工作区、只读检查外部锁（`--global-lock-state`）；这种隔离配置存在检查后的竞态，无法原子阻止外部任务抢占；检测到外部新占用会按自有 PID 停止本模型，视觉阶段已完成的帧保存在 visual.partial.json，重跑恢复。报告须说明。不会修改外部锁脚本。

## 产物与证据

`--manifest batch.json` 接收数组，每项有 `id`、`input`，可选 `label`、`allen_context`；或直接传入本地文件与完整抖音分享文字。抖音只读公共页面，不登录、不持久保存 cookie。`--dy-fetch-script` 可只读调用用户已有脚本的 capture_all，不调用它的 ASR。公开详情未返回则记录失败，不绕过验证。

每条保存 meta.json、transcript.md、ocr.md/json、frames/、frames.json、contact.jpg、visual.md/json、summary.md 和原始模型响应；总目录保存 index.md、batch_status.json。已有有效产物会复用；换输入或视觉模型时用新目录，或先归档旧 visual.md/json 再重跑。旧 editorial_review.json 只适用于未看图的摘要，不覆盖新的视觉总结。

摘要区分 `[画面 时间码]`、`[声音 时间码]`、`[字幕/OCR 时间码]`。模型描述可能错，关键动作应核对原帧。单帧不能证明运镜、AI 来源或人物身份；稀疏帧不能确定真实切镜频率。OCR 原文和音轨引用由代码重建，不让总结模型改写。Whisper 可能把配乐幻听成口播，歌曲不当作作者解说。平台缺失数据写未提供，作品内收入和成绩属于视频自述，未经核实。

测试：`.venv/bin/python -m unittest discover -s tests -v`。代码、模型、结果和凭据分开，见 .gitignore 和 [THIRD_PARTY.md](THIRD_PARTY.md)。工具不执行发布、建远端或推送。

## English

Local video evidence and summaries on Apple Silicon. MLX Whisper, scene/uniform sampling, macOS OCR, then optional MLX-VLM descriptions of 8–12 frames and the contact sheet. A separate Qwen 35B text stage merges timestamped visual, audio, and OCR evidence. The vision child exits before the text server starts; only owned PIDs are terminated. Repeated video hashes reuse visual observations.

Set `VIDSUM_VISION_MODEL` to enable vision; `--no-visual` disables it. Use `--stage prepare`, `visual`, or `summarize` to resume stages, and `--force-summary` to preserve the original as summary_v1.md. Use local model paths; no automatic weight downloads. 32GB is recommended for the existing 35B 4bit model; smaller 8B vision models need separate measurement. Share the same heavy-lock state with other tools for atomic exclusion. Descriptions are sampled evidence, not verified continuous motion or definitive AI provenance. Public Douyin retrieval uses no login. No publishing or GitHub push is performed.
