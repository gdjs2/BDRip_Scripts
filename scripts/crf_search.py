"""Measure a movie's CRF/bitrate relationship using PyAV sample encodes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import tempfile
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from rich.console import Console
from rich.table import Table

if __package__:
    from .crf_config import load_config, parse_time, validate_config
    from .crf_crop import detect_crop
    from .crf_encode import check_encoder, validate_source_settings
    from .crf_runtime import BudgetExpired, run_probe, run_process
    from .crf_optimizer import auto_search, measured_summary, next_trial
else:
    from crf_config import load_config, parse_time, validate_config
    from crf_crop import detect_crop
    from crf_encode import check_encoder, validate_source_settings
    from crf_runtime import BudgetExpired, run_probe, run_process
    from crf_optimizer import auto_search, measured_summary, next_trial


SCHEMA_VERSION = 1
ENCODER_KEYS = ("preset", "tune", "pixel_format", "profile", "level", "params", "options")
CONSOLE = Console(stderr=True)


def percentile(values: list[float], fraction: float) -> float:
    """Linearly interpolate between sorted observations at (n - 1) * fraction."""
    if not values or not 0 <= fraction <= 1:
        raise ValueError("Percentile needs observations and a fraction between 0 and 1")
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def select_samples(duration: float, sampling: dict) -> list[dict]:
    """Select centered windows in equal time bins, plus separate stress samples."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be finite and positive")
    start = parse_time(sampling.get("start", 0))
    end = duration if sampling.get("end") is None else parse_time(sampling["end"])
    length = float(sampling["seconds"])
    count = sampling["count"]
    if not (0 <= start < end <= duration + 1e-6):
        raise ValueError("Sampling bounds must satisfy 0 <= start < end <= video duration")
    if not math.isfinite(length) or length <= 0 or not isinstance(count, int) or count < 1:
        raise ValueError("Sample duration and count must be positive")
    end = min(end, duration)
    span = end - start
    length = min(length, span)
    count = min(count, max(1, math.floor(span / length)))
    samples = [
        {"kind": "representative", "start": start + (i + 0.5) * span / count - length / 2,
         "duration": length}
        for i in range(count)
    ]
    seen = set()
    for value in sampling.get("stress_starts", []):
        position = parse_time(value)
        if not 0 <= position < duration:
            raise ValueError("Stress sample timestamps must lie inside the video")
        if position not in seen:
            samples.append({"kind": "stress", "start": position,
                            "duration": min(float(sampling["seconds"]), duration - position)})
            seen.add(position)
    return samples


def resolve_crop(source: Path, info: dict, config: dict, samples: list[dict],
                 *, timeout: float | None = None) -> dict:
    """Resolve automatic/manual settings once, before either codec is tested."""
    video = config["video"]
    crop = video["crop"]
    if crop == "auto":
        if timeout is not None:
            return run_probe("crop", {"input": str(source), "video_index": video["stream"],
                                     "samples": samples, "source": info,
                                     "settings": video["cropdetect"]}, timeout=timeout)
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


def summarize_trial(crf: float, results: list[dict], policy: dict) -> dict:
    representative = [r for r in results if r["sample"]["kind"] == "representative"]
    stress = [r for r in results if r["sample"]["kind"] == "stress"]
    if not representative:
        raise ValueError("At least one representative sample is required")
    for result in results:
        if (result["duration"] <= 0 or result["video_bytes"] <= 0 or not result["qp"]
                or not all(math.isfinite(v) for v in result["qp"].values())):
            raise ValueError("Incomplete sample metrics; refusing to recommend a CRF")
    duration = sum(r["duration"] for r in representative)
    bitrate = 8 * sum(r["video_bytes"] for r in representative) / duration / 1_000_000
    qps = [max(r["qp"].values()) for r in representative]
    qp95 = percentile(qps, 0.95)
    stress_qp = max((max(r["qp"].values()) for r in stress), default=None)
    gate = max(qp95, stress_qp) if stress_qp is not None else qp95
    passed = gate <= policy["qp_target"]
    if not passed:
        status = "QP limit exceeded" if gate >= policy["qp_limit"] else "Above QP safety target"
    elif bitrate > policy["emergency_bitrate_mbps"]:
        status = "Over emergency bitrate preference"
    elif bitrate > policy["normal_bitrate_mbps"][1]:
        status = "High bitrate exception"
    elif bitrate < policy["normal_bitrate_mbps"][0]:
        status = "PASS (below preferred bitrate)"
    else:
        status = "PASS"
    return {
        "crf": crf, "measurement": "sample encode", "bitrate_mbps": bitrate,
        "qp95": qp95, "worst_qp": max(qps), "stress_qp": stress_qp,
        "qp_pass": passed, "status": status, "sample_duration": duration,
        "samples": results,
    }


