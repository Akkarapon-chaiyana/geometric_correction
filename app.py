import os, uuid, json, threading, webbrowser, zipfile, tempfile
import numpy as np
from flask import Flask, render_template, request, jsonify, send_file
from PIL import Image
from osgeo import gdal, ogr, osr
from scipy.interpolate import RBFInterpolator

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
EXPORT_DIR = os.path.join(os.path.dirname(__file__), 'exports')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

_session = {}   # holds current file metadata


# ── helpers ──────────────────────────────────────────────────────────────────

def make_preview(tif_path, max_dim=3000):
    """Read any GeoTIFF via GDAL and return a normalised RGB preview image."""
    gdal.UseExceptions()
    ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError('GDAL could not open the file — unsupported format or corrupt TIF')

    w, h   = ds.RasterXSize, ds.RasterYSize
    n_bands = ds.RasterCount
    if n_bands == 0:
        raise RuntimeError('TIF contains no raster bands')

    scale = min(1.0, max_dim / max(w, h))
    pw, ph = max(1, int(w * scale)), max(1, int(h * scale))

    # choose which source bands to use for RGB display
    if n_bands >= 3:
        src_bands = [1, 2, 3]
    else:
        src_bands = [1]

    raw = []
    for b_idx in src_bands:
        band   = ds.GetRasterBand(b_idx)
        nodata = band.GetNoDataValue()
        # ReadAsArray with buf_* does the resampling in GDAL (no PIL/rasterio quirks)
        arr = band.ReadAsArray(buf_xsize=pw, buf_ysize=ph)
        if arr is None:
            raise RuntimeError(f'Could not read band {b_idx} — '
                               f'file may be truncated or use unsupported compression')
        arr = arr.astype(np.float32)
        if nodata is not None:
            arr[arr == nodata] = np.nan
        raw.append(arr)

    ds = None  # close dataset

    if len(raw) == 1:
        raw = raw * 3   # greyscale → RGB

    out = np.zeros((ph, pw, 3), dtype=np.uint8)
    for i, plane in enumerate(raw):
        valid = plane[np.isfinite(plane)]
        if valid.size == 0:
            continue
        lo, hi = np.percentile(valid, [2, 98])
        if hi > lo:
            norm = np.clip((plane - lo) / (hi - lo) * 255.0, 0, 255)
        else:
            norm = np.full_like(plane, 128.0)
        # nodata pixels → black
        norm = np.where(np.isfinite(plane), norm, 0.0)
        out[:, :, i] = norm.astype(np.uint8)

    img = Image.fromarray(out)   # no deprecated mode arg
    return img, w, h, pw, ph, scale


def build_transform_fn(method, ctrl_px, ctrl_map):
    if method == 'affine':
        A = np.column_stack([ctrl_px, np.ones(len(ctrl_px))])
        cx, _, _, _ = np.linalg.lstsq(A, ctrl_map[:, 0], rcond=None)
        cy, _, _, _ = np.linalg.lstsq(A, ctrl_map[:, 1], rcond=None)
        def fn(pts):
            M = np.column_stack([pts, np.ones(len(pts))])
            return np.column_stack([M @ cx, M @ cy])

    elif method == 'polynomial2':
        def design(pts):
            x, y = pts[:, 0], pts[:, 1]
            return np.column_stack([x, y, x**2, x*y, y**2, np.ones(len(x))])
        A = design(ctrl_px)
        cx, _, _, _ = np.linalg.lstsq(A, ctrl_map[:, 0], rcond=None)
        cy, _, _, _ = np.linalg.lstsq(A, ctrl_map[:, 1], rcond=None)
        def fn(pts):
            M = design(pts)
            return np.column_stack([M @ cx, M @ cy])

    elif method == 'tps':
        rbf_x = RBFInterpolator(ctrl_px, ctrl_map[:, 0], kernel='thin_plate_spline')
        rbf_y = RBFInterpolator(ctrl_px, ctrl_map[:, 1], kernel='thin_plate_spline')
        def fn(pts):
            return np.column_stack([rbf_x(pts), rbf_y(pts)])
    else:
        raise ValueError(f'Unknown method: {method}')
    return fn


# ── routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file'}), 400

    fid = str(uuid.uuid4())
    tif_path = os.path.join(UPLOAD_DIR, fid + '.tif')
    f.save(tif_path)

    try:
        img, orig_w, orig_h, prev_w, prev_h, scale = make_preview(tif_path)
    except Exception as e:
        os.remove(tif_path)
        return jsonify({'error': str(e)}), 500

    prev_path = os.path.join(UPLOAD_DIR, fid + '_preview.png')
    img.save(prev_path)

    _session.update(dict(id=fid, tif=tif_path,
                         orig_w=orig_w, orig_h=orig_h,
                         prev_w=prev_w, prev_h=prev_h,
                         img_scale=scale))

    return jsonify(dict(id=fid,
                        orig_w=orig_w, orig_h=orig_h,
                        prev_w=prev_w, prev_h=prev_h,
                        img_scale=scale))


@app.route('/preview/<fid>')
def preview(fid):
    path = os.path.join(UPLOAD_DIR, fid + '_preview.png')
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, mimetype='image/png')


@app.route('/compute_rmse', methods=['POST'])
def compute_rmse():
    data = request.get_json()
    gcps   = data.get('gcps', [])
    method = data.get('method', 'affine')

    control    = [g for g in gcps if g['role'] == 'control']
    validation = [g for g in gcps if g['role'] == 'validation']

    min_pts = {'affine': 3, 'polynomial2': 6, 'tps': 3}
    needed = min_pts.get(method, 3)
    if len(control) < needed:
        return jsonify({'error': f'{method} needs ≥ {needed} control points, got {len(control)}'}), 400

    ctrl_px  = np.array([[g['px'], g['py']] for g in control])
    ctrl_map = np.array([[g['lon'], g['lat']] for g in control])

    try:
        fn = build_transform_fn(method, ctrl_px, ctrl_map)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # residuals in degrees → approximate metres (1° ≈ 111 km at equator)
    ctrl_pred = fn(ctrl_px)
    ctrl_res  = np.sqrt(np.sum(((ctrl_pred - ctrl_map) * np.array([111320, 110540])) ** 2, axis=1))
    ctrl_rmse = float(np.sqrt(np.mean(ctrl_res ** 2)))

    val_rmse = None
    val_res  = []
    if validation:
        val_px  = np.array([[g['px'], g['py']] for g in validation])
        val_map = np.array([[g['lon'], g['lat']] for g in validation])
        val_pred = fn(val_px)
        val_res_arr = np.sqrt(np.sum(((val_pred - val_map) * np.array([111320, 110540])) ** 2, axis=1))
        val_rmse = float(np.sqrt(np.mean(val_res_arr ** 2)))
        val_res  = val_res_arr.tolist()

    return jsonify(dict(ctrl_rmse=ctrl_rmse,
                        val_rmse=val_rmse,
                        ctrl_residuals=ctrl_res.tolist(),
                        val_residuals=val_res))


