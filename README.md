# normalise_videos.py

Normalises the audio levels of two video files to a common loudness target using the EBU R128 standard (LUFS), then remuxes the result back into the original video container — or exports audio-only MP3s. The video stream is never re-encoded.

## Requirements

- Python 3.10+
- FFmpeg (must be on `PATH`)
- [uv](https://docs.astral.sh/uv/) package manager

---

## Setup

### 1. Install uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# or with Homebrew
brew install uv
```

### 2. Create the environment and install dependencies

```bash
uv sync
```

This reads `pyproject.toml`, creates a `.venv/` in the project directory, and installs all dependencies in one step.

### 3. Verify FFmpeg is available

```bash
ffmpeg -version
ffprobe -version
```

If FFmpeg is not installed: `brew install ffmpeg` (macOS) or see [ffmpeg.org/download.html](https://ffmpeg.org/download.html).

---

## Basic usage

```bash
uv run normalise_videos.py video_1.webm video_2.webm
```

Outputs normalised files to `./out/` by default:

```
out/video_1_normalised.mkv
out/video_2_normalised.mkv
```

---

## How it works

The script runs in numbered phases:

| Phase | Description |
|-------|-------------|
| **2** | Extract audio from each video to a lossless 24-bit WAV at 48 kHz |
| **2.5** | *(optional)* Compress dynamic range with `dynaudnorm` before analysis |
| **3** | Measure integrated loudness (EBU R128, Pass 1) |
| **4** | Normalise to the target LUFS using `loudnorm` linear mode (Pass 2) |
| **5** | Re-encode audio (FLAC / AAC / MP3) and remux back into the video container |
| **6** | Validate: re-measure output loudness and confirm it is within ±1 LU of the target |
| **6.5** | Equalise: if the two outputs differ by more than 0.2 LU, boost the quieter one to match the louder |
| **6.7** | *(optional)* Report max momentary and max short-term LUFS for both outputs |
| **6.8** | *(optional)* Apply a static volume gain to the quieter file so both files match in max momentary loudness |
| **7** | *(optional)* Re-measure both final outputs, average all five loudnorm metrics, and save a JSON reference profile |

---

## Options

### `--output-dir DIR`
Directory to write output files into.  
**Default:** `./out`

```bash
uv run normalise_videos.py video_1.webm video_2.webm --output-dir /tmp/exports
```

---

### `--codec {flac,aac}`
Audio codec for the remuxed output.

| Value | Description |
|-------|-------------|
| `flac` | 24-bit lossless FLAC (default). WebM and MP4 containers are automatically upgraded to MKV since they do not support FLAC. |
| `aac` | AAC at 320 kbps — near-transparent lossy. Compatible with MP4 and WebM. |

```bash
uv run normalise_videos.py video_1.webm video_2.webm --codec aac
```

---

### `--target LUFS`
Integrated loudness target in LUFS (Loudness Units relative to Full Scale).  
**Default:** `-14.0` (EBU R128 / Spotify / YouTube standard)

```bash
uv run normalise_videos.py video_1.webm video_2.webm --target -16.0
```

---

### `--keep-wav`
Copy the lossless WAV intermediates (raw extracted audio and normalised audio) into `--output-dir` instead of discarding them after processing.

```bash
uv run normalise_videos.py video_1.webm video_2.webm --keep-wav
```

---

### `--audio-only`
Export normalised audio as **MP3 320 kbps / 48 kHz** instead of remuxing back into the video container. The video stream is discarded entirely and the remux step is skipped. Output filenames end in `_normalised.mp3`.

```bash
uv run normalise_videos.py video_1.webm video_2.webm --audio-only
```

---

### `--perceived {dynaudnorm,shortterm,both}`
Extra processing to improve perceived loudness matching between the two files.

| Value | Phase | Description |
|-------|-------|-------------|
| `dynaudnorm` | 2.5 | Compresses the dynamic range of each file with FFmpeg `dynaudnorm` before loudnorm runs. Raises quiet passages and reduces the loudness range (LRA), making the integrated LUFS a better proxy for how loud the audio *sounds*. |
| `shortterm` | 6.7 | After normalisation, measures and reports the **max momentary** (400 ms window) and **max short-term** (3 s window) LUFS for both outputs. Useful for diagnosing why two files can have the same integrated LUFS but still sound different. |
| `both` | 2.5 + 6.7 | Applies both of the above. |

```bash
uv run normalise_videos.py video_1.webm video_2.webm --perceived both
```

---

### `--match-momentary`
**(Phase 6.8)** After normalisation, measures the **max momentary loudness** (400 ms window) of each output. If the files differ by more than 0.5 LU, applies a static volume gain to the quieter file to bring its momentary peak up to match the louder one.

This directly addresses cases where two files have matching integrated LUFS but one still sounds louder — typically because its short transient peaks are higher. The gain is applied to the normalised WAV before final encoding, so no quality is lost.

A before/after comparison table is printed on completion.

```bash
uv run normalise_videos.py video_1.webm video_2.webm --match-momentary
```

---

### `--save-profile FILE.json`
**(Phase 7)** After all processing and equalisation is complete, re-measure both output files with a fresh `loudnorm` analysis pass and compute a **reference profile** by averaging the five key metrics across both files. The profile is written to `FILE.json`.

The two input files are treated as the extremes of an acceptable loudness range (one loud, one quiet). After the pipeline brings them together, their averaged final metrics form a calibrated reference that can be loaded by other scripts or applications to normalise further audio to the same standard.

**Metrics averaged:**
- Integrated Loudness (LUFS)
- True Peak (dBTP)
- Loudness Range (LU)
- Threshold (LUFS)
- Target Offset (LU)

```bash
uv run normalise_videos.py video_1.webm video_2.webm --save-profile out/reference.json
```

**Example output JSON:**
```json
{
  "schema_version": "1.0",
  "created_at": "2026-05-07T14:23:45Z",
  "source_files": ["video_1_normalised.mp3", "video_2_normalised.mp3"],
  "pipeline_options": {
    "target_lufs": -14.0,
    "codec": "flac",
    "perceived": "both",
    "audio_only": true,
    "match_momentary": true
  },
  "reference_profile": {
    "integrated_lufs": -14.05,
    "true_peak_dbtp": -1.23,
    "loudness_range_lu": 9.8,
    "threshold_lufs": -28.4,
    "target_offset_lu": 0.05
  },
  "individual_metrics": {
    "file_1": {
      "integrated_lufs": -14.02,
      "true_peak_dbtp": -1.15,
      "loudness_range_lu": 9.6,
      "threshold_lufs": -28.2,
      "target_offset_lu": 0.02
    },
    "file_2": {
      "integrated_lufs": -14.08,
      "true_peak_dbtp": -1.31,
      "loudness_range_lu": 10.0,
      "threshold_lufs": -28.6,
      "target_offset_lu": 0.08
    }
  },
  "variance": {
    "integrated_lufs_std_dev": 0.042,
    "true_peak_dbtp_std_dev": 0.081,
    "loudness_range_lu_std_dev": 0.2,
    "threshold_lufs_std_dev": 0.2,
    "target_offset_lu_std_dev": 0.03
  }
}
```

The `reference_profile` block contains the values a future application needs. The `individual_metrics` and `variance` blocks provide an audit trail and a confidence signal — a high std dev indicates the two source files were very different, which may affect how reliable the reference is for other audio.

The output file is overwritten if it already exists.

```bash
python normalise_videos.py video_1.webm video_2.webm \
    --audio-only \
    --perceived both \
    --match-momentary \
    --save-profile out/reference.json \
    --output-dir ./exports
```

This will:
1. Extract audio from both files
2. Compress dynamic range (dynaudnorm)
3. Measure and normalise to -14.0 LUFS
4. Export as MP3 320 kbps
5. Validate integrated loudness
6. Equalise integrated levels (Phase 6.5)
7. Report short-term / momentary loudness (Phase 6.7)
8. Equalise momentary peaks (Phase 6.8)
9. Save a JSON reference profile to `out/reference.json` (Phase 7)

---

## Container compatibility

| Source container | `--codec flac` output | `--codec aac` output | `--audio-only` output |
|---|---|---|---|
| `.mp4` | `.mkv` (upgraded) | `.mp4` | `.mp3` |
| `.webm` | `.mkv` (upgraded) | `.webm` | `.mp3` |
| `.mkv` | `.mkv` | `.mkv` | `.mp3` |
| `.mov` | `.mov` | `.mov` | `.mp3` |

---

## Terminal output

If the `rich` package is installed, the script renders coloured tables and styled progress lines. If `rich` is not available it falls back to plain text automatically. `rich` is included in `pyproject.toml` and installed automatically by `uv sync`.
