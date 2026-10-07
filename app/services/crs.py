"""Projected-CRS selection.

Strategy
--------
Every geometry is first normalised to WGS84 lon/lat (EPSG:4326) and then projected
into a *local* metric CRS chosen from the feature's own centroid:

* **Area**   -> Lambert Azimuthal Equal-Area centred on the centroid. The projection is
  equal-area by construction, so area distortion is ~0 anywhere on the globe (poles
  included) and no zone logic is needed.
* **Length** -> UTM zone of the centroid (UPS polar stereographic beyond 84N / 80S).
  UTM is conformal with a scale error <= 0.04 % inside its zone, which is the usual
  surveying standard for distances.
"""
from __future__ import annotations

import math
from functools import lru_cache

from pyproj import CRS, Transformer

WGS84 = CRS.from_epsg(4326)


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the UTM (or UPS at the poles) system covering a lon/lat point."""
    if lat > 84:
        return 32661  # WGS84 / UPS North
    if lat < -80:
        return 32761  # WGS84 / UPS South
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def laea_crs(lon: float, lat: float) -> CRS:
    """Equal-area CRS centred on lon/lat (coordinates rounded to share cached objects)."""
    return _laea_cached(round(lon, 1), round(lat, 1))


@lru_cache(maxsize=512)
def _laea_cached(lon: float, lat: float) -> CRS:
    return CRS.from_proj4(f"+proj=laea +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")


@lru_cache(maxsize=512)
def get_transformer(src: CRS, dst: CRS) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def describe_crs(crs: CRS | None) -> str | None:
    """'EPSG:4326' when the CRS has an authority code, otherwise its name."""
    if crs is None:
        return None
    try:
        epsg = crs.to_epsg(min_confidence=70)
    except Exception:  # pragma: no cover - defensive
        epsg = None
    if epsg:
        return f"EPSG:{epsg}"
    auth = crs.to_authority(min_confidence=70)
    return f"{auth[0]}:{auth[1]}" if auth else (crs.name or crs.to_wkt())