def fit_relationship(rows: list[dict]) -> dict | None:
    """Describe a local log-linear bitrate fit, never presented as measurements."""
    if len(rows) < 3:
        return None
    xs = [r["crf"] for r in rows]
    ys = [math.log(r["bitrate_mbps"]) for r in rows]
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - xmean) ** 2 for x in xs)
    if not variance:
        return None
    slope = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) / variance
    intercept = ymean - slope * xmean
    residual = sum((y - intercept - slope * x) ** 2 for x, y in zip(xs, ys))
    total = sum((y - ymean) ** 2 for y in ys)
    return {"formula": "bitrate_mbps = exp(intercept + slope * crf)",
            "intercept": intercept, "slope": slope,
            "r_squared_log_space": 1 - residual / total if total else None,
            "valid_crf_interval": [min(xs), max(xs)],
            "note": "Descriptive sample fit; predictions are not verified measurements."}


def sample_floor(info: dict, config: dict, codecs: list[str]) -> float:
    """Allow several seconds and the configured lookahead before shortening clips."""
    frames = 0
    for codec in codecs:
        params = config["codecs"][codec]["params"]
        try:
            frames = max(frames, int(params.get("rc-lookahead", 60))
                         + int(params.get("bframes", 10)) + 1)
        except ValueError:
            # The encoder will diagnose unsupported option values itself.
            frames = max(frames, 71)
    return min(config["sampling"]["seconds"], max(3.0, frames / float(Fraction(info["fps"]))))


def choose_sample_seconds(maximum: float, minimum: float, timings: list[dict],
                          sample_count: int, planned_trials: int, available: float) -> float:
    """Budget three measured points using pilot speed, with a 25% allowance."""
    rate = sum(item["wall_seconds"] / item["duration"] for item in timings)
    if rate <= 0:
        return maximum
    length = available / (sample_count * planned_trials * rate * 1.25)
    return max(minimum, min(maximum, math.floor(length * 10) / 10))


def show_settings(config: dict, codecs: list[str], crfs: list[float] | None) -> None:
    """Print resolved user settings before any timing pilot or CRF trial."""
    CONSOLE.print("Encoding options (fixed throughout calibration and search):")
    for codec in codecs:
        settings = config["codecs"][codec]
        CONSOLE.print(
            f"{codec}: preset={settings['preset']}, profile={settings['profile']}, "
            f"level={settings['level'] or 'auto'}, pixel_format={settings['pixel_format']}, "
            f"tune={settings['tune'] or 'none'}", markup=False)
        CONSOLE.print(f"  {codec}-params: " + ":".join(
            f"{key}={value}" for key, value in settings["params"].items()), markup=False)
        options = {"thread_type": "0", **settings["options"]}
        CONSOLE.print("  options: " + ", ".join(f"{key}={value}" for key, value in options.items()),
                      markup=False)
    search = config["search"]
    CONSOLE.print("CRFs: " + (", ".join(f"{crf:g}" for crf in crfs) if crfs else
                  f"automatic {search['min_crf']:g}–{search['max_crf']:g}, "
                  f"at most {search['max_trials']} trials per codec"))
    if crfs is None:
        CONSOLE.print("Automatic selection: find the highest CRF meeting the QP target; "
                      "each measurement chooses the next CRF. Prioritize up to three "
                      "comparison trials per codec within the hard time budget.")
    runtime = config["runtime"]
    CONSOLE.print(f"Time budget for the whole run: target {runtime['target_seconds']:g}s, "
                  f"maximum {runtime['max_seconds']:g}s (including crop and calibration).")


