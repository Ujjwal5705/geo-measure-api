import pytest
from pyproj import CRS, Geod, Transformer
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
import numpy as np
import shapely

from app.services.crs import utm_epsg
from app.services.measure import (
    ERROR,
    MEASURED,
    NOT_APPLICABLE,
    UNSUPPORTED,
    measure_geometry,
)

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")


def _to_4326(geom, epsg):
    t = Transformer.from_crs(epsg, 4326, always_xy=True)

    def fn(c):
        x, y = t.transform(c[:, 0], c[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, fn)


def test_polygon_area_matches_known_square():
    # 1 km x 1 km square defined in UTM 43N, delivered as lon/lat
    square = _to_4326(box(500_000, 2_500_000, 501_000, 2_501_000), 32643)
    r = measure_geometry(square, WGS84)
    assert r.status == MEASURED and r.type == "area"
    assert r.value == pytest.approx(
        1_000_000, rel=2e-3
    )  # UTM scale factor ~0.9996 => ~0.08%


@pytest.mark.parametrize(
    "lon,lat", [(75.86, 22.72), (-122.4, 37.8), (151.2, -33.9), (10, 70), (0, 89.5)]
)
def test_polygon_area_agrees_with_geodesic(lon, lat):
    poly = Polygon(
        [(lon, lat), (lon + 0.05, lat), (lon + 0.05, lat + 0.03), (lon, lat + 0.03)]
    )
    expected = abs(GEOD.geometry_area_perimeter(poly)[0])
    r = measure_geometry(poly, WGS84)
    assert r.value == pytest.approx(expected, rel=1e-3)


def test_line_length_agrees_with_geodesic():
    line = LineString([(75.85, 22.70), (75.86, 22.71), (75.88, 22.72)])
    r = measure_geometry(line, WGS84)
    assert r.status == MEASURED and r.type == "length"
    assert r.projected_crs == "EPSG:32643"
    assert r.value == pytest.approx(GEOD.geometry_length(line), rel=1e-3)


def test_degrees_are_never_used_directly():
    poly = box(75.0, 22.0, 75.1, 22.1)
    assert poly.area < 0.02  # in degrees^2
    assert measure_geometry(poly, WGS84).value > 1e7  # in m^2


def test_projected_input_crs_is_normalised():
    utm = box(500_000, 2_500_000, 500_100, 2_500_200)
    r = measure_geometry(utm, CRS.from_epsg(32643))
    assert r.value == pytest.approx(20_000, rel=2e-3)


def test_multipolygon_and_z_values():
    mp = MultiPolygon([box(75, 22, 75.01, 22.01), box(75.1, 22, 75.11, 22.01)])
    single = measure_geometry(box(75, 22, 75.01, 22.01), WGS84).value
    assert measure_geometry(mp, WGS84).value == pytest.approx(2 * single, rel=1e-3)
    p3d = Polygon(
        [(75, 22, 500), (75.01, 22, 500), (75.01, 22.01, 900), (75, 22.01, 900)]
    )
    assert measure_geometry(p3d, WGS84).status == MEASURED


def test_point_has_no_measurement():
    r = measure_geometry(Point(75.8, 22.7), WGS84)
    assert r.status == NOT_APPLICABLE and r.value is None


def test_unsupported_and_missing_geometry_are_graceful():
    gc = GeometryCollection([Point(1, 1), LineString([(0, 0), (1, 1)])])
    assert measure_geometry(gc, WGS84).status == UNSUPPORTED
    assert measure_geometry(None, WGS84).status == UNSUPPORTED
    assert measure_geometry(Polygon(), WGS84).status == UNSUPPORTED


def test_bad_inputs_become_error_not_exception():
    assert measure_geometry(box(0, 0, 1, 1), None).status == ERROR
    assert (
        measure_geometry(box(500, 500, 600, 600), WGS84).status == ERROR
    )  # impossible lon/lat


def test_invalid_polygon_emits_warning():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    r = measure_geometry(bowtie, WGS84)
    assert r.status == MEASURED and r.warnings


def test_utm_zone_selection():
    assert utm_epsg(75.86, 22.72) == 32643  # Indore
    assert utm_epsg(-122.4, 37.8) == 32610  # San Francisco
    assert utm_epsg(151.2, -33.9) == 32756  # Sydney
    assert utm_epsg(180.0, 10) == 32601  # antimeridian wraps to zone 1
    assert utm_epsg(0, 88) == 32661  # polar
