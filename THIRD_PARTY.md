# Third-party components

reel's own code is MIT-licensed (see `LICENSE`). It bundles no third-party code. It installs Python packages into its
own virtualenv and calls external programs as separate processes. Licences below were read from each project's
package metadata or licence file; check them again before you redistribute anything.

## Python packages

| Package | Installed by | Used for | Licence |
|---|---|---|---|
| numpy | `install.sh` (core) | frame and audio analysis, example media | BSD-3-Clause (wheels also include 0BSD, MIT, Zlib and CC0-1.0 parts) |
| Pillow | `install.sh` (core) | grid frames, timeline images, example cards | MIT-CMU (HPND) |
| librosa | `install.sh --beats` | beat detection (fallback backend) | ISC |
| beat-this | `install.sh --beats-ml` | beat and downbeat detection | MIT (code and published model weights; the project notes that some of its training data is under other licences) |
| torch, torchaudio | `install.sh --beats-ml`, `--vision` | beat_this and OWLv2 inference | BSD-style (torch metadata: Apache-2.0, BSD-2/3-Clause, BSL-1.0, ISC, MIT, Zlib parts) |
| transformers | `install.sh --vision` | OWLv2 open-vocabulary detection | Apache-2.0 |
| scipy | `install.sh --vision` | installed with transformers for OWLv2 (the remote setup installs the same set) | BSD-3-Clause |
| playwright | `install.sh --web` | rendering web shots in Chromium | Apache-2.0 (the Chromium build it downloads has its own BSD-style and other licences) |
| pypinyin | `install.sh --zh-voice` | comparing Chinese voice auditions by pinyin | MIT |

Packages pulled in by the optional extras include numba (BSD-2-Clause), llvmlite (BSD-2-Clause and Apache-2.0 with
LLVM exception), scikit-learn (BSD-3-Clause), soundfile (BSD-3-Clause), audioread (MIT), pooch (BSD-3-Clause),
joblib (BSD-3-Clause), msgpack (Apache-2.0), einops (MIT), rotary-embedding-torch (MIT) and **soxr
(LGPL-2.1-or-later)**, a resampling library used by librosa and beat-this. soxr is a dynamically loaded dependency
installed by pip; reel does not modify or redistribute it.

## Model weights downloaded on first use

| Model | Downloaded by | Licence |
|---|---|---|
| beat_this `final0` checkpoint | beat-this, on the first `reel beats` call after `--beats-ml` | MIT |
| `google/owlv2-base-patch16-ensemble` | transformers, on the first local `reel locate` (or `reel remote setup-vision`) | Apache-2.0 |
| whisper.cpp models (for example `ggml-large-v3-turbo.bin`) | you, manually, for `reel words` / `reel voice check` | MIT |

## External programs (installed by you, called as separate processes)

| Program | Used for | Licence |
|---|---|---|
| FFmpeg / ffprobe | all decoding, encoding, filtering and checks | LGPL-2.1-or-later or GPL, depending on the build. The Homebrew build is configured with `--enable-gpl` and libx264 and is GPL-3.0-or-later |
| mpv | burning subtitles when ffmpeg lacks libass | GPL-2.0-or-later and LGPL-2.1-or-later parts |
| whisper.cpp (`whisper-cli`) | per-word timing, voice audition | MIT |
| ssh, rsync | optional remote GPU host | their own licences (system tools) |

reel only runs these programs; it does not link against or ship them. Videos you produce are your own material and
are not covered by these programs' licences, but codec patents and the licences of fonts, music and images you use
are your responsibility.

## Fonts

Subtitles default to the system font PingFang SC (with mpv) or Noto Sans CJK SC (with libass); timeline images use
system fonts when present and Pillow's bundled default font otherwise. Set `REEL_SUB_FONT` to a font whose licence you
have checked (for example an OFL-licensed font) before publishing. The example cards use Pillow's bundled default
font.

## Not used

- No AGPL-licensed or commercially restricted package is installed or required. Object detection uses OWLv2
  (Apache-2.0); no YOLO/Ultralytics (AGPL-3.0) code is included.
- No paid generation service (AI music, image or video generation, cloud text-to-speech) is integrated. Program names
  such as `remotion` and `hyperframes` appear only as patterns in `hooks/deliver_gate.py`, which recognises render
  commands; neither is a dependency.
- Hypit (https://hypit.ai) is a separate third-party product; no Hypit code is included. Its licence and terms were not
  reviewed for this repository.
