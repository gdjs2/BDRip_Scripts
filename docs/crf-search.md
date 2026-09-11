Yes. With these principles, I would **stop assigning a fixed CRF by movie genre alone**. Instead, I’d use the movie-specific visual analysis only to choose an initial CRF, then let a small automated sample encode find the actual sweet point.

CRF is especially suitable for this because it is fundamentally quality-controlled variable bitrate: it does not try to hit a bitrate target; difficult material simply consumes more bits. ([x265 Documentation][1])

One terminology note: you mean **CRF**, not CFR. CFR is constant frame rate.

## 1. I would formalize your policy like this

Assuming your QP limit means the **encoder-reported average QP** rather than literally requiring every individual frame to have QP < 20/21:

| Constraint            |                 x264 |                x265 Main10 |
| --------------------- | -------------------: | -------------------------: |
| Absolute QP limit     |              **<20** |                    **<21** |
| Practical target      |            **≤19.5** |                  **≤20.5** |
| Normal avg bitrate    |            8–15 Mbps |                  5–10 Mbps |
| Grain-heavy exception |       up to ~28 Mbps |             up to ~25 Mbps |
| Compatibility         | High@4.1 + VBV 30/40 | Main10, Auto level, no VBV |

I intentionally use:

> **19.5 x264 / 20.5 x265**

as the automatic target rather than exactly 20/21.

That 0.5-QP margin protects against sampling error and unusually difficult sections of the full movie.

Your 28/25 Mbps values should be interpreted as **emergency average-bitrate allowances**. They are separate from x264's instantaneous:

```text
vbv-bufsize=30000:vbv-maxrate=40000
```

constraint.

---

# 2. The most important rule

I would define your sweet-point CRF as:

> **The highest CRF that keeps difficult representative portions of the movie below your QP threshold, while preferably staying inside your normal bitrate range.**

That's slightly different from:

> “Find a CRF that generates 10 Mbps.”

And *She Rides Shotgun* demonstrated exactly why that distinction matters.

Your CRF 15 encode resulted in:

```text
I Avg QP = 13.00
P Avg QP = 16.50
B Avg QP = 17.42

Bitrate = 33.93 Mbps
```



That tells us the source is simply extremely expensive at high quality.

If we force it to 10 Mbps, x264 has no alternative except increasing QP drastically.

Under your new policy, the program would correctly classify it as:

> **grain-heavy exception**

instead of trying to force it into 8–15 Mbps.

---

# 3. I would use a three-level priority

The decision should be:

### Priority 1 — QP

Hard requirement:

```text
x264 QP < 20
x265 QP < 21
```

If violated:

> **lower CRF**

regardless of bitrate.

### Priority 2 — normal bitrate range

Once QP passes:

```text
x264: 8–15 Mbps
x265: 5–10 Mbps
```

is preferred.

### Priority 3 — grain exception

If QP cannot remain below your limit inside the normal bitrate range:

```text
x264: allow >15 Mbps, up to ~28 Mbps
x265: allow >10 Mbps, up to ~25 Mbps
```

Do **not** increase CRF merely to force the bitrate down if doing so violates your QP criterion.

This is the crucial change.

---

# 4. A simple decision table

For x264, for example:

| Result           | Decision                         |
| ---------------- | -------------------------------- |
| QP 18.0, 11 Mbps | Increase CRF                     |
| QP 19.4, 11 Mbps | **Excellent sweet point**        |
| QP 19.8, 11 Mbps | Borderline; decrease CRF ~0.25   |
| QP 20.4, 10 Mbps | Decrease CRF                     |
| QP 18.5, 17 Mbps | Increase CRF if possible         |
| QP 19.5, 18 Mbps | **Accept as difficult source**   |
| QP 19.5, 25 Mbps | **Accept as grain-heavy source** |
| QP 20.5, 25 Mbps | Decrease CRF despite bitrate     |
| QP 20+, >28 Mbps | No setting satisfies your policy |

In that final situation, you would have to choose among:

* exceeding your emergency bitrate preference;
* relaxing QP;
* or keeping/remuxing the original.

Since you said QP is the **hard** constraint, I would prioritize QP.

---

# 5. Don't test only one random 10-minute clip

HandBrake itself recommends short test encodes when selecting constant quality. ([HandBrake][2])

But for your use, I would improve on that.

Take approximately **8–12 samples**, each around **30–60 seconds**.

For a two-hour movie, something like:

