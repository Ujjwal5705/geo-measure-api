from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import files
from app.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    version="1.0.0",
    description="Upload a zipped Shapefile or a KML and get per-feature area/length measurements.",
    lifespan=lifespan,
)
app.include_router(files.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
