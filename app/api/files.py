import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import schemas
from app.config import settings
from app.database import get_db
from app.models import Feature, UploadedFile
from app.services.ingest import IngestError, detect_file_type, process_file
from app.services.measure import ERROR, MEASURED, NOT_APPLICABLE, UNSUPPORTED

router = APIRouter(prefix="/api/files", tags=["files"])

_AREA_CONV = {"hectares": 1e-4, "square_kilometers": 1e-6}
_LENGTH_CONV = {"kilometers": 1e-3}


def _get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return record


def _safe_name(name: str) -> str:
    name = Path(name or "upload").name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:200] or "upload"


@router.post("/", response_model=schemas.FileOut, status_code=status.HTTP_201_CREATED)
def upload_file(
    file: UploadFile = File(..., description="A .zip containing a Shapefile, or a .kml file"),
    assume_crs: str | None = Form(
        None, description="Only used for shapefiles lacking a .prj, e.g. 'EPSG:4326'"
    ),
    db: Session = Depends(get_db),
):
    """Upload, parse and measure a geospatial file in one request."""
    original_name = file.filename or "upload"
    try:
        file_type = detect_file_type(original_name)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record = UploadedFile(filename=original_name, file_type=file_type)
    db.add(record)
    db.commit()

    folder = settings.uploads_dir / record.id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / _safe_name(original_name)

    # stream to disk while enforcing the size limit
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    with open(dest, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                shutil.rmtree(folder, ignore_errors=True)
                db.delete(record)
                db.commit()
                raise HTTPException(status_code=413, detail=f"File exceeds the {settings.max_upload_mb} MB limit.")
            out.write(chunk)

    if written == 0:
        record.status, record.error = "FAILED", "Uploaded file is empty."
        db.commit()
        raise HTTPException(status_code=422, detail={"message": record.error, "id": record.id})

    try:
        process_file(db, record, dest, assume_crs)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "id": record.id})
    return record


@router.get("/", response_model=schemas.FileList)
def list_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(UploadedFile)) or 0
    rows = db.scalars(
        select(UploadedFile).order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset, "results": rows}


@router.get("/{file_id}/", response_model=schemas.FileOut)
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _get_file_or_404(db, file_id)


@router.delete("/{file_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, db: Session = Depends(get_db)):
    record = _get_file_or_404(db, file_id)
    db.delete(record)
    db.commit()
    shutil.rmtree(settings.uploads_dir / file_id, ignore_errors=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{file_id}/features/", response_model=schemas.FeatureList)
def list_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    geometry_type: str | None = Query(None, description="Filter, e.g. Polygon"),
    db: Session = Depends(get_db),
):
    _get_file_or_404(db, file_id)
    q = select(Feature).where(Feature.file_id == file_id)
    if geometry_type:
        q = q.where(Feature.geometry_type == geometry_type)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Feature.idx).limit(limit).offset(offset)).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "results": [
            schemas.FeatureOut(
                id=f.idx, layer=f.layer, geometry_type=f.geometry_type, geometry=f.geometry,
                crs=f.crs, properties=f.properties, measurement_status=f.measurement_status,
            )
            for f in rows
        ],
    }


@router.get("/{file_id}/measurements/", response_model=schemas.MeasurementList)
def get_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    geometry_type: str | None = Query(None, description="Filter, e.g. Polygon"),
    db: Session = Depends(get_db),
):
    record = _get_file_or_404(db, file_id)
    if record.status != "COMPLETED":
        raise HTTPException(status_code=409, detail=f"File is not processed (status: {record.status}).")

    # file-level summary computed in SQL so it is cheap even for large files
    def _count(s):
        return func.coalesce(func.sum(case((Feature.measurement_status == s, 1), else_=0)), 0)

    def _sum(t):
        return func.coalesce(func.sum(case((Feature.measurement_type == t, Feature.measurement_value), else_=0.0)), 0.0)

    agg = db.execute(
        select(
            func.count(), _count(MEASURED), _count(NOT_APPLICABLE), _count(UNSUPPORTED), _count(ERROR),
            _sum("area"), _sum("length"),
        ).where(Feature.file_id == file_id)
    ).one()
    summary = schemas.MeasurementSummary(
        feature_count=agg[0], measured=agg[1], not_applicable=agg[2], unsupported=agg[3],
        error=agg[4], total_area_m2=agg[5], total_length_m=agg[6],
    )

    q = select(Feature).where(Feature.file_id == file_id)
    if geometry_type:
        q = q.where(Feature.geometry_type == geometry_type)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Feature.idx).limit(limit).offset(offset)).all()

    results = []
    for f in rows:
        measurement = None
        if f.measurement_status == MEASURED and f.measurement_value is not None:
            is_area = f.measurement_type == "area"
            conv = _AREA_CONV if is_area else _LENGTH_CONV
            measurement = schemas.Measurement(
                type=f.measurement_type,
                value=f.measurement_value,
                unit="square_meters" if is_area else "meters",
                conversions={k: f.measurement_value * v for k, v in conv.items()},
                projected_crs=f.projected_crs,
                method=f.measurement_method,
            )
        results.append(
            schemas.MeasurementItem(
                feature_id=f.idx, layer=f.layer, geometry_type=f.geometry_type,
                status=f.measurement_status, measurement=measurement, warnings=f.warnings or [],
            )
        )
    return {
        "file_id": file_id, "total": total, "limit": limit, "offset": offset,
        "summary": summary, "results": results,
    }