```text
05:00
15:00
25:00
35:00
45:00
55:00
65:00
75:00
85:00
95:00
105:00
115:00
```

Then additionally replace a few with manually selected difficult scenes if you know them:

* grainiest scene
* darkest scene
* fastest action
* foliage/rain/snow
* smoke
* crowd
* credits are usually not useful

HandBrakeCLI supports encoding duration ranges using `--start-at` and `--stop-at`, so this can be fully automated. ([HandBrake][3])

---

# 6. The QP metric I'd actually use

Don't rely only on the combined average of all test clips.

For each sample, record:

```text
Avg QP I
Avg QP P
Avg QP B
```

Then define:

```text
sample_QP = max(I_QP, P_QP, B_QP)
```

Normally B will be highest.

If your 10 sample values were:

```text
18.3
18.7
19.0
18.4
19.3
18.9
19.6
18.5
19.1
18.8
```

I would care particularly about:

```text
average ≈ 18.96

and

worst / high-percentile ≈ 19.5–19.6
```

For automatic tuning, I'd use approximately the **95th percentile sample QP**, rather than the arithmetic mean.

So your actual automated constraints become:

```text
x264:
Q95 <= 19.5

x265:
Q95 <= 20.5
```

This gives you protection against difficult scenes without letting one bizarre frame control the entire encode.

---

# 7. CRF can then be searched automatically

You don't need machine learning.

A simple iterative search is enough.

I'd start at:

```text
x264 initial CRF = 17.5
x265 initial CRF = 18.5
```

Encode all sample clips.

Suppose x264 gives:

```text
CRF 17.5

estimated bitrate = 9.8 Mbps
Q95 = 18.7
```

You have quality headroom.

Try:

```text
CRF 18.5
```

Suppose that gives:

```text
8.2 Mbps
Q95 = 19.6
```

Now you've gone slightly too far.

Try:

```text
CRF 18.0
```

and perhaps:

```text
CRF 18.1
CRF 18.2
```

Your final answer might become:

> **CRF 18.1**

There is absolutely no reason you have to limit yourself to integer or half-integer CRFs.

---

# 8. You can make it faster with interpolation

CRF versus QP is reasonably smooth locally, and bitrate versus CRF is approximately exponential locally.

So after testing three CRFs, for example:

```text
CRF 17
CRF 18
CRF 19
```

your program can fit:

$$
QP(c) \approx a + bc
$$

and:

$$
\ln B(c) \approx d + ec
$$

where:

* \(c\) = CRF
* \(QP(c)\) = predicted QP
* \(B(c)\) = predicted bitrate

The logarithm for bitrate is important because bitrate doesn't behave linearly with CRF.

Then the program can evaluate:

```text
CRF 14.00
CRF 14.05
CRF 14.10
...
CRF 23.00
```

using the fitted functions and identify the predicted optimum.

Then you perform **one final sample encode** at that CRF to verify it.

You don't need to trust the prediction blindly.

---

# 9. My objective function would be very simple

You don't even really need a mathematical weighted score.

Use constraints in order:

```python
if qp95 > qp_limit:
    reject

elif bitrate > emergency_max:
    reject_or_warn

elif normal_bitrate_min <= bitrate <= normal_bitrate_max:
    excellent

else:
    acceptable_exception
```

Then among all acceptable CRFs:

> choose the **highest CRF whose QP95 is below the safety target**.

For x264:

```text
QP95 <= 19.5
```

For x265:

```text
QP95 <= 20.5
```

If several values satisfy that and you care about staying inside your normal bitrate range, prefer one inside that range.

---

# 10. I would make the program output something like this

For example:

```text
====================================================
x264 Analysis
====================================================

CRF     QP95    Est. bitrate    Status
15.0    16.9    18.7 Mbps       Grain exception
16.0    17.7    15.6 Mbps       Slightly high
17.0    18.6    13.1 Mbps       PASS
17.5    19.0    12.0 Mbps       PASS
18.0    19.4    10.9 Mbps       BEST
18.5    19.9     9.9 Mbps       Too close to QP limit
19.0    20.4     9.0 Mbps       FAIL

Recommended x264:
CRF = 18.0
Expected bitrate = ~10.9 Mbps
Expected QP95 = ~19.4
```

And:

