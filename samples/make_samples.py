"""Regenerates the sample inputs in this folder:  python samples/make_samples.py"""
import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

HERE = Path(__file__).parent

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Indore survey</name>
    <Folder>
      <name>Plots</name>
      <Placemark>
        <name>Plot A</name>
        <description>Residential plot</description>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>
          75.8577,22.7196,0 75.8667,22.7196,0 75.8667,22.7286,0 75.8577,22.7286,0 75.8577,22.7196,0
        </coordinates></LinearRing></outerBoundaryIs></Polygon>
      </Placemark>
      <Placemark>
        <name>Road 1</name>
        <LineString><coordinates>75.85,22.70,0 75.86,22.71,0 75.88,22.72,0</coordinates></LineString>
      </Placemark>
      <Placemark>
        <name>Survey point</name>
        <Point><coordinates>75.8577,22.7196,0</coordinates></Point>
      </Placemark>
    </Folder>
  </Document>
</kml>
"""


def main() -> None:
    (HERE / "survey.kml").write_text(KML, encoding="utf-8")

    # EPSG:4326 shapefile with all three geometry kinds mixed (different files per kind,
    # because a shapefile holds a single geometry type) -> zip two shapefiles together.
    polys = gpd.GeoDataFrame(
        {"name": ["Plot A", "Plot B"], "landuse": ["residential", "farm"]},
        geometry=[
            Polygon([(75.85, 22.70), (75.86, 22.70), (75.86, 22.71), (75.85, 22.71)]),
            Polygon([(75.90, 22.70), (75.92, 22.70), (75.92, 22.72), (75.90, 22.72)],
                    holes=[[(75.905, 22.705), (75.915, 22.705), (75.915, 22.715), (75.905, 22.715)]]),
        ],
        crs=4326,
    )
    lines = gpd.GeoDataFrame(
        {"name": ["Road 1"], "lanes": [2]},
        geometry=[LineString([(75.85, 22.70), (75.86, 22.71), (75.88, 22.72)])],
        crs=4326,
    )
    points = gpd.GeoDataFrame({"name": ["Benchmark"]}, geometry=[Point(75.8577, 22.7196)], crs=4326)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        polys.to_file(tmp / "plots.shp")
        lines.to_file(tmp / "roads.shp")
        points.to_file(tmp / "benchmarks.shp")
        shutil.make_archive(str(HERE / "survey_wgs84"), "zip", tmp)

    # same polygon in a projected CRS (UTM 43N) to show projected input also works
    with tempfile.TemporaryDirectory() as tmp:
        polys.to_crs(32643).to_file(Path(tmp) / "plots_utm.shp")
        shutil.make_archive(str(HERE / "plots_utm43n"), "zip", tmp)


if __name__ == "__main__":
    main()
