"""Regression checks for user encoder arguments and CRF configuration."""

import json
import tempfile
import unittest
from pathlib import Path

from bdrip.common.time import parse_time
from bdrip.crf.config import (
    DEFAULT_CONFIG,
    X264_PARAMS,
    X265_PARAMS,
    load_config,
    validate_config,
)


class ConfigTests(unittest.TestCase):
    def test_example_preserves_requested_encoder_arguments(self):
        example = Path(__file__).resolve().parents[2] / "crf_search.example.json"
        config = load_config(example)
        self.assertEqual(config, load_config())
        for codec, expected in (("x264", X264_PARAMS), ("x265", X265_PARAMS)):
            actual = ":".join(
                f"{key}={value}"
                for key, value in config["codecs"][codec]["params"].items()
            )
            self.assertEqual(actual, expected)

    def test_partial_overlay_does_not_mutate_defaults(self):
        config = validate_config(
            {"sampling": {"count": 3}, "codecs": {"x264": {"preset": "fast"}}}
        )
        config["codecs"]["x264"]["params"]["bframes"] = "2"
        self.assertEqual(config["sampling"], {"count": 3, "seconds": 10, "seed": 0})
        self.assertEqual(load_config()["sampling"]["count"], 10)
        self.assertEqual(load_config()["codecs"]["x264"]["params"]["bframes"], "10")
        self.assertEqual(DEFAULT_CONFIG["codecs"]["x264"]["preset"], "placebo")

    def test_default_profiles_presets_and_auto_crop(self):
        config = load_config()
        self.assertEqual(
            config["video"],
            {
                "stream": 0,
                "crop": "auto",
                "cropdetect": {"limit": 24 / 255, "seconds": 2.0},
            },
        )
        for name, preset, profile, level in (
            ("x264", "placebo", "high", "4.1"),
            ("x265", "slower", "main10", None),
        ):
            codec = config["codecs"][name]
            self.assertEqual(
                (codec["preset"], codec["profile"], codec["level"]),
                (preset, profile, level),
            )

    def test_legacy_sampling_preserves_count_seed_and_encoder_options(self):
        legacy = {
            "sampling": {
                "count": 10,
                "min_seconds": 5,
                "max_seconds": 10,
                "seed": 123,
                "start": 30,
                "end": 570,
            },
            "codecs": {"x264": {"preset": "fast", "params": "bframes=4"}},
        }
        before = json.dumps(legacy)
        config = validate_config(legacy)
        self.assertEqual(set(config), {"video", "codecs", "sampling"})
        self.assertEqual(config["sampling"], {"count": 10, "seconds": 10, "seed": 123})
        self.assertEqual(config["codecs"]["x264"]["preset"], "fast")
        self.assertEqual(config["codecs"]["x264"]["params"], {"bframes": "4"})
        self.assertEqual(json.dumps(legacy), before)

    def test_unknown_retired_settings_are_rejected(self):
        for old_setting in (
            {"runtime": {}},
            {"search": {}},
            {"codecs": {"x264": {"qp_target": 19.5}}},
        ):
            with (
                self.subTest(old_setting=old_setting),
                self.assertRaisesRegex(ValueError, "Unknown config key"),
            ):
                validate_config(old_setting)

    def test_sampling_settings_are_validated(self):
        for field, values in (
            ("count", (0, -1, True, 1.5, "10", None)),
            ("seconds", (0, -1, True, "10", None, float("nan"), float("inf"))),
            ("seed", (-1, True, 1.5, "0", float("inf"))),
        ):
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    validate_config({"sampling": {field: value}})
        self.assertEqual(
            validate_config({"sampling": {"seed": None}})["sampling"]["seed"], 0
        )

    def test_auto_manual_and_disabled_crop_are_distinct(self):
        for crop in ("auto", "1920:800:0:140", "1920:804:0:137", "1280:720:1:3", None):
            with self.subTest(crop=crop):
                config = validate_config({"video": {"crop": crop}})
                self.assertEqual(config["video"]["crop"], crop)
                self.assertEqual(config["video"]["cropdetect"]["seconds"], 2.0)
        config = validate_config({"video": {"cropdetect": {"seconds": 4}}})
        self.assertEqual(
            config["video"]["cropdetect"], {"limit": 24 / 255, "seconds": 4.0}
        )
        config["video"]["cropdetect"]["limit"] = 0.2
        self.assertEqual(load_config()["video"]["cropdetect"]["limit"], 24 / 255)

    def test_manual_crop_keeps_exact_dimensions_and_odd_offsets(self):
        config = validate_config({"video": {"crop": "1920:804:0:137"}})
        self.assertEqual(config["video"]["crop"], "1920:804:0:137")
        for crop in ("1919:804:0:137", "1920:803:0:137", "1919:803:1:137"):
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                validate_config({"video": {"crop": crop}})

    def test_crop_detection_settings_are_validated(self):
        for field, values in (
            ("limit", (0, -0.1, 1, 24, True, "0.1", float("nan"), float("inf"))),
            ("seconds", (0, -1, True, "2", float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        validate_config({"video": {"cropdetect": {field: value}}})
        for crop in ("none", "AUTO", False, [], {}):
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                validate_config({"video": {"crop": crop}})
        for value in (None, [], {"threshold": 0.1}):
            with self.subTest(cropdetect=value), self.assertRaises(ValueError):
                validate_config({"video": {"cropdetect": value}})

    def test_provided_params_replace_all_default_parameters(self):
        for params in ({}, "", {"bframes": 3}, "bframes=3"):
            with self.subTest(params=params):
                config = validate_config({"codecs": {"x264": {"params": params}}})
                actual = config["codecs"]["x264"]["params"]
                self.assertEqual(actual, {} if not params else {"bframes": "3"})
                self.assertEqual(
                    config["codecs"]["x265"]["params"],
                    DEFAULT_CONFIG["codecs"]["x265"]["params"],
                )

    def test_encoder_mapping_values_normalized_for_pyav(self):
        config = validate_config(
            {
                "codecs": {
                    "x265": {
                        "params": {
                            "early-skip": True,
                            "rect": False,
                            "aq-strength": 0.9,
                        },
                        "options": {"threads": 2},
                    }
                }
            }
        )
        self.assertEqual(
            config["codecs"]["x265"]["params"],
            {
                "early-skip": "1",
                "rect": "0",
                "aq-strength": "0.9",
            },
        )
        self.assertEqual(config["codecs"]["x265"]["options"], {"threads": "2"})

    def test_timestamp_conversion_and_invalid_values(self):
        for value, expected in (
            (0, 0.0),
            (12.5, 12.5),
            ("12.5", 12.5),
            ("03:04.5", 184.5),
            ("01:02:03.5", 3723.5),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_time(value), expected)
        for value in (
            -1,
            True,
            None,
            float("inf"),
            float("nan"),
            "01:60:00",
            "00:01:60",
            "bad",
            "-2",
            "nan",
            "1:2:3:4",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_time(value)

    def test_unknown_keys_are_errors(self):
        for config in (
            {"codec": {}},
            {"codecs": {"x266": {}}},
            {"sampling": {"counts": 5}},
            {"codecs": {"x264": {"initial": 18}}},
        ):
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "Unknown config key"):
                    validate_config(config)

    def test_invalid_video_and_encoder_settings_are_errors(self):
        configs = [
            {"sampling": []},
            {"codecs": {"x264": {"pixel_format": "yuv420p10le"}}},
            {"codecs": {"x265": {"profile": "main"}}},
            {"video": {"stream": -1}},
            {"video": {"crop": "1920:801:0:1"}},
            {"video": {"crop": "iw:ih:0:0"}},
        ]
        for config in configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_config(config)

    def test_conflicting_search_or_log_options_are_errors(self):
        for field in ("params", "options"):
            for key in (
                "crf",
                "crf-max",
                "crf_max",
                "qp",
                "bitrate",
                "pass",
                "stats",
                "csv",
                "csv-log-level",
                "log-level",
                "global_quality",
                "x264opts",
                "x265-params",
                "preset",
                "pix_fmt",
                "map",
                "vf",
                "ss",
                "output",
                "flags",
                "flags2",
            ):
                with self.subTest(field=field, key=key):
                    with self.assertRaises(ValueError):
                        validate_config({"codecs": {"x264": {field: {key: "1"}}}})

    def test_invalid_parameter_syntax_is_rejected(self):
        for params in (
            "bframes",
            "bframes=2:bframes=3",
            {"bframes": None},
            {"bframes": [3]},
            {"bframes": "3:crf=40"},
            {"-crf": "18"},
            {"aq-strength": float("inf")},
        ):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    validate_config({"codecs": {"x264": {"params": params}}})

    def test_file_loading_rejects_duplicate_keys_and_reports_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                '{"sampling": {"count": 2, "count": 3}}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Duplicate JSON config key"):
                load_config(config_path)
            config_path.write_text("{invalid}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "config.json"):
                load_config(config_path)
            config_path.write_text(
                json.dumps({"sampling": {"start": "00:05"}}), encoding="utf-8-sig"
            )
            self.assertEqual(load_config(config_path), load_config())


if __name__ == "__main__":
    unittest.main()
