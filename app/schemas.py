from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class LayerInfo(BaseModel):
    name: str
    feature_count: int
    crs: str | None = None


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    feature_count: int
    crs: str | None
    status: str
    error: str | None = None
    layers: list[LayerInfo] | None = None
    created_at: datetime


class FeatureOut(BaseModel):
    id: int  # 0-based feature index within the file
    layer: str | None
    geometry_type: str | None
    geometry: dict[str, Any] | None  # GeoJSON, expressed in the file's native CRS
    crs: str | None
    properties: dict[str, Any]
    measurement_status: str


class Measurement(BaseModel):
    type: str  # "area" | "length"
    value: float
    unit: str  # "square_meters" | "meters"
    conversions: dict[str, float]
    projected_crs: str | None
    method: str | None


class MeasurementItem(BaseModel):
    feature_id: int
    layer: str | None
    geometry_type: str | None
    status: str  # MEASURED | NOT_APPLICABLE | UNSUPPORTED | ERROR
    measurement: Measurement | None
    warnings: list[str]


class MeasurementSummary(BaseModel):
    feature_count: int
    measured: int
    not_applicable: int
    unsupported: int
    error: int
    total_area_m2: float
    total_length_m: float


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class FeatureList(Page):
    results: list[FeatureOut]


class MeasurementList(Page):
    file_id: str
    summary: MeasurementSummary
    results: list[MeasurementItem]


class FileList(Page):
    results: list[FileOut]