```text
====================================================
x265 Analysis
====================================================

CRF     QP95    Est. bitrate    Status
17.0    18.4    8.3 Mbps        PASS
18.0    19.3    7.1 Mbps        PASS
18.5    19.8    6.6 Mbps        PASS
19.0    20.3    6.1 Mbps        BEST
19.5    20.9    5.6 Mbps        Borderline
20.0    21.4    5.2 Mbps        FAIL

Recommended x265:
CRF = 19.0
Expected bitrate = ~6.1 Mbps
Expected QP95 = ~20.3
```

This is much more useful than saying:

> “For every film use x264 CRF 17.5.”

---

# 11. Grainy movies naturally fall out of the same algorithm

This is the nice part.

You don't need to classify *She Rides Shotgun* manually.

Suppose the program gets:

```text
x264

CRF 15 → QP95 17.8 / 34 Mbps
CRF 16 → QP95 18.7 / 30 Mbps
CRF 17 → QP95 19.6 / 26 Mbps
CRF 18 → QP95 20.5 / 22 Mbps
```

The normal 8–15 Mbps target is impossible.

The algorithm automatically concludes:

> **CRF ~16.8–17.0, grain-heavy exception, ~26–27 Mbps**

instead of forcing 12 Mbps.

That's exactly what we learned from your actual *She Rides Shotgun* log.

---

# 12. One caveat: average QP isn't everything

I think your rule is useful, but I would **not make QP the only quality metric**.

AQ deliberately gives different blocks different QPs; x265 documentation explicitly notes that AQ changes block quantization based on spatial complexity, and higher AQ strength can cause substantial QP offsets. ([x265 Documentation][4])

So two encodes with:

```text
Avg QP = 19
```

are not necessarily visually identical.

Long-term, I would add one more quality check:

> **VMAF / SSIM / XPSNR against the source**

but I would still keep your QP criterion as the first gate because it matches your preservation philosophy very well.

---

## The system I'd recommend for you

Your automatic selector should therefore work like this:

```text
                    Source movie
                         │
                         ▼
              Select 8–12 sample clips
                         │
                         ▼
              Encode initial CRF samples
                         │
                 ┌───────┴───────┐
                 ▼               ▼
             Measure QP       Measure bitrate
                 │               │
                 └───────┬───────┘
                         ▼
                  Fit local model
                         │
                         ▼
                  Predict sweet CRF
                         │
                         ▼
                 Verification encode
                         │
                         ▼
             ┌───────────┴───────────┐
             │                       │
       QP below limit?              No
             │                       │
            Yes               Lower CRF
             │
             ▼
     Bitrate normal range?
          /        \
        yes        no
        │           │
     ACCEPT      Grain/noise?
                    │
               up to 28/25 Mbps
                    │
                  ACCEPT
```

And I would make the **automatic target**:

> **x264: QP95 ≈ 19.0–19.5**
> **x265: QP95 ≈ 20.0–20.5**

rather than trying to guess a universal CRF.

That gives you what I think you're really after: **the highest-compression CRF that still stays comfortably inside your own definition of transparent quality**.

If you implement this in Python, it can launch HandBrakeCLI samples, parse the x264/x265 logs, fit the CRF→QP/bitrate curves, and return a recommended CRF automatically. That would be a much more reliable system for your ripping workflow than my manually assigning CRFs from the movie's cinematography alone.

[1]: https://x265.readthedocs.io/en/stable/cli.html?utm_source=chatgpt.com "Command Line Options — x265 documentation"
[2]: https://handbrake.fr/docs/en/1.6.0/workflow/adjust-quality.html?utm_source=chatgpt.com "HandBrake Documentation — Adjusting quality"
[3]: https://handbrake.fr/docs/en/latest/cli/command-line-reference.html?utm_source=chatgpt.com "HandBrake Documentation — Command line reference"
[4]: https://x265.readthedocs.io/en/latest/cli.html?highlight=hevc-aq&utm_source=chatgpt.com "Command Line Options — x265 documentation"

## Implemented PyAV analyzer

The implementation is `scripts/crf_search.py`, using PyAV's FFmpeg libraries
directly for video decoding and x264/x265 encoding. It does not invoke an
FFmpeg, FFprobe, or HandBrake executable. Install project dependencies with
`uv sync`, or let `uv run` install them on the first run. The installed PyAV
build must include the requested encoder and pixel format; the analyzer checks
these before processing samples. The project targets the tested PyAV 18 API.

Copy `crf_search.example.json` to `crf_search.json` and edit it for the movie:

```sh
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json
```

