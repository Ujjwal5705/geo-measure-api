# Geospatial File Measurement API

A FastAPI backend that accepts a **zipped Shapefile** or a **KML**, extracts every feature
(id, geometry type, geometry, CRS, properties) and returns **area** (polygons) and **length**
(lines) computed in an appropriate **projected CRS** - never in raw lat/lon degrees.

## Setup

Requires **Python 3.10+** (tested on 3.11 and 3.12). Wheels for `geopandas`, `pyogrio`, `shapely` and
`pyproj` bundle GDAL/PROJ, so no system GDAL install is needed.

```bash
git clone https://github.com/Ujjwal5705/geo-measure-api.git
cd geo-measure-api

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # use requirements.txt if you don't need tests

uvicorn app.main:app --reload        # http://127.0.0.1:8000
```

* Interactive docs (Swagger UI): http://127.0.0.1:8000/docs
* Run tests: `python -m pytest -q` (28 tests: measurement accuracy against geodesic calculations, CRS handling, API behaviour, error cases)
* Docker alternative: `docker build -t geo-api . && docker run -p 8000:8000 geo-api`

**Try it in 30 seconds** (server running, from the project root):

```bash
curl -F "file=@samples/survey.kml" http://127.0.0.1:8000/api/files/              # note the returned "id"
curl http://127.0.0.1:8000/api/files/<id>/
curl http://127.0.0.1:8000/api/files/<id>/measurements/ | python3 -m json.tool
curl -F "file=@samples/survey_wgs84.zip" http://127.0.0.1:8000/api/files/        # zipped shapefiles
```

**Troubleshooting**
* `python --version` must be 3.10 or newer *inside the activated venv*. If it is older, recreate the venv with
  a newer interpreter (e.g. `python3.11 -m venv .venv`).
* Use `python -m pytest` rather than bare `pytest` so tests run with the venv's interpreter and packages.

Data (SQLite DB + uploaded files) is stored in `./data`. Configuration via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `GEO_DATA_DIR` | `./data` | where the DB and uploads live |
| `GEO_DATABASE_URL` | sqlite in data dir | any SQLAlchemy URL (e.g. Postgres) |
| `GEO_MAX_UPLOAD_MB` | 50 | max upload size |
| `GEO_MAX_UNZIPPED_MB` | 300 | zip-bomb guard |
| `GEO_MAX_FEATURES` | 200000 | max features per file |

Sample inputs are in `samples/` (`survey.kml`, `survey_wgs84.zip`, `plots_utm43n.zip`).

## API

### `POST /api/files/` - upload and process
Multipart form: `file` (`.zip` with a Shapefile, or `.kml`); optional `assume_crs`
(e.g. `EPSG:4326`, only for shapefiles missing a `.prj`).

```bash
curl -F "file=@samples/survey.kml" http://127.0.0.1:8000/api/files/
```
```json
{
  "id": "17756c173d214e41a2afabacc3c5b549",
  "filename": "survey.kml",
  "file_type": "kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "layers": [{"name": "Plots", "feature_count": 3, "crs": "EPSG:4326"}],
  "created_at": "2026-10-06T16:53:25.356836Z"
}
```
Errors: `422` (bad type / corrupt / unsafe zip / no CRS - body contains the message and the id of
the recorded FAILED upload), `413` (too large).

### `GET /api/files/{id}/` - file information
Returns the object above (`status`: `PROCESSING | COMPLETED | FAILED`; `error` set on failure).