def explain_trial(crf: float, rows: list[dict], policy: dict) -> str:
    """Describe the observation that led to the next real encode."""
    if not rows:
        return "measure the initial CRF"
    passing = [row for row in rows if row["qp_pass"]]
    failing = [row for row in rows if not row["qp_pass"]]
    if passing and failing:
        return (f"refine the boundary between passing CRF {max(row['crf'] for row in passing):g} "
                f"and failing CRF {min(row['crf'] for row in failing):g}")
    previous = min(rows, key=lambda row: abs(row["crf"] - crf))
    gate = max(previous["qp95"], previous.get("stress_qp")
               if previous.get("stress_qp") is not None else previous["qp95"])
    direction = "increase" if crf > previous["crf"] else "decrease"
    return (f"{direction} CRF after measured QP {gate:.3f} at CRF {previous['crf']:g} "
            f"({'passes' if previous['qp_pass'] else 'exceeds'} target {policy['qp_target']:g})")


def sampling_identity(signature: dict) -> dict | None:
    """Keep calibrated plans when only search policy changes, including old reports.

    Trial limits and QP targets do not change the encoded samples. Earlier
    reports stored the full config here; normalize both forms so an optimizer
    update can reuse measurements made before that update.
    """
    try:
        config = signature["config"]
        return {**signature, "config": {
            "video": config["video"], "sampling": config["sampling"], "runtime": config["runtime"],
            "codecs": {codec: {key: config["codecs"][codec][key] for key in ENCODER_KEYS}
                       for codec in signature["codecs"]},
        }}
    except (KeyError, TypeError):
        return None


def write_json(path: Path, data: dict) -> None:
    """Publish complete JSON atomically so interrupted runs keep valid results."""
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