Both codecs run by default. Use `--codec x264` or `--codec x265` to select one.
Omitting `--config` uses the same defaults as the example. This is a standalone
analyzer; it does not change the existing HandBrake release pipeline.
The default runtime target is 180 seconds, with a maximum of 300 seconds for
the combined analysis. Short, calibrated samples provide preliminary estimates
within this budget; a complete table is not guaranteed for slow encoders.

### Encoder configuration

The example includes these exact starting parameters:

```text
x264: deblock=-3,-3:bframes=10:rc-lookahead=60:aq-strength=0.85:me=umh:merange=64:psy-rd=0.85,0.10:qcomp=0.70:vbv-bufsize=30000:vbv-maxrate=40000

x265: deblock=-3,-3:ctu=32:rskip=2:early-skip=1:tu-inter-depth=3:tu-intra-depth=3:rect=0:amp=0:cbqpoffs=-2:crqpoffs=-2:me=hex:subme=7:merange=32:ref=5:max-merge=3:keyint=250:min-keyint=1:bframes=10:aq-mode=2:aq-strength=0.90:pbratio=1.2:rd=4:psy-rd=2.0:psy-rdoq=1.0:rdoq-level=2:rc-lookahead=60:lookahead-slices=0:scenecut=40:qcomp=0.68
```

`codecs.x264` and `codecs.x265` each contain:

| Field | Meaning |
| --- | --- |
| `preset` | Fixed encoder preset; default `placebo` for x264 and `slower` for x265. |
| `tune` | Optional encoder tune; default `null`. |
| `pixel_format` | `yuv420p` for x264 or `yuv420p10le` for x265. |
| `profile` | `high` for x264 or `main10` for x265. |
| `level` | String level or `null`; defaults to `"4.1"` for x264 and automatic for x265. |
| `params` | Encoder-specific parameters, as a colon-separated `key=value` string or a JSON object. |
| `options` | Additional PyAV codec AVOptions as a JSON object, such as `{"threads": 4}`. |
| `initial_crf` | Starting point within the automatic search bounds; defaults 17.5 / 18.5. |
| `qp_target` | Selection safety target; defaults 19.5 / 20.5. |
| `qp_limit` | Threshold for labelling a failed trial as over the limit; defaults 20 / 21. |
| `normal_bitrate_mbps` | Preferred video bitrate interval; defaults `[8,15]` / `[5,10]`. |
| `emergency_bitrate_mbps` | Warning threshold; defaults 28 / 25. |

Before any encoding, the analyzer prints each selected codec's preset,
profile, level, pixel format, tune, encoder parameters, and additional codec
AVOptions. These remain fixed during calibration and all CRF trials.

Partial JSON configurations merge with defaults. **Providing `params` or
`options` replaces that entire dictionary**, allowing defaults to be removed:
`"params": {}` clears all custom encoder parameters while retaining the preset.
To adjust just one of the supplied arguments, copy the complete example and
edit its parameter string, or supply a complete replacement mapping. Values in
parameter mappings may be strings, numbers, or booleans; booleans become `1`/`0`.
For example, these two complete replacement values are equivalent:

```json
"params": "bframes=10:aq-strength=0.85"
```

```json
"params": {"bframes": 10, "aq-strength": 0.85}
```

Use encoder parameter names without leading dashes. `options` contains codec
AVOptions, not FFmpeg command-line arguments. The analyzer reserves CRF,
constant-QP/bitrate and multipass controls, timing, core format settings, and
statistics/logging fields to ensure every measurement uses the requested CRF
and comparable sample boundaries. Put preset, tune, profile, and level in their
dedicated fields. Unknown schema keys, malformed times, nonfinite values, and
inconsistent numeric ranges are errors. Encoder-specific options remain subject
to the capabilities of the installed libraries.

### Sampling and sweeps

