"""Per-plot cross-correlation alignment engine.

Finds the optimal (dx, dy) translation for each plot by cross-correlating
the rasterised plot boundary against edge features extracted from satellite
imagery and the pre-computed boundary hints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.windows import from_bounds
from scipy import signal as sp_signal
from shapely.geometry.base import BaseGeometry

from bhume.geo import (
    Patch,
    geom_to_imagery_crs,
    open_imagery,
    patch_for_plot,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AlignmentResult:
    """Result of aligning a single plot."""
    plot_number: str
    dx_m: float            # shift in x (east) in metres
    dy_m: float            # shift in y (north) in metres
    ncc_peak: float        # peak normalised cross-correlation score
    peak_sharpness: float  # ratio of peak to second-best peak region
    search_succeeded: bool # whether xcorr found a believable peak


# ---------------------------------------------------------------------------
# Edge extraction
# ---------------------------------------------------------------------------

def _canny_edges(img: np.ndarray, low: int = 30, high: int = 100) -> np.ndarray:
    """Extract Canny edges from an RGB image → float32 [0, 1]."""
    gray = np.mean(img.astype(np.float32), axis=2)
    gray_u8 = np.clip(gray, 0, 255).astype(np.uint8)
    edges = cv2.Canny(gray_u8, low, high)
    return edges.astype(np.float32) / 255.0


def _read_boundary_patch(
    bnd: rasterio.DatasetReader,
    geom_4326: BaseGeometry,
    pad_m: float,
) -> np.ndarray:
    """Read the single-band boundaries.tif patch around *geom_4326*."""
    from pyproj import Transformer

    tf = Transformer.from_crs("EPSG:4326", bnd.crs, always_xy=True)
    from shapely.ops import transform as shp_transform

    geom_proj = shp_transform(lambda xs, ys, z=None: tf.transform(xs, ys), geom_4326)
    minx, miny, maxx, maxy = geom_proj.bounds
    left = max(minx - pad_m, bnd.bounds.left)
    bottom = max(miny - pad_m, bnd.bounds.bottom)
    right = min(maxx + pad_m, bnd.bounds.right)
    top = min(maxy + pad_m, bnd.bounds.top)

    if right <= left or top <= bottom:
        return np.zeros((1, 1), dtype=np.float32)

    window = from_bounds(left, bottom, right, top, transform=bnd.transform)
    data = bnd.read(1, window=window)
    return data.astype(np.float32) / 255.0


# ---------------------------------------------------------------------------
# Template rasterisation
# ---------------------------------------------------------------------------

def _rasterize_boundary(
    geom_in_crs: BaseGeometry,
    transform,
    shape: tuple[int, int],
    buffer_px: float = 1.5,
) -> np.ndarray:
    """Rasterise a polygon *boundary* into a float32 edge mask."""
    if geom_in_crs.geom_type == "MultiPolygon":
        boundaries = [p.boundary.buffer(buffer_px) for p in geom_in_crs.geoms]
    elif geom_in_crs.geom_type == "Polygon":
        boundaries = [geom_in_crs.boundary.buffer(buffer_px)]
    else:
        boundaries = [geom_in_crs.buffer(buffer_px)]

    if not boundaries:
        return np.zeros(shape, dtype=np.float32)

    return rasterize(
        [(b, 1.0) for b in boundaries],
        out_shape=shape,
        transform=transform,
        fill=0.0,
        dtype=np.float32,
    )


# ---------------------------------------------------------------------------
# Cross-correlation
# ---------------------------------------------------------------------------

def _xcorr_shift(
    template: np.ndarray,
    target: np.ndarray,
    max_shift_px: int = 30,
) -> tuple[int, int, float, float]:
    """FFT cross-correlate *template* against *target*.

    Returns (dy_px, dx_px, ncc_peak, peak_sharpness).
    """
    t_std = template.std()
    if t_std < 1e-6 or template.size == 0:
        return 0, 0, 0.0, 0.0

    # Normalise template
    template_norm = (template - template.mean()) / t_std

    # Pad target so we can search ±max_shift_px
    pad = max_shift_px
    target_padded = np.pad(target, pad, mode="constant", constant_values=0)

    # Full cross-correlation via FFT
    result = sp_signal.fftconvolve(
        target_padded, template_norm[::-1, ::-1], mode="same"
    )

    # Extract the search window around the centre
    cy, cx = target_padded.shape[0] // 2, target_padded.shape[1] // 2
    sr = result[
        cy - max_shift_px : cy + max_shift_px + 1,
        cx - max_shift_px : cx + max_shift_px + 1,
    ]

    if sr.size == 0:
        return 0, 0, 0.0, 0.0

    peak_idx = np.unravel_index(np.argmax(sr), sr.shape)
    dy_px = int(peak_idx[0] - max_shift_px)
    dx_px = int(peak_idx[1] - max_shift_px)
    peak_val = float(sr[peak_idx])

    # Normalise by number of edge pixels (not total area) for size-invariance
    edge_count = np.sum(template > 0.1)
    ncc_peak = peak_val / max(edge_count, 1)

    # Peak sharpness: ratio of peak to the mean of the search region
    sr_mean = sr.mean()
    sr_std = sr.std()
    if sr_std > 1e-8:
        peak_sharpness = float((peak_val - sr_mean) / sr_std)
    else:
        peak_sharpness = 0.0

    return dy_px, dx_px, ncc_peak, peak_sharpness


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def align_plot(
    src: rasterio.DatasetReader,
    bnd: Optional[rasterio.DatasetReader],
    geom_4326: BaseGeometry,
    plot_number: str,
    pad_m: float = 80.0,
    max_shift_px: int = 30,
) -> AlignmentResult:
    """Compute the optimal (dx, dy) shift for a single plot.

    Parameters
    ----------
    src : open rasterio dataset for imagery.tif
    bnd : open rasterio dataset for boundaries.tif (or None)
    geom_4326 : official plot geometry in EPSG:4326
    plot_number : plot identifier
    pad_m : padding in metres around the plot for the search patch
    max_shift_px : maximum search radius in pixels

    Returns
    -------
    AlignmentResult with shift in metres and quality scores.
    """
    # 1. Extract imagery patch and compute Canny edges
    try:
        patch = patch_for_plot(src, geom_4326, pad_m=pad_m)
    except (ValueError, Exception):
        return AlignmentResult(plot_number, 0, 0, 0, 0, False)

    edges = _canny_edges(patch.image)

    # 2. Read boundary hints (if available) and combine
    if bnd is not None:
        bnd_edge = _read_boundary_patch(bnd, geom_4326, pad_m=pad_m)
        # Resize to match imagery patch if different
        if bnd_edge.shape != edges.shape:
            bnd_edge = cv2.resize(
                bnd_edge, (edges.shape[1], edges.shape[0]),
                interpolation=cv2.INTER_LINEAR,
            )
        combined = np.maximum(edges, bnd_edge * 0.8)
    else:
        combined = edges

    # 3. Rasterise the official boundary as a template
    geom_proj = geom_to_imagery_crs(src, geom_4326)
    template = _rasterize_boundary(geom_proj, patch.transform, edges.shape)

    # 4. Cross-correlate
    dy_px, dx_px, ncc_peak, sharpness = _xcorr_shift(
        template, combined, max_shift_px=max_shift_px,
    )

    # 5. Convert pixel shift → metres
    res = abs(src.res[0])  # metres per pixel
    dx_m = float(dx_px * res)
    dy_m = float(-dy_px * res)  # image row grows downward

    # Sanity: if NCC is extremely weak or template too sparse, mark as failed
    template_coverage = float(np.sum(template > 0.5)) / template.size if template.size > 0 else 0
    succeeded = (
        ncc_peak > 0.10
        and sharpness > 1.5
        and template_coverage > 0.005  # template needs enough edge pixels
    )

    return AlignmentResult(
        plot_number=plot_number,
        dx_m=dx_m,
        dy_m=dy_m,
        ncc_peak=ncc_peak,
        peak_sharpness=sharpness,
        search_succeeded=succeeded,
    )


def align_all_plots(village, pad_m: float = 80.0, max_shift_px: int = 30) -> list[AlignmentResult]:
    """Run two-pass per-plot alignment on every plot in the village.

    Pass 1: Align every plot from its official position.
    Pass 2: Estimate a robust global shift from high-confidence results,
            then re-align failed/low-quality plots from the globally-shifted
            position (tighter search window). This recovers plots where the
            initial search missed the true position.

    Returns a list of AlignmentResult, one per plot.
    """
    from shapely.affinity import translate as shp_translate

    results: list[AlignmentResult] = []

    bnd_path = village.boundaries_path
    with open_imagery(village.imagery_path) as src:
        bnd_ctx = rasterio.open(str(bnd_path)) if bnd_path else None
        try:
            total = len(village.plots)

            # --- Pass 1: raw per-plot alignment ---
            for i, pn in enumerate(village.plots.index):
                if (i + 1) % 500 == 0 or i == 0:
                    print(f"  [pass 1] aligning plot {i+1}/{total} ...")
                geom = village.plot(pn)
                ar = align_plot(
                    src, bnd_ctx, geom, str(pn),
                    pad_m=pad_m, max_shift_px=max_shift_px,
                )
                results.append(ar)

            # --- Estimate robust global shift from top-quality results ---
            good = [r for r in results if r.search_succeeded and r.peak_sharpness > 2.5]
            if len(good) >= 10:
                dxs = [r.dx_m for r in good]
                dys = [r.dy_m for r in good]
                global_dx = float(np.median(dxs))
                global_dy = float(np.median(dys))
                print(f"  global shift estimate: dx={global_dx:.1f}m, dy={global_dy:.1f}m "
                      f"(from {len(good)} high-quality plots)")
            else:
                global_dx, global_dy = 0.0, 0.0
                print("  insufficient high-quality plots for global shift estimate")

            # --- Pass 2: re-align failed/low-quality plots from global shift ---
            redo_indices = [
                i for i, r in enumerate(results)
                if not r.search_succeeded or r.peak_sharpness < 2.0
            ]

            if redo_indices and (abs(global_dx) > 1 or abs(global_dy) > 1):
                print(f"  [pass 2] re-aligning {len(redo_indices)} plots "
                      f"from global shift ...")
                for idx in redo_indices:
                    pn = results[idx].plot_number
                    geom = village.plot(pn)
                    # Pre-shift geometry by global offset, then search locally
                    # We need to work in a projected CRS for metre shifts
                    from pyproj import Transformer
                    from shapely.ops import transform as shp_transform
                    tf_fwd = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                    tf_rev = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
                    geom_proj = shp_transform(lambda x, y, z=None: tf_fwd.transform(x, y), geom)
                    geom_shifted = shp_translate(geom_proj, global_dx, global_dy)
                    geom_shifted_4326 = shp_transform(lambda x, y, z=None: tf_rev.transform(x, y), geom_shifted)

                    ar2 = align_plot(
                        src, bnd_ctx, geom_shifted_4326, str(pn),
                        pad_m=pad_m, max_shift_px=max_shift_px,
                    )
                    # Total shift = global + residual
                    ar2 = AlignmentResult(
                        plot_number=ar2.plot_number,
                        dx_m=global_dx + ar2.dx_m,
                        dy_m=global_dy + ar2.dy_m,
                        ncc_peak=ar2.ncc_peak,
                        peak_sharpness=ar2.peak_sharpness,
                        search_succeeded=ar2.search_succeeded,
                    )
                    # Keep whichever pass gave a better result
                    old = results[idx]
                    if ar2.ncc_peak > old.ncc_peak and ar2.search_succeeded:
                        results[idx] = ar2

                improved = sum(1 for i in redo_indices if results[i].search_succeeded)
                print(f"  pass 2 recovered: {improved}/{len(redo_indices)} plots now succeeded")

        finally:
            if bnd_ctx is not None:
                bnd_ctx.close()

    return results

