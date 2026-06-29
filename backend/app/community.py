"""Shared helpers for the engagement-tiered trust model (mirrors migration 0006).

The Python tier functions MUST match the SQL functions correction_required_support /
retire_deny_floor in migrations/0006_corrections_and_signals.sql — they exist only so the API
can report a location's tier/threshold without a second round-trip. The DB is the source of
truth for what actually auto-applies; these are for display.
"""

# Engagement tier cutoffs: Cold E<3 · Warm 3..14 · Hot E>=15.
_WARM_FLOOR = 3
_HOT_FLOOR = 15


def engagement_tier(engagement: int) -> str:
    if engagement < _WARM_FLOOR:
        return "cold"
    if engagement < _HOT_FLOOR:
        return "warm"
    return "hot"


def required_support(engagement: int) -> int:
    """Weighted support a correction needs to auto-apply at this engagement level."""
    if engagement < _WARM_FLOOR:
        return 1
    if engagement < _HOT_FLOOR:
        return 2
    return 4


def retire_deny_floor(engagement: int) -> int:
    """Deny count needed to retire a location at this engagement level."""
    if engagement < _WARM_FLOOR:
        return 2
    if engagement < _HOT_FLOOR:
        return 4
    return 8


# Per-attribute display metadata (also bounds the accepted value range in the router).
ATTRIBUTE_MAX = {"safety": 3, "condition": 3, "bins": 50}


async def attribute_aggregates(conn, loc_id: int) -> dict:
    """{attribute: {count, avg, median}} over all community ratings for a location."""
    cur = await conn.execute(
        """SELECT attribute,
                  count(*)                                                            AS n,
                  round(avg(value)::numeric, 2)                                       AS avg_value,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY value)::numeric, 1) AS median_value
           FROM attribute_votes WHERE location_id = %s GROUP BY attribute""",
        (loc_id,),
    )
    out: dict = {}
    for r in await cur.fetchall():
        out[r["attribute"]] = {
            "count": r["n"],
            "avg": float(r["avg_value"]),
            "median": float(r["median_value"]),
        }
    return out