| Config field | Default and behavior |
| --- | --- |
| `video.stream` | `0`: zero-based index among video streams. Other stream types are ignored. |
| `video.crop` | `"auto"` detects black margins before encoding; `null` disables cropping. A fixed `width:height:x:y` overrides detection. Width and height must be positive even integers; x/y offsets are nonnegative integers and may be odd. |
| `video.cropdetect.limit` | `0.09411764705882353` (`24/255`): normalized raw luminance threshold, scaled to the video's bit depth; must be greater than 0 and less than 1. |
| `video.cropdetect.seconds` | `2.0`: maximum scan duration centered within each representative and stress sample; must be positive. |
| `sampling.count` | `10` representative windows centered in equally sized time intervals. |
| `sampling.seconds` | `6.0`: maximum seconds per window; calibrated encoding speed can shorten it. Short inputs reduce count and then duration as needed. |
| `sampling.start` / `sampling.end` | `0` / `null` (video end); both are relative to the video's start. |
| `sampling.stress_starts` | `[]`; extra known difficult scenes, each using the configured sample duration or remaining video duration. |
| `search.min_crf` / `search.max_crf` | `14.0` / `23.0`; automatic search bounds within 0–51. |
| `search.precision` | `0.1`; refinement step, at least 0.01 and no larger than the search interval. |
| `search.max_trials` | `6` per codec; integer of at least 3, subject to the shared runtime budget. |
| `runtime.target_seconds` | `180.0`: soft runtime target, checked before additional CRF trials after each codec's first attempt. |
| `runtime.max_seconds` | `300.0`: maximum runtime; stops an active worker at the deadline. Must be at least `target_seconds`. |

Times accept seconds, `MM:SS`, or `HH:MM:SS`, including fractional seconds.
Representative sampling spans the full video by default; it does not detect
credits automatically. Override config sampling for a run with `--samples`,
`--sample-seconds`, `--start`, `--end`, and repeatable `--stress-start`. Command-line
stress starts replace the config's stress list. `--crop auto`, `--crop none`,
`--crop width:height:x:y`, or `--no-crop` override the configured crop mode;
`--crop` and `--no-crop` are mutually exclusive. `--video-stream` also overrides
the corresponding config value. For example:

```sh
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --codec x265 --start 00:05:00 --end 01:45:00 --stress-start 00:37:15 --stress-start 01:12:00
```

Stress starts may be outside the representative interval but must lie inside
the video. All CRFs reuse the same representative and stress sample ranges.
The analyzer preserves presentation timing and measures only the chosen video
stream. Resolution remains at the source size apart from the resolved crop.

### Runtime budget and calibration

The runtime budget is shared by all selected codecs and includes source
inspection, crop detection, calibration, and CRF trials. Complete CRF trials
alternate between codecs, with reserved shares of the remaining maximum
budget so each can attempt its first trial even after the soft target. The
target gates additional trials; a finished codec's unused share is distributed
to the others. The maximum stops active work at the deadline.
Both values must be positive finite seconds, with the target no greater than
the maximum. Override them for one run with:

```sh
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --target-seconds 120 --max-seconds 180
```

Supplying `--max-seconds` alone also lowers the existing target when necessary.

Calibration encodes a short sample using the selected settings to measure
speed. Its duration accounts for encoder lookahead and B-frame buffering:
at least `max(3, (rc-lookahead + bframes + 1) / fps)` seconds, capped by the
configured sample duration and available source interval. The measured speed
determines a common sample length no greater than `sampling.seconds`. This
final length and the sample ranges stay fixed across all CRFs and codecs.
The analyzer does not change presets or custom parameters to fit the budget.
Calibration is skipped when the requested or available clip length is already
at or below this minimum. A resumed run with the same source and config reuses
its previously fixed calibrated sample plan.

Short samples are preliminary estimates: they contain proportionally more GOP
startup and less scene coverage than longer encodes. The runtime limit may
arrive before any complete CRF row is available, especially with expensive
presets or large frames. Only completed trials contribute table rows and
recommendations. The report records `time_limit` when the budget stops work
and preserves completed results; an unfinished trial is not extrapolated into
a measured row. Increase the runtime budget and sample-duration limit when
longer measurements are needed.

### Automatic black-margin crop

With the default `video.crop: "auto"`, crop detection runs once before the CRF
trials. PyAV decodes frames in a short window centered within each
representative and stress sample; the default is up to two seconds per window.
FFmpeg's `bbox` filter, called through PyAV, finds the bounds of pixels above
the configured raw luminance threshold, scaled to the video's bit depth as
`floor(limit * (2**bit_depth - 1))`. The default threshold is 24 for 8-bit video
and 96 for 10-bit video; pixels at or below it count as black. Planar YUV and
grayscale inputs retain their native bit depth. Unsupported formats or formats
without a luminance plane retain the full frame. This requires no external
executable or FFmpeg `cropdetect` filter.

The detector takes the exact union of all usable content rectangles and keeps
its width, height, and x/y offsets unchanged. The resulting crop retains content
seen across the inspected scenes, including changes of aspect ratio. If no
usable bounds are found, the combined bounds cover the full frame, or the
detected rectangle is suspiciously small, it retains the full frame. The
automatic crop must retain at least half the original area and at least half
of each original dimension. This is a conservative sample-based decision: a
scene outside the inspected windows can still contain content nearer an edge.

