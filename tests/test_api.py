import io
import shutil
import zipfile
from pathlib import Path

import pytest
from pyproj import Geod
from shapely.geometry import shape

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _upload(client, name, content, **data):
    return client.post("/api/files/", files={"file": (name, content)}, data=data)


def _sample(name):
    return (SAMPLES / name).read_bytes()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_kml_upload_info_and_measurements(client):
    r = _upload(client, "survey.kml", _sample("survey.kml"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"

    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["id"] == body["id"] and info["feature_count"] == 3

    m = client.get(f"/api/files/{body['id']}/measurements/").json()
    by_type = {i["geometry_type"]: i for i in m["results"]}
    assert set(by_type) == {"Polygon", "LineString", "Point"}
    # 0.009 deg lon x 0.009 deg lat near 22.7N  ~ 0.92 km x 1.0 km
    assert by_type["Polygon"]["measurement"]["type"] == "area"
    assert 8.5e5 < by_type["Polygon"]["measurement"]["value"] < 1.0e6
    assert by_type["LineString"]["measurement"]["unit"] == "meters"
    assert by_type["Point"]["measurement"] is None and by_type["Point"]["status"] == "NOT_APPLICABLE"
    assert m["summary"]["measured"] == 2 and m["summary"]["not_applicable"] == 1


def test_features_endpoint_has_geometry_crs_properties(client):
    fid = _upload(client, "survey.kml", _sample("survey.kml")).json()["id"]
    feats = client.get(f"/api/files/{fid}/features/").json()
    assert feats["total"] == 3
    poly = next(f for f in feats["results"] if f["geometry_type"] == "Polygon")
    assert poly["geometry"]["type"] == "Polygon" and poly["crs"] == "EPSG:4326"
    assert poly["properties"]["Name"] == "Plot A"
    only_pts = client.get(f"/api/files/{fid}/features/?geometry_type=Point").json()
    assert only_pts["total"] == 1


def test_shapefile_zip_wgs84(client):
    r = _upload(client, "survey_wgs84.zip", _sample("survey_wgs84.zip"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["feature_count"] == 4 and body["crs"] == "EPSG:4326"
    assert {l["name"] for l in body["layers"]} == {"plots", "roads", "benchmarks"}
    m = client.get(f"/api/files/{body['id']}/measurements/").json()
    assert m["summary"]["total_area_m2"] > 0 and m["summary"]["total_length_m"] > 0
    feats = client.get(f"/api/files/{body['id']}/features/?geometry_type=Polygon").json()["results"]
    b = next(f for f in feats if f["properties"]["name"] == "Plot B")
    plot_b = next(i for i in m["results"] if i["feature_id"] == b["id"])
    # Plot B: 0.02 x 0.02 deg box with a 0.01 x 0.01 hole -> hole must be subtracted
    expected = abs(Geod(ellps="WGS84").geometry_area_perimeter(shape(b["geometry"]))[0])
    assert plot_b["measurement"]["value"] == pytest.approx(expected, rel=1e-3)


def test_projected_and_geographic_agree(client):
    a = _upload(client, "survey_wgs84.zip", _sample("survey_wgs84.zip")).json()["id"]
    b = _upload(client, "plots_utm43n.zip", _sample("plots_utm43n.zip"))
    assert b.status_code == 201 and b.json()["crs"] == "EPSG:32643"
    ma = client.get(f"/api/files/{a}/measurements/?geometry_type=Polygon").json()["results"]
    mb = client.get(f"/api/files/{b.json()['id']}/measurements/").json()["results"]
    for x, y in zip(ma, mb):
        assert x["measurement"]["value"] == pytest.approx(y["measurement"]["value"], rel=1e-6)


def test_pagination(client):
    fid = _upload(client, "survey.kml", _sample("survey.kml")).json()["id"]
    page = client.get(f"/api/files/{fid}/measurements/?limit=1&offset=2").json()
    assert page["total"] == 3 and len(page["results"]) == 1 and page["results"][0]["feature_id"] == 2


def test_unsupported_extension(client):
    r = _upload(client, "data.geojson", b"{}")
    assert r.status_code == 422


def test_corrupt_files(client):
    assert _upload(client, "bad.zip", b"not a zip").status_code == 422
    assert _upload(client, "bad.kml", b"<kml><oops").status_code == 422
    assert _upload(client, "empty.kml", b"").status_code == 422


def test_failed_upload_is_recorded(client):
    r = _upload(client, "bad.zip", b"not a zip")
    fid = r.json()["detail"]["id"]
    info = client.get(f"/api/files/{fid}/").json()
    assert info["status"] == "FAILED" and info["error"]
    assert client.get(f"/api/files/{fid}/measurements/").status_code == 409


def test_zip_without_shp_or_prj(client, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hi")
    assert "does not contain a .shp" in _upload(client, "x.zip", buf.getvalue()).json()["detail"]["message"]

    # shapefile with the .prj stripped
    src = tmp_path / "src"
    with zipfile.ZipFile(SAMPLES / "survey_wgs84.zip") as z:
        z.extractall(src)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in src.glob("plots.*"):
            if p.suffix != ".prj":
                z.write(p, p.name)
    r = _upload(client, "noprj.zip", buf.getvalue())
    assert r.status_code == 422 and "no CRS" in r.json()["detail"]["message"]
    ok = _upload(client, "noprj.zip", buf.getvalue(), assume_crs="EPSG:4326")
    assert ok.status_code == 201 and ok.json()["crs"] == "EPSG:4326"


def test_zip_slip_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.shp", "x")
    r = _upload(client, "evil.zip", buf.getvalue())
    assert r.status_code == 422 and "unsafe" in r.json()["detail"]["message"]


def test_404_and_delete(client):
    assert client.get("/api/files/nope/").status_code == 404
    assert client.get("/api/files/nope/measurements/").status_code == 404
    fid = _upload(client, "survey.kml", _sample("survey.kml")).json()["id"]
    assert client.delete(f"/api/files/{fid}/").status_code == 204
    assert client.get(f"/api/files/{fid}/").status_code == 404


def test_list_files(client):
    _upload(client, "survey.kml", _sample("survey.kml"))
    r = client.get("/api/files/?limit=2").json()
    assert r["total"] >= 1 and len(r["results"]) <= 2