def run_sample(job: dict, source_id: dict, versions: dict, output: Path, resume: bool,
               *, timeout: float | None = None) -> dict:
    cutoff = None if timeout is None else time.monotonic() + timeout
    if timeout is not None and timeout <= 0:
        raise BudgetExpired("No time remains for another encoding sample")
    backend = Path(__file__).with_name("crf_encode.py").resolve()
    fingerprint = {"schema": SCHEMA_VERSION, "source": source_id, "versions": versions,
                   "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(), "job": job}
    key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    cache = output / "cache" / f"{key}.json"
    if resume and cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            result = saved["result"]
            if (saved["fingerprint"] == fingerprint and result["duration"] > 0
                    and result["video_bytes"] > 0 and result["frames"] > 0
                    and result["qp"]):
                return {**result, "cached": True, "cache_key": key}
        except (ValueError, KeyError, TypeError):
            pass
    log = output / "logs" / f"{job['codec']}-crf{job['crf']:g}-{key[:12]}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="crf-job-") as temporary:
        request = Path(temporary) / "job.json"
        request.write_text(json.dumps(job), encoding="utf-8")
        with log.open("w", encoding="utf-8") as stderr:
            try:
                stdout = run_process([sys.executable, str(backend), "--job", str(request)],
                                     timeout=None if cutoff is None else cutoff - time.monotonic(),
                                     stderr=stderr)
            except BudgetExpired:
                raise
            except RuntimeError as exc:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
                raise RuntimeError(f"{job['codec']} CRF {job['crf']:g} sample at {job['start']:g}s failed. "
                                   f"See {log}\n" + "\n".join(tail)) from exc
    try:
        result = json.loads(stdout)
        if (result["frames"] <= 0 or result["duration"] <= 0 or result["video_bytes"] <= 0
                or not result["qp"]):
            raise ValueError("Missing encoded video or QP metrics")
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid encoder result; see {log}") from exc
    result["wall_seconds"] = time.monotonic() - started
    write_json(cache, {"fingerprint": fingerprint, "result": result})
    return {**result, "cached": False, "cache_key": key}


def write_reports(output: Path, report: dict) -> None:
    write_json(output / "results.json", report)
    fields = ["codec", "crf", "bitrate_mbps", "qp95", "worst_qp", "stress_qp", "qp_pass",
              "recommended", "status", "measurement", "best_tested", "search_outcome",
              "search_converged", "recommendation_status"]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for codec, analysis in report["codecs"].items():
            summary = analysis.get("search", {})
            recommended = summary.get("recommended_crf")
            best = summary.get("best_tested_crf")
            for row in analysis["rows"]:
                writer.writerow({**row, "codec": codec, "recommended": row["crf"] == recommended,
                                 "best_tested": row["crf"] == best,
                                 "search_outcome": summary.get("reason", "running"),
                                 "search_converged": summary.get("converged", False),
                                 "recommendation_status": summary.get("recommendation_status", "running")})


def show_table(codec: str, rows: list[dict], summary: dict) -> None:
    table = Table(title=f"{codec}: measured samples; estimated movie video bitrate")
    for name in ("CRF", "Video Mbps", "QP95", "Worst QP", "Stress QP", "Status"):
        table.add_column(name, justify="left" if name == "Status" else "right")
    for row in rows:
        best = row["crf"] == summary["recommended_crf"]
        provisional = not best and row["crf"] == summary.get("best_tested_crf")
        table.add_row(f"{row['crf']:g}", f"{row['bitrate_mbps']:.3f}", f"{row['qp95']:.2f}",
                      f"{row['worst_qp']:.2f}",
                      "—" if row["stress_qp"] is None else f"{row['stress_qp']:.2f}",
                      ("RECOMMENDED: " if best else "CANDIDATE: " if provisional else "") + row["status"],
                      style="green" if best else "")
    CONSOLE.print(table)
    if summary["recommended_crf"] is not None:
        row = next(row for row in rows if row["crf"] == summary["recommended_crf"])
        CONSOLE.print(f"Recommended {codec} CRF: {row['crf']:g} — "
                      f"{row['bitrate_mbps']:.3f} Mbps, QP95 {row['qp95']:.3f}. "
                      "Confirmed by measured CRF comparisons within the configured bounds.")
    elif summary.get("best_tested_crf") is not None:
        CONSOLE.print(f"Best tested passing CRF: {summary['best_tested_crf']:g} "
                      "(provisional; no converged automatic recommendation).")
    else:
        CONSOLE.print("No tested CRF satisfied the QP safety target.")
    if len(rows) < 2:
        CONSOLE.print(f"Only {len(rows)} CRF value(s) measured; insufficient comparisons to select a best CRF.")
    CONSOLE.print(f"Search outcome: {summary['reason']}")


def parse_crfs(value: str) -> list[float]:
    try:
        values = sorted(set(float(item.strip()) for item in value.split(",")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("CRFs must be comma-separated numbers") from exc
    if not values or any(not math.isfinite(v) or not 0 <= v <= 51 for v in values):
        raise argparse.ArgumentTypeError("CRFs must be finite numbers between 0 and 51")
    return values


def parse_crf_range(value: str) -> list[float]:
    from decimal import Decimal, InvalidOperation
    try:
        low, high, step = (Decimal(item.strip()) for item in value.split(":"))
        if (not all(v.is_finite() for v in (low, high, step))
                or not 0 <= low <= high <= 51 or step <= 0):
            raise ValueError
        count = int((high - low) / step) + 1
        if count > 1000:
            raise ValueError
        return [float(low + i * step) for i in range(count)]
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            "CRF range must be MIN:MAX:STEP within 0–51, with positive step and at most 1000 trials"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--config", type=Path, help="JSON encoder, sampling and search settings")
    parser.add_argument("--codec", choices=("x264", "x265", "both"), default="both")
    sweep = parser.add_mutually_exclusive_group()
    sweep.add_argument("--crf-values", type=parse_crfs, help="Measure this CRF list instead of automatic search")
    sweep.add_argument("--crf-range", type=parse_crf_range, help="Measure MIN:MAX:STEP, e.g. 16:22:0.5")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--sample-seconds", type=float)
    parser.add_argument("--target-seconds", type=float, help="Whole-run time target (default 180)")
    parser.add_argument("--max-seconds", type=float, help="Whole-run hard time limit (default 300)")
    parser.add_argument("--start", help="Sampling interval start, seconds or HH:MM:SS")
    parser.add_argument("--end", help="Sampling interval end, seconds or HH:MM:SS")
    parser.add_argument("--stress-start", action="append", help="Additional difficult scene, seconds or HH:MM:SS")
    crop_options = parser.add_mutually_exclusive_group()
    crop_options.add_argument("--crop", help="auto (default), none, or fixed w:h:x:y")
    crop_options.add_argument("--no-crop", action="store_true", help="Disable automatic cropping")
    parser.add_argument("--video-stream", type=int)
    parser.add_argument("--no-resume", action="store_true", help="Re-encode samples even when cached")
    args = parser.parse_args(argv)
    crfs = args.crf_values if args.crf_values is not None else args.crf_range
    report = None
    output = None
    try:
        source = args.input.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Input movie does not exist: {source}")
        config = load_config(args.config)
        for option, field in (("samples", "count"), ("sample_seconds", "seconds"),
                              ("start", "start"), ("end", "end"), ("stress_start", "stress_starts")):
            value = getattr(args, option)
            if value is not None:
                config["sampling"][field] = value
        if args.crop is not None:
            value = args.crop.strip()
            config["video"]["crop"] = None if value.lower() == "none" else value
        if args.no_crop:
            config["video"]["crop"] = None
        if args.video_stream is not None:
            config["video"]["stream"] = args.video_stream
        for name in ("target_seconds", "max_seconds"):
            if getattr(args, name) is not None:
                config["runtime"][name] = getattr(args, name)
        if args.max_seconds is not None and args.target_seconds is None:
            config["runtime"]["target_seconds"] = min(config["runtime"]["target_seconds"],
                                                       args.max_seconds)
        config = validate_config(config)
        codecs = ["x264", "x265"] if args.codec == "both" else [args.codec]
        runtime = config["runtime"]
        # Leave a small reserve to publish reports after native workers stop.
        deadline = started + runtime["max_seconds"] - min(1.0, runtime["max_seconds"] * 0.05)
        target = started + runtime["target_seconds"]

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        show_settings(config, codecs, crfs)
        for codec in codecs:
            check_encoder(codec, config["codecs"][codec])
        output = (args.output_dir.expanduser().resolve() if args.output_dir
                  else source.parent / f"{source.stem}.crf-search")
        output.mkdir(parents=True, exist_ok=True)
        previous_report = None
        if not args.no_resume and (output / "results.json").is_file():
            try:
                previous_report = json.loads((output / "results.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        import av
        versions = {"pyav": av.__version__,
                    "libraries": {k: list(v) for k, v in av.library_versions.items()}}
        stat = source.stat()
        source_id = {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        report = {"schema_version": SCHEMA_VERSION,
                  "created_utc": datetime.now(timezone.utc).isoformat(), "state": "running",
                  "source": source_id, "versions": versions, "config": config,
                  "runtime": {**runtime, "elapsed_seconds": 0.0},
                  "sample_plan": [], "codecs": {codec: {"rows": [], "search": {},
                                                        "relationship_fit": None} for codec in codecs},
                  "notes": ["Bitrate is encoded video payload Mbps (decimal), excluding container overhead.",
                            "Movie bitrate is estimated from representative samples; stress samples are excluded.",
                            "QP95 is the interpolated 95th percentile of sample max(I/P/B average QP).",
                            "Each stress sample must also meet the QP safety target.",
                            "Automatic recommendations require a measured passing/failing boundary or a passing upper bound, with at least two distinct CRFs compared.",
                            "QP is an encoder policy metric, not a guarantee of visual transparency.",
                            "Short clips give preliminary estimates; GOP startup and limited scene coverage affect results."]}
        write_reports(output, report)
        info = run_probe("inspect", {"input": str(source), "video_index": config["video"]["stream"]},
                         timeout=remaining())
        report["video"] = info
        minimum = sample_floor(info, config, codecs)
        samples = select_samples(info["duration"], {**config["sampling"], "seconds": minimum})
        minimum = samples[0]["duration"]
        sample_start = config["sampling"]["start"]
        sample_end = config["sampling"]["end"] or info["duration"]
        representative_count = sum(sample["kind"] == "representative" for sample in samples)
        maximum = min(config["sampling"]["seconds"],
                      (sample_end - sample_start) / representative_count)
        with CONSOLE.status("Preparing video crop before encoding samples"):
            crop_detection = resolve_crop(source, info, config, samples, timeout=remaining())
        report.update(crop_detection=crop_detection, sample_plan=samples)
        for codec in codecs:
            validate_source_settings(info, codec, config["codecs"][codec], crop_detection["crop"])
        CONSOLE.print(f"Video: {info['width']}×{info['height']}, {info['duration']:.2f}s; "
                      f"{len(samples)} samples per CRF. Preset and custom options stay fixed.")
        CONSOLE.print(f"Crop ({crop_detection['mode']}): {crop_detection['crop'] or 'full frame'}; "
                      f"encoding {crop_detection['width']}×{crop_detection['height']}. "
                      f"{crop_detection['reason']}")

        def make_job(codec: str, crf: float, sample: dict) -> dict:
            policy = config["codecs"][codec]
            return {"input": str(source), "video_index": config["video"]["stream"],
                    "start": sample["start"], "duration": sample["duration"],
                    "codec": codec, "crf": crf, "settings": {key: policy[key] for key in ENCODER_KEYS},
                    "crop": crop_detection["crop"]}

        timings = {}
        length = minimum
        active = list(codecs)
        signature = {"source": source_id, "versions": versions, "config": config,
                     "codecs": codecs, "crop": crop_detection["crop"],
                     "backend_sha256": hashlib.sha256(Path(__file__).with_name("crf_encode.py").read_bytes()).hexdigest()}
        signature = sampling_identity(signature)
        saved_plan = (previous_report.get("sampling_calibration", {})
                      if isinstance(previous_report, dict) else {})
        saved_length = saved_plan.get("seconds")
        if (sampling_identity(saved_plan.get("signature")) == signature
                and isinstance(saved_length, (int, float)) and minimum <= saved_length <= maximum):
            length = saved_length
            timings = saved_plan.get("timings", {})
            CONSOLE.print(f"Reusing the previous {length:g}s sample plan for cached measurements.")
        elif maximum > minimum + 1e-6:
            for codec in codecs:
                pilot_crf = crfs[0] if crfs else config["codecs"][codec]["initial_crf"]
                CONSOLE.print(f"Calibrating {codec} speed with a {minimum:g}s clip at CRF {pilot_crf:g}.")
                try:
                    # If a single pilot exceeds this share, ten such samples
                    # cannot fit. Give the other codec a chance to be measured.
                    timeout = min(remaining(), max(
                        remaining() / (len(active) * len(samples)),
                        max(0.0, target - time.monotonic()) / (3 * len(active))))
                    pilot = run_sample(make_job(codec, pilot_crf, samples[0]), source_id,
                                       versions, output, not args.no_resume, timeout=timeout)
                    timings[codec] = {"duration": pilot["duration"],
                                      "wall_seconds": pilot.get("wall_seconds", timeout)}
                except BudgetExpired:
                    report["codecs"][codec]["search"] = measured_summary([], "time_limit", config["search"])
                    active.remove(codec)
                    CONSOLE.print(f"{codec}: timing pilot exceeded its budget; no complete trial measured.")
            if timings:
                length = choose_sample_seconds(maximum, minimum, list(timings.values()), len(samples),
                                               min(3, len(crfs)) if crfs else 3,
                                               max(0.0, target - time.monotonic()))
        # Keep the same centers and count used for crop detection. Every CRF
        # and codec receives this identical plan, including any stress windows.
        samples = [{**sample,
                    "start": sample["start"] - (length - sample["duration"]) / 2
                    if sample["kind"] == "representative" else sample["start"],
                    "duration": length if sample["kind"] == "representative"
                    else min(length, info["duration"] - sample["start"])} for sample in samples]
        report.update(sample_plan=samples, sampling_calibration={"seconds": length, "timings": timings,
                                                                 "signature": signature})
        CONSOLE.print(f"Testing {representative_count} representative clips of {length:g}s each "
                      f"({representative_count * length:g}s of video per CRF), plus "
                      f"{len(samples) - representative_count} stress clips.")
        CONSOLE.print("Short-clip results are preliminary estimates; only complete sample sets enter the table.")
        quota = {codec: remaining() / len(active) for codec in active}
        trial_times = {}

        def retire(codec: str) -> None:
            active.remove(codec)
            # Once a codec has finished, its unused share can help the other
            # codec finish a full trial; the global deadline still applies.
            if active:
                share = max(0.0, quota[codec]) / len(active)
                for other in active:
                    quota[other] += share
            quota[codec] = 0.0

        while active:
            for codec in list(active):
                analysis = report["codecs"][codec]
                rows = analysis["rows"]
                policy = config["codecs"][codec]
                if crfs:
                    measured = {row["crf"] for row in rows}
                    candidate = next((value for value in crfs if value not in measured), None)
                    summary = measured_summary(rows, "explicit_sweep") if candidate is None else None
                else:
                    candidate, summary = next_trial(rows, policy, config["search"])
                if summary is not None:
                    analysis["search"] = summary
                    retire(codec)
                    continue
                estimated = trial_times.get(codec, 0.0)
                priority_comparison = crfs is None and len(rows) < min(3, config["search"]["max_trials"])
                if (remaining() <= 0 or quota[codec] <= 0
                        or (rows and not priority_comparison and time.monotonic() >= target)
                        or (rows and not priority_comparison and estimated > target - time.monotonic())):
                    analysis["search"] = measured_summary(rows, "time_limit", config["search"])
                    retire(codec)
                    continue
                decision = explain_trial(candidate, rows, policy) if crfs is None else "measure the requested sweep value"
                CONSOLE.print(f"{codec}: testing CRF {candidate:g} — {decision}.")
                if priority_comparison:
                    CONSOLE.print(f"Comparison {len(rows) + 1}/3 has priority over the soft time target; "
                                  f"{min(remaining(), quota[codec]):.0f}s available in the hard budget.")
                entry = {"crf": candidate, "reason": decision, "completed": False}
                analysis.setdefault("decisions", []).append(entry)
                trial_started = time.monotonic()
                results = []
                timed_out = False
                try:
                    with CONSOLE.status(f"{codec} CRF {candidate:g}") as progress:
                        for index, sample in enumerate(samples):
                            progress.update(f"{codec} CRF {candidate:g}: sample {index + 1}/{len(samples)}; "
                                            f"{remaining():.0f}s remaining")
                            timeout = min(remaining(), quota[codec] - (time.monotonic() - trial_started))
                            result = run_sample(make_job(codec, candidate, sample), source_id, versions,
                                                output, not args.no_resume, timeout=timeout)
                            results.append({**result, "sample": sample})
                except BudgetExpired:
                    analysis["search"] = measured_summary(rows, "time_limit", config["search"])
                    analysis["incomplete_trial"] = {"crf": candidate, "completed_samples": len(results),
                                                    "required_samples": len(samples)}
                    timed_out = True
                else:
                    row = summarize_trial(candidate, results, policy)
                    entry["completed"] = True
                    rows.append(row)
                    rows.sort(key=lambda item: item["crf"])
                    analysis["search"] = measured_summary(rows, "running", config["search"])
                    CONSOLE.print(f"{codec} CRF {candidate:g}: {row['bitrate_mbps']:.3f} Mbps, "
                                  f"QP95 {row['qp95']:.2f} — {row['status']}")
                elapsed = time.monotonic() - trial_started
                quota[codec] -= elapsed
                if timed_out:
                    retire(codec)
                trial_times[codec] = elapsed * 1.25
                report["runtime"]["elapsed_seconds"] = time.monotonic() - started
                write_reports(output, report)
        for codec, analysis in report["codecs"].items():
            analysis["relationship_fit"] = fit_relationship(analysis["rows"])
            show_table(codec, analysis["rows"], analysis["search"])
        summaries = [analysis["search"] for analysis in report["codecs"].values()]
        report["state"] = ("time_limit" if any(summary["reason"] == "time_limit" for summary in summaries)
                           else "incomplete" if crfs is None and any(not summary["converged"] for summary in summaries)
                           else "complete")
        report["runtime"]["elapsed_seconds"] = time.monotonic() - started
        write_reports(output, report)
        CONSOLE.print(f"Finished in {report['runtime']['elapsed_seconds']:.1f}s; {report['state']}.")
        CONSOLE.print(f"Reports: {output / 'summary.csv'} and {output / 'results.json'}")
        return 0
    except BudgetExpired:
        if report is not None and output is not None:
            report["state"] = "time_limit"
            report["runtime"]["elapsed_seconds"] = time.monotonic() - started
            for codec, analysis in report["codecs"].items():
                analysis["search"] = measured_summary(analysis["rows"], "time_limit", config["search"])
                analysis["relationship_fit"] = fit_relationship(analysis["rows"])
                show_table(codec, analysis["rows"], analysis["search"])
            write_reports(output, report)
            CONSOLE.print(f"Time limit reached. Completed measurements saved to {output}.")
        return 0
    except KeyboardInterrupt:
        if report is not None and output is not None:
            report["state"] = "interrupted"
            report["runtime"]["elapsed_seconds"] = time.monotonic() - started
            write_reports(output, report)
        CONSOLE.print("Interrupted. Completed samples are cached; rerun the command to resume.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if report is not None and output is not None:
            report.update(state="failed", error=str(exc))
            report["runtime"]["elapsed_seconds"] = time.monotonic() - started
            write_reports(output, report)
        CONSOLE.print(f"Error: {exc}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
