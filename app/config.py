"""Application settings. Override any value with an environment variable prefixed GEO_
(e.g. GEO_MAX_UPLOAD_MB=100) or via a local .env file."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    data_dir: Path = BASE_DIR / "data"
    database_url: str | None = None  # defaults to sqlite file inside data_dir
    max_upload_mb: int = 50  # compressed upload size limit
    max_unzipped_mb: int = 300  # zip-bomb guard
    max_features: int = 200_000  # refuse absurdly large files

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'geo.db'}"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