@app.route('/export', methods=['POST'])
def export_cog():
    data   = request.get_json()
    gcps   = data.get('gcps', [])
    method = data.get('method', 'affine')

    if not _session.get('id'):
        return jsonify({'error': 'No file uploaded'}), 400

    tif_path = _session['tif']
    fid      = _session['id']
    img_scale = _session.get('img_scale', 1.0)   # preview→original scale
    tmp_path  = os.path.join(EXPORT_DIR, fid + '_warped.tif')
    cog_path  = os.path.join(EXPORT_DIR, fid + '_cog.tif')

    # GCPs come in preview-pixel space; scale to original pixel space
    ctrl_gcps = [g for g in gcps if g['role'] == 'control']
    if len(ctrl_gcps) < 3:
        return jsonify({'error': 'Need at least 3 control points'}), 400

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    wkt = srs.ExportToWkt()

    gdal_gcps = []
    for g in ctrl_gcps:
        orig_px = g['px'] / img_scale
        orig_py = g['py'] / img_scale
        gdal_gcps.append(gdal.GCP(float(g['lon']), float(g['lat']), 0.0,
                                   float(orig_px), float(orig_py)))

    ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
    ds.SetGCPs(gdal_gcps, wkt)

    warp_kwargs = dict(
        format='GTiff',
        dstSRS='EPSG:4326',
        resampleAlg='bilinear',
        creationOptions=['COMPRESS=LZW', 'TILED=YES',
                         'BLOCKXSIZE=512', 'BLOCKYSIZE=512'],
        errorThreshold=0
    )
    if method == 'tps':
        warp_kwargs['tps'] = True
    else:
        warp_kwargs['polynomialOrder'] = {'affine': 1, 'polynomial2': 2}.get(method, 1)

    try:
        result = gdal.Warp(tmp_path, ds, **warp_kwargs)
        if result is None:
            raise RuntimeError('gdalwarp returned None — check GCPs and input file')
        result = None
        ds = None
    except Exception as e:
        ds = None
        return jsonify({'error': str(e)}), 500

    # Add overviews and create COG
    try:
        tmp_ds = gdal.Open(tmp_path, gdal.GA_Update)
        tmp_ds.BuildOverviews('NEAREST', [2, 4, 8, 16, 32])
        tmp_ds = None

        gdal.Translate(cog_path, tmp_path,
                       creationOptions=['COPY_SRC_OVERVIEWS=YES',
                                        'COMPRESS=LZW', 'TILED=YES',
                                        'BLOCKXSIZE=512', 'BLOCKYSIZE=512'])
        os.remove(tmp_path)
    except Exception as e:
        return jsonify({'error': f'COG creation failed: {e}'}), 500

    return send_file(cog_path, as_attachment=True,
                     download_name='corrected_cog.tif',
                     mimetype='image/tiff')


@app.route('/preview_warp', methods=['POST'])
def preview_warp():
    data      = request.get_json()
    gcps      = data.get('gcps', [])
    method    = data.get('method', 'affine')

    if not _session.get('id'):
        return jsonify({'error': 'No file uploaded'}), 400

    tif_path  = _session['tif']
    fid       = _session['id']
    img_scale = _session.get('img_scale', 1.0)
    tmp_warp  = os.path.join(UPLOAD_DIR, fid + '_prevwarp.tif')
    png_path  = os.path.join(UPLOAD_DIR, fid + '_prevwarp.png')

    ctrl_gcps = [g for g in gcps if g['role'] == 'control']
    if len(ctrl_gcps) < 3:
        return jsonify({'error': 'Need at least 3 control points'}), 400

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    wkt = srs.ExportToWkt()

    gdal_gcps = [
        gdal.GCP(float(g['lon']), float(g['lat']), 0.0,
                 float(g['px'] / img_scale), float(g['py'] / img_scale))
        for g in ctrl_gcps
    ]

    ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
    ds.SetGCPs(gdal_gcps, wkt)

    warp_kwargs = dict(
        format='GTiff',
        dstSRS='EPSG:4326',
        resampleAlg='bilinear',
        errorThreshold=0,
        creationOptions=['COMPRESS=LZW']
    )
    if method == 'tps':
        warp_kwargs['tps'] = True
    else:
        warp_kwargs['polynomialOrder'] = {'affine': 1, 'polynomial2': 2}.get(method, 1)

    try:
        result = gdal.Warp(tmp_warp, ds, **warp_kwargs)
        if result is None:
            raise RuntimeError('Warp returned None')
        result = None
        ds = None
    except Exception as e:
        ds = None
        return jsonify({'error': str(e)}), 500

    # Read geographic extent from warped file
    wd = gdal.Open(tmp_warp)
    gt   = wd.GetGeoTransform()
    cols = wd.RasterXSize
    rows = wd.RasterYSize
    west  = gt[0]
    north = gt[3]
    east  = gt[0] + gt[1] * cols
    south = gt[3] + gt[5] * rows
    wd = None

    # Convert warped TIF to display PNG (max 1500px)
    try:
        img, _, _, _, _, _ = make_preview(tmp_warp, max_dim=1500)
        img.save(png_path)
    except Exception as e:
        return jsonify({'error': f'Preview PNG failed: {e}'}), 500

    return jsonify(dict(url=f'/warp_preview/{fid}',
                        west=west, south=south, east=east, north=north))


@app.route('/warp_preview/<fid>')
def warp_preview_img(fid):
    path = os.path.join(UPLOAD_DIR, fid + '_prevwarp.png')
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, mimetype='image/png')


