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
    # The migration this image's code expects to be present. At boot the API checks schema_migrations
    # for this row: in prod it REFUSES to start if missing (blocks 'new code vs old schema' drift);
    # in dev it only warns. Bump this whenever a new migration is required by the code.
    expected_schema_version: str = "0010_moderation_audit_and_thresholds.sql"
    # Extra comma-separated words rejected in submissions, MERGED with the baked-in default
    # denylist (moderation._DEFAULT_DENYLIST). Operators extend, they don't replace.
    content_denylist: str = ""

    # Operator/moderation auth. Empty => all operator endpoints return 404 (feature disabled,
    # surface hidden). Set OPERATOR_TOKEN to a long random secret to enable takedown/revert.
    # Compared in constant time; sent by the operator as the `X-Operator-Token` header.
    operator_token: str = ""

    media_dir: str = "/app/media"
    image_max_bytes: int = 6_000_000
    image_uploads_per_ip_per_day: int = 8
    # Global media disk ceiling (sum of stored bytes). Uploads past this are refused with 507
    # so a flood of photos can't fill the host volume. ~5 GB default; raise via MEDIA_MAX_TOTAL_BYTES.
    media_max_total_bytes: int = 5_000_000_000

    # Public "report this" rate limit (per IP per day), shared across location + image reports.
    reports_per_ip_per_day: int = 30
    # Distinct unresolved reporters needed to auto-hide a PHOTO (reversible soft-hide via removed_at,
    # file kept). A lone report only files a complaint — this keeps a single actor from hiding any
    # photo while still pulling genuinely-flagged UGC fast. Locations are NEVER auto-hidden.
    report_image_hide_threshold: int = 2
    # Hard cap on rows returned by the full-dataset /export dump (defence against unbounded scans).
    export_max_rows: int = 100000

    # --- Community pin corrections + signals ---
    corrections_per_ip_per_day: int = 15
    attributes_per_ip_per_day: int = 60  # distinct (location, attribute) ratings one IP may write/day
    # A correction is an accuracy fix, not a relocation. MUST stay in sync with the 2 km guard
    # baked into recompute_correction() — anchored to locations.origin_geom as of migration
    # 0007_correction_anchor_and_retire_fix.sql (both the API guard and the trigger measure the
    # move from the immutable origin, not the current geom, so corrections cannot walk a pin).
    correction_max_move_m: int = 2000
    # Radius within which a client may claim GPS corroboration ("I'm standing here"). Used by the
    # frontend only — the server never receives coordinates, just the resulting boolean.
    gps_corroboration_radius_m: int = 75

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
