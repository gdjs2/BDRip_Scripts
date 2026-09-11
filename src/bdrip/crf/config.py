"""Validated encoder settings for calibration on one centered minute of video."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from bdrip.common.validation import integer as _integer
from bdrip.common.validation import number as _number

X264_PARAMS = (
    "deblock=-3,-3:bframes=10:rc-lookahead=60:aq-strength=0.85:me=umh:"
    "merange=64:psy-rd=0.85,0.10:qcomp=0.70:vbv-bufsize=30000:vbv-maxrate=40000"
)
X265_PARAMS = (
    "deblock=-3,-3:ctu=32:rskip=2:early-skip=1:tu-inter-depth=3:"
    "tu-intra-depth=3:rect=0:amp=0:cbqpoffs=-2:crqpoffs=-2:me=hex:"
    "subme=7:merange=32:ref=5:max-merge=3:keyint=250:min-keyint=1:"
    "bframes=10:aq-mode=2:aq-strength=0.90:pbratio=1.2:rd=4:psy-rd=2.0:"
    "psy-rdoq=1.0:rdoq-level=2:rc-lookahead=60:lookahead-slices=0:"
    "scenecut=40:qcomp=0.68"
)

DEFAULT_CONFIG: dict[str, Any] = {
    "video": {
        "stream": 0,
        "crop": "auto",
        "cropdetect": {"limit": 24 / 255, "seconds": 2.0},
    },
    "codecs": {
        "x264": {
            "preset": "placebo",
            "tune": None,
            "pixel_format": "yuv420p",
            "profile": "high",
            "level": "4.1",
            "params": dict(item.split("=", 1) for item in X264_PARAMS.split(":")),
            "options": {},
        },
        "x265": {
            "preset": "slower",
            "tune": None,
            "pixel_format": "yuv420p10le",
            "profile": "main10",
            "level": None,
            "params": dict(item.split("=", 1) for item in X265_PARAMS.split(":")),
            "options": {},
        },
    },
}

_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
}
_RATE_CONTROL_KEYS = {
    "crf",
    "crf-max",
    "crf-min",
    "crfmax",
    "crfmin",
    "qp",
    "qpmin",
    "qpmax",
    "qp-min",
    "qp-max",
    "qpfile",
    "qp-file",
    "bitrate",
    "bit-rate",
    "b",
    "ab",
    "cqp",
    "q",
    "qmin",
    "qmax",
    "qscale",
    "global-quality",
    "pass",
    "passlogfile",
    "stats",
    "stats-read",
    "stats-write",
    "slow-firstpass",
    "fast-firstpass",
    "lossless",
    "zones",
    "zonefile",
    "zones-file",
    "rc",
    "rc-mode",
    "rc-method",
}
_LOG_OUTPUT_KEYS = {
    "csv",
    "csv-log-level",
    "log-level",
    "log",
    "logfile",
    "log-file",
    "quiet",
    "verbose",
    "output",
    "output-depth",
    "output-csp",
    "output-res",
    "dump-yuv",
    "dump-recon",
    "recon",
    "recon-depth",
    "recon-y4m-exec",
    "analysis-save",
    "analysis-load",
    "analysis-reuse-file",
    "analysis-file",
}
_CORE_KEYS = {
    "preset",
    "tune",
    "profile",
    "level",
    "level-idc",
    "pix-fmt",
    "pixel-format",
    "format",
    "width",
    "height",
    "video-size",
    "s",
    "input-res",
    "input-csp",
    "input-depth",
    "fps",
    "r",
    "framerate",
    "time-base",
    "enc-time-base",
    "filter",
    "vf",
    "filter-complex",
    "crop",
    "scale",
    "vcodec",
    "codec",
    "c",
    "x264-params",
    "x264opts",
    "x265-params",
    "x265opts",
    "flags",
    "flags2",
}
_CLI_KEYS = {
    "i",
    "f",
    "map",
    "map-metadata",
    "map-chapters",
    "an",
    "sn",
    "dn",
    "vn",
    "ss",
    "sseof",
    "t",
    "to",
    "frames",
    "vframes",
    "y",
    "n",
    "shortest",
    "copyts",
    "start-at-zero",
    "vsync",
    "fps-mode",
    "metadata",
    "movflags",
    "filter-threads",
    "hwaccel",
    "hwaccel-device",
    "hwaccel-output-format",
    "hide-banner",
    "nostdin",
    "loglevel",
    "report",
    "progress",
}


def _merge(defaults: dict[str, Any], overlay: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(overlay, dict):
        raise ValueError(f"{prefix or 'config'} must be an object")
    result = copy.deepcopy(defaults)
    for key, value in overlay.items():
        field = f"{prefix}.{key}" if prefix else str(key)
        if key not in defaults:
            raise ValueError(f"Unknown config key: {field}")
        if isinstance(defaults[key], dict) and key not in {"params", "options"}:
            result[key] = _merge(defaults[key], value, field)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _settings(value: Any, field: str, *, params: bool) -> dict[str, str]:
    if params and isinstance(value, str):
        result: dict[str, Any] = {}
        for item in value.split(":") if value.strip() else []:
            if "=" not in item:
                raise ValueError(f"{field} requires colon-separated key=value entries")
            key, item_value = item.split("=", 1)
            key = key.strip()
            if key in result:
                raise ValueError(f"Duplicate encoder parameter: {field}.{key}")
            result[key] = item_value.strip()
        value = result
    if not isinstance(value, dict):
        raise ValueError(
            f"{field} must be an object" + (" or key=value string" if params else "")
        )
    result = {}
    forbidden = _RATE_CONTROL_KEYS | _LOG_OUTPUT_KEYS | _CORE_KEYS | _CLI_KEYS
    for key, item_value in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", key):
            raise ValueError(
                f"{field} keys must be encoder option names without leading dashes"
            )
        canonical = key.lower().replace("_", "-")
        if canonical in forbidden or (
            canonical.startswith("no-") and canonical[3:] in forbidden
        ):
            raise ValueError(
                f"{field}.{key} overrides analyzer-controlled encoding, timing, or statistics; "
                "use the dedicated config fields where available"
            )
        if isinstance(item_value, bool):
            item_value = "1" if item_value else "0"
        elif isinstance(item_value, (int, float)):
            if not math.isfinite(item_value):
                raise ValueError(f"{field}.{key} must be finite")
            item_value = str(item_value)
        elif not isinstance(item_value, str):
            raise ValueError(f"{field}.{key} must be a string, number, or boolean")
        if not item_value or any(char in item_value for char in ("\x00", "\n", "\r")):
            raise ValueError(f"{field}.{key} must be a nonempty single-line value")
        if params and ":" in item_value:
            raise ValueError(
                f"{field}.{key} cannot contain ':' in an encoder parameter value"
            )
        if key in result:
            raise ValueError(f"Duplicate encoder parameter: {field}.{key}")
        result[key] = item_value
    return result


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial config with defaults and return a validated independent copy.

    Encoder params/options replace those dictionaries entirely when supplied.
    All other objects merge recursively; unknown schema keys are errors.
    """
    if isinstance(config, dict) and "sampling" in config:
        # Import encoder settings from prior versions without retaining their
        # random sampling policy. New exports use only video and codecs.
        config = copy.deepcopy(config)
        legacy = config.pop("sampling")
        allowed = {
            "count",
            "min_seconds",
            "max_seconds",
            "seed",
            "start",
            "end",
            "seconds",
        }
        if not isinstance(legacy, dict) or set(legacy) - allowed:
            raise ValueError("Unknown config key or invalid legacy sampling object")
    config = _merge(DEFAULT_CONFIG, config)
    video = config["video"]
    video["stream"] = _integer(video["stream"], "video.stream", 0)
    crop = video["crop"]
    if crop is not None and crop != "auto":
        if not isinstance(crop, str) or not re.fullmatch(r"\d+:\d+:\d+:\d+", crop):
            raise ValueError(
                "video.crop must be 'auto', null, or integer 'width:height:x:y'"
            )
        width, height, x, y = map(int, crop.split(":"))
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError(
                "video.crop width and height must be positive and even for 4:2:0; offsets may be odd"
            )
    cropdetect = video["cropdetect"]
    cropdetect["limit"] = _number(
        cropdetect["limit"], "video.cropdetect.limit", positive=True
    )
    if cropdetect["limit"] >= 1:
        raise ValueError(
            "video.cropdetect.limit must be greater than 0 and less than 1"
        )
    cropdetect["seconds"] = _number(
        cropdetect["seconds"], "video.cropdetect.seconds", positive=True
    )

    for name, codec in config["codecs"].items():
        prefix = f"codecs.{name}"
        if not isinstance(codec["preset"], str) or codec["preset"] not in _PRESETS:
            raise ValueError(f"{prefix}.preset is not an x264/x265 preset")
        for key in ("tune", "level"):
            value = codec[key]
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or any(char in value for char in ("\n", "\r", "\x00"))
            ):
                raise ValueError(f"{prefix}.{key} must be null or a nonempty string")
        expected_format, expected_profile = (
            ("yuv420p", "high") if name == "x264" else ("yuv420p10le", "main10")
        )
        if codec["pixel_format"] != expected_format:
            raise ValueError(f"{prefix}.pixel_format must be {expected_format}")
        if codec["profile"] != expected_profile:
            raise ValueError(f"{prefix}.profile must be {expected_profile}")
        codec["params"] = _settings(codec["params"], f"{prefix}.params", params=True)
        codec["options"] = _settings(
            codec["options"], f"{prefix}.options", params=False
        )
    return config


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON config key: {key}")
        result[key] = value
    return result


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read UTF-8 JSON or return validated defaults; never modify DEFAULT_CONFIG."""
    if path is None:
        return validate_config({})
    try:
        config = json.loads(
            Path(path).read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read CRF config {path}: {exc}") from exc
    return validate_config(config)