@app.route('/back_transform', methods=['POST'])
def back_transform():
    """Inverse transform: (lon, lat) on corrected map → original pixel (px, py)."""
    data   = request.get_json()
    lon    = float(data['lon'])
    lat    = float(data['lat'])
    gcps   = data.get('gcps', [])
    method = data.get('method', 'affine')

    control = [g for g in gcps if g['role'] == 'control']
    if len(control) < 3:
        return jsonify({'error': 'Need at least 3 control points'}), 400

    ctrl_px  = np.array([[g['px'], g['py']] for g in control])
    ctrl_map = np.array([[g['lon'], g['lat']] for g in control])

    # Build the inverse: map_coords → pixel_coords (swap inputs)
    try:
        fn_inv = build_transform_fn(method, ctrl_map, ctrl_px)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    result = fn_inv(np.array([[lon, lat]]))
    px = float(np.clip(result[0, 0], 0, _session.get('prev_w', 1e6) - 1))
    py = float(np.clip(result[0, 1], 0, _session.get('prev_h', 1e6) - 1))

    return jsonify({'px': px, 'py': py})


@app.route('/upload_shapefile', methods=['POST'])
def upload_shapefile():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file provided'}), 400

    ext = os.path.splitext(f.filename)[1].lower()

    with tempfile.TemporaryDirectory() as tmpdir:
        if ext == '.zip':
            zip_path = os.path.join(tmpdir, 'upload.zip')
            f.save(zip_path)
            try:
                with zipfile.ZipFile(zip_path) as z:
                    z.extractall(tmpdir)
            except zipfile.BadZipFile:
                return jsonify({'error': 'Invalid ZIP file'}), 400
            shp_files = [
                os.path.join(root, fn)
                for root, _, files in os.walk(tmpdir)
                for fn in files if fn.lower().endswith('.shp')
            ]
            if not shp_files:
                return jsonify({'error': 'No .shp file found inside ZIP'}), 400
            shp_path = shp_files[0]
        elif ext == '.shp':
            shp_path = os.path.join(tmpdir, f.filename)
            f.save(shp_path)
        else:
            return jsonify({'error': 'Upload a .zip shapefile bundle or a .shp file'}), 400

        ogr.UseExceptions()
        # Rebuild missing .shx index automatically (common with downloaded shapefiles)
        gdal.SetConfigOption('SHAPE_RESTORE_SHX', 'YES')
        try:
            ds = ogr.Open(shp_path)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 400
        finally:
            gdal.SetConfigOption('SHAPE_RESTORE_SHX', None)
        if ds is None:
            return jsonify({'error': 'OGR cannot open the shapefile — it may be corrupt or missing companion files'}), 400

        layer = ds.GetLayer(0)
        src_srs = layer.GetSpatialRef()

        tgt_srs = osr.SpatialReference()
        tgt_srs.ImportFromEPSG(4326)
        tgt_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        coord_transform = None
        if src_srs:
            src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            coord_transform = osr.CoordinateTransformation(src_srs, tgt_srs)

        features = []
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None:
                continue
            geom = geom.Clone()
            if coord_transform:
                try:
                    geom.Transform(coord_transform)
                except Exception:
                    pass  # leave in original CRS if transform fails

            # serialise properties, coercing non-JSON-safe values to str
            props = {}
            for i in range(feat.GetFieldCount()):
                name = feat.GetFieldDefnRef(i).GetName()
                val  = feat.GetField(i)
                props[name] = val if isinstance(val, (int, float, str, type(None))) else str(val)

            geojson_geom = geom.ExportToJson()
            if geojson_geom:
                features.append({
                    'type': 'Feature',
                    'geometry': json.loads(geojson_geom),
                    'properties': props
                })

        layer_name = layer.GetName()
        ds = None

        return jsonify({
            'type': 'FeatureCollection',
            'name': layer_name,
            'features': features,
            'count': len(features),
            'crs_wkt': src_srs.ExportToWkt() if src_srs else 'unknown'
        })


if __name__ == '__main__':
    def _open():
        webbrowser.open('http://127.0.0.1:5001')
    threading.Timer(1.2, _open).start()
    app.run(debug=False, port=5001)
