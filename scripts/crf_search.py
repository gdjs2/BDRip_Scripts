"""Fit B-frame QP and video bitrate from CRF 13/20 encodes of one centered minute."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

if __package__:
    from .crf_config import load_config, validate_config
    from .crf_crop import detect_crop
    from .crf_encode import check_encoder, inspect_video, validate_source_settings
    from .crf_plot import write_plot
    from .crf_progress import Progress, read_json, write_json
    from .crf_model import CRF_VALUES, METHOD, SAMPLE_SECONDS, fit_models, predict, select_sample
else:
    from crf_config import load_config, validate_config
    from crf_crop import detect_crop
    from crf_encode import check_encoder, inspect_video, validate_source_settings
    from crf_plot import write_plot
    from crf_progress import Progress, read_json, write_json
    from crf_model import CRF_VALUES, METHOD, SAMPLE_SECONDS, fit_models, predict, select_sample


CONSOLE = Console(stderr=True)


def resolve_crop(source: Path, info: dict, config: dict, samples: list[dict],
                 progress: Progress | None = None) -> dict:
    video = config["video"]
    crop = video["crop"]
    if crop == "auto":
        return detect_crop(source, video["stream"], samples, info, video["cropdetect"],
                           check_cancelled=progress.check_cancelled if progress else None)
    if crop is None:
        return {"mode": "disabled", "crop": None, "width": info["width"],
                "height": info["height"], "reason": "Automatic cropping disabled"}
    width, height, x, y = map(int, crop.split(":"))
    if (min(width, height) <= 0 or min(x, y) < 0
            or x + width > info["width"] or y + height > info["height"]
            or width % 2 or height % 2):
        raise ValueError("Manual crop must fit the source and use even width and height; offsets may be odd")
    return {"mode": "manual", "crop": crop, "width": width, "height": height,
            "reason": "Explicit crop supplied"}


def sample_metrics(result: dict) -> dict:
    """Use B-frame QP only; bitrate still includes every encoded video frame."""
    try:
        frames, duration, video_bytes = result["frames"], result["duration"], result["video_bytes"]
        counts, qps = result["frame_counts"], result["qp"]
        valid = (
            isinstance(frames, int) and not isinstance(frames, bool) and frames > 0
            and math.isfinite(duration) and duration > 0
            and isinstance(video_bytes, int) and not isinstance(video_bytes, bool) and video_bytes > 0
            and bool(counts) and set(counts) == set(qps) and set(counts) <= {"I", "P", "B"}
            and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in counts.values())
            and sum(counts.values()) == frames
            and all(math.isfinite(qp) for qp in qps.values())
        )
        if valid:
            return {"average_qp": qps.get("B"), "b_frames": counts.get("B", 0),
                    "average_bitrate_mbps": video_bytes * 8 / duration / 1_000_000}
    except (KeyError, TypeError, ValueError):
        pass
    raise ValueError("Incomplete video or I/P/B frame statistics; cannot calculate averages")


def summarize_trial(crf: int, results: list[dict]) -> dict:
    if not results:
        raise ValueError("At least one completed sample is required")
    measured = [{**result, **sample_metrics(result)} for result in results]
    frames = sum(result["frames"] for result in measured)
    b_frames = sum(result["b_frames"] for result in measured)
    duration = math.fsum(result["duration"] for result in measured)
    video_bytes = sum(result["video_bytes"] for result in measured)
    return {
        "crf": crf,
        "average_qp": (math.fsum(result["average_qp"] * result["b_frames"]
                                 for result in measured if result["b_frames"]) / b_frames
                       if b_frames else None),
        "average_bitrate_mbps": video_bytes * 8 / duration / 1_000_000,
        "sample_count": len(measured), "frames": frames, "b_frames": b_frames,
        "duration_seconds": duration, "video_bytes": video_bytes,
        "samples": measured,
    }


def run_sample(job: dict, source_id: dict, versions: dict, output: Path, resume: bool,
               progress: Progress | None = None) -> dict:
    """Isolate native encoder logs, cache complete metrics, and reap on cancellation."""
    backend = Path(__file__).with_name("crf_encode.py").resolve()
    fingerprint = {"schema": 1, "source": source_id, "versions": versions,
                   "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(), "job": job}
    key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    cache = output / "cache" / f"{key}.json"
    if resume and cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved["fingerprint"] == fingerprint:
                sample_metrics(saved["result"])
                return {**saved["result"], "cached": True, "cache_key": key}
        except (ValueError, KeyError, TypeError):
            pass
    log = output / "logs" / f"{job['codec']}-crf{job['crf']:g}-{key[:12]}.log"
    if log.exists():
        log = log.with_stem(f"{log.stem}-retry-{time.time_ns()}")
    log.parent.mkdir(parents=True, exist_ok=True)
    worker_job = dict(job)
    if progress and progress.enabled:
        progress.update(log_path=str(log))
        if progress.sample_path:
            progress.sample_path.unlink(missing_ok=True)
            worker_job["progress_file"] = str(progress.sample_path)
    with tempfile.TemporaryDirectory(prefix="crf-job-") as temporary:
        request = Path(temporary) / "job.json"
        request.write_text(json.dumps(worker_job), encoding="utf-8")
        with log.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen([sys.executable, str(backend), "--job", str(request)],
                                       stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8")
            try:
                if progress and progress.enabled:
                    progress.update(encoder_pid=process.pid)
                while True:
                    if progress and progress.enabled:
                        progress.check_cancelled()
                    try:
                        stdout, _ = process.communicate(timeout=0.25 if progress and progress.enabled else None)
                        break
                    except subprocess.TimeoutExpired:
                        sample_progress = read_json(progress.sample_path) if progress.sample_path else {}
                        progress.update(sample_fraction=sample_progress.get("fraction", 0.0),
                                        frames=sample_progress.get("frames", 0),
                                        flushing=sample_progress.get("flushing", False))
            except BaseException:
                process.kill()
                process.communicate()
                raise
    if process.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        raise RuntimeError(f"{job['codec']} CRF {job['crf']:g} sample at {job['start']:g}s failed. "
                           f"See {log}\n" + "\n".join(tail))
    try:
        result = json.loads(stdout)
        sample_metrics(result)
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid encoder result; see {log}") from exc
    write_json(cache, {"fingerprint": fingerprint, "result": result})
    return {**result, "cached": False, "cache_key": key}


def write_reports(output: Path, report: dict) -> None:
    for analysis in report["codecs"].values():
        analysis["models"] = fit_models(analysis["rows"])
    write_json(output / "results.json", report)
    fields = ["codec", "crf", "average_qp", "average_bitrate_mbps", "sample_count",
              "frames", "b_frames", "duration_seconds", "video_bytes"]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for codec, analysis in report["codecs"].items():
            for row in analysis["rows"]:
                writer.writerow({"codec": codec, **row})
    with (output / "estimates.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["codec", "crf", "estimated_b_frame_qp", "estimated_bitrate_mbps"])
        writer.writeheader()
        for codec, analysis in report["codecs"].items():
            if analysis["models"]["log_bitrate"]:
                for crf in range(CRF_VALUES[0], CRF_VALUES[1] + 1):
                    estimate = predict(analysis["models"], crf)
                    writer.writerow({"codec": codec, "crf": crf, "estimated_b_frame_qp": estimate["average_qp"],
                                     "estimated_bitrate_mbps": estimate["average_bitrate_mbps"]})
    write_plot(output, report)


def show_settings(config: dict, codecs: list[str]) -> None:
    CONSOLE.print("One centered 60-second sample; measured CRFs: 13 and 20. Encoding options:")
    for codec in codecs:
        settings = config["codecs"][codec]
        CONSOLE.print(f"{codec}: preset={settings['preset']}, profile={settings['profile']}, "
                      f"level={settings['level'] or 'auto'}, pixel_format={settings['pixel_format']}, "
                      f"tune={settings['tune'] or 'none'}", markup=False)
        CONSOLE.print(f"  {codec}-params: " + ":".join(
            f"{key}={value}" for key, value in settings["params"].items()), markup=False)
        CONSOLE.print("  options: " + ", ".join(f"{key}={value}" for key, value in
                      {"thread_type": "0", **settings["options"]}.items()), markup=False)


def show_table(report: dict) -> None:
    table = Table(title="Measured B-frame QP and video bitrate")
    for name in ("Codec", "CRF", "Average B-frame QP", "Average Mbps", "Samples"):
        table.add_column(name, justify="left" if name == "Codec" else "right")
    for codec, analysis in report["codecs"].items():
        for row in analysis["rows"]:
            qp = f"{row['average_qp']:.3f}" if row["average_qp"] is not None else "N/A (no B-frames)"
            table.add_row(codec, str(row["crf"]), qp,
                          f"{row['average_bitrate_mbps']:.3f}", str(row["sample_count"]))
    CONSOLE.print(table)
    for codec, analysis in report["codecs"].items():
        qp, bitrate = analysis["models"]["qp"], analysis["models"]["log_bitrate"]
        if qp:
            CONSOLE.print(f"{codec}: QP(c) = {qp['a']:.6g} {qp['b']:+.6g} * c")
        else:
            CONSOLE.print(f"{codec}: {analysis['models']['qp_status']}")
        if bitrate:
            CONSOLE.print(f"{codec}: ln R(c) = {bitrate['d']:.6g} {bitrate['e']:+.6g} * c; R in Mbps")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--config", type=Path, help="JSON video and encoder settings")
    parser.add_argument("--codec", choices=("x264", "x265", "both"), default="both")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--video-stream", type=int)
    crop_options = parser.add_mutually_exclusive_group()
    crop_options.add_argument("--crop", help="auto (default), none, or fixed w:h:x:y")
    crop_options.add_argument("--no-crop", action="store_true")
    parser.add_argument("--no-resume", action="store_true", help="Re-encode matching cached samples")
    parser.add_argument("--progress-file", type=Path, help="Write atomic JSON progress for a GUI or task runner")
    parser.add_argument("--cancel-file", type=Path, help="Stop when this cancellation marker exists")
    args = parser.parse_args(argv)
    source_path = args.input.expanduser().resolve()
    output_path = (args.output_dir.expanduser().resolve() if args.output_dir
                   else source_path.parent / f"{source_path.stem}.crf-model")
    protected = {source_path, *(output_path / name for name in (
        "results.json", "summary.csv", "estimates.csv", "qp-bitrate.png", "qp-bitrate.svg", "config.json"))}
    if args.config:
        protected.add(args.config.expanduser().resolve())
    if args.progress_file:
        progress_path = args.progress_file.expanduser().resolve()
        progress_paths = {progress_path, progress_path.with_name("sample-progress.json")}
        if progress_paths & protected or (args.cancel_file and args.cancel_file.expanduser().resolve() in progress_paths):
            parser.error("Progress files must be separate from the input, configuration, reports, and cancel marker")
    output = report = None
    started = time.monotonic()
    tracking = Progress(args.progress_file, args.cancel_file)
    try:
        tracking.update(stage="inspecting", message="Inspecting video and encoder settings")
        source = args.input.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Input movie does not exist: {source}")
        config = load_config(args.config)
        if args.video_stream is not None:
            config["video"]["stream"] = args.video_stream
        if args.crop is not None:
            config["video"]["crop"] = None if args.crop.lower() == "none" else args.crop
        if args.no_crop:
            config["video"]["crop"] = None
        config = validate_config(config)
        codecs = ["x264", "x265"] if args.codec == "both" else [args.codec]
        show_settings(config, codecs)
        for codec in codecs:
            check_encoder(codec, config["codecs"][codec])
        info = inspect_video(source, config["video"]["stream"])
        sample = select_sample(info["duration"])
        samples = [sample]
        tracking.update(stage="cropping", message="Detecting black margins",
                        total=len(samples) * len(codecs) * len(CRF_VALUES))
        with CONSOLE.status("Detecting black margins"):
            crop = resolve_crop(source, info, config, samples, tracking)
        for codec in codecs:
            validate_source_settings(info, codec, config["codecs"][codec], crop["crop"])
        CONSOLE.print(f"Crop: {crop['crop'] or 'full frame'}; encoding {crop['width']}×{crop['height']}.")
        CONSOLE.print(f"Centered sample: start {sample['start']:.3f}s, duration {sample['duration']:.3f}s.")
        if info["duration"] < SAMPLE_SECONDS:
            CONSOLE.print("The video is shorter than 60 seconds; using the whole video.")
        output = (args.output_dir.expanduser().resolve() if args.output_dir
                  else source.parent / f"{source.stem}.crf-model")
        output.mkdir(parents=True, exist_ok=True)
        import av
        versions = {"pyav": av.__version__, "libraries": {k: list(v) for k, v in av.library_versions.items()}}
        stat = source.stat()
        source_id = {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        report = {"schema_version": 4, "method": METHOD, "qp_frame_type": "B", "state": "running",
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "source": source_id, "video": info, "versions": versions, "config": config,
                  "crf_values": list(CRF_VALUES), "sample_plan": samples, "crop_detection": crop,
                  "codecs": {codec: {"rows": []} for codec in codecs}, "elapsed_seconds": 0.0,
                  "notes": ["One 60-second sample centered in the video, or the whole video if shorter.",
                            "Only CRF 13 and 20 are encoded; all other values are model estimates.",
                            "QP(c) = a + b*c; ln R(c) = d + e*c, with R in Mbps.",
                            "Average QP uses only B-frames. Missing B-frame QP prevents only the QP fit.",
                            "Average video Mbps is 8 * total encoded video bytes / total presentation seconds / 1e6.",
                            "Audio, subtitles, and container overhead are excluded.",
                            "All codecs and CRFs encode the same sample ranges and crop."]}
        tracking.update(stage="reporting", message="Preparing figure and reports")
        write_reports(output, report)
        completed_samples = 0
        for codec in codecs:
            for crf in CRF_VALUES:
                with CONSOLE.status(f"{codec} CRF {crf}"):
                    tracking.update(stage="encoding", codec=codec, crf=crf,
                                    completed=completed_samples, sample_fraction=0.0,
                                    frames=0, flushing=False, log_path=None, encoder_pid=None,
                                    message=f"{codec} · CRF {crf} · centered {sample['duration']:g}s sample")
                    job = {"input": str(source), "video_index": config["video"]["stream"],
                           "start": sample["start"], "duration": sample["duration"],
                           "codec": codec, "crf": crf, "settings": config["codecs"][codec], "crop": crop["crop"]}
                    result = run_sample(job, source_id, versions, output, not args.no_resume, tracking)
                    completed_samples += 1
                    tracking.update(completed=completed_samples, sample_fraction=0.0)
                row = summarize_trial(crf, [{**result, "sample": sample}])
                report["codecs"][codec]["rows"].append(row)
                report["elapsed_seconds"] = time.monotonic() - started
                tracking.update(stage="reporting", message=f"Saving {codec} CRF {crf} figure and reports")
                write_reports(output, report)
                qp = f"{row['average_qp']:.3f}" if row["average_qp"] is not None else "N/A (no B-frames)"
                CONSOLE.print(f"{codec} CRF {crf}: average B-frame QP {qp}, "
                              f"average video bitrate {row['average_bitrate_mbps']:.3f} Mbps")
        report.update(state="complete", elapsed_seconds=time.monotonic() - started)
        write_reports(output, report)
        show_table(report)
        CONSOLE.print(f"Saved table and figure: {output}", markup=False)
        tracking.update(check=False, state="complete", stage="complete", message="Completed",
                        sample_fraction=0.0)
        return 0
    except KeyboardInterrupt:
        if report is not None:
            report.update(state="interrupted", elapsed_seconds=time.monotonic() - started)
            write_reports(output, report)
        CONSOLE.print("Interrupted. Completed samples are cached; rerun with the same settings to resume.")
        tracking.update(check=False, state="interrupted", stage="interrupted", message="Cancelled; completed results saved")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if report is not None:
            report.update(state="failed", error=str(exc), elapsed_seconds=time.monotonic() - started)
            write_reports(output, report)
        CONSOLE.print(f"Error: {exc}", style="red", markup=False)
        tracking.update(check=False, state="failed", stage="failed", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
