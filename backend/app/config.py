from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://opendrop:opendrop@db:5432/opendrop"
    cors_origins: str = "http://localhost:8080"
    ip_hash_salt: str = "change-me-in-prod"
    submit_per_ip_per_day: int = 10
    point_cap: int = 2000
    cluster_cap: int = 400

    turnstile_secret: str = "1x0000000000000000000000000000000AA"
    turnstile_sitekey: str = "1x00000000000000000000AA"

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    seed_region_bbox: str = "39.80,-83.25,40.18,-82.75"

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

# Confidence -> UI bucket thresholds (mirror DATA_MODEL §7).
BUCKETS = {"high": 70, "medium": 40, "low": 25}


def bucket(confidence: float | None) -> str:
    c = confidence or 0
    if c >= BUCKETS["high"]:
        return "high"
    if c >= BUCKETS["medium"]:
        return "medium"
    return "low"
