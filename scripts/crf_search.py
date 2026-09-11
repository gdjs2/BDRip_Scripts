"""Measure a movie's CRF/bitrate relationship using PyAV sample encodes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

if __package__:
    from .crf_config import load_config, parse_time, validate_config
    from .crf_crop import detect_crop
    from .crf_encode import check_encoder, inspect_video, validate_source_settings
else:
    from crf_config import load_config, parse_time, validate_config
    from crf_crop import detect_crop
    from crf_encode import check_encoder, inspect_video, validate_source_settings


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


def resolve_crop(source: Path, info: dict, config: dict, samples: list[dict]) -> dict:
    """Resolve automatic/manual settings once, before either codec is tested."""
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
            or any(value % 2 for value in (width, height, x, y))):
        raise ValueError("Manual crop must fit the source and use even dimensions and offsets")
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


def auto_search(evaluate: Callable[[float], dict], policy: dict, search: dict) -> tuple[list[dict], dict]:
    """Bracket a passing CRF and refine it; interpolate only to choose real trials."""
    low, high = search["min_crf"], search["max_crf"]
    precision, budget = search["precision"], search["max_trials"]
    measured: dict[float, dict] = {}

    def snap(value: float) -> float:
        if value <= low:
            return low
        if value >= high:
            return high
        candidate = low + round((value - low) / precision) * precision
        if candidate <= low:
            return low
        if candidate >= high:
            return high
        return round(candidate, 8)

    def trial(value: float) -> None:
        value = snap(value)
        if value not in measured and len(measured) < budget:
            measured[value] = evaluate(value)

    initial = snap(policy["initial_crf"])
    trial(initial)
    # Neighbors expose a local slope; subsequent trials expand or narrow the bracket.
    step = max(precision, 1.0)
    for candidate in (initial - step, initial + step):
        trial(candidate)

    reason = "trial_limit"
    while True:
        ordered = sorted(measured)
        passes = [c for c in ordered if measured[c]["qp_pass"]]
        fails = [c for c in ordered if not measured[c]["qp_pass"]]
        # A failed point below a passing one contradicts the search assumption.
        if any(f < p for f in fails for p in passes):
            reason = "nonmonotonic_observations"
            break
        if passes and max(passes) >= high - 1e-8:
            reason = "upper_bound_passes"
            break
        if not passes and min(ordered) <= low + 1e-8:
            reason = "no_passing_crf_in_bounds"
            break
        if passes and fails and min(fails) - max(passes) <= precision + 1e-8:
            reason = "precision_reached"
            break
        if len(measured) >= budget:
            reason = "trial_limit"
            break
        if not passes:
            candidate = snap(max(low, min(ordered) - step))
            step *= 2
        elif not fails:
            candidate = snap(min(high, max(ordered) + step))
            step *= 2
        else:
            left, right = max(passes), min(fails)
            # Secant interpolation of QP, clamped away from either endpoint.
            def gate(row: dict) -> float:
                return max(row["qp95"], row.get("stress_qp") or row["qp95"])
            qleft, qright = gate(measured[left]), gate(measured[right])
            ratio = ((policy["qp_target"] - qleft) / (qright - qleft)
                     if qright > qleft else 0.5)
            ratio = min(0.75, max(0.25, ratio))
            candidate = snap(left + (right - left) * ratio)
            if candidate in measured:
                candidate = snap((left + right) / 2)
        if candidate in measured:
            reason = "precision_reached"
            break
        before = len(measured)
        trial(candidate)
        if len(measured) == before:
            reason = "precision_reached"
            break
    rows = [measured[c] for c in sorted(measured)]
    passing = [r for r in rows if r["qp_pass"]]
    return rows, {
        "reason": reason,
        "trials": len(rows),
        "recommended_crf": max((r["crf"] for r in passing), default=None),
        "verified": bool(passing),
        "precision": precision,
        "bounds": [low, high],
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


def run_sample(job: dict, source_id: dict, versions: dict, output: Path, resume: bool) -> dict:
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
    with tempfile.TemporaryDirectory(prefix="crf-job-") as temporary:
        request = Path(temporary) / "job.json"
        request.write_text(json.dumps(job), encoding="utf-8")
        with log.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen([sys.executable, str(backend), "--job", str(request)],
                                       stdout=subprocess.PIPE, stderr=stderr, text=True,
                                       encoding="utf-8")
            try:
                stdout, _ = process.communicate()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
    if process.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        raise RuntimeError(f"{job['codec']} CRF {job['crf']:g} sample at {job['start']:g}s failed. "
                           f"See {log}\n" + "\n".join(tail))
    try:
        result = json.loads(stdout)
        if (result["frames"] <= 0 or result["duration"] <= 0 or result["video_bytes"] <= 0
                or not result["qp"]):
            raise ValueError("Missing encoded video or QP metrics")
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid encoder result; see {log}") from exc
    write_json(cache, {"fingerprint": fingerprint, "result": result})
    return {**result, "cached": False, "cache_key": key}


def write_reports(output: Path, report: dict) -> None:
    write_json(output / "results.json", report)
    fields = ["codec", "crf", "bitrate_mbps", "qp95", "worst_qp", "stress_qp", "qp_pass",
              "recommended", "status", "measurement"]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for codec, analysis in report["codecs"].items():
            best = analysis.get("search", {}).get("recommended_crf")
            for row in analysis["rows"]:
                writer.writerow({**row, "codec": codec, "recommended": row["crf"] == best})


def show_table(codec: str, rows: list[dict], summary: dict) -> None:
    table = Table(title=f"{codec}: measured samples; estimated movie video bitrate")
    for name in ("CRF", "Video Mbps", "QP95", "Worst QP", "Stress QP", "Status"):
        table.add_column(name, justify="left" if name == "Status" else "right")
    for row in rows:
        best = row["crf"] == summary["recommended_crf"]
        table.add_row(f"{row['crf']:g}", f"{row['bitrate_mbps']:.3f}", f"{row['qp95']:.2f}",
                      f"{row['worst_qp']:.2f}",
                      "—" if row["stress_qp"] is None else f"{row['stress_qp']:.2f}",
                      ("BEST: " if best else "") + row["status"], style="green" if best else "")
    CONSOLE.print(table)
    if summary["recommended_crf"] is None:
        CONSOLE.print("No tested CRF satisfied the QP safety target.")
    else:
        CONSOLE.print(f"Highest tested passing CRF: {summary['recommended_crf']:g}")
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
        config = validate_config(config)
        codecs = ["x264", "x265"] if args.codec == "both" else [args.codec]
        for codec in codecs:
            check_encoder(codec, config["codecs"][codec])
        info = inspect_video(source, config["video"]["stream"])
        samples = select_samples(info["duration"], config["sampling"])
        with CONSOLE.status("Preparing video crop before encoding samples"):
            crop_detection = resolve_crop(source, info, config, samples)
        for codec in codecs:
            validate_source_settings(info, codec, config["codecs"][codec], crop_detection["crop"])
        output = (args.output_dir.expanduser().resolve() if args.output_dir
                  else source.parent / f"{source.stem}.crf-search")
        output.mkdir(parents=True, exist_ok=True)
        import av
        versions = {"pyav": av.__version__,
                    "libraries": {k: list(v) for k, v in av.library_versions.items()}}
        stat = source.stat()
        source_id = {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        report = {"schema_version": SCHEMA_VERSION,
                  "created_utc": datetime.now(timezone.utc).isoformat(), "state": "running",
                  "source": source_id, "video": info, "versions": versions,
                  "config": config, "sample_plan": samples, "crop_detection": crop_detection,
                  "codecs": {},
                  "notes": ["Bitrate is encoded video payload Mbps (decimal), excluding container overhead.",
                            "Movie bitrate is estimated from representative samples; stress samples are excluded.",
                            "QP95 is the interpolated 95th percentile of sample max(I/P/B average QP).",
                            "Each stress sample must also meet the QP safety target.",
                            "QP is an encoder policy metric, not a guarantee of visual transparency."]}
        CONSOLE.print(f"Video: {info['width']}×{info['height']}, {info['duration']:.2f}s; "
                      f"{len(samples)} samples per CRF. Preset and custom options stay fixed.")
        CONSOLE.print(f"Crop ({crop_detection['mode']}): {crop_detection['crop'] or 'full frame'}; "
                      f"encoding {crop_detection['width']}×{crop_detection['height']}. "
                      f"{crop_detection['reason']}")
        for codec in codecs:
            policy = config["codecs"][codec]
            settings = {key: policy[key] for key in ENCODER_KEYS}
            analysis = {"rows": [], "search": {}, "relationship_fit": None}
            report["codecs"][codec] = analysis
            write_reports(output, report)

            def evaluate(crf: float) -> dict:
                results = []
                with CONSOLE.status(f"{codec} CRF {crf:g}") as progress:
                    for index, sample in enumerate(samples):
                        progress.update(f"{codec} CRF {crf:g}: sample {index + 1}/{len(samples)}")
                        job = {"input": str(source), "video_index": config["video"]["stream"],
                               "start": sample["start"], "duration": sample["duration"],
                               "codec": codec, "crf": crf, "settings": settings,
                               "crop": crop_detection["crop"]}
                        result = run_sample(job, source_id, versions, output, not args.no_resume)
                        results.append({**result, "sample": sample})
                row = summarize_trial(crf, results, policy)
                analysis["rows"].append(row)
                analysis["rows"].sort(key=lambda r: r["crf"])
                write_reports(output, report)
                CONSOLE.print(f"{codec} CRF {crf:g}: {row['bitrate_mbps']:.3f} Mbps, "
                              f"QP95 {row['qp95']:.2f} — {row['status']}")
                return row

            if crfs:
                rows = [evaluate(c) for c in crfs]
                passing = [r["crf"] for r in rows if r["qp_pass"]]
                summary = {"reason": "explicit_sweep", "trials": len(rows),
                           "recommended_crf": max(passing, default=None), "verified": bool(passing)}
            else:
                rows, summary = auto_search(evaluate, policy, config["search"])
            analysis.update(rows=rows, search=summary, relationship_fit=fit_relationship(rows))
            write_reports(output, report)
            show_table(codec, rows, summary)
        report["state"] = "complete"
        write_reports(output, report)
        CONSOLE.print(f"Reports: {output / 'summary.csv'} and {output / 'results.json'}")
        return 0
    except KeyboardInterrupt:
        if report is not None and output is not None:
            report["state"] = "interrupted"
            write_reports(output, report)
        CONSOLE.print("Interrupted. Completed samples are cached; rerun the command to resume.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if report is not None and output is not None:
            report.update(state="failed", error=str(exc))
            write_reports(output, report)
        CONSOLE.print(f"Error: {exc}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
