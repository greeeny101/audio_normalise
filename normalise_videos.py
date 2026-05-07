#!/usr/bin/env python3
"""
normalise_videos.py
-------------------
Extract audio from two video files, measure their loudness using EBU R128
(LUFS), normalise both to the same integrated loudness target using FFmpeg
two-pass loudnorm (linear mode), re-encode the normalised audio as FLAC 24-bit
(or AAC 320 kbps), and remux back into the video container — video stream is
copied with no re-encode.

Usage:
    python normalise_videos.py video1.mp4 video2.mp4 [options]

Options:
    --output-dir DIR        Directory for output files  (default: ./out)
    --codec {flac,aac}      Output audio codec          (default: flac)
    --target LUFS           Integrated loudness target  (default: -14.0)
    --keep-wav              Keep lossless WAV intermediates in --output-dir

Notes:
    • MP4 containers do not support FLAC; affected outputs are automatically
      re-containerised to MKV (video stream still copied, no re-encode).
    • Phase 6 validates the output files by re-measuring their integrated
      loudness and confirming it is within ±1 LU of the target.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional rich terminal output — falls back to plain print gracefully
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.table import Table

    _console = Console()
    RICH = True
except ImportError:
    _console = None
    RICH = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TARGET_LUFS = -14.0  # EBU R128 / streaming standard (Spotify, YouTube)
DEFAULT_TRUE_PEAK = -1.0  # dBTP ceiling — headroom before clipping
DEFAULT_LRA = 11.0  # Loudness Range in LU
SAMPLE_RATE = 48000  # Hz — broadcast standard
WAV_CODEC = "pcm_s24le"  # lossless 24-bit WAV for intermediates
DYNAUDNORM_FRAME_MS = 500  # dynaudnorm frame size in ms
DYNAUDNORM_MAX_GAIN = 15.0  # dynaudnorm maximum gain (dB)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def check_ffmpeg() -> None:
    """Verify that both ffmpeg and ffprobe are available on PATH."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"ERROR: '{tool}' not found on PATH. Please install FFmpeg.")


def log(plain: str, markup: str | None = None) -> None:
    """
    Print a message.  If rich is available, render *markup*; otherwise
    fall back to printing *plain* via the standard print() function.
    """
    if RICH and _console is not None:
        _console.print(markup if markup is not None else plain)
    else:
        print(plain)


def print_comparison_table(rows: list[dict], title: str) -> None:
    """Render a two-column comparison table (Video 1 vs Video 2)."""
    if RICH and _console is not None:
        table = Table(title=title, show_header=True, header_style="bold cyan")
        table.add_column("Property", style="bold")
        table.add_column("Video 1", justify="right")
        table.add_column("Video 2", justify="right")
        for row in rows:
            table.add_row(row["prop"], row["v1"], row["v2"])
        _console.print(table)
    else:
        print(f"\n=== {title} ===")
        print(f"{'Property':<32} {'Video 1':>12} {'Video 2':>12}")
        print("-" * 58)
        for row in rows:
            print(f"{row['prop']:<32} {row['v1']:>12} {row['v2']:>12}")
        print()


