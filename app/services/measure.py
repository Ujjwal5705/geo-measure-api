"""Geometry measurement: area for polygons, length for lines, nothing for points."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.services import crs as crs_utils

MEASURED = "MEASURED"
NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. Point: valid, nothing to measure
UNSUPPORTED = "UNSUPPORTED"  # e.g. GeometryCollection, missing geometry
ERROR = "ERROR"  # unexpected failure on this one feature

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString", "LinearRing"}
POINTLIKE = {"Point", "MultiPoint"}


@dataclass
class MeasurementResult:
    status: str
    type: str | None = None  # "area" | "length"
    value: float | None = None  # square metres or metres
    projected_crs: str | None = None
    method: str | None = None
    warnings: list[str] = field(default_factory=list)


def measure_geometry(
    geom: BaseGeometry | None, src_crs: CRS | None
) -> MeasurementResult:
    """Never raises: any failure is reported through the returned status."""
    try:
        return _measure(geom, src_crs)
    except Exception as exc:  # one bad feature must not sink the whole file
        return MeasurementResult(ERROR, warnings=[f"Measurement failed: {exc}"])


def _measure(geom: BaseGeometry | None, src_crs: CRS | None) -> MeasurementResult:
    if geom is None or geom.is_empty:
        return MeasurementResult(UNSUPPORTED, warnings=["Feature has no geometry."])

    gtype = geom.geom_type
    if gtype in POINTLIKE:
        return MeasurementResult(NOT_APPLICABLE, warnings=[])
    if gtype not in POLYGONAL | LINEAR:
        return MeasurementResult(
            UNSUPPORTED,
            warnings=[f"Measurement is not supported for geometry type '{gtype}'."],
        )
    if src_crs is None:
        return MeasurementResult(ERROR, warnings=["Unknown CRS; cannot measure."])

    geom = shapely.force_2d(geom)  # altitude never contributes to planar area/length
    warnings: list[str] = []
    if gtype in POLYGONAL and not geom.is_valid:
        warnings.append(
            "Polygon geometry is invalid (e.g. self-intersection); area may be unreliable."
        )

    lonlat = _to_wgs84(geom, src_crs)
    _assert_finite(lonlat)
    centroid = lonlat.centroid
    lon, lat = centroid.x, centroid.y
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(
            f"Coordinates fall outside the valid lon/lat range ({lon:.3f}, {lat:.3f})."
        )

    if gtype in POLYGONAL:
        target = crs_utils.laea_crs(lon, lat)
        projected = _project(lonlat, crs_utils.WGS84, target)
        value, mtype, method = (
            projected.area,
            "area",
            "Lambert azimuthal equal-area centred on feature centroid",
        )
        label = "custom LAEA " + f"(lon_0={round(lon, 1)}, lat_0={round(lat, 1)})"
    else:
        target = CRS.from_epsg(crs_utils.utm_epsg(lon, lat))
        projected = _project(lonlat, crs_utils.WGS84, target)
        value, mtype, method = (
            projected.length,
            "length",
            "UTM zone of feature centroid",
        )
        label = crs_utils.describe_crs(target)

    if not math.isfinite(value):
        raise ValueError("Projection produced a non-finite result.")
    return MeasurementResult(MEASURED, mtype, float(value), label, method, warnings)


def _to_wgs84(geom: BaseGeometry, src: CRS) -> BaseGeometry:
    if src == crs_utils.WGS84 or src.to_epsg() == 4326:
        return geom
    return _project(geom, src, crs_utils.WGS84)


def _project(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    """Reproject a 2D geometry (vectorised; geometry must already be 2D)."""
    transformer = crs_utils.get_transformer(src, dst)

    def _fn(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _fn)


def _assert_finite(geom: BaseGeometry) -> None:
    minx, miny, maxx, maxy = geom.bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise ValueError("Coordinates could not be transformed to WGS84.")