### `GET /api/files/{id}/measurements/` - measurements
Query: `limit` (1-1000, default 100), `offset`, `geometry_type` (e.g. `Polygon`).
```json
{
  "file_id": "17756c...", "total": 3, "limit": 100, "offset": 0,
  "summary": {"feature_count": 3, "measured": 2, "not_applicable": 1, "unsupported": 0,
              "error": 0, "total_area_m2": 921480.81, "total_length_m": 3843.60},
  "results": [
    {"feature_id": 0, "layer": "Plots", "geometry_type": "Polygon", "status": "MEASURED",
     "measurement": {"type": "area", "value": 921480.81, "unit": "square_meters",
                     "conversions": {"hectares": 92.148, "square_kilometers": 0.9215},
                     "projected_crs": "custom LAEA (lon_0=75.9, lat_0=22.7)",
                     "method": "Lambert azimuthal equal-area centred on feature centroid"},
     "warnings": []},
    {"feature_id": 1, "geometry_type": "LineString", "status": "MEASURED",
     "measurement": {"type": "length", "value": 3843.60, "unit": "meters",
                     "conversions": {"kilometers": 3.8436}, "projected_crs": "EPSG:32643",
                     "method": "UTM zone of feature centroid"}, "warnings": []},
    {"feature_id": 2, "geometry_type": "Point", "status": "NOT_APPLICABLE",
     "measurement": null, "warnings": []}
  ]
}
```
Per-feature `status`: `MEASURED`, `NOT_APPLICABLE` (points), `UNSUPPORTED` (e.g. GeometryCollection,
missing/empty geometry), `ERROR` (unexpected failure on that one feature). These never fail the request.

