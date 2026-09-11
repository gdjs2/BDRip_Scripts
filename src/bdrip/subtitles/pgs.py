"""PGS subtitle conversion: pgs."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

import numpy as np
from PIL import Image

from .models import VisualEvent


def import_pgs_components():
    try:
        from sup2srt.sup_decoder import decode_display_set
        from sup2srt.sup_parser import DisplaySet, SupParser
    except ImportError as exc:
        raise RuntimeError(
            "sup2srt is not installed. Run:\n  python -m pip install sup2srt==0.1.3"
        ) from exc
    return DisplaySet, SupParser, decode_display_set


def materialize_display_sets(display_sets: Sequence[Any], DisplaySet: Any) -> list[Any]:
    """
    PGS may reuse an earlier palette or object in a later PCS. The upstream
    decoder expects each display set to contain its own PDS/ODS, so carry the
    most recently defined resources forward.
    """
    palette_cache: dict[int, Any] = {}
    object_cache: dict[int, Any] = {}
    latest_palette: Any | None = None
    output: list[Any] = []

    for ds in display_sets:
        for pds in ds.pds_list:
            palette_cache[pds.palette_id] = pds
            latest_palette = pds

        for ods in ds.ods_list:
            object_cache[ods.object_id] = ods

        if ds.pcs is None or not ds.pcs.objects:
            output.append(ds)
            continue

        palette = palette_cache.get(ds.pcs.palette_id, latest_palette)
        objects = [
            object_cache[obj.object_id]
            for obj in ds.pcs.objects
            if obj.object_id in object_cache
        ]

        effective = DisplaySet(
            pcs=ds.pcs,
            wds=ds.wds,
            pds_list=[palette] if palette is not None else [],
            ods_list=objects,
        )
        output.append(effective)

    return output


def select_forced_objects(ds: Any, DisplaySet: Any) -> Any:
    if ds.pcs is None:
        return ds

    forced_objects = [obj for obj in ds.pcs.objects if obj.forced]
    if not forced_objects:
        return DisplaySet(pcs=replace(ds.pcs, objects=[]))

    wanted = {obj.object_id for obj in forced_objects}
    return DisplaySet(
        pcs=replace(ds.pcs, objects=forced_objects),
        wds=ds.wds,
        pds_list=ds.pds_list,
        ods_list=[ods for ods in ds.ods_list if ods.object_id in wanted],
    )


def compose_decoded_images(decoded: Sequence[Any]) -> Image.Image | None:
    if not decoded:
        return None

    left = min(item.x for item in decoded)
    top = min(item.y for item in decoded)
    right = max(item.x + item.image.width for item in decoded)
    bottom = max(item.y + item.image.height for item in decoded)

    if right <= left or bottom <= top:
        return None

    canvas = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    for item in sorted(decoded, key=lambda d: (d.y, d.x)):
        canvas.alpha_composite(
            item.image.convert("RGBA"), (item.x - left, item.y - top)
        )

    return crop_alpha(canvas, threshold=1)


def crop_alpha(image: Image.Image, threshold: int) -> Image.Image | None:
    rgba = np.asarray(image.convert("RGBA"))
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > threshold)
    if len(xs) == 0:
        return None

    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    return image.crop((x1, y1, x2, y2))


def build_visual_events(
    display_sets: Sequence[Any],
    decode_display_set: Any,
    DisplaySet: Any,
    *,
    forced_only: bool,
    default_duration_ms: float,
    minimum_duration_ms: float,
) -> tuple[list[VisualEvent], int]:
    events: list[VisualEvent] = []
    decode_failures = 0

    pcs_indices = [i for i, ds in enumerate(display_sets) if ds.pcs is not None]

    for position, ds_index in enumerate(pcs_indices):
        ds = display_sets[ds_index]
        if not ds.pcs.objects:
            continue

        effective = select_forced_objects(ds, DisplaySet) if forced_only else ds
        if not effective.pcs.objects:
            continue

        start_ms = float(effective.pcs.pts_ms)

        if position + 1 < len(pcs_indices):
            next_ds = display_sets[pcs_indices[position + 1]]
            end_ms = float(next_ds.pcs.pts_ms)
        else:
            end_ms = start_ms + default_duration_ms

        if end_ms - start_ms < minimum_duration_ms:
            end_ms = start_ms + minimum_duration_ms

        decoded = decode_display_set(effective)
        rgba = compose_decoded_images(decoded)
        if rgba is None:
            decode_failures += 1
            continue

        is_forced = any(obj.forced for obj in effective.pcs.objects)
        events.append(
            VisualEvent(
                source_index=ds_index,
                start_ms=start_ms,
                end_ms=end_ms,
                rgba=rgba,
                forced=is_forced,
            )
        )

    return events, decode_failures