def run_cmd(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    """
    Execute *cmd* as a subprocess.  Exits with a descriptive message on
    non-zero return code.  Returns the CompletedProcess on success.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            f"\nERROR: {label} failed (exit code {result.returncode}).\n"
            f"Command : {' '.join(cmd)}\n"
            f"FFmpeg  : {result.stderr[-2000:]}"
        )
    return result


# ---------------------------------------------------------------------------
# Phase 2 — Audio extraction
# ---------------------------------------------------------------------------


def extract_audio(video_path: Path, out_wav: Path) -> None:
    """
    Extract the first audio stream from *video_path* to a lossless 24-bit
    WAV at 48 kHz stereo.  The video stream is discarded (-vn).
    """
    log(
        f"  Extracting : {video_path.name} -> {out_wav.name}",
        f"  [dim]Extracting :[/dim] {video_path.name} [dim]->[/dim] {out_wav.name}",
    )
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(video_path),
            "-vn",  # drop video stream
            "-acodec",
            WAV_CODEC,  # pcm_s24le  (lossless 24-bit)
            "-ar",
            str(SAMPLE_RATE),  # 48 000 Hz
            "-ac",
            "2",  # stereo (upmix mono if needed)
            str(out_wav),
        ],
        label=f"extract audio from {video_path.name}",
    )


# ---------------------------------------------------------------------------
# Phase 2.5 — Dynamic range compression  (optional dynaudnorm pre-pass)
# ---------------------------------------------------------------------------


def apply_dynaudnorm(wav_in: Path, wav_out: Path) -> None:
    """
    Compress the dynamic range of *wav_in* with FFmpeg dynaudnorm and write
    a lossless 24-bit WAV to *wav_out*.  This brings quiet passages closer
    to the loud ones *before* loudnorm runs, making the integrated LUFS a
    better proxy for perceived loudness across both files.
    """
    log(
        f"  DynAudNorm : {wav_in.name} -> {wav_out.name}",
        f"  [dim]DynAudNorm :[/dim] {wav_in.name} [dim]->[/dim] {wav_out.name}",
    )
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(wav_in),
            "-af",
            f"dynaudnorm=f={DYNAUDNORM_FRAME_MS}:g={DYNAUDNORM_MAX_GAIN}",
            "-acodec",
            WAV_CODEC,
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            str(wav_out),
        ],
        label=f"dynaudnorm {wav_in.name}",
    )


# ---------------------------------------------------------------------------
# Phase 3 — Loudness analysis  (FFmpeg loudnorm Pass 1)
# ---------------------------------------------------------------------------


def analyse_loudness(wav_path: Path, target_i: float) -> dict:
    """
    Run FFmpeg loudnorm in analysis mode (null output) and parse the JSON
    that loudnorm writes to stderr.

    Returns a dict with keys:
        input_i, input_tp, input_lra, input_thresh, target_offset
    """
    log(
        f"  Analysing  : {wav_path.name}",
        f"  [dim]Analysing  :[/dim] {wav_path.name}",
    )
    lnorm_filter = (
        f"loudnorm="
        f"I={target_i}:"
        f"TP={DEFAULT_TRUE_PEAK}:"
        f"LRA={DEFAULT_LRA}:"
        f"print_format=json"
    )
    # loudnorm writes its JSON summary to stderr even on success;
    # we therefore do NOT use run_cmd() here (stderr is expected output).
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(wav_path),
            "-af",
            lnorm_filter,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    # Extract the JSON block from stderr
    match = re.search(r"\{.*?\}", result.stderr, re.DOTALL)
    if not match:
        sys.exit(
            f"ERROR: Could not parse loudnorm JSON for {wav_path.name}.\n"
            f"FFmpeg stderr:\n{result.stderr[-2000:]}"
        )
    return json.loads(match.group())


# ---------------------------------------------------------------------------
# Phase 4 — Normalisation  (FFmpeg loudnorm Pass 2 — linear mode)
# ---------------------------------------------------------------------------


def normalise_audio(
    wav_in: Path,
    measured: dict,
    wav_out: Path,
    target_i: float,
) -> None:
    """
    Apply loudnorm in linear mode, feeding it the measured values from
    Pass 1.  Linear mode applies a precise gain offset rather than the
    less accurate dynamic mode.  Output is a lossless 24-bit WAV.
    """
    log(
        f"  Normalising: {wav_in.name} -> {wav_out.name}",
        f"  [dim]Normalising:[/dim] {wav_in.name} [dim]->[/dim] {wav_out.name}",
    )
    lnorm_filter = (
        f"loudnorm="
        f"I={target_i}:"
        f"TP={DEFAULT_TRUE_PEAK}:"
        f"LRA={DEFAULT_LRA}:"
        f"measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured.get('target_offset', '0.0')}:"
        f"linear=true:"
        f"print_format=summary"
    )
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(wav_in),
            "-af",
            lnorm_filter,
            "-acodec",
            WAV_CODEC,
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            str(wav_out),
        ],
        label=f"normalise {wav_in.name}",
    )


# ---------------------------------------------------------------------------
# Phase 5 — Re-encode audio and remux into video container
# ---------------------------------------------------------------------------


def output_extension(source: Path, codec: str) -> str:
    """
    Determine the correct output file extension.

    MP4 does not support FLAC audio; when codec == 'flac' and the source
    is an MP4, the container is upgraded to MKV.  The video stream is still
    copied — no re-encode is performed.
    """
    src_ext = source.suffix.lower()
    if codec == "flac" and src_ext in (".mp4", ".webm"):
        log(
            f"  Note: {src_ext.upper()[1:]} does not support FLAC. "
            f"Upgrading container to MKV for: {source.name}",
            f"  [yellow]Note:[/yellow] {src_ext.upper()[1:]} does not support FLAC. "
            f"Upgrading container to MKV for: [bold]{source.name}[/bold]",
        )
        return ".mkv"
    return src_ext


def remux(
    video_path: Path,
    norm_wav: Path,
    out_path: Path,
    codec: str,
) -> None:
    """
    Build the output file by:
      • mapping the video stream from the original source  (-map 0:v:0)
      • mapping the normalised audio from the WAV          (-map 1:a:0)
      • copying the video stream unchanged                 (-c:v copy)
      • re-encoding audio to FLAC 24-bit or AAC 320 kbps

    FLAC flags : -c:a flac -sample_fmt s32  (24-bit lossless)
    AAC  flags : -c:a aac  -b:a 320k        (near-transparent lossy)
    """
    log(
        f"  Remuxing   : {video_path.name} -> {out_path.name}",
        f"  [dim]Remuxing   :[/dim] {video_path.name} [dim]->[/dim] {out_path.name}",
    )
    if codec == "flac":
        audio_flags = ["-c:a", "flac", "-sample_fmt", "s32"]
    else:
        audio_flags = ["-c:a", "aac", "-b:a", "320k"]

    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(video_path),  # input 0 — original video  (video stream)
            "-i",
            str(norm_wav),  # input 1 — normalised audio (audio stream)
            "-map",
            "0:v:0",  # take video track from input 0
            "-map",
            "1:a:0",  # take audio track from input 1
            "-c:v",
            "copy",  # copy video — zero quality loss, fast
            *audio_flags,
            str(out_path),
        ],
        label=f"remux {out_path.name}",
    )


# ---------------------------------------------------------------------------
# Phase 6.7 — Short-term loudness analysis  (optional)
# ---------------------------------------------------------------------------


def analyse_shortterm(wav_path: Path) -> dict:
    """
    Run FFmpeg ebur128 on *wav_path* and return the maximum momentary (M,
    400 ms window) and maximum short-term (S, 3 s window) loudness values.
    These capture transient loudness peaks that integrated LUFS can mask,
    and are a better indicator of perceived loudness differences.

    Returns a dict with keys: max_momentary, max_shortterm  (LUFS floats).
    """
    log(
        f"  Short-term : {wav_path.name}",
        f"  [dim]Short-term :[/dim] {wav_path.name}",
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(wav_path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    # Frame-level lines look like:  M: -23.4     S: -25.1
    momentary = [float(v) for v in re.findall(r"\bM:\s*([-\d.]+)", result.stderr)]
    shortterm = [float(v) for v in re.findall(r"\bS:\s*([-\d.]+)", result.stderr)]
    return {
        "max_momentary": max(momentary) if momentary else float("nan"),
        "max_shortterm": max(shortterm) if shortterm else float("nan"),
    }


def apply_volume_gain(wav_in: Path, gain_db: float, wav_out: Path) -> None:
    """
    Apply a static *gain_db* offset to *wav_in* and write the result to
    *wav_out* as a lossless 24-bit WAV.  Positive values boost; negative
    values attenuate.  Used to equalise max momentary loudness between files.
    """
    log(
        f"  Volume adj : {wav_in.name}  {gain_db:+.2f} dB -> {wav_out.name}",
        f"  [dim]Volume adj :[/dim] {wav_in.name}  [bold]{gain_db:+.2f} dB[/bold] [dim]->[/dim] {wav_out.name}",
    )
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(wav_in),
            "-af",
            f"volume={gain_db:.4f}dB",
            "-acodec",
            WAV_CODEC,
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            str(wav_out),
        ],
        label=f"volume gain {wav_in.name}",
    )


def encode_mp3(wav_in: Path, out_path: Path) -> None:
    """
    Encode *wav_in* to a 320 kbps MP3 at 48 kHz using the libmp3lame encoder.
    This is the highest practical quality for MP3 and avoids any video remux.
    """
    log(
        f"  MP3 encode : {wav_in.name} -> {out_path.name}",
        f"  [dim]MP3 encode :[/dim] {wav_in.name} [dim]->[/dim] {out_path.name}",
    )
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(wav_in),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-id3v2_version",
            "3",
            str(out_path),
        ],
        label=f"mp3 encode {wav_in.name}",
    )


# ---------------------------------------------------------------------------
# Phase 7 — Reference profile
# ---------------------------------------------------------------------------


def build_reference_profile(
    metrics: list[dict],
    output_files: list[Path],
    args: object,
) -> dict:
    """
    Average the five key loudnorm metrics across *metrics* (one dict per
    output file, as returned by analyse_loudness) and return a structured
    reference profile dict ready for JSON serialisation.

    Metric keys expected in each dict (FFmpeg loudnorm names):
        input_i, input_tp, input_lra, input_thresh, target_offset

    NaN-safe: if a value cannot be parsed, the averaged field is set to
    None and a 'warnings' list entry is added to the profile.
    """
    import math
    from datetime import datetime, timezone

    FIELD_MAP = [
        ("input_i", "integrated_lufs"),
        ("input_tp", "true_peak_dbtp"),
        ("input_lra", "loudness_range_lu"),
        ("input_thresh", "threshold_lufs"),
        ("target_offset", "target_offset_lu"),
    ]

    warnings: list[str] = []
    individual: list[dict] = []
    avgs: dict[str, float | None] = {}
    std_devs: dict[str, float | None] = {}

    for i, m in enumerate(metrics):
        entry: dict[str, float | None] = {}
        for src_key, dst_key in FIELD_MAP:
            try:
                entry[dst_key] = float(m[src_key])
            except (KeyError, TypeError, ValueError):
                entry[dst_key] = None
                warnings.append(
                    f"file_{i + 1}: could not parse '{src_key}' — set to null"
                )
        individual.append(entry)

    for _, dst_key in FIELD_MAP:
        vals = [ind[dst_key] for ind in individual if ind[dst_key] is not None]
        if not vals:
            avgs[dst_key] = None
            std_devs[f"{dst_key}_std_dev"] = None
        else:
            mean = sum(vals) / len(vals)
            avgs[dst_key] = round(mean, 4)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            std_devs[f"{dst_key}_std_dev"] = round(math.sqrt(variance), 4)

    pipeline_options = {
        "target_lufs": getattr(args, "target", None),
        "codec": getattr(args, "codec", None),
        "perceived": getattr(args, "perceived", None),
        "audio_only": getattr(args, "audio_only", False),
        "match_momentary": getattr(args, "match_momentary", False),
    }

    profile: dict = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_files": [f.name for f in output_files],
        "pipeline_options": pipeline_options,
        "reference_profile": avgs,
        "individual_metrics": {
            f"file_{i + 1}": ind for i, ind in enumerate(individual)
        },
        "variance": std_devs,
    }
    if warnings:
        profile["warnings"] = warnings
    return profile


def save_reference_profile(profile: dict, path: Path) -> None:
    """Write *profile* as formatted JSON to *path*, overwriting if it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    log(
        f"  Profile saved: {path}",
        f"  [green]✓[/green] Profile saved: {path}",
    )


# ---------------------------------------------------------------------------
# Phase 6.5 — Level equalisation helper
# ---------------------------------------------------------------------------


def _write_output(
    video_path: Path,
    norm_wav: Path,
    out_path: Path,
    codec: str,
    audio_only: bool,
) -> None:
    """Dispatch to encode_mp3 or remux depending on *audio_only*."""
    if audio_only:
        encode_mp3(norm_wav, out_path)
    else:
        remux(video_path, norm_wav, out_path, codec)


# ---------------------------------------------------------------------------
# Phase 6 — Validation
# ---------------------------------------------------------------------------


def validate_output(
    output_files: list[Path],
    target_i: float,
    tolerance_lu: float = 1.0,
) -> tuple[bool, list[float]]:
    """
    Re-measure the integrated loudness of each output file and confirm it
    is within *tolerance_lu* LU of *target_i*.

    Returns (all_pass, measured_lufs) where measured_lufs contains the
    integrated loudness (LUFS) for each file (nan if measurement failed).
    """
    log(
        "\n=== Phase 6: Validation ===",
        "\n[bold cyan]=== Phase 6: Validation ===[/bold cyan]",
    )

    lnorm_filter = (
        f"loudnorm="
        f"I={target_i}:"
        f"TP={DEFAULT_TRUE_PEAK}:"
        f"LRA={DEFAULT_LRA}:"
        f"print_format=json"
    )

    all_pass = True
    measured_lufs: list[float] = []

    measured_out: list[dict] = []
    for i, out_path in enumerate(output_files):
        log(
            f"  Measuring  : {out_path.name}",
            f"  [dim]Measuring  :[/dim] {out_path.name}",
        )
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(out_path),
                "-vn",
                "-af",
                lnorm_filter,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        match = re.search(r"\{.*?\}", result.stderr, re.DOTALL)
        if not match:
            log(
                f"  WARNING: Could not parse loudnorm JSON for {out_path.name}.",
                f"  [bold red]WARNING:[/bold red] Could not parse loudnorm JSON for {out_path.name}.",
            )
            measured_out.append({})
            measured_lufs.append(float("nan"))
            all_pass = False
            continue
        stats = json.loads(match.group())
        measured_out.append(stats)

    for i, (out_path, stats) in enumerate(zip(output_files, measured_out)):
        if not stats:
            continue
        try:
            actual_i = float(stats["input_i"])
        except (KeyError, ValueError):
            actual_i = float("nan")

        measured_lufs.append(actual_i)
        delta = actual_i - target_i
        passed = abs(delta) <= tolerance_lu
        if not passed:
            all_pass = False
        status = "PASS" if passed else "FAIL"

        log(
            f"  Video {i + 1}: measured {actual_i:.2f} LUFS  "
            f"(target {target_i:.2f}, delta {delta:+.2f} LU)  [{status}]",
            f"  Video {i + 1}: measured [bold]{actual_i:.2f}[/bold] LUFS  "
            f"(target {target_i:.2f}, delta {delta:+.2f} LU)  "
            + (
                "[bold green][PASS][/bold green]"
                if passed
                else "[bold red][FAIL][/bold red]"
            ),
        )

    print_comparison_table(
        rows=[
            {
                "prop": "Integrated Loudness (LUFS)",
                "v1": f"{float(measured_out[0].get('input_i', 'nan')):.2f}"
                if measured_out[0]
                else "n/a",
                "v2": f"{float(measured_out[1].get('input_i', 'nan')):.2f}"
                if len(measured_out) > 1 and measured_out[1]
                else "n/a",
            },
            {
                "prop": "True Peak (dBTP)",
                "v1": f"{float(measured_out[0].get('input_tp', 'nan')):.2f}"
                if measured_out[0]
                else "n/a",
                "v2": f"{float(measured_out[1].get('input_tp', 'nan')):.2f}"
                if len(measured_out) > 1 and measured_out[1]
                else "n/a",
            },
            {
                "prop": "Loudness Range (LU)",
                "v1": f"{float(measured_out[0].get('input_lra', 'nan')):.2f}"
                if measured_out[0]
                else "n/a",
                "v2": f"{float(measured_out[1].get('input_lra', 'nan')):.2f}"
                if len(measured_out) > 1 and measured_out[1]
                else "n/a",
            },
        ],
        title=f"Measured Loudness — After Normalisation (target: {target_i:.1f} LUFS)",
    )

    return all_pass, measured_lufs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Normalise audio levels in two video files to a target LUFS "
            "and remux with re-encoded FLAC 24-bit (or AAC 320 kbps) audio."
        )
    )
    parser.add_argument("video1", type=Path, help="First video file")
    parser.add_argument("video2", type=Path, help="Second video file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out"),
        help="Output directory (default: ./out)",
    )
    parser.add_argument(
        "--codec",
        choices=["flac", "aac"],
        default="flac",
        help="Output audio codec — flac (default, lossless) or aac (320 kbps)",
    )
    parser.add_argument(
        "--target",
        type=float,
        default=DEFAULT_TARGET_LUFS,
        metavar="LUFS",
        help=f"Integrated loudness target in LUFS (default: {DEFAULT_TARGET_LUFS})",
    )
    parser.add_argument(
        "--keep-wav",
        action="store_true",
        help="Copy lossless WAV intermediates into --output-dir before cleanup",
    )
    parser.add_argument(
        "--perceived",
        choices=["dynaudnorm", "shortterm", "both"],
        default=None,
        metavar="MODE",
        help=(
            "Extra perceived-loudness processing. "
            "dynaudnorm: compress dynamic range before loudnorm (reduces LRA, "
            "raises quiet passages); "
            "shortterm: report max momentary & short-term LUFS on the outputs; "
            "both: apply dynaudnorm and report short-term stats."
        ),
    )
    parser.add_argument(
        "--audio-only",
        action="store_true",
        help=(
            "Output normalised audio as MP3 320 kbps / 48 kHz instead of "
            "remuxing back into the video container. The video stream is "
            "discarded and the remux step is skipped entirely."
        ),
    )
    parser.add_argument(
        "--match-momentary",
        action="store_true",
        help=(
            "Measure the max momentary loudness (400 ms window) of each "
            "output and apply a volume gain to the quieter file so both "
            "files match in perceived peak loudness (Phase 6.8)."
        ),
    )
    parser.add_argument(
        "--save-profile",
        type=Path,
        default=None,
        metavar="FILE.json",
        help=(
            "After all processing is complete, re-measure both output files, "
            "average their loudnorm metrics, and save a JSON reference profile "
            "to FILE.json (Phase 7). The profile can be loaded by other scripts "
            "as a calibrated loudness reference."
        ),
    )
    args = parser.parse_args()

    # ── Pre-flight checks ────────────────────────────────────────────────────
    check_ffmpeg()
    for vpath in (args.video1, args.video2):
        if not vpath.exists():
            sys.exit(f"ERROR: File not found: {vpath}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Work inside a private temp directory; always cleaned up on exit
    tmpdir = Path(tempfile.mkdtemp(prefix="norm_audio_"))

    try:
        videos = [args.video1, args.video2]
        wav_raw = [tmpdir / f"raw_{i + 1}.wav" for i in range(2)]
        wav_norm = [tmpdir / f"norm_{i + 1}.wav" for i in range(2)]

        use_dynaudnorm = args.perceived in ("dynaudnorm", "both")
        use_shortterm = args.perceived in ("shortterm", "both")
        wav_input = wav_raw  # files fed into loudnorm; replaced by wav_dyn below
        wav_dyn: list[Path] = []  # populated only if use_dynaudnorm

        # ── Phase 2 : Extract audio ─────────────────────────────────────────────
        log(
            "\n=== Phase 2: Audio Extraction ===",
            "\n[bold cyan]=== Phase 2: Audio Extraction ===[/bold cyan]",
        )
        for i in range(2):
            extract_audio(videos[i], wav_raw[i])

        # ── Phase 2.5 : Dynamic range compression (dynaudnorm) ───────────────
        if use_dynaudnorm:
            log(
                "\n=== Phase 2.5: Dynamic Range Compression (dynaudnorm) ===",
                "\n[bold cyan]=== Phase 2.5: Dynamic Range Compression (dynaudnorm) ===[/bold cyan]",
            )
            wav_dyn = [tmpdir / f"dyn_{i + 1}.wav" for i in range(2)]
            for i in range(2):
                apply_dynaudnorm(wav_raw[i], wav_dyn[i])
            wav_input = wav_dyn

        # ── Phase 3 : Loudness analysis (Pass 1) ─────────────────────────────
        log(
            "\n=== Phase 3: Loudness Analysis (Pass 1) ===",
            "\n[bold cyan]=== Phase 3: Loudness Analysis (Pass 1) ===[/bold cyan]",
        )
        measured = [analyse_loudness(wav_input[i], args.target) for i in range(2)]

        print_comparison_table(
            rows=[
                {
                    "prop": "Integrated Loudness (LUFS)",
                    "v1": measured[0].get("input_i", "n/a"),
                    "v2": measured[1].get("input_i", "n/a"),
                },
                {
                    "prop": "True Peak (dBTP)",
                    "v1": measured[0].get("input_tp", "n/a"),
                    "v2": measured[1].get("input_tp", "n/a"),
                },
                {
                    "prop": "Loudness Range (LU)",
                    "v1": measured[0].get("input_lra", "n/a"),
                    "v2": measured[1].get("input_lra", "n/a"),
                },
                {
                    "prop": "Threshold (LUFS)",
                    "v1": measured[0].get("input_thresh", "n/a"),
                    "v2": measured[1].get("input_thresh", "n/a"),
                },
                {
                    "prop": "Target Offset (LU)",
                    "v1": measured[0].get("target_offset", "n/a"),
                    "v2": measured[1].get("target_offset", "n/a"),
                },
            ],
            title="Measured Loudness — Before Normalisation",
        )

        # ── Phase 4 : Normalisation (Pass 2 — linear mode) ──────────────────
        log(
            "\n=== Phase 4: Normalisation (Pass 2 — Linear Mode) ===",
            "\n[bold cyan]=== Phase 4: Normalisation (Pass 2 — Linear Mode) ===[/bold cyan]",
        )
        for i in range(2):
            normalise_audio(wav_input[i], measured[i], wav_norm[i], args.target)

        # ── Phase 5 : Re-encode audio and remux ──────────────────────────────
        if args.audio_only:
            log(
                "\n=== Phase 5: MP3 Audio Export ===",
                "\n[bold cyan]=== Phase 5: MP3 Audio Export ===[/bold cyan]",
            )
        else:
            log(
                "\n=== Phase 5: Re-encode & Remux ===",
                "\n[bold cyan]=== Phase 5: Re-encode & Remux ===[/bold cyan]",
            )
        output_files: list[Path] = []
        for i, vpath in enumerate(videos):
            if args.audio_only:
                out_path = args.output_dir / f"{vpath.stem}_normalised.mp3"
            else:
                ext = output_extension(vpath, args.codec)
                out_path = args.output_dir / f"{vpath.stem}_normalised{ext}"
            _write_output(vpath, wav_norm[i], out_path, args.codec, args.audio_only)
            output_files.append(out_path)
            log(
                f"  Written: {out_path}",
                f"  [green]✓[/green] Written: {out_path}",
            )

        # ── Optionally preserve WAV intermediates ────────────────────────────
        if args.keep_wav:
            wavs_to_keep = [*wav_raw, *wav_norm]
            if use_dynaudnorm:
                wavs_to_keep.extend(wav_dyn)
            for wav in wavs_to_keep:
                dest = args.output_dir / wav.name
                shutil.copy2(wav, dest)
                log(f"  WAV saved: {dest}")

        # ── Phase 6 : Validation ─────────────────────────────────────────────
        passed, measured_lufs = validate_output(output_files, args.target)

        # ── Phase 6.5 : Equalise levels between the two outputs ──────────────
        # If the two files differ by more than 0.2 LU, boost the quieter one
        # up to match the louder one so both sound equally loud.
        EQUALIZE_TOLERANCE = 0.2  # LU
        if (
            len(measured_lufs) == 2
            and not any(lufs != lufs for lufs in measured_lufs)  # no NaN
            and abs(measured_lufs[0] - measured_lufs[1]) > EQUALIZE_TOLERANCE
        ):
            quieter_idx = 0 if measured_lufs[0] < measured_lufs[1] else 1
            louder_lufs = max(measured_lufs)
            log(
                f"\n=== Phase 6.5: Level Equalisation ==="
                f"\n  Video {quieter_idx + 1} is {abs(measured_lufs[0] - measured_lufs[1]):.2f} LU quieter."
                f"\n  Boosting Video {quieter_idx + 1} to {louder_lufs:.2f} LUFS to match.",
                f"\n[bold cyan]=== Phase 6.5: Level Equalisation ===[/bold cyan]"
                f"\n  Video {quieter_idx + 1} is [bold]{abs(measured_lufs[0] - measured_lufs[1]):.2f} LU[/bold] quieter."
                f"\n  Boosting Video {quieter_idx + 1} to [bold]{louder_lufs:.2f} LUFS[/bold] to match.",
            )
            eq_measured = analyse_loudness(wav_input[quieter_idx], louder_lufs)
            normalise_audio(
                wav_input[quieter_idx], eq_measured, wav_norm[quieter_idx], louder_lufs
            )
            _write_output(
                videos[quieter_idx],
                wav_norm[quieter_idx],
                output_files[quieter_idx],
                args.codec,
                args.audio_only,
            )
            log(
                f"  Re-written: {output_files[quieter_idx]}",
                f"  [green]✓[/green] Re-written: {output_files[quieter_idx]}",
            )
            # Re-validate after equalisation
            passed, measured_lufs = validate_output(output_files, louder_lufs)

        # ── Phase 6.7 : Short-term loudness analysis ─────────────────────────
        st: list[dict] = []
        if use_shortterm or args.match_momentary:
            if use_shortterm:
                log(
                    "\n=== Phase 6.7: Short-term Loudness Analysis ===",
                    "\n[bold cyan]=== Phase 6.7: Short-term Loudness Analysis ===[/bold cyan]",
                )
            st = [analyse_shortterm(out) for out in output_files]
            if use_shortterm:
                print_comparison_table(
                    rows=[
                        {
                            "prop": "Max Momentary (LUFS)",
                            "v1": f"{st[0]['max_momentary']:.2f}",
                            "v2": f"{st[1]['max_momentary']:.2f}",
                        },
                        {
                            "prop": "Max Short-term (LUFS)",
                            "v1": f"{st[0]['max_shortterm']:.2f}",
                            "v2": f"{st[1]['max_shortterm']:.2f}",
                        },
                    ],
                    title="Short-term Loudness — Output Files",
                )

        # ── Phase 6.8 : Momentary loudness equalisation ───────────────────────
        MOMENTARY_TOLERANCE = 0.5  # LU
        if args.match_momentary:
            log(
                "\n=== Phase 6.8: Momentary Loudness Equalisation ===",
                "\n[bold cyan]=== Phase 6.8: Momentary Loudness Equalisation ===[/bold cyan]",
            )
            momentary_lufs = [s["max_momentary"] for s in st]
            delta_m = momentary_lufs[0] - momentary_lufs[1]
            log(
                f"  Max momentary: Video 1 = {momentary_lufs[0]:.2f} LUFS, "
                f"Video 2 = {momentary_lufs[1]:.2f} LUFS  (delta {delta_m:+.2f} LU)",
                f"  Max momentary: Video 1 = [bold]{momentary_lufs[0]:.2f}[/bold] LUFS, "
                f"Video 2 = [bold]{momentary_lufs[1]:.2f}[/bold] LUFS  "
                f"(delta [bold]{delta_m:+.2f} LU[/bold])",
            )
            if abs(delta_m) <= MOMENTARY_TOLERANCE:
                log(
                    f"  Within tolerance ({MOMENTARY_TOLERANCE} LU) — no adjustment needed.",
                    f"  [green]Within tolerance ({MOMENTARY_TOLERANCE} LU) — no adjustment needed.[/green]",
                )
            else:
                quieter_m_idx = 0 if momentary_lufs[0] < momentary_lufs[1] else 1
                gain_db = abs(delta_m)
                wav_adjusted = tmpdir / f"adj_{quieter_m_idx + 1}.wav"
                apply_volume_gain(wav_norm[quieter_m_idx], gain_db, wav_adjusted)
                _write_output(
                    videos[quieter_m_idx],
                    wav_adjusted,
                    output_files[quieter_m_idx],
                    args.codec,
                    args.audio_only,
                )
                log(
                    f"  Re-written: {output_files[quieter_m_idx]}",
                    f"  [green]✓[/green] Re-written: {output_files[quieter_m_idx]}",
                )
                st_final = [analyse_shortterm(out) for out in output_files]
                print_comparison_table(
                    rows=[
                        {
                            "prop": "Max Momentary (LUFS)",
                            "v1": f"{st_final[0]['max_momentary']:.2f}",
                            "v2": f"{st_final[1]['max_momentary']:.2f}",
                        },
                        {
                            "prop": "Max Short-term (LUFS)",
                            "v1": f"{st_final[0]['max_shortterm']:.2f}",
                            "v2": f"{st_final[1]['max_shortterm']:.2f}",
                        },
                    ],
                    title="Short-term Loudness — After Momentary Equalisation",
                )

        # ── Phase 7 : Reference profile ────────────────────────────────────────────
        if args.save_profile:
            log(
                "\n=== Phase 7: Reference Profile ===",
                "\n[bold cyan]=== Phase 7: Reference Profile ===[/bold cyan]",
            )
            # Re-measure final outputs so profile reflects all adjustments
            final_metrics = [analyse_loudness(out, args.target) for out in output_files]
            profile = build_reference_profile(final_metrics, output_files, args)
            save_reference_profile(profile, args.save_profile)

            # Display the averaged reference values
            ref = profile["reference_profile"]

            def _fmt(v: object) -> str:
                return f"{v:.4f}" if isinstance(v, float) else "n/a"

            print_comparison_table(
                rows=[
                    {
                        "prop": "Integrated Loudness (LUFS)",
                        "v1": _fmt(
                            profile["individual_metrics"]["file_1"]["integrated_lufs"]
                        ),
                        "v2": _fmt(
                            profile["individual_metrics"]["file_2"]["integrated_lufs"]
                        ),
                    },
                    {
                        "prop": "True Peak (dBTP)",
                        "v1": _fmt(
                            profile["individual_metrics"]["file_1"]["true_peak_dbtp"]
                        ),
                        "v2": _fmt(
                            profile["individual_metrics"]["file_2"]["true_peak_dbtp"]
                        ),
                    },
                    {
                        "prop": "Loudness Range (LU)",
                        "v1": _fmt(
                            profile["individual_metrics"]["file_1"]["loudness_range_lu"]
                        ),
                        "v2": _fmt(
                            profile["individual_metrics"]["file_2"]["loudness_range_lu"]
                        ),
                    },
                    {
                        "prop": "Threshold (LUFS)",
                        "v1": _fmt(
                            profile["individual_metrics"]["file_1"]["threshold_lufs"]
                        ),
                        "v2": _fmt(
                            profile["individual_metrics"]["file_2"]["threshold_lufs"]
                        ),
                    },
                    {
                        "prop": "Target Offset (LU)",
                        "v1": _fmt(
                            profile["individual_metrics"]["file_1"]["target_offset_lu"]
                        ),
                        "v2": _fmt(
                            profile["individual_metrics"]["file_2"]["target_offset_lu"]
                        ),
                    },
                ],
                title="Individual Metrics — Final Outputs",
            )
            log(
                f"\n  Reference averages:"
                f"\n    Integrated Loudness : {_fmt(ref['integrated_lufs'])} LUFS"
                f"\n    True Peak           : {_fmt(ref['true_peak_dbtp'])} dBTP"
                f"\n    Loudness Range      : {_fmt(ref['loudness_range_lu'])} LU"
                f"\n    Threshold           : {_fmt(ref['threshold_lufs'])} LUFS"
                f"\n    Target Offset       : {_fmt(ref['target_offset_lu'])} LU",
                f"\n  [bold]Reference averages:[/bold]"
                f"\n    Integrated Loudness : [cyan]{_fmt(ref['integrated_lufs'])}[/cyan] LUFS"
                f"\n    True Peak           : [cyan]{_fmt(ref['true_peak_dbtp'])}[/cyan] dBTP"
                f"\n    Loudness Range      : [cyan]{_fmt(ref['loudness_range_lu'])}[/cyan] LU"
                f"\n    Threshold           : [cyan]{_fmt(ref['threshold_lufs'])}[/cyan] LUFS"
                f"\n    Target Offset       : [cyan]{_fmt(ref['target_offset_lu'])}[/cyan] LU",
            )
            if "warnings" in profile:
                for w in profile["warnings"]:
                    log(f"  WARNING: {w}", f"  [bold red]WARNING:[/bold red] {w}")

        # ── Summary ──────────────────────────────────────────────────────────
        if passed:
            log(
                "\nAll phases complete. Both output files passed loudness validation.",
                "\n[bold green]All phases complete.[/bold green] "
                "Both output files passed loudness validation.",
            )
        else:
            log(
                "\nAll phases complete. WARNING: one or more output files failed "
                "loudness validation — check delta above.",
                "\n[bold yellow]All phases complete.[/bold yellow] "
                "[bold red]WARNING:[/bold red] one or more output files failed "
                "loudness validation — check delta above.",
            )
        log("\nOutput files:")
        for f in output_files:
            log(f"  {f}")

    finally:
        # Always remove the temp directory, even if an error occurred
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
