"""Measure average B-frame QP and video bitrate at CRFs 14–18 using random movie clips."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

if __package__:
    from .crf_config import load_config, parse_time, validate_config
    from .crf_crop import detect_crop
    from .crf_encode import check_encoder, inspect_video, validate_source_settings
    from .crf_plot import write_plot
else:
    from crf_config import load_config, parse_time, validate_config
    from crf_crop import detect_crop
    from crf_encode import check_encoder, inspect_video, validate_source_settings
    from crf_plot import write_plot


CRF_VALUES = (14, 15, 16, 17, 18)
CONSOLE = Console(stderr=True)


def select_samples(duration: float, sampling: dict) -> list[dict]:
    """Draw one uniform random clip inside each equal temporal stratum."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be finite and positive")
    start = parse_time(sampling["start"])
    end = duration if sampling["end"] is None else parse_time(sampling["end"])
    count = sampling["count"]
    minimum, maximum = sampling["min_seconds"], sampling["max_seconds"]
    if not 0 <= start < end <= duration + 1e-6:
        raise ValueError("Sampling bounds must satisfy 0 <= start < end <= video duration")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Sample count must be a positive integer")
    if not (math.isfinite(minimum) and math.isfinite(maximum) and 0 < minimum <= maximum):
        raise ValueError("Sample durations must be finite and satisfy 0 < min_seconds <= max_seconds")
    end = min(end, duration)
    bin_seconds = (end - start) / count
    if bin_seconds + 1e-9 < minimum:
        raise ValueError(
            f"The sampling interval cannot fit {count} clips of at least {minimum:g}s. "
            "Reduce sampling.count or sampling.min_seconds, or extend the interval."
        )
    rng = random.Random(sampling["seed"])
    samples = []
    for index in range(count):
        left, right = start + index * bin_seconds, start + (index + 1) * bin_seconds
        length = rng.uniform(min(minimum, bin_seconds), min(maximum, bin_seconds))
        position = rng.uniform(left, max(left, right - length))
        samples.append({"kind": "representative", "start": position, "duration": length})
    return samples


