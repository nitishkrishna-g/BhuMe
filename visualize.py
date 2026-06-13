#!/usr/bin/env python3
"""
Generate a single interactive HTML visualization for ALL villages.

Creates a self-contained HTML with:
  - Village toggle tabs (like BhuMe playground)
  - Satellite basemap with plot overlays
  - Color-coded by status/confidence
  - Before/After layer toggle
  - Shift arrows layer
  - Click-to-inspect popups
  - Plot search by number
  - Confidence filter slider
  - Stats panel that updates per village
  - Fly-to animation on village switch

Usage:
    python visualize.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np

from bhume import load
from bhume.io import read_predictions


VILLAGES = [
    "data/34855_vadnerbhairav_chandavad_nashik",
    "data/12429_malatavadi_chandgad_kolhapur",
]


def _utm_for(geom) -> str:
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"


def _conf_color(conf, status: str) -> str:
    if status == "flagged":
        return "#ef4444"
    if conf is None:
        return "#6b7280"
    if conf >= 0.75:
        return "#22c55e"
    if conf >= 0.50:
        return "#eab308"
    if conf >= 0.35:
        return "#f97316"
    return "#ef4444"


def _build_village_data(village_dir: str) -> dict:
    """Build all GeoJSON + stats for one village."""
    village = load(village_dir)
    preds = read_predictions(f"{village_dir}/predictions.geojson")
    slug = village.slug

    # Display name
    parts = slug.split("_")
    # e.g. "34855_vadnerbhairav_chandavad_nashik" -> "Vadnerbhairav" + "Nashik"
    display_name = parts[1].title() if len(parts) > 1 else slug
    district = parts[-1].title() if len(parts) > 2 else ""

    utm = _utm_for(village.plots.geometry.iloc[0])
    plots_u = village.plots.to_crs(utm)
    preds_u = preds.to_crs(utm)

    bounds = village.plots.total_bounds
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    corrected = preds[preds["status"] == "corrected"]
    flagged = preds[preds["status"] == "flagged"]
    n_total = len(preds)
    n_corrected = len(corrected)
    n_flagged = len(flagged)
    confs = corrected["confidence"].dropna().values
    median_conf = float(np.median(confs)) if len(confs) > 0 else 0

    # --- Prediction features ---
    pred_features = []
    for _, row in preds.iterrows():
        pn = str(row["plot_number"])
        status = row["status"]
        conf = row.get("confidence")
        note = row.get("method_note", "") or ""
        color = _conf_color(conf, status)

        shift_mag = 0.0
        if status == "corrected" and pn in plots_u.index and pn in preds_u.index:
            oc = plots_u.loc[pn, "geometry"].centroid
            pc = preds_u.loc[pn, "geometry"].centroid
            shift_mag = float(np.sqrt((pc.x - oc.x)**2 + (pc.y - oc.y)**2))

        area = 0
        if pn in village.plots.index:
            a = village.plots.loc[pn, "map_area_sqm"]
            if a is not None and not np.isnan(a):
                area = float(a)

        geom_json = json.loads(gpd.GeoSeries([row.geometry], crs="EPSG:4326").to_json())
        feat_geom = geom_json["features"][0]["geometry"]

        conf_val = round(float(conf), 3) if conf is not None and not np.isnan(conf) else None

        pred_features.append({
            "type": "Feature",
            "geometry": feat_geom,
            "properties": {
                "pn": pn,
                "st": status,
                "c": conf_val,
                "cl": color,
                "sh": round(shift_mag, 1),
                "ar": round(area),
                "nt": note[:120],
            }
        })

    # --- Original boundary features ---
    orig_features = []
    for _, row in village.plots.iterrows():
        geom_json = json.loads(gpd.GeoSeries([row.geometry], crs="EPSG:4326").to_json())
        orig_features.append({
            "type": "Feature",
            "geometry": geom_json["features"][0]["geometry"],
            "properties": {"pn": str(row["plot_number"])}
        })

    # --- Shift arrows (every 3rd corrected) ---
    arrow_features = []
    for i, pn in enumerate(corrected.index):
        if i % 3 != 0:
            continue
        if pn not in village.plots.index:
            continue
        oc = village.plots.loc[pn, "geometry"].centroid
        pc = preds.loc[pn, "geometry"].centroid
        arrow_features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[oc.x, oc.y], [pc.x, pc.y]]
            },
            "properties": {"pn": str(pn)}
        })

    return {
        "slug": slug,
        "name": display_name,
        "district": district,
        "center": center,
        "zoom": 14 if n_total < 2500 else 13,
        "stats": {
            "total": n_total,
            "corrected": n_corrected,
            "flagged": n_flagged,
            "medianConf": round(median_conf, 2),
        },
        "predictions": {"type": "FeatureCollection", "features": pred_features},
        "originals": {"type": "FeatureCollection", "features": orig_features},
        "arrows": {"type": "FeatureCollection", "features": arrow_features},
    }


def generate_visualization() -> None:
    print("Building visualization for all villages ...")

    village_data = []
    for vd in VILLAGES:
        if Path(vd).exists():
            print(f"  Processing {vd} ...")
            village_data.append(_build_village_data(vd))

    if not village_data:
        print("No village data found!")
        sys.exit(1)

    all_data_json = json.dumps(village_data)

    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>BhuMe — Prediction Visualizer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Inter',system-ui,sans-serif; background:#0b1120; color:#e2e8f0; overflow:hidden; }
  #map { position:absolute; inset:0; z-index:1; }

  /* === TOP BAR === */
  .topbar {
    position:absolute; top:0; left:0; right:0; z-index:1000;
    display:flex; align-items:center; justify-content:center;
    padding:12px 24px;
    background:linear-gradient(180deg, rgba(11,17,32,0.95) 0%, rgba(11,17,32,0.0) 100%);
    pointer-events:none;
  }
  .topbar > * { pointer-events:auto; }
  .village-tabs {
    display:flex; gap:6px; background:rgba(15,23,42,0.85); backdrop-filter:blur(16px);
    padding:4px; border-radius:10px; border:1px solid rgba(255,255,255,0.08);
  }
  .vtab {
    padding:8px 20px; border-radius:7px; cursor:pointer; font-size:13px; font-weight:600;
    transition:all 0.25s; color:#94a3b8; display:flex; align-items:center; gap:8px;
    user-select:none;
  }
  .vtab:hover { color:#e2e8f0; background:rgba(255,255,255,0.05); }
  .vtab.active { color:#22c55e; background:rgba(34,197,94,0.12); }
  .vtab-dot {
    width:8px; height:8px; border-radius:50%;
    background:#334155; transition:background 0.25s;
  }
  .vtab.active .vtab-dot { background:#22c55e; }

  /* === LEFT PANEL === */
  .panel {
    position:absolute; top:70px; left:16px; z-index:1000;
    background:rgba(15,23,42,0.88); backdrop-filter:blur(16px);
    border:1px solid rgba(255,255,255,0.07); border-radius:14px;
    padding:20px; width:300px;
    box-shadow:0 8px 32px rgba(0,0,0,0.5);
  }
  .panel-title { font-size:11px; letter-spacing:1.5px; text-transform:uppercase; color:#64748b; margin-bottom:14px; font-weight:600; }

  .stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:16px; }
  .stat-box {
    background:rgba(255,255,255,0.04); border-radius:8px; padding:10px 12px;
    border:1px solid rgba(255,255,255,0.05);
  }
  .stat-val { font-size:24px; font-weight:800; font-family:'JetBrains Mono',monospace; color:#f8fafc; }
  .stat-lbl { font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px; }

  /* Layer toggles */
  .layer-section { margin-bottom:14px; }
  .layer-row {
    display:flex; align-items:center; gap:10px; padding:7px 0; cursor:pointer;
    transition:opacity 0.2s; font-size:13px;
  }
  .layer-row:hover { opacity:0.85; }
  .layer-check {
    width:18px; height:18px; border-radius:4px; border:2px solid #475569;
    display:flex; align-items:center; justify-content:center; transition:all 0.2s; flex-shrink:0;
  }
  .layer-check.on { background:#22c55e; border-color:#22c55e; }
  .layer-check.on::after { content:'✓'; color:#fff; font-size:11px; font-weight:700; }
  .layer-swatch {
    width:14px; height:14px; border-radius:3px; flex-shrink:0;
  }

  /* Search */
  .search-box {
    width:100%; padding:8px 12px; border-radius:8px; border:1px solid rgba(255,255,255,0.1);
    background:rgba(255,255,255,0.05); color:#e2e8f0; font-size:13px; font-family:'Inter',sans-serif;
    outline:none; transition:border-color 0.2s; margin-bottom:14px;
  }
  .search-box:focus { border-color:#22c55e; }
  .search-box::placeholder { color:#475569; }

  /* Confidence slider */
  .slider-section { margin-top:2px; }
  .slider-label { font-size:11px; color:#64748b; margin-bottom:6px; display:flex; justify-content:space-between; }
  .slider-label span { color:#22c55e; font-family:'JetBrains Mono',monospace; font-weight:600; }
  input[type=range] {
    width:100%; -webkit-appearance:none; height:4px; border-radius:2px;
    background:linear-gradient(90deg,#ef4444,#f97316,#eab308,#22c55e); outline:none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; width:16px; height:16px; border-radius:50%;
    background:#f8fafc; border:2px solid #22c55e; cursor:pointer;
    box-shadow:0 2px 6px rgba(0,0,0,0.3);
  }

  /* Legend */
  .legend { margin-top:14px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.06); }
  .legend-row { display:flex; align-items:center; gap:8px; font-size:11px; color:#94a3b8; margin-bottom:4px; }
  .legend-dot { width:12px; height:12px; border-radius:3px; flex-shrink:0; }

  /* === RIGHT PANEL (village info) === */
  .info-panel {
    position:absolute; top:70px; right:16px; z-index:1000;
    background:rgba(15,23,42,0.88); backdrop-filter:blur(16px);
    border:1px solid rgba(255,255,255,0.07); border-radius:14px;
    padding:20px; width:260px;
    box-shadow:0 8px 32px rgba(0,0,0,0.5);
  }
  .info-village { font-size:20px; font-weight:800; color:#f8fafc; }
  .info-district { font-size:13px; color:#64748b; margin-bottom:12px; }
  .info-text { font-size:12px; color:#94a3b8; line-height:1.6; }
  .info-text strong { color:#e2e8f0; }

  /* Popup */
  .pp { font-family:'Inter',sans-serif; font-size:13px; line-height:1.5; min-width:220px; }
  .pp-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  .pp-pn { font-size:16px; font-weight:800; color:#0f172a; }
  .pp-badge { padding:3px 10px; border-radius:5px; font-size:10px; font-weight:700; color:#fff; text-transform:uppercase; letter-spacing:0.5px; }
  .pp-row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #f1f5f9; }
  .pp-k { color:#64748b; font-size:11px; text-transform:uppercase; }
  .pp-v { font-weight:600; color:#0f172a; font-family:'JetBrains Mono',monospace; font-size:12px; }
  .pp-note { margin-top:6px; font-size:11px; color:#64748b; word-break:break-all; }

  /* Bottom hint */
  .bottom-hint {
    position:absolute; bottom:16px; left:50%; transform:translateX(-50%); z-index:1000;
    background:rgba(15,23,42,0.88); backdrop-filter:blur(16px);
    border:1px solid rgba(255,255,255,0.07); border-radius:10px;
    padding:10px 20px; font-size:12px; color:#94a3b8;
    display:flex; align-items:center; gap:12px;
  }
  .hint-dot { width:6px; height:6px; background:#22c55e; border-radius:50%; }
</style>
</head>
<body>

<div id="map"></div>

<!-- Top village tabs -->
<div class="topbar">
  <div class="village-tabs" id="villageTabs"></div>
</div>

<!-- Left control panel -->
<div class="panel">
  <div class="panel-title">Controls</div>
  <input class="search-box" id="searchBox" type="text" placeholder="Search plot number..." />

  <div class="stat-grid" id="statsGrid">
    <div class="stat-box"><div class="stat-val" id="statTotal">—</div><div class="stat-lbl">Total</div></div>
    <div class="stat-box"><div class="stat-val" id="statCorrected" style="color:#22c55e">—</div><div class="stat-lbl">Corrected</div></div>
    <div class="stat-box"><div class="stat-val" id="statFlagged" style="color:#ef4444">—</div><div class="stat-lbl">Flagged</div></div>
    <div class="stat-box"><div class="stat-val" id="statConf">—</div><div class="stat-lbl">Median Conf</div></div>
  </div>

  <div class="layer-section">
    <div class="layer-row" onclick="toggleLayer('preds')">
      <div class="layer-check on" id="chkPreds"></div>
      <div class="layer-swatch" style="background:#22c55e"></div>
      Predictions
    </div>
    <div class="layer-row" onclick="toggleLayer('orig')">
      <div class="layer-check" id="chkOrig"></div>
      <div class="layer-swatch" style="background:transparent;border:2px dashed #ef4444"></div>
      Original boundaries
    </div>
    <div class="layer-row" onclick="toggleLayer('arrows')">
      <div class="layer-check" id="chkArrows"></div>
      <div class="layer-swatch" style="background:#3b82f6"></div>
      Shift arrows
    </div>
  </div>

  <div class="slider-section">
    <div class="slider-label">Confidence filter <span id="sliderVal">0.00</span></div>
    <input type="range" id="confSlider" min="0" max="100" value="0" />
  </div>

  <div class="legend">
    <div class="legend-row"><div class="legend-dot" style="background:#22c55e"></div> High conf ≥0.75</div>
    <div class="legend-row"><div class="legend-dot" style="background:#eab308"></div> Medium 0.50–0.75</div>
    <div class="legend-row"><div class="legend-dot" style="background:#f97316"></div> Low 0.35–0.50</div>
    <div class="legend-row"><div class="legend-dot" style="background:#ef4444"></div> Flagged / very low</div>
  </div>
</div>

<!-- Right info panel -->
<div class="info-panel">
  <div class="info-village" id="infoName">—</div>
  <div class="info-district" id="infoDistrict"></div>
  <div class="info-text">
    Click any plot to inspect it. Toggle <strong>Original</strong> to compare
    before/after boundaries. Use the <strong>confidence slider</strong> to filter
    low-confidence corrections. <strong>Search</strong> by plot number to jump directly.
  </div>
</div>

<!-- Bottom hint -->
<div class="bottom-hint">
  <div class="hint-dot"></div>
  Click a plot to inspect &nbsp;·&nbsp; Toggle layers to compare &nbsp;·&nbsp; Slide to filter by confidence
</div>

<script>
// ============================================================
// DATA
// ============================================================
const VILLAGES = """ + all_data_json + r""";

// ============================================================
// MAP
// ============================================================
const map = L.map('map', { center:[20,74], zoom:13, zoomControl:false });
L.control.zoom({ position:'bottomright' }).addTo(map);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution:'Esri', maxZoom:19
}).addTo(map);
L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
  maxZoom:19, opacity:0.4
}).addTo(map);

// ============================================================
// STATE
// ============================================================
let activeIdx = 0;
let predLayer = null, origLayer = null, arrowGroup = null;
let layerState = { preds:true, orig:false, arrows:false };
let confThreshold = 0;
let highlightLayer = null;

// ============================================================
// BUILD TABS
// ============================================================
const tabsEl = document.getElementById('villageTabs');
VILLAGES.forEach(function(v, i) {
  const tab = document.createElement('div');
  tab.className = 'vtab' + (i===0 ? ' active' : '');
  tab.innerHTML = '<div class="vtab-dot"></div>' + v.name;
  tab.onclick = function() { switchVillage(i); };
  tabsEl.appendChild(tab);
});

// ============================================================
// POPUP BUILDER
// ============================================================
function makePopup(p) {
  const bg = p.st === 'corrected' ? '#22c55e' : '#ef4444';
  const confStr = p.c !== null ? p.c.toFixed(3) : 'N/A';
  return '<div class="pp">' +
    '<div class="pp-head"><span class="pp-pn">Plot ' + p.pn + '</span>' +
    '<span class="pp-badge" style="background:' + bg + '">' + p.st + '</span></div>' +
    '<div class="pp-row"><span class="pp-k">Confidence</span><span class="pp-v">' + confStr + '</span></div>' +
    '<div class="pp-row"><span class="pp-k">Shift</span><span class="pp-v">' + p.sh + ' m</span></div>' +
    '<div class="pp-row"><span class="pp-k">Area</span><span class="pp-v">' + p.ar.toLocaleString() + ' m²</span></div>' +
    (p.nt ? '<div class="pp-note">' + p.nt + '</div>' : '') +
    '</div>';
}

// ============================================================
// RENDER VILLAGE
// ============================================================
function renderVillage(idx) {
  const v = VILLAGES[idx];

  // Clear existing layers
  if (predLayer) map.removeLayer(predLayer);
  if (origLayer) map.removeLayer(origLayer);
  if (arrowGroup) map.removeLayer(arrowGroup);
  if (highlightLayer) { map.removeLayer(highlightLayer); highlightLayer = null; }

  // Predictions
  predLayer = L.geoJSON(v.predictions, {
    style: function(f) {
      const p = f.properties;
      const vis = (p.st === 'flagged' || p.c === null) ? true : p.c >= confThreshold;
      return {
        fillColor: p.cl,
        fillOpacity: vis ? 0.45 : 0.05,
        color: p.cl,
        weight: vis ? 1.5 : 0.3,
        opacity: vis ? 0.8 : 0.1,
      };
    },
    onEachFeature: function(f, layer) {
      layer.bindPopup(makePopup(f.properties), { maxWidth:300 });
    }
  });

  // Originals
  origLayer = L.geoJSON(v.originals, {
    style: { fillColor:'transparent', fillOpacity:0, color:'#ef4444', weight:2, opacity:0.7, dashArray:'6,4' },
    onEachFeature: function(f, layer) {
      layer.bindPopup('<div class="pp"><div class="pp-pn">Original: Plot ' + f.properties.pn + '</div></div>');
    }
  });

  // Arrows
  arrowGroup = L.featureGroup();
  L.geoJSON(v.arrows, { style:{ color:'#3b82f6', weight:2, opacity:0.7 } }).addTo(arrowGroup);
  v.arrows.features.forEach(function(f) {
    var coords = f.geometry.coordinates;
    if (coords.length >= 2) {
      var end = coords[coords.length - 1];
      L.circleMarker([end[1], end[0]], {
        radius:3, fillColor:'#3b82f6', fillOpacity:0.9, color:'#3b82f6', weight:1
      }).addTo(arrowGroup);
    }
  });

  // Add active layers
  if (layerState.preds) predLayer.addTo(map);
  if (layerState.orig) origLayer.addTo(map);
  if (layerState.arrows) arrowGroup.addTo(map);

  // Update stats
  document.getElementById('statTotal').textContent = v.stats.total;
  document.getElementById('statCorrected').textContent = v.stats.corrected;
  document.getElementById('statFlagged').textContent = v.stats.flagged;
  document.getElementById('statConf').textContent = v.stats.medianConf.toFixed(2);
  document.getElementById('infoName').textContent = v.name;
  document.getElementById('infoDistrict').textContent = v.district;
}

// ============================================================
// SWITCH VILLAGE
// ============================================================
function switchVillage(idx) {
  activeIdx = idx;
  // Update tabs
  document.querySelectorAll('.vtab').forEach(function(t, i) {
    t.className = 'vtab' + (i === idx ? ' active' : '');
  });
  renderVillage(idx);
  var v = VILLAGES[idx];
  map.flyTo(v.center, v.zoom, { duration:1.2 });
}

// ============================================================
// TOGGLE LAYERS
// ============================================================
function toggleLayer(name) {
  layerState[name] = !layerState[name];
  var layerMap = { preds: predLayer, orig: origLayer, arrows: arrowGroup };
  var chkMap = { preds:'chkPreds', orig:'chkOrig', arrows:'chkArrows' };
  var layer = layerMap[name];
  var chk = document.getElementById(chkMap[name]);

  if (layerState[name]) {
    if (layer) layer.addTo(map);
    chk.className = 'layer-check on';
  } else {
    if (layer) map.removeLayer(layer);
    chk.className = 'layer-check';
  }
}

// ============================================================
// CONFIDENCE SLIDER
// ============================================================
var slider = document.getElementById('confSlider');
var sliderLabel = document.getElementById('sliderVal');
slider.addEventListener('input', function() {
  confThreshold = parseInt(this.value) / 100;
  sliderLabel.textContent = confThreshold.toFixed(2);
  // Re-style predictions
  if (predLayer) {
    predLayer.eachLayer(function(layer) {
      var p = layer.feature.properties;
      var vis = (p.st === 'flagged' || p.c === null) ? true : p.c >= confThreshold;
      layer.setStyle({
        fillOpacity: vis ? 0.45 : 0.05,
        weight: vis ? 1.5 : 0.3,
        opacity: vis ? 0.8 : 0.1,
      });
    });
  }
});

// ============================================================
// PLOT SEARCH
// ============================================================
var searchBox = document.getElementById('searchBox');
searchBox.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter') return;
  var q = this.value.trim();
  if (!q) return;

  if (highlightLayer) { map.removeLayer(highlightLayer); highlightLayer = null; }

  // Find in predictions
  var found = false;
  if (predLayer) {
    predLayer.eachLayer(function(layer) {
      if (found) return;
      if (layer.feature.properties.pn === q) {
        found = true;
        map.fitBounds(layer.getBounds(), { maxZoom:18, padding:[100,100] });
        layer.openPopup();
        // Highlight ring
        highlightLayer = L.geoJSON(layer.feature, {
          style: { fillColor:'transparent', fillOpacity:0, color:'#ffffff', weight:4, opacity:1 }
        }).addTo(map);
        setTimeout(function() {
          if (highlightLayer) {
            highlightLayer.setStyle({ color:'#22c55e', weight:3, dashArray:'8,6' });
          }
        }, 600);
      }
    });
  }
  if (!found) {
    this.style.borderColor = '#ef4444';
    setTimeout(function() { searchBox.style.borderColor = ''; }, 1000);
  }
});

// ============================================================
// INIT
// ============================================================
switchVillage(0);
</script>
</body>
</html>"""

    out_path = Path("visualization.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\n  Wrote -> {out_path.absolute()}")
    print(f"  Contains {len(village_data)} villages, open in any browser!")


if __name__ == "__main__":
    generate_visualization()
