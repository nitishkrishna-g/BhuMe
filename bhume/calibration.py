"""Multi-signal confidence estimation and corrected/flagged decision logic.

Combines cross-correlation quality, area-ratio validation, spatial coherence,
and plot-size heuristics into a single calibrated confidence score that
tracks actual alignment accuracy (the metric BhuMe watches most).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry.base import BaseGeometry

from bhume.matcher import AlignmentResult


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Area-ratio thresholds: outside these, shape mismatch is likely structural
AREA_RATIO_FLAG_LO = 0.45
AREA_RATIO_FLAG_HI = 2.2

# Area-ratio "safe" band — plots here get a bonus
AREA_RATIO_GOOD_LO = 0.80
AREA_RATIO_GOOD_HI = 1.20

# Minimum NCC to consider a match trustworthy (edge-pixel-normalised scale)
NCC_FLOOR = 0.15

# Shift magnitude below which a plot might already be correct
NEAR_ZERO_SHIFT_M = 3.0

# Minimum confidence to mark as "corrected" rather than "flagged"
CONFIDENCE_FLAG_THRESHOLD = 0.25


# ---------------------------------------------------------------------------
# Per-plot confidence signals
# ---------------------------------------------------------------------------

def _area_ratio(row) -> float | None:
    """Compute drawn_area / total_recorded_area for a plot row."""
    map_area = row.get("map_area_sqm")
    rec_area = row.get("recorded_area_sqm")
    pot_kharaba = row.get("pot_kharaba_ha")

    if map_area is None or rec_area is None:
        return None
    if np.isnan(map_area) or np.isnan(rec_area):
        return None

    total_rec = float(rec_area)
    if pot_kharaba is not None and not np.isnan(pot_kharaba):
        total_rec += float(pot_kharaba) * 10_000  # ha → m²

    if total_rec <= 0:
        return None

    return float(map_area) / total_rec


def _area_ratio_score(ratio: float | None) -> float:
    """Convert area ratio into a 0-1 score (1 = perfect match)."""
    if ratio is None:
        return 0.4  # unknown → cautious middle

    if ratio < AREA_RATIO_FLAG_LO or ratio > AREA_RATIO_FLAG_HI:
        return 0.0  # structural mismatch

    # Linear ramp from edges of flag zone to good zone
    if AREA_RATIO_GOOD_LO <= ratio <= AREA_RATIO_GOOD_HI:
        return 1.0

    if ratio < AREA_RATIO_GOOD_LO:
        return max(0.0, (ratio - AREA_RATIO_FLAG_LO) / (AREA_RATIO_GOOD_LO - AREA_RATIO_FLAG_LO))
    else:
        return max(0.0, (AREA_RATIO_FLAG_HI - ratio) / (AREA_RATIO_FLAG_HI - AREA_RATIO_GOOD_HI))


def _ncc_score(ncc_peak: float) -> float:
    """Map raw NCC peak to a 0-1 quality score (edge-pixel-normalised scale)."""
    # Good matches have NCC ~0.5-1.2, bad ones < 0.2
    if ncc_peak < NCC_FLOOR:
        return 0.0
    # Ramp: 0.15 -> 0, 1.0 -> 1.0, saturates at 1.0
    score = min(1.0, (ncc_peak - NCC_FLOOR) / 0.85)
    return float(score)


def _sharpness_score(peak_sharpness: float) -> float:
    """Map peak sharpness (z-score above mean) to 0-1 quality."""
    if peak_sharpness < 1.0:
        return 0.0
    return float(min(1.0, (peak_sharpness - 1.0) / 5.0))


def _size_score(map_area_sqm: float | None) -> float:
    """Larger plots -> more reliable xcorr. Ramp from 500 to 5000 m2."""
    if map_area_sqm is None or np.isnan(map_area_sqm):
        return 0.3
    if map_area_sqm < 300:
        return 0.0  # tiny plots have too few edge pixels for reliable xcorr
    if map_area_sqm < 500:
        return 0.1
    if map_area_sqm > 5000:
        return 1.0
    return float(0.1 + 0.9 * (map_area_sqm - 500) / 4500)


# ---------------------------------------------------------------------------
# Spatial coherence
# ---------------------------------------------------------------------------

def compute_spatial_coherence(
    centroids_xy: np.ndarray,
    shifts_xy: np.ndarray,
    k: int = 8,
) -> np.ndarray:
    """For each plot, measure how well its shift agrees with its k nearest neighbours.

    Returns an array of coherence scores in [0, 1] (1 = perfectly agrees).
    """
    n = len(centroids_xy)
    if n < 3:
        return np.ones(n, dtype=np.float64)

    tree = cKDTree(centroids_xy)
    k_actual = min(k + 1, n)  # +1 because query includes self
    _, indices = tree.query(centroids_xy, k=k_actual)

    coherence = np.zeros(n, dtype=np.float64)
    for i in range(n):
        nbrs = indices[i, 1:]  # exclude self
        nbrs = nbrs[nbrs < n]  # guard against edge cases
        if len(nbrs) == 0:
            coherence[i] = 0.5
            continue

        my_shift = shifts_xy[i]
        nbr_shifts = shifts_xy[nbrs]
        median_nbr = np.median(nbr_shifts, axis=0)

        # Distance of my shift from the local median
        diff = np.sqrt(np.sum((my_shift - median_nbr) ** 2))

        # Normalise: if diff < 3m → coherent, diff > 15m → incoherent
        coherence[i] = float(max(0.0, min(1.0, 1.0 - (diff - 3.0) / 12.0)))

    return coherence


# ---------------------------------------------------------------------------
# Combined confidence
# ---------------------------------------------------------------------------

@dataclass
class PlotDecision:
    """Final decision for a single plot."""
    plot_number: str
    status: str           # "corrected" or "flagged"
    confidence: float     # 0–1, meaningful
    dx_m: float
    dy_m: float
    method_note: str


def compute_decisions(
    alignments: list[AlignmentResult],
    village,
) -> list[PlotDecision]:
    """Combine all signals into per-plot corrected/flagged decisions.

    Parameters
    ----------
    alignments : per-plot AlignmentResult from matcher
    village : loaded Village object (for area data and centroids)
    """
    plots = village.plots

    # --- Compute spatial coherence across all plots ---
    utm_epsg = _utm_for(plots.geometry.iloc[0])
    plots_u = plots.to_crs(utm_epsg)

    centroids = np.array([
        [g.centroid.x, g.centroid.y] for g in plots_u.geometry
    ])
    shifts = np.array([[a.dx_m, a.dy_m] for a in alignments])
    coherence = compute_spatial_coherence(centroids, shifts, k=8)

    # --- Per-plot decision ---
    decisions: list[PlotDecision] = []

    for idx, ar in enumerate(alignments):
        pn = ar.plot_number
        row = plots.loc[pn] if pn in plots.index else None

        # Individual signal scores
        ncc_s = _ncc_score(ar.ncc_peak)
        sharp_s = _sharpness_score(ar.peak_sharpness)
        coh_s = float(coherence[idx])

        if row is not None:
            a_ratio = _area_ratio(row)
            area_s = _area_ratio_score(a_ratio)
            size_s = _size_score(row.get("map_area_sqm"))
        else:
            a_ratio = None
            area_s = 0.4
            size_s = 0.5

        # Weighted combination
        # NCC and sharpness are the primary signal (validated by prototype)
        # Area ratio is a strong structural check
        # Coherence is a secondary check but can mask failures on tiny plots
        # Size acts as a reliability gate
        raw_conf = (
            0.35 * ncc_s
            + 0.25 * sharp_s
            + 0.20 * area_s
            + 0.08 * coh_s
            + 0.12 * size_s
        )

        # Clamp to [0.05, 0.95] — avoid extreme overconfidence
        confidence = float(max(0.05, min(0.95, raw_conf)))

        # Tiny plots: cap confidence since xcorr has insufficient edge pixels
        plot_area = row.get("map_area_sqm") if row is not None else None
        if plot_area is not None and not np.isnan(plot_area) and plot_area < 500:
            confidence = min(confidence, 0.20)

        shift_mag = np.sqrt(ar.dx_m ** 2 + ar.dy_m ** 2)

        # --- Decision logic ---
        notes = []

        # Hard flag: structural area mismatch
        if a_ratio is not None and (a_ratio < AREA_RATIO_FLAG_LO or a_ratio > AREA_RATIO_FLAG_HI):
            status = "flagged"
            confidence = 0.0
            notes.append(f"area ratio {a_ratio:.2f} outside [{AREA_RATIO_FLAG_LO}, {AREA_RATIO_FLAG_HI}]")

        # Hard flag: no recorded area at all
        elif row is not None and (row.get("recorded_area_sqm") is None or
                                   (isinstance(row.get("recorded_area_sqm"), float) and
                                    np.isnan(row.get("recorded_area_sqm")))):
            status = "flagged"
            confidence = 0.0
            notes.append("no recorded area on file")

        # Hard flag: xcorr completely failed
        elif not ar.search_succeeded:
            status = "flagged"
            confidence = 0.0
            notes.append("cross-correlation failed")

        # Near-zero shift: plot might already be correct → restraint
        elif shift_mag < NEAR_ZERO_SHIFT_M:
            if confidence < 0.4:
                status = "flagged"
                notes.append(f"near-zero shift ({shift_mag:.1f}m), low confidence")
            else:
                status = "corrected"
                # Reduce confidence slightly — we're not very sure this needs moving
                confidence = confidence * 0.8
                notes.append(f"small shift ({shift_mag:.1f}m)")

        # Confidence too low → flag
        elif confidence < CONFIDENCE_FLAG_THRESHOLD:
            status = "flagged"
            notes.append(f"confidence {confidence:.2f} below threshold")

        # Normal corrected plot
        else:
            status = "corrected"
            notes.append(f"xcorr shift ({ar.dx_m:.1f}, {ar.dy_m:.1f})m")

        method_note = "; ".join(notes)
        if status == "corrected":
            method_note += f" | ncc={ar.ncc_peak:.4f} sharp={ar.peak_sharpness:.1f} coh={coh_s:.2f}"

        decisions.append(PlotDecision(
            plot_number=pn,
            status=status,
            confidence=round(confidence, 3),
            dx_m=ar.dx_m if status == "corrected" else 0.0,
            dy_m=ar.dy_m if status == "corrected" else 0.0,
            method_note=method_note,
        ))

    return decisions


def _utm_for(geom: BaseGeometry) -> str:
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"
