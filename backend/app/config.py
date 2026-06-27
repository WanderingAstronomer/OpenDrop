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

    app_env: str = "dev"  # set APP_ENV=prod to enforce the secrets guard below
    content_denylist: str = ""  # optional comma-separated words rejected in submissions

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def assert_production_secrets(self) -> None:
        """In APP_ENV=prod, refuse to boot with known-insecure defaults. No-op in dev."""
        if self.app_env.lower() != "prod":
            return
        problems = []
        if self.ip_hash_salt in ("", "change-me-in-prod"):
            problems.append("IP_HASH_SALT is unset/default (per-IP hashes become predictable)")
        if self.turnstile_secret.startswith(("1x0000", "2x0000", "3x0000")):
            problems.append("TURNSTILE_SECRET is a Cloudflare TEST key (bot protection disabled)")
        if ":opendrop@" in self.database_url:
            problems.append("DATABASE_URL still uses the default 'opendrop' password")
        if problems:
            raise RuntimeError(
                "Refusing to start in production with insecure defaults:\n  - " + "\n  - ".join(problems)
            )


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
