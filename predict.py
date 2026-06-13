#!/usr/bin/env python3
"""
BhuMe Boundary Correction Pipeline
====================================
Processes a village bundle end-to-end:
  1. Load village data (plots, imagery, boundary hints)
  2. Per-plot cross-correlation alignment
  3. Multi-signal confidence scoring + corrected/flagged decisions
  4. Write predictions.geojson
  5. Self-score against example truths

Usage:
    python predict.py data/34855_vadnerbhairav_chandavad_nashik
    python predict.py data/12429_malatavadi_chandgad_kolhapur
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.affinity import translate

from bhume import load, score, write_predictions
from bhume.matcher import align_all_plots
from bhume.calibration import compute_decisions


def predict(village_dir: str) -> None:
    """Run the full prediction pipeline on a village."""

    t0 = time.time()
    village = load(village_dir)
    n_truth = 0 if village.example_truths is None else len(village.example_truths)
    print(f"Loaded {village.slug}")
    print(f"  {len(village.plots)} plots | {n_truth} example truths | "
          f"boundaries={'yes' if village.boundaries_path else 'none'}")
    print()

    # ------------------------------------------------------------------
    # Stage 1+2: Per-plot cross-correlation alignment
    # ------------------------------------------------------------------
    print("Stage 1: Per-plot cross-correlation alignment ...")
    alignments = align_all_plots(
        village,
        pad_m=80.0,
        max_shift_px=30,
    )
    t1 = time.time()
    print(f"  aligned {len(alignments)} plots in {t1 - t0:.1f}s")
    succeeded = sum(1 for a in alignments if a.search_succeeded)
    print(f"  xcorr succeeded: {succeeded}/{len(alignments)}")
    print()

    # ------------------------------------------------------------------
    # Stage 3: Confidence scoring + corrected/flagged decisions
    # ------------------------------------------------------------------
    print("Stage 2: Confidence scoring and decisions ...")
    decisions = compute_decisions(alignments, village)
    n_corrected = sum(1 for d in decisions if d.status == "corrected")
    n_flagged = sum(1 for d in decisions if d.status == "flagged")
    print(f"  corrected: {n_corrected} | flagged: {n_flagged}")

    if n_corrected > 0:
        confs = [d.confidence for d in decisions if d.status == "corrected"]
        print(f"  confidence range: [{min(confs):.3f}, {max(confs):.3f}], "
              f"median={np.median(confs):.3f}")
    print()

    # ------------------------------------------------------------------
    # Build predictions GeoDataFrame
    # ------------------------------------------------------------------
    print("Stage 3: Building predictions ...")

    # Project to UTM for applying metre-based shifts to corrected plots
    utm_epsg = _utm_for(village.plots.geometry.iloc[0])
    plots_u = village.plots.to_crs(utm_epsg)

    corrected_rows = []  # will be in UTM, then reprojected
    flagged_rows = []    # stay in original EPSG:4326 (exact coordinates)

    for d in decisions:
        pn = d.plot_number

        # Guard confidence against NaN
        conf = d.confidence if d.status == "corrected" else None
        if conf is not None and (np.isnan(conf) or conf < 0 or conf > 1):
            conf = 0.5  # fallback safe value

        if d.status == "corrected":
            geom_u = plots_u.loc[pn, "geometry"]
            shifted = translate(geom_u, d.dx_m, d.dy_m)
            # Fix invalid geometries (self-intersections from rounding)
            if not shifted.is_valid:
                shifted = shifted.buffer(0)
            corrected_rows.append({
                "plot_number": pn,
                "status": "corrected",
                "confidence": round(conf, 3),
                "method_note": d.method_note,
                "geometry": shifted,
            })
        else:
            # Flagged: use EXACT original geometry (no CRS round-trip)
            original_geom = village.plots.loc[pn, "geometry"]
            flagged_rows.append({
                "plot_number": pn,
                "status": "flagged",
                "confidence": None,
                "method_note": d.method_note,
                "geometry": original_geom,
            })

    # Build separate GeoDataFrames and combine
    parts = []
    if corrected_rows:
        gdf_corr = gpd.GeoDataFrame(corrected_rows, crs=utm_epsg)
        gdf_corr = gdf_corr.to_crs("EPSG:4326")
        parts.append(gdf_corr)
    if flagged_rows:
        gdf_flag = gpd.GeoDataFrame(flagged_rows, crs="EPSG:4326")
        parts.append(gdf_flag)

    import pandas as pd
    preds = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    preds = preds.set_index("plot_number", drop=False)

    # Write predictions
    out_path = Path(village_dir) / "predictions.geojson"
    write_predictions(out_path, preds)
    print(f"  wrote {len(preds)} predictions -> {out_path}")
    print()

    # ------------------------------------------------------------------
    # Self-score against example truths
    # ------------------------------------------------------------------
    if village.example_truths is not None:
        print("Self-score against example truths:")
        print(score(preds, village))
    else:
        print("No example truths available for scoring.")

    t2 = time.time()
    print(f"\nTotal time: {t2 - t0:.1f}s")


def _utm_for(geom) -> str:
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python predict.py data/<village_slug>")
        print("  e.g. python predict.py data/34855_vadnerbhairav_chandavad_nashik")
        sys.exit(1)
    predict(sys.argv[1])