def resolve_crop(source: Path, info: dict, config: dict, samples: list[dict]) -> dict:
    video = config["video"]
    crop = video["crop"]
    if crop == "auto":
        return detect_crop(source, video["stream"], samples, info, video["cropdetect"])
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


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".crf-", suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_sample(job: dict, source_id: dict, versions: dict, output: Path, resume: bool) -> dict:
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
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="crf-job-") as temporary:
        request = Path(temporary) / "job.json"
        request.write_text(json.dumps(job), encoding="utf-8")
        with log.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen([sys.executable, str(backend), "--job", str(request)],
                                       stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8")
            try:
                stdout, _ = process.communicate()
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
    write_json(output / "results.json", report)
    fields = ["codec", "crf", "average_qp", "average_bitrate_mbps", "sample_count",
              "frames", "b_frames", "duration_seconds", "video_bytes"]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for codec, analysis in report["codecs"].items():
            for row in analysis["rows"]:
                writer.writerow({"codec": codec, **row})
    write_plot(output, report)


def show_settings(config: dict, codecs: list[str]) -> None:
    CONSOLE.print("CRF sweep: 14, 15, 16, 17, 18. Encoding options:")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--config", type=Path, help="JSON sampling and encoder settings")
    parser.add_argument("--codec", choices=("x264", "x265", "both"), default="both")
    parser.add_argument("--samples", type=int, help="Number of evenly distributed random clips (default 10)")
    parser.add_argument("--min-sample-seconds", type=float, help="Minimum random clip length (default 5)")
    parser.add_argument("--max-sample-seconds", type=float, help="Maximum random clip length (default 10)")
    parser.add_argument("--sample-seconds", type=float, help="Use this fixed duration for every clip")
    parser.add_argument("--seed", type=int, help="Random seed for repeatable sample selection (default 0)")
    parser.add_argument("--start", help="Sampling interval start, seconds or HH:MM:SS")
    parser.add_argument("--end", help="Sampling interval end, seconds or HH:MM:SS")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--video-stream", type=int)
    crop_options = parser.add_mutually_exclusive_group()
    crop_options.add_argument("--crop", help="auto (default), none, or fixed w:h:x:y")
    crop_options.add_argument("--no-crop", action="store_true")
    parser.add_argument("--no-resume", action="store_true", help="Re-encode matching cached samples")
    args = parser.parse_args(argv)
    if args.sample_seconds is not None and (args.min_sample_seconds is not None or args.max_sample_seconds is not None):
        parser.error("--sample-seconds cannot be combined with minimum/maximum sample duration options")
    output = report = None
    started = time.monotonic()
    try:
        source = args.input.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Input movie does not exist: {source}")
        config = load_config(args.config)
        for argument, field in (("samples", "count"), ("min_sample_seconds", "min_seconds"),
                                ("max_sample_seconds", "max_seconds"), ("seed", "seed"),
                                ("start", "start"), ("end", "end")):
            if getattr(args, argument) is not None:
                config["sampling"][field] = getattr(args, argument)
        if args.sample_seconds is not None:
            config["sampling"].update(min_seconds=args.sample_seconds, max_seconds=args.sample_seconds)
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
        samples = select_samples(info["duration"], config["sampling"])
        with CONSOLE.status("Detecting black margins"):
            crop = resolve_crop(source, info, config, samples)
        for codec in codecs:
            validate_source_settings(info, codec, config["codecs"][codec], crop["crop"])
        CONSOLE.print(f"Crop: {crop['crop'] or 'full frame'}; encoding {crop['width']}×{crop['height']}.")
        CONSOLE.print(f"Random seed {config['sampling']['seed']}; {len(samples)} clips, "
                      f"{sum(sample['duration'] for sample in samples):.2f}s of video per CRF.")
        for index, sample in enumerate(samples, 1):
            CONSOLE.print(f"  Clip {index}: start {sample['start']:.3f}s, duration {sample['duration']:.3f}s")
        output = (args.output_dir.expanduser().resolve() if args.output_dir
                  else source.parent / f"{source.stem}.crf-sweep")
        output.mkdir(parents=True, exist_ok=True)
        import av
        versions = {"pyav": av.__version__, "libraries": {k: list(v) for k, v in av.library_versions.items()}}
        stat = source.stat()
        source_id = {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        report = {"schema_version": 3, "qp_frame_type": "B", "state": "running",
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "source": source_id, "video": info, "versions": versions, "config": config,
                  "crf_values": list(CRF_VALUES), "sample_plan": samples, "crop_detection": crop,
                  "codecs": {codec: {"rows": []} for codec in codecs}, "elapsed_seconds": 0.0,
                  "notes": ["Average QP uses only B-frames, weighted by B-frame counts across all samples.",
                            "Without B-frames, average_qp is null and the point is omitted from the figure.",
                            "Average video Mbps is 8 * total encoded video bytes / total presentation seconds / 1e6.",
                            "Audio, subtitles, and container overhead are excluded.",
                            "All codecs and CRFs encode the same sample ranges and crop."]}
        write_reports(output, report)
        for crf in CRF_VALUES:
            for codec in codecs:
                results = []
                with CONSOLE.status(f"{codec} CRF {crf}") as progress:
                    for index, sample in enumerate(samples, 1):
                        progress.update(f"{codec} CRF {crf}: clip {index}/{len(samples)}")
                        job = {"input": str(source), "video_index": config["video"]["stream"],
                               "start": sample["start"], "duration": sample["duration"],
                               "codec": codec, "crf": crf, "settings": config["codecs"][codec], "crop": crop["crop"]}
                        result = run_sample(job, source_id, versions, output, not args.no_resume)
                        results.append({**result, "sample": sample})
                row = summarize_trial(crf, results)
                report["codecs"][codec]["rows"].append(row)
                report["elapsed_seconds"] = time.monotonic() - started
                write_reports(output, report)
                qp = f"{row['average_qp']:.3f}" if row["average_qp"] is not None else "N/A (no B-frames)"
                CONSOLE.print(f"{codec} CRF {crf}: average B-frame QP {qp}, "
                              f"average video bitrate {row['average_bitrate_mbps']:.3f} Mbps")
        report.update(state="complete", elapsed_seconds=time.monotonic() - started)
        write_reports(output, report)
        show_table(report)
        CONSOLE.print(f"Saved table and figure: {output}", markup=False)
        return 0
    except KeyboardInterrupt:
        if report is not None:
            report.update(state="interrupted", elapsed_seconds=time.monotonic() - started)
            write_reports(output, report)
        CONSOLE.print("Interrupted. Completed samples are cached; rerun with the same settings to resume.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if report is not None:
            report.update(state="failed", error=str(exc), elapsed_seconds=time.monotonic() - started)
            write_reports(output, report)
        CONSOLE.print(f"Error: {exc}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
