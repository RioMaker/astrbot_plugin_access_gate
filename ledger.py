"""Transactional local credits ledger; money uses integer nanoyuan."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal

MONEY = 1_000_000_000
POINTS = 1_000_000
# sys survives plugin module reloads, but not an AstrBot process restart.
if not hasattr(sys, "_access_gate_process_token"):
    sys._access_gate_process_token = uuid.uuid4().hex
PROCESS_TOKEN = sys._access_gate_process_token
if not hasattr(sys, "_access_gate_failed_requests"):
    sys._access_gate_failed_requests = set()
FAILED_REQUESTS = sys._access_gate_failed_requests


def units(value, scale):
    return int((Decimal(str(value)) * scale).to_integral_value(rounding=ROUND_HALF_UP))


def amount(value, scale):
    return float(Decimal(value) / scale)


class Ledger:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            self.db.close()
            raise ValueError("积分数据库版本高于本插件，不能降级读取。")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                platform TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(platform,user_id));
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
                day TEXT NOT NULL, occurred_at TEXT NOT NULL,
                platform TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                name TEXT NOT NULL, source_plugin TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
                points_delta INTEGER NOT NULL DEFAULT 0, balance_after INTEGER,
                cost INTEGER, reserved INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER, details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ledger_day ON ledger(day,kind,status);
            CREATE INDEX IF NOT EXISTS ledger_user ON ledger(platform,user_id,id);
            CREATE INDEX IF NOT EXISTS ledger_group ON ledger(platform,group_id,day);
            CREATE TABLE IF NOT EXISTS rp_claims (
                source TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY(source,user_id,day));
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        # An interrupted process may already have sent its request upstream.
        recovered = []
        for row in self.db.execute(
            "SELECT request_id,details FROM ledger WHERE status='reserved'"
        ).fetchall():
            if (
                json.loads(row["details"]).get("process_token") != PROCESS_TOKEN
                or row["request_id"] in FAILED_REQUESTS
            ):
                self.db.execute(
                    "UPDATE ledger SET status='unknown' WHERE request_id=?",
                    (row["request_id"],),
                )
                recovered.append(row["request_id"])
        self.db.commit()
        FAILED_REQUESTS.difference_update(recovered)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def settings(self, defaults):
        with self.lock:
            row = self.db.execute("SELECT * FROM settings WHERE id=1").fetchone()
            return (
                {"revision": row["revision"], "values": json.loads(row["data"])}
                if row
                else {"revision": 0, "values": defaults}
            )

    def save_settings(self, revision, values, actor, now):
        with self.transaction():
            current = self.db.execute(
                "SELECT revision FROM settings WHERE id=1"
            ).fetchone()
            if revision != (current[0] if current else 0):
                raise ValueError("设置已被其他页面修改，请刷新后重试。")
            self.db.execute(
                "INSERT INTO settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (revision + 1, json.dumps(values, ensure_ascii=False)),
            )
            self._audit(now, actor, "settings", values)
        return {"revision": revision + 1, "values": values}

    def _audit(self, now, actor, action, details):
        self.db.execute(
            "INSERT INTO audit(occurred_at,actor,action,details) VALUES(?,?,?,?)",
            (now, actor, action, json.dumps(details, ensure_ascii=False)),
        )

    def _user(self, identity):
        platform, user, name = (
            identity["platform"],
            identity["user_id"],
            identity.get("name", ""),
        )
        self.db.execute(
            "INSERT INTO users(platform,user_id,name) VALUES(?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET name=CASE WHEN excluded.name='' THEN users.name ELSE excluded.name END",
            (platform, user, name),
        )
        return self.db.execute(
            "SELECT balance FROM users WHERE platform=? AND user_id=?", (platform, user)
        ).fetchone()[0]

    def _insert(
        self, request_id, identity, now, kind, status, plugin, details, **extra
    ):
        values = dict(
            request_id=request_id,
            day=now[:10],
            occurred_at=now,
            platform=identity["platform"],
            user_id=identity["user_id"],
            group_id=identity.get("group_id", ""),
            name=identity.get("name", ""),
            source_plugin=plugin,
            kind=kind,
            status=status,
            details=json.dumps(details, ensure_ascii=False),
            **extra,
        )
        self.db.execute(
            f"INSERT INTO ledger({','.join(values)}) VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def award(self, identity, day, rp, points, now, with_receipt=False):
        with self.transaction():
            claim = self.db.execute(
                "INSERT INTO rp_claims VALUES('taiko_rp',?,?) ON CONFLICT DO NOTHING",
                (identity["user_id"], day),
            )
            if not claim.rowcount:
                return (
                    self._rp_receipt(identity, day, awarded=False)
                    if with_receipt
                    else False
                )
            balance = self._user(identity) + units(points, POINTS)
            self.db.execute(
                "UPDATE users SET balance=? WHERE platform=? AND user_id=?",
                (balance, identity["platform"], identity["user_id"]),
            )
            self._insert(
                f"rp:{identity['user_id']}:{day}",
                identity,
                now,
                "rp",
                "settled",
                "astrbot_plugin_taiko_rp",
                {"rp": rp, "rp_date": day, "points": str(points)},
                points_delta=units(points, POINTS),
                balance_after=balance,
            )
            return (
                self._rp_receipt(identity, day, awarded=True) if with_receipt else True
            )

    def _rp_receipt(self, identity, day, *, awarded):
        # Read the reward and current balance under the same write transaction.
        # A repeat uses its original reward even if the administrator changed the
        # reward formula after this user's first claim.
        row = self.db.execute(
            "SELECT points_delta FROM ledger WHERE request_id=? AND kind='rp'",
            (f"rp:{identity['user_id']}:{day}",),
        ).fetchone()
        if row is None:
            raise ValueError("签到凭证缺少积分流水，请管理员核对账本。")
        balance = self.db.execute(
            "SELECT balance FROM users WHERE platform=? AND user_id=?",
            (identity["platform"], identity["user_id"]),
        ).fetchone()
        daily_points = amount(row["points_delta"], POINTS)
        return {
            "status": "awarded" if awarded else "claimed",
            "awarded_points": daily_points if awarded else 0,
            "daily_points": daily_points,
            "balance": amount(balance[0], POINTS) if balance else 0,
            "date": day,
        }

    def _daily(self, day, platform=None, group=None):
        where, args = "day=? AND kind='consumption'", [day]
        if group is not None:
            where += " AND platform=? AND group_id=?"
            args += [platform, group]
        row = self.db.execute(
            f"SELECT COALESCE(SUM(cost),0),COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') THEN reserved ELSE 0 END),0) FROM ledger WHERE {where}",
            args,
        ).fetchone()
        return row[0], row[1]

    def reserve(
        self, request_id, identity, now, plugin, charged, snapshot, limits, reserve
    ):
        with self.transaction():
            balance = self._user(identity)
            spent, held = self._daily(now[:10])
            reserve_n = units(reserve, MONEY)
            if spent + held >= units(
                limits["global"], MONEY
            ) or spent + held + reserve_n > units(limits["global"], MONEY):
                raise ValueError(
                    "今日全局 LLM 额度不足（含在途预留），请明日再试或联系管理员。"
                )
            if identity.get("group_id"):
                spent, held = self._daily(
                    now[:10], identity["platform"], identity["group_id"]
                )
                if spent + held >= units(
                    limits["group"], MONEY
                ) or spent + held + reserve_n > units(limits["group"], MONEY):
                    raise ValueError(
                        "本群今日 LLM 额度不足（含在途预留），请明日再试。"
                    )
            if charged:
                if balance <= 0:
                    raise ValueError("积分不足，请先通过 /rp 获取今日积分。")
                row = self.db.execute(
                    "SELECT 1 FROM ledger WHERE platform=? AND user_id=? AND charged=1 AND status IN ('reserved','unknown') LIMIT 1",
                    (identity["platform"], identity["user_id"]),
                ).fetchone()
                if row:
                    raise ValueError("你有正在结算或用量待核对的请求，请稍后再试。")
            snapshot = {**snapshot, "process_token": PROCESS_TOKEN}
            self._insert(
                request_id,
                identity,
                now,
                "consumption",
                "reserved",
                plugin,
                snapshot,
                charged=int(charged),
                reserved=reserve_n,
            )

    def settle(
        self, request_id, usage, cost, failed, now, actor="", reason="", cancel=False
    ):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM ledger WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row or row["status"] not in {"reserved", "unknown"}:
                return False
            details = json.loads(row["details"])
            details.update(failed=bool(failed), settled_at=now)
            if actor:
                if row["status"] == "reserved":
                    raise ValueError("请求仍在执行，不能人工结算。")
                details.update(resolved_by=actor, resolution_reason=reason)
                self._audit(
                    now,
                    actor,
                    "resolve",
                    {
                        "request_id": request_id,
                        "reason": reason,
                        "usage": usage,
                        "cancel": cancel,
                    },
                )
            if usage is None and not cancel:
                self.db.execute(
                    "UPDATE ledger SET status='unknown',details=? WHERE request_id=?",
                    (json.dumps(details, ensure_ascii=False), request_id),
                )
                return True
            details["usage"] = usage
            cost_n = units(cost or 0, MONEY)
            deduction = (
                units(
                    Decimal(cost_n)
                    / MONEY
                    * Decimal(str(details["price"]["points_per_yuan"])),
                    POINTS,
                )
                if row["charged"]
                else 0
            )
            self.db.execute(
                "UPDATE users SET balance=balance-? WHERE platform=? AND user_id=?",
                (deduction, row["platform"], row["user_id"]),
            )
            balance = self.db.execute(
                "SELECT balance FROM users WHERE platform=? AND user_id=?",
                (row["platform"], row["user_id"]),
            ).fetchone()[0]
            self.db.execute(
                "UPDATE ledger SET status=?,cost=?,points_delta=?,balance_after=?,total_tokens=?,details=? WHERE request_id=?",
                (
                    "cancelled" if cancel else "settled",
                    cost_n,
                    -deduction,
                    balance,
                    usage["total_tokens"] if usage else None,
                    json.dumps(details, ensure_ascii=False),
                    request_id,
                ),
            )
            return True

    def get_request(self, request_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM ledger WHERE request_id=?", (request_id,)
            ).fetchone()
            return self._row(row) if row else None

    @staticmethod
    def _row(row):
        result = dict(row)
        result["details"] = json.loads(result["details"])
        result["points_delta"] = amount(result["points_delta"], POINTS)
        result["balance_after"] = (
            amount(result["balance_after"], POINTS)
            if result["balance_after"] is not None
            else None
        )
        result["cost_yuan"] = (
            amount(result.pop("cost"), MONEY) if result["cost"] is not None else None
        )
        result["reserved_yuan"] = amount(result.pop("reserved"), MONEY)
        return result

    def adjust(self, identity, delta, reason, request_id, actor, now):
        with self.transaction():
            old = self.db.execute(
                "SELECT * FROM ledger WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if (
                    old["kind"] != "adjustment"
                    or old["platform"] != identity["platform"]
                    or old["user_id"] != identity["user_id"]
                    or old["points_delta"] != units(delta, POINTS)
                ):
                    raise ValueError("操作编号冲突，请刷新后重试。")
                return self._row(old)
            balance = self._user(identity) + units(delta, POINTS)
            self.db.execute(
                "UPDATE users SET balance=? WHERE platform=? AND user_id=?",
                (balance, identity["platform"], identity["user_id"]),
            )
            self._insert(
                request_id,
                identity,
                now,
                "adjustment",
                "settled",
                "admin",
                {"actor": actor, "reason": reason},
                points_delta=units(delta, POINTS),
                balance_after=balance,
            )
            self._audit(
                now,
                actor,
                "adjust",
                {
                    "request_id": request_id,
                    "reason": reason,
                    "delta": delta,
                    **identity,
                },
            )
        return self.get_request(request_id)

    def users(self, query, page, ratio):
        with self.lock:
            where, args = "user_id LIKE ? OR name LIKE ?", [f"%{query}%"] * 2
            total = self.db.execute(
                f"SELECT COUNT(*) FROM users WHERE {where}", args
            ).fetchone()[0]
            rows = self.db.execute(
                f"SELECT * FROM users WHERE {where} ORDER BY balance DESC,user_id LIMIT 30 OFFSET ?",
                [*args, (page - 1) * 30],
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["balance"] = amount(item["balance"], POINTS)
                item["amount_yuan"] = item["balance"] / ratio
                items.append(item)
            return {"items": items, "page": page, "page_size": 30, "total": total}

    def balance(self, platform, user_id):
        with self.lock:
            row = self.db.execute(
                "SELECT balance FROM users WHERE platform=? AND user_id=?",
                (platform, user_id),
            ).fetchone()
            return amount(row[0], POINTS) if row else 0

    def history(self, filters):
        with self.lock:
            clauses, args = [], []
            for key in ("day", "user_id", "group_id", "platform", "kind", "status"):
                if filters.get(key):
                    clauses.append(key + "=?")
                    args.append(filters[key])
            where = " AND ".join(clauses) or "1=1"
            total = self.db.execute(
                f"SELECT COUNT(*) FROM ledger WHERE {where}", args
            ).fetchone()[0]
            page = filters.get("page", 1)
            rows = self.db.execute(
                f"SELECT * FROM ledger WHERE {where} ORDER BY id DESC LIMIT 30 OFFSET ?",
                [*args, (page - 1) * 30],
            ).fetchall()
            return {
                "items": [self._row(row) for row in rows],
                "page": page,
                "page_size": 30,
                "total": total,
            }

    def overview(self, day, settings):
        def budget(spent, reserved, limit):
            return {
                "spent_yuan": amount(spent, MONEY),
                "reserved_yuan": amount(reserved, MONEY),
                "limit_yuan": float(limit),
                "remaining_yuan": max(
                    0, float(limit) - amount(spent + reserved, MONEY)
                ),
            }

        with self.lock:
            spent, reserved = self._daily(day)
            groups = []
            rows = self.db.execute(
                "SELECT DISTINCT platform,group_id FROM ledger WHERE group_id!=''"
            ).fetchall()
            found = {(row[0], row[1]) for row in rows}
            for key in settings["group_overrides"]:
                if ":" in key:
                    found.add(tuple(key.split(":", 1)))
                elif not any(group == key for _, group in found):
                    found.add(("", key))
            for platform, group in sorted(found):
                used, held = self._daily(day, platform, group)
                overrides = settings["group_overrides"]
                limit = overrides.get(
                    f"{platform}:{group}",
                    overrides.get(group, settings["group_daily_yuan"]),
                )
                groups.append(
                    dict(
                        platform=platform,
                        group_id=group,
                        is_override=f"{platform}:{group}" in overrides
                        or group in overrides,
                        **budget(used, held, limit),
                    )
                )
            totals = self.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(balance),0) FROM users"
            ).fetchone()
            pending = self.db.execute(
                "SELECT COUNT(*) FROM ledger WHERE status='unknown'"
            ).fetchone()[0]
            return {
                "day": day,
                "currency": "CNY",
                "global_": budget(spent, reserved, settings["global_daily_yuan"]),
                "groups": groups,
                "totals": {"users": totals[0], "points": amount(totals[1], POINTS)},
                "pending_count": pending,
            }
