# PGS subtitle conversion

`bdrip subtitles` converts `.sup` PGS bitmap subtitles to SRT using PaddleOCR.
The existing conversion is organized under `src/bdrip/subtitles/`: bitmap
decoding, preprocessing/recognition, cache persistence, and SRT output are
separate modules.

OCR still uses the separate dependencies in `requirements_paddle_pgs.txt` and
a compatible PaddlePaddle inference runtime for the selected CPU/GPU. That
runtime and its downloaded OCR models are not bundled with the CRF program.
The package reorganization does not change the existing OCR installation.
Once that environment and this project are installed:

```sh
bdrip subtitles input.sup output.srt --lang ch --device cpu
bdrip subtitles --help
```

The default language is Simplified Chinese (`ch`); use `chinese_cht` for
Traditional Chinese or `en` for English. Output defaults to the input filename
with an `.srt` extension. `--overwrite` allows replacing an existing SRT.

Conversion caches OCR candidates in `<output-stem>.ocr-cache.jsonl` and writes
a review report to `<output-stem>.review.tsv`. Options control preprocessing
variants, batching, confidence thresholds, debug images, and cue merging.
Use `bdrip subtitles` in place of the removed `scripts/pgs2srt.py` launcher;
its conversion arguments remain the same.