Crop coordinates are exact: detected `1920:804:0:137` produces 1920×804 video
at offset x=0, y=137. Odd offsets are allowed. Width and height must be even for
the configured 4:2:0 encoders; an odd detected width or height causes a clear
error before encoding. The analyzer does not round dimensions, add padding,
or discard pixels to meet that requirement. Supply an explicit manual crop
with even dimensions when a different rectangle is intended.

The resolved crop and dimensions are displayed before encoding. Both codecs
and every CRF use that same crop. `results.json` retains the requested crop
mode in `config.video.crop` and records the resolved decision separately under
`crop_detection`, including `mode`, `crop`, `width`, `height`, and `reason`.
Automatic detection is identified as `detector: "pyav-bbox-luma"`.

To supply known bounds or retain the whole frame:

```sh
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --crop 1920:804:0:137
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --no-crop
```

The equivalent config values are `"crop": "1920:804:0:137"` and `"crop": null`.
`--crop none` is equivalent to `--no-crop`; `--crop auto` re-enables detection
for a config that otherwise disables it.

### Explicit CRF sweeps

To request a measured relationship table at chosen CRFs, use one of:

```sh
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --codec x265 --crf-values 16,17,18,19,20,21,22
uv run python scripts/crf_search.py "movie.mkv" --config crf_search.json --codec x264 --crf-range 16:22:0.5
```

The range includes its maximum if the step reaches it exactly. Sweep values
must be within 0–51; they are independent of the automatic search's configured
bounds and trial limit. `--crf-values` and `--crf-range` are mutually exclusive.
The global runtime budget also applies to sweeps, so a sweep can finish with
only the rows completed before the deadline.

### Selection policy and outputs

Each sample's QP is the maximum of the encoder-reported average I/P/B QPs that
exist in that sample. QP95 is the linearly interpolated 95th percentile of the
representative sample QPs. A trial passes only when **representative QP95 is at
or below `qp_target` and every extra stress sample is also at or below that
target**. The report also shows the worst representative sample separately.
The `qp_limit` field labels failures; the stricter `qp_target` drives selection.
These are average-QP statistics, not per-frame or per-block upper limits, and
do not guarantee visual transparency.

Automatic search tests the initial CRF and nearby values, expands toward a
pass/fail bracket, and uses local QP interpolation to choose further real
encodes. It stops at the requested precision, a bound, the trial limit, the
runtime budget, or observed nonmonotonic QP behavior. The recommendation is always the **highest
tested passing CRF**, and may be absent if no tested value passed. Reaching a
bound, trial limit, or runtime limit does not establish a global optimum. The same encoder
settings and preset apply to every trial; only CRF changes.

Normal and emergency bitrate values are preferences and warnings, not hard
caps. High bitrate does not establish the presence of grain, so the report
uses “high bitrate exception” instead of classifying the source as grainy.
No bitrate preference can make a failed QP trial pass.

Measured video bitrate is `8 * encoded_packet_bytes / presentation_seconds`,
including delayed packets emitted when the encoder flushes. It excludes audio,
subtitles, and container overhead. The combined representative bitrate uses
total packet bytes divided by total sample presentation duration; stress
samples are excluded from this calculation. This is a measured sample bitrate
and an estimate of full-movie video bitrate, specific to the source, settings,
and sample selection.

The default output directory is `<movie-stem>.crf-search` beside the movie:

- `summary.csv`: measured CRF/bitrate/QP table and recommendation flags.
- `results.json`: source and encoder/library information, normalized config,
  resolved crop detection, sample plan, individual measurements, recommendation,
  search outcome, and
  a descriptive log-linear bitrate fit when at least three CRFs are measured.
- `logs/`: individual encoder logs for diagnosis.
- `cache/`: completed sample results, reused only for matching source identity,
  encoder settings, sample ranges, and library/backend versions.

Use `--output-dir` to choose another directory. Completed samples are cached
through interruption; rerun the command to resume. Matching resumed runs reuse
the fixed sample plan without recalibration. `--no-resume` forces fresh sample
encodes. Changing the budget can change the calibrated sample ranges; only
samples matching those ranges can be reused. Existing reports in the chosen directory are updated for the
current run, so use separate output directories to retain multiple reports.
The relationship fit is labelled as a prediction model and never replaces a
measured table row or verification encode.
