"""DB write helpers shared by the loader and dedup. Synchronous psycopg."""
from __future__ import annotations

from psycopg.types.json import Json

_POINT = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"  # (lon, lat)

# Canonical display columns populated from a record dict.
_LOC_COLS = ["name", "org_type", "org_name", "brand_key", "address_line", "house_number",
             "city", "state", "postal_code", "hours", "hours_raw", "accepted_items", "phone", "website"]


def get_source(conn, code):
    return conn.execute(
        "SELECT code, storage_policy, authority_weight FROM sources WHERE code = %s", (code,)
    ).fetchone()


def start_scrape_log(conn, code) -> int:
    row = conn.execute("INSERT INTO scrape_log (source_code) VALUES (%s) RETURNING id", (code,)).fetchone()
    conn.commit()
    return row["id"]


def finish_scrape_log(conn, log_id, status, fetched, upserted, new, merged, detail=None, error=None):
    conn.execute(
        """UPDATE scrape_log SET run_finished_at=now(), status=%s, records_fetched=%s,
               records_upserted=%s, records_new=%s, records_merged=%s, detail=%s, error=%s
           WHERE id=%s""",
        (status, fetched, upserted, new, merged, Json(detail) if detail is not None else None, error, log_id),
    )
    conn.commit()


def insert_location(conn, rec: dict) -> int:
    row = conn.execute(
        f"""INSERT INTO locations
            (geom, name, org_type, org_name, brand_key, address_line, house_number,
             city, state, postal_code, hours, hours_raw, accepted_items, phone, website)
            VALUES ({_POINT}, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (rec["lon"], rec["lat"], rec["name"], rec.get("org_type") or "other", rec.get("org_name"),
         rec.get("brand_key"), rec.get("address_line"), rec.get("house_number"), rec.get("city"),
         rec.get("state"), rec.get("postal_code"),
         Json(rec["hours"]) if rec.get("hours") else None, rec.get("hours_raw"),
         rec.get("accepted_items"), rec.get("phone"), rec.get("website")),
    ).fetchone()
    return row["id"]


def upsert_source(conn, loc_id, code, source_ref, lon, lat, source_name, payload: dict):
    conn.execute(
        f"""INSERT INTO location_sources
            (location_id, source_code, source_ref, source_geom, source_name, payload)
            VALUES (%s, %s, %s, {_POINT}, %s, %s)
            ON CONFLICT (source_code, source_ref) DO UPDATE
              SET location_id = EXCLUDED.location_id, last_seen_at = now(),
                  source_geom = EXCLUDED.source_geom, payload = EXCLUDED.payload""",
        (loc_id, code, source_ref, lon, lat, source_name, Json(payload)),
    )


def refresh_location_fields(conn, loc_id):
    """Field-provenance invariant: canonical display columns come ONLY from the
    highest-authority INGEST source (then most recent). Lower/enrich sources never win.
    Coalesce so a top source missing a field keeps the existing value."""
    row = conn.execute(
        """SELECT ls.payload FROM location_sources ls
           JOIN sources s ON s.code = ls.source_code AND s.storage_policy = 'ingest'
           WHERE ls.location_id = %s
           ORDER BY s.authority_weight DESC, ls.last_seen_at DESC LIMIT 1""",
        (loc_id,),
    ).fetchone()
    if not row or not row["payload"]:
        return
    p = row["payload"]
    conn.execute(
        """UPDATE locations SET
              name          = COALESCE(%s, name),
              org_type      = COALESCE(%s::org_type, org_type),
              org_name      = COALESCE(%s, org_name),
              brand_key     = COALESCE(%s, brand_key),
              address_line  = COALESCE(%s, address_line),
              house_number  = COALESCE(%s, house_number),
              city          = COALESCE(%s, city),
              state         = COALESCE(%s, state),
              postal_code   = COALESCE(%s, postal_code),
              hours         = COALESCE(%s::jsonb, hours),
              hours_raw     = COALESCE(%s, hours_raw),
              accepted_items= COALESCE(%s::text[], accepted_items),
              phone         = COALESCE(%s, phone),
              website       = COALESCE(%s, website)
           WHERE id = %s""",
        (p.get("name"), p.get("org_type"), p.get("org_name"), p.get("brand_key"),
         p.get("address_line"), p.get("house_number"), p.get("city"), p.get("state"),
         p.get("postal_code"), Json(p["hours"]) if p.get("hours") else None, p.get("hours_raw"),
         p.get("accepted_items"), p.get("phone"), p.get("website"), loc_id),
    )
