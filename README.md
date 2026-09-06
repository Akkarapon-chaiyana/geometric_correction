# Geometric Correction Tool

A browser-based GIS tool for georeferencing unregistered GeoTIFF files against a satellite basemap using Ground Control Points (GCPs). Built with Python (Flask + GDAL) and Leaflet.js.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![GDAL](https://img.shields.io/badge/GDAL-3.0%2B-green)

---

## Features

- **Two-panel interface** — Esri World Imagery satellite reference (left) + uploaded TIF viewer (right)
- **Location search** — Nominatim geocoder with autocomplete; jump to any place by name
- **AOI drawing** — click two corners to define an Area of Interest; map zooms to fit
- **Shapefile overlay** — load `.zip` or `.shp` boundary files over the basemap; auto-reprojects from any CRS to WGS84; click features to inspect attributes
- **GCP picking** — click the satellite map to set a reference coordinate, then click the matching location on your TIF
- **Transform models** — Affine (1st order), Polynomial (2nd order), Thin Plate Spline
- **RMSE analysis** — per-point residuals in metres, colour-coded (green < 10 m, orange < 50 m, red > 50 m); separate control and validation RMSE
- **COG export** — outputs a Cloud Optimized GeoTIFF (LZW compressed, tiled, with overviews) in EPSG:4326

---

## Requirements

- Python 3.9+
- GDAL 3.0+ (with Python bindings `osgeo`)
- Conda (recommended) or system GDAL

### Python packages

```
flask>=3.0
numpy>=1.24
pillow>=10.0
scipy>=1.11
gdal>=3.0
```

Install with pip (after GDAL is available in your environment):

```bash
pip install flask numpy pillow scipy
```

Or with conda:

```bash
conda install -c conda-forge flask numpy pillow scipy gdal
```

---

## Quickstart

```bash
git clone <repo-url>
cd geometric_correction
python3 app.py
```

The app opens automatically in your browser at `http://127.0.0.1:5001`.

---

## Workflow

### 1. Navigate to your study area

- Type a place name in the **Search** box (top of left panel) — results appear as a dropdown
- Click a result to zoom the satellite map to that location

### 2. Define an Area of Interest (optional)

- Click **Draw AOI**
- Click two corners on the map to draw a dashed rectangle
- The map zooms to fit your AOI
- Click **Clear AOI** to remove it

### 3. Load a boundary shapefile (optional)

- Click **Load Shapefile** and select a `.zip` (containing `.shp`, `.dbf`, `.prj`) or a bare `.shp`
- The layer renders in orange over the satellite map; click any feature to see its attributes
- Use **Hide / Show** and **Remove** to manage the layer

### 4. Upload your TIF

- Click **Upload TIF** and select your unregistered GeoTIFF
- The image appears in the right panel; use scroll to zoom and drag to pan

### 5. Place Ground Control Points

- Click **+ Add GCP**
- **Step 1** — click the matching reference location on the satellite map (left panel)
- **Step 2** — click the corresponding location on your TIF (right panel)
- The GCP pair appears in the table below with its pixel and geographic coordinates
- Repeat for at least 15 points; use the **Role** dropdown to mark 5 as **Validation**

### 6. Compute RMSE

- Choose a transform model from the **Transform** dropdown:
  - **Affine** — shift, rotate, scale; minimum 3 control points
  - **Polynomial 2nd order** — non-linear warp; minimum 6 control points
  - **Thin Plate Spline** — rubber-sheet correction for irregular distortions; minimum 3 control points
- Click **Compute RMSE**
- Residuals appear per row; overall Control RMSE and Validation RMSE are shown in the panel header
- Delete or reclassify outlier points and recompute as needed

### 7. Export as COG

- Click **Export COG**
- A Cloud Optimized GeoTIFF (`corrected_cog.tif`) is warped with your GCPs and downloaded
- Output is in **EPSG:4326 (WGS84)**, LZW compressed, 512×512 tiled, with overview levels 2–32

---

## RMSE interpretation

| Residual | Quality |
|---|---|
| < 10 m | Good — suitable for most mapping tasks |
| 10–50 m | Acceptable — depends on image resolution |
| > 50 m | Poor — review and reposition outlier GCPs |

---

## Project structure

```
geometric_correction/
├── app.py                  # Flask backend (GDAL, OGR, scipy)
├── templates/
│   └── index.html          # Single-page frontend (Leaflet, inline JS/CSS)
├── uploads/                # Uploaded TIFs and preview PNGs (auto-created)
├── exports/                # Warped COG outputs (auto-created)
├── requirements.txt
└── README.md
```

---

## Notes

- **Port** — runs on `5001` (macOS port 5000 is reserved by AirPlay Receiver / Control Center)
- **Shapefile without `.shx`** — the tool auto-rebuilds the index file using GDAL's `SHAPE_RESTORE_SHX` option
- **Large TIFs** — previews are downsampled to max 3000 px on the longest side; the full-resolution file is used for warping
- **Basemap** — Esri World Imagery (free, no API key required); labels overlay from Esri Reference layer
- **Geocoder** — OpenStreetMap Nominatim (free, no API key required)

---

## License

MIT
