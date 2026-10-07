"""File ingestion: validate -> (safely unzip) -> read layers -> build Feature rows."""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import CRS
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Feature, UploadedFile
from app.services import crs as crs_utils
from app.services.measure import measure_geometry

SHAPEFILE_EXTS = {".zip"}
KML_EXTS = {".kml"}


class IngestError(Exception):
    """A problem with the user's file (maps to HTTP 422)."""


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in SHAPEFILE_EXTS:
        return "shapefile"
    if ext in KML_EXTS:
        return "kml"
    raise IngestError("Unsupported file type. Upload a .zip (containing a Shapefile) or a .kml file.")


# --------------------------------------------------------------------------- reading
def read_layers(path: Path, file_type: str, assume_crs: str | None) -> list[tuple[str, gpd.GeoDataFrame]]:
    """Return [(layer_name, GeoDataFrame)] for the uploaded file."""
    with tempfile.TemporaryDirectory(prefix="geo_") as tmp:
        if file_type == "shapefile":
            sources = _shapefiles_from_zip(path, Path(tmp))
        else:
            sources = [(path.stem, path, None)]

        layers: list[tuple[str, gpd.GeoDataFrame]] = []
        for name, src, layer in sources:
            try:
                if layer is None and file_type == "kml":
                    for lname in _kml_layer_names(src):
                        layers.append((lname, _read(src, lname, assume_crs, file_type)))
                else:
                    layers.append((name, _read(src, layer, assume_crs, file_type)))
            except IngestError:
                raise
            except Exception as exc:
                raise IngestError(f"Could not read geospatial data from '{src.name}': {exc}") from exc
    if not layers:
        raise IngestError("No readable layers were found in the file.")
    return layers


def _kml_layer_names(path: Path) -> list[str]:
    names = [str(row[0]) for row in pyogrio.list_layers(path)]
    if not names:
        raise IngestError("The KML file contains no layers.")
    return names


def _read(path: Path, layer: str | None, assume_crs: str | None, file_type: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, layer=layer, engine="pyogrio")
    if gdf.crs is None:
        if file_type == "kml":
            gdf = gdf.set_crs(4326)  # KML is defined to be WGS84 lon/lat
        elif assume_crs:
            try:
                gdf = gdf.set_crs(CRS.from_user_input(assume_crs))
            except Exception as exc:
                raise IngestError(f"Invalid assume_crs value '{assume_crs}'.") from exc
        else:
            raise IngestError(
                "The shapefile has no CRS (.prj file missing or unreadable). Include the .prj file, "
                "or pass the form field assume_crs (e.g. EPSG:4326) if you know the coordinate system."
            )
    return gdf


def _shapefiles_from_zip(zip_path: Path, workdir: Path) -> list[tuple[str, Path, None]]:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise IngestError("The uploaded file is not a valid zip archive.") from exc

    with zf:
        members = [m for m in zf.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX")]
        if sum(m.file_size for m in members) > settings.max_unzipped_mb * 1024 * 1024:
            raise IngestError(f"Archive expands to more than {settings.max_unzipped_mb} MB; refusing to extract.")
        root = workdir.resolve()
        for m in members:
            target = (root / m.filename).resolve()
            if root not in target.parents:  # zip-slip protection
                raise IngestError("Archive contains an unsafe file path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

    shps = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".shp" and not p.name.startswith("._"))
    if not shps:
        raise IngestError("The zip archive does not contain a .shp file.")
    for shp in shps:
        siblings = {p.suffix.lower() for p in shp.parent.glob(shp.stem + ".*")}
        missing = {".dbf"} - siblings
        if missing:
            raise IngestError(f"Shapefile '{shp.name}' is incomplete; missing: {', '.join(sorted(missing))}.")
    return [(shp.stem, shp, None) for shp in shps]


# --------------------------------------------------------------------------- JSON helpers
def _json_safe(value):
    """Convert numpy/pandas scalars (NaN, NaT, Timestamp, ...) into plain JSON values."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value if isinstance(value, (str, int, bool)) else str(value)


def _properties(row: pd.Series, geom_col: str) -> dict:
    props = {}
    for key, val in row.items():
        if key == geom_col:
            continue
        clean = _json_safe(val)
        if clean is None or clean == "":
            continue  # KML exports are full of empty fields; keep payloads small
        props[str(key)] = clean
    return props


# --------------------------------------------------------------------------- orchestration
def process_file(db: Session, record: UploadedFile, path: Path, assume_crs: str | None = None) -> UploadedFile:
    """Read the file, measure every feature and persist the results.

    On failure the record is marked FAILED with the reason, then IngestError is re-raised.
    """
    try:
        layers = read_layers(path, record.file_type, assume_crs)
        total = sum(len(gdf) for _, gdf in layers)
        if total > settings.max_features:
            raise IngestError(f"File has {total} features; the limit is {settings.max_features}.")

        rows: list[Feature] = []
        crs_names: list[str] = []
        idx = 0
        for layer_name, gdf in layers:
            layer_crs = gdf.crs
            crs_label = crs_utils.describe_crs(layer_crs)
            crs_names.append(crs_label or "UNKNOWN")
            geom_col = gdf.geometry.name
            for _, row in gdf.iterrows():
                geom = row[geom_col]
                if geom is not None and not isinstance(geom, shapely.geometry.base.BaseGeometry):
                    geom = None
                result = measure_geometry(geom, layer_crs)
                rows.append(
                    Feature(
                        file_id=record.id,
                        idx=idx,
                        layer=layer_name,
                        geometry_type=geom.geom_type if geom is not None and not geom.is_empty else None,
                        geometry=json.loads(shapely.to_geojson(geom)) if geom is not None and not geom.is_empty else None,
                        crs=crs_label,
                        properties=_properties(row, geom_col),
                        measurement_status=result.status,
                        measurement_type=result.type,
                        measurement_value=result.value,
                        projected_crs=result.projected_crs,
                        measurement_method=result.method,
                        warnings=result.warnings,
                    )
                )
                idx += 1

        unique_crs = sorted(set(crs_names))
        record.crs = unique_crs[0] if len(unique_crs) == 1 else "MIXED"
        record.layers = [{"name": n, "feature_count": len(g), "crs": c} for (n, g), c in zip(layers, crs_names)]
        record.feature_count = len(rows)
        db.add_all(rows)
        record.status = "COMPLETED"
        db.commit()
        return record
    except IngestError as exc:
        _mark_failed(db, record, str(exc))
        raise
    except Exception as exc:  # unexpected: still leave a FAILED record behind
        _mark_failed(db, record, f"Unexpected processing error: {exc}")
        raise IngestError(f"Unexpected processing error: {exc}") from exc


def _mark_failed(db: Session, record: UploadedFile, message: str) -> None:
    db.rollback()
    record.status = "FAILED"
    record.error = message
    db.merge(record)
    db.commit()
