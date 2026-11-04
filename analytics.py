"""Bounded, hourly cost analytics from the existing local ledger only."""

from __future__ import annotations

from datetime import timedelta

from .ledger import MONEY, amount
from .pricing import CHINA_TZ


def consumption_report(db, now, period):
    """Caller holds the ledger lock. Costs follow request-start Beijing time."""
    now = now.astimezone(CHINA_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        start -= timedelta(days=6)
    elif period == "month":
        start = start.replace(day=1)
    elif period != "today":
        raise ValueError("统计范围必须是 today、7d 或 month。")
    where = "kind='consumption' AND day>=? AND day<=? AND occurred_at<=?"
    args = [start.date().isoformat(), now.date().isoformat(), now.isoformat()]
    settled = where + " AND status='settled'"
    total = db.execute(
        f"SELECT COALESCE(SUM(cost),0),COUNT(*),COALESCE(SUM(total_tokens),0) FROM ledger WHERE {settled}",
        args,
    ).fetchone()
    pending = db.execute(
        f"SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM ledger WHERE {where} AND status IN ('reserved','unknown')",
        args,
    ).fetchone()
    hours = {
        row[0]: row[1:]
        for row in db.execute(
            f"SELECT substr(occurred_at,1,13),COALESCE(SUM(cost),0),COUNT(*),COALESCE(SUM(total_tokens),0) "
            f"FROM ledger WHERE {settled} GROUP BY substr(occurred_at,1,13)",
            args,
        )
    }
    series = []
    profile = [
        {"hour": hour, "cost_yuan": 0, "observed_hours": 0, "requests": 0}
        for hour in range(24)
    ]
    cursor = start
    while cursor <= now:
        end = min(cursor + timedelta(hours=1), now)
        elapsed = (end - cursor).total_seconds() / 3600
        cost, count, tokens = hours.get(cursor.isoformat()[:13], (0, 0, 0))
        yuan = amount(cost, MONEY)
        series.append(
            {
                "start": cursor.isoformat(),
                "cost_yuan": yuan,
                "requests": count,
                "total_tokens": tokens,
                "elapsed_hours": elapsed,
                # One second minimum prevents infinity at exact midnight.
                "rate_yuan_per_hour": yuan / max(elapsed, 1 / 3600),
                "partial": elapsed < 1,
            }
        )
        item = profile[cursor.hour]
        item["cost_yuan"] += yuan
        item["requests"] += count
        item["observed_hours"] += elapsed
        cursor += timedelta(hours=1)
    for item in profile:
        item["average_yuan_per_hour"] = (
            item["cost_yuan"] / max(item["observed_hours"], 1 / 3600)
            if item["observed_hours"] > 0 or item["requests"]
            else None
        )
    observed = [item for item in profile if item["observed_hours"] > 0]
    peak = sorted(
        observed, key=lambda item: (-item["average_yuan_per_hour"], item["hour"])
    )
    quiet = sorted(
        observed, key=lambda item: (item["average_yuan_per_hour"], item["hour"])
    )
    # Equal rates do not provide evidence for a meaningful peak/quiet distinction.
    distinct = bool(
        total[0]
        and peak
        and peak[0]["average_yuan_per_hour"] > quiet[0]["average_yuan_per_hour"]
    )

    def ranking(column):
        # column is selected by this module, never supplied by a client.
        rows = db.execute(
            f"SELECT platform,{column},COALESCE(SUM(cost),0) AS spent,COUNT(*),COALESCE(SUM(total_tokens),0) "
            f"FROM ledger WHERE {settled} GROUP BY platform,{column} "
            f"ORDER BY spent DESC,platform,{column} LIMIT 20",
            args,
        ).fetchall()
        items = []
        for platform, identity, cost, count, tokens in rows:
            item = {
                "platform": platform,
                column: identity,
                "cost_yuan": amount(cost, MONEY),
                "requests": count,
                "total_tokens": tokens,
                "share": cost / total[0] if total[0] else 0,
            }
            if column == "user_id":
                user = db.execute(
                    "SELECT name FROM users WHERE platform=? AND user_id=?",
                    (platform, identity),
                ).fetchone()
                item["name"] = user[0] if user else ""
            items.append(item)
        return items

    # This rolling window is independent of the selected chart range.
    recent = db.execute(
        "SELECT COALESCE(SUM(cost),0) FROM ledger WHERE kind='consumption' AND status='settled' "
        "AND day>=? AND day<=? AND occurred_at>? AND occurred_at<=?",
        (
            (now - timedelta(hours=1)).date().isoformat(),
            now.date().isoformat(),
            (now - timedelta(hours=1)).isoformat(),
            now.isoformat(),
        ),
    ).fetchone()[0]
    return {
        "period": period,
        "start": start.isoformat(),
        "end": now.isoformat(),
        "timezone": "Asia/Shanghai",
        "cost_yuan": amount(total[0], MONEY),
        "requests": total[1],
        "total_tokens": total[2],
        "recent_hour_yuan": amount(recent, MONEY),
        "pending_count": pending[0],
        "reserved_yuan": amount(pending[1], MONEY),
        "series": series,
        "hour_profile": profile,
        "peak_hours": [
            item["hour"]
            for item in peak
            if item["average_yuan_per_hour"] > quiet[0]["average_yuan_per_hour"]
        ][:3]
        if distinct
        else [],
        "quiet_hours": [
            item["hour"]
            for item in quiet
            if item["average_yuan_per_hour"] < peak[0]["average_yuan_per_hour"]
        ][:3]
        if distinct
        else [],
        "groups": ranking("group_id"),
        "users": ranking("user_id"),
    }
