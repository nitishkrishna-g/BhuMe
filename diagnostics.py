#!/usr/bin/env python3
"""
Generate a diagnostic report with shift distribution, confidence histograms,
and per-plot quality analysis. Useful for understanding pipeline behavior
and demonstrating the approach in the 5-minute video.

Usage:
    python diagnostics.py data/34855_vadnerbhairav_chandavad_nashik
    python diagnostics.py data/12429_malatavadi_chandgad_kolhapur
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np
from bhume import load
from bhume.io import read_predictions


def generate_report(village_dir: str) -> None:
    village = load(village_dir)
    preds = read_predictions(f"{village_dir}/predictions.geojson")

    slug = village.slug
    out_dir = Path(village_dir)

    corrected = preds[preds["status"] == "corrected"]
    flagged = preds[preds["status"] == "flagged"]

    print(f"=== Diagnostics: {slug} ===")
    print(f"  Total: {len(preds)} | Corrected: {len(corrected)} | Flagged: {len(flagged)}")

    # Compute shifts for corrected plots
    utm = _utm_for(village.plots.geometry.iloc[0])
    plots_u = village.plots.to_crs(utm)
    preds_u = preds.to_crs(utm)

    dxs, dys, confs = [], [], []
    for pn in corrected.index:
        if pn in plots_u.index:
            oc = plots_u.loc[pn, "geometry"].centroid
            pc = preds_u.loc[pn, "geometry"].centroid
            dxs.append(pc.x - oc.x)
            dys.append(pc.y - oc.y)
            confs.append(corrected.loc[pn, "confidence"])

    dxs, dys, confs = np.array(dxs), np.array(dys), np.array(confs)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(f"Diagnostic Report: {slug}", fontsize=14, fontweight="bold")

    # 1. Shift distribution (quiver-like scatter)
    ax = axes[0, 0]
    mags = np.sqrt(dxs**2 + dys**2)
    sc = ax.scatter(dxs, dys, c=confs, cmap="RdYlGn", s=5, alpha=0.6,
                    vmin=0, vmax=1)
    ax.set_xlabel("dx (m, east)")
    ax.set_ylabel("dy (m, north)")
    ax.set_title("Shift Vectors (colour = confidence)")
    ax.axhline(0, color="gray", linewidth=0.5)
    ax.axvline(0, color="gray", linewidth=0.5)
    plt.colorbar(sc, ax=ax, label="Confidence")

    # 2. Confidence histogram
    ax = axes[0, 1]
    ax.hist(confs, bins=30, color="#4CAF50", edgecolor="black", alpha=0.8)
    ax.axvline(np.median(confs), color="red", linestyle="--",
               label=f"Median={np.median(confs):.2f}")
    ax.set_xlabel("Confidence")
    ax.set_ylabel("Count")
    ax.set_title("Confidence Distribution (corrected plots)")
    ax.legend()

    # 3. Shift magnitude histogram
    ax = axes[1, 0]
    ax.hist(mags, bins=40, color="#2196F3", edgecolor="black", alpha=0.8)
    ax.axvline(np.median(mags), color="red", linestyle="--",
               label=f"Median={np.median(mags):.1f}m")
    ax.set_xlabel("Shift magnitude (m)")
    ax.set_ylabel("Count")
    ax.set_title("Shift Magnitude Distribution")
    ax.legend()

    # 4. Confidence vs plot area
    ax = axes[1, 1]
    areas, plot_confs = [], []
    for pn in corrected.index:
        if pn in village.plots.index:
            a = village.plots.loc[pn, "map_area_sqm"]
            if a is not None and not np.isnan(a):
                areas.append(a)
                plot_confs.append(corrected.loc[pn, "confidence"])
    ax.scatter(areas, plot_confs, s=3, alpha=0.4, color="#FF5722")
    ax.set_xlabel("Plot area (m²)")
    ax.set_ylabel("Confidence")
    ax.set_title("Confidence vs Plot Size")
    ax.set_xscale("log")

    plt.tight_layout()
    report_path = out_dir / "diagnostics.png"
    plt.savefig(report_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved diagnostic report -> {report_path}")

    # Summary stats
    print(f"\n  Shift Statistics (corrected plots):")
    print(f"    dx: median={np.median(dxs):.1f}m, IQR=[{np.percentile(dxs,25):.1f}, {np.percentile(dxs,75):.1f}]")
    print(f"    dy: median={np.median(dys):.1f}m, IQR=[{np.percentile(dys,25):.1f}, {np.percentile(dys,75):.1f}]")
    print(f"    magnitude: median={np.median(mags):.1f}m, max={np.max(mags):.1f}m")
    print(f"    confidence: median={np.median(confs):.3f}, range=[{np.min(confs):.3f}, {np.max(confs):.3f}]")

    # Flagged analysis
    flag_reasons = {}
    for pn in flagged.index:
        note = preds.loc[pn, "method_note"]
        reason = note.split(";")[0].split("|")[0].strip() if note else "unknown"
        flag_reasons[reason] = flag_reasons.get(reason, 0) + 1

    print(f"\n  Flagged Reasons ({len(flagged)} plots):")
    for reason, count in sorted(flag_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")


def _utm_for(geom) -> str:
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python diagnostics.py data/<village_slug>")
        sys.exit(1)
    generate_report(sys.argv[1])