### Extra endpoints
* `GET /api/files/{id}/features/` - features with `id`, `layer`, `geometry_type`, `geometry` (GeoJSON, in the
  file's native CRS), `crs`, `properties`. Supports `limit`, `offset`, `geometry_type`.
* `GET /api/files/` - list uploads. `DELETE /api/files/{id}/` - remove an upload and its data.
* `GET /health`

## Architecture

```
app/
  main.py            FastAPI app, lifespan (creates tables)
  api/files.py       HTTP layer: validation, streaming upload, pagination, response shaping
  services/ingest.py read file -> layers -> Feature rows (the processing pipeline)
  services/measure.py per-geometry measurement, never raises
  services/crs.py    projected-CRS selection + cached transformers
  models.py          SQLAlchemy: UploadedFile, Feature
  schemas.py         Pydantic response models
tests/               unit tests (measurement accuracy vs. geodesic) + API tests
samples/             sample inputs and generator script (samples/make_samples.py)
Dockerfile           container image for the API
pytest.ini           test configuration
```

**File-processing flow**
1. `POST` validates the extension, creates an `UploadedFile` (`PROCESSING`) and streams the upload to
   `data/uploads/<id>/` with a size cap.
2. Shapefile zips are extracted to a temp dir with zip-slip and zip-bomb protection; every `.shp`
   inside becomes a layer (`.dbf` required, `.prj` required unless `assume_crs` is given).
   KMLs are read layer-by-layer (one layer per `<Folder>`); KML is always WGS84.
3. Each feature is converted to a row: index, layer, geometry type, GeoJSON geometry, CRS,
   JSON-safe properties (NaN/NaT/numpy types cleaned, empty KML fields dropped).
4. Each geometry is measured (below); results are stored as columns so summaries are SQL aggregates.
5. Status becomes `COMPLETED`, or `FAILED` with an error message if the file itself is unusable.

**Measurement flow**
`geometry -> classify` (Polygon/MultiPolygon -> area, LineString/MultiLineString -> length,
Point/MultiPoint -> nothing, anything else -> `UNSUPPORTED`) `-> drop Z -> normalise to WGS84 ->
pick local projected CRS from centroid -> project (vectorised `shapely.transform`) -> shapely .area / .length`.

**CRS handling**
* The file CRS is read from `.prj` (shapefile) or assumed WGS84 (KML). Geometry is returned in its native CRS.
* For measuring, geometry is first normalised to EPSG:4326, then projected into a *local* metric CRS
  chosen per feature from its centroid:
  * **Area** -> Lambert Azimuthal Equal-Area centred on the feature. Equal-area by construction, so area
    is accurate anywhere, including near the poles.
  * **Length** -> UTM zone of the centroid (UPS north of 84N / south of 80S). Conformal, scale error
    <= 0.04 % inside the zone.
* Already-projected inputs (e.g. UTM, State Plane) go through the same path, so units (feet vs. metres) and
  distortion of the source CRS never leak into results. Tests confirm the same data in EPSG:4326 and EPSG:32643 give identical numbers.
* Accuracy is verified in tests against `pyproj.Geod` ellipsoidal calculations (within 0.1 %).

## Design decisions

| Decision | Why / alternatives considered |
|---|---|
| **FastAPI** over Django+DRF | Lightweight, typed, automatic OpenAPI docs; no ORM/admin needed for this scope. |
| **geopandas + pyogrio (GDAL)** for reading | One reader handles Shapefile and KML (incl. multi-layer). Alternatives: `fiona` (slower, being superseded by pyogrio), hand-parsing KML with `lxml` (more code, misses edge cases). |
| **Per-feature local projection** instead of one CRS for the file | A file can span several UTM zones; a single zone would distort far-away features. Alternative: one equal-area CRS (e.g. EPSG:6933) is simple but has noticeable length distortion. Cost: a transformer per feature (cached by CRS). |
| **Different CRS for area vs. length** | Equal-area is the right property for area, conformal for distance. |
| **Synchronous processing in `POST`** | Simple and the user gets results immediately; endpoint is a plain `def` so FastAPI runs it in a threadpool. For huge files see future scope. |
| **SQLite + SQLAlchemy** | Zero-setup for local runs, swap to Postgres via `GEO_DATABASE_URL`. Measurements are stored in columns, not blobs, so totals are cheap SQL. |
| **Graceful statuses instead of errors** | `NOT_APPLICABLE / UNSUPPORTED / ERROR` per feature; one bad feature never fails a file. |
| **Reject shapefiles with no CRS** unless `assume_crs` is provided | Guessing a CRS silently produces wrong areas, which is worse than a clear error. |
| **Security** | Zip-slip check, unzipped-size cap, upload-size cap, sanitised filenames, feature-count cap. |

Known limitations: features crossing the antimeridian are not split (centroid may be misplaced);
very long lines spanning several UTM zones lose some accuracy; KMZ and GeoJSON are not accepted;
invalid polygons are measured as-is with a warning rather than repaired.

## Learnings

* A shapefile holds one geometry type and `.prj` is optional in the wild - missing CRS is the most common
  real-world failure, so it deserves an explicit, actionable error.
* KML always means lon/lat WGS84, carries Z values and lots of empty fields; altitude must be dropped before
  planar measurement.
* Validating against an independent geodesic calculation (`pyproj.Geod`) caught my own wrong test
  expectations and is a much stronger check than comparing against a hand-computed constant.
* Geospatial libraries evolve quickly: running the tests on a fresh clone with newer dependency versions surfaced a
  deprecated Shapely API (`shapely.ops.transform`), which is now replaced by the vectorised `shapely.transform`.
* Equal-area vs. conformal is a genuine trade-off; one "universal" projected CRS is rarely the best for both
  area and length.

## Future scope

* Async processing (Celery/RQ or FastAPI background tasks) with progress polling for very large files.
* Support KMZ, GeoJSON, GeoPackage, and shapefiles in `.zip` with multiple CRS.
* Geodesic (ellipsoidal) measurements as an alternative/cross-check mode, and perimeter for polygons.
* Geometry repair (`make_valid`), antimeridian splitting, and 3D length.
* Spatial indexing (PostGIS) for bounding-box queries; vector-tile / GeoJSON export of results.
* Authentication, per-user storage, rate limiting and Alembic migrations.
* CI: a GitHub Actions workflow running the test-suite on every push (Python 3.10-3.12 matrix).
* Pin dependency versions with a lock file for fully reproducible installs.

---

Author: **Ujjwal Sharma**