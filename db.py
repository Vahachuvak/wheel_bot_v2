import asyncio
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    deposited_stars INTEGER NOT NULL DEFAULT 0,
    withdrawn_stars INTEGER NOT NULL DEFAULT 0,
    total_won INTEGER NOT NULL DEFAULT 0,
    spins INTEGER NOT NULL DEFAULT 0,
    last_free_spin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    coins INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bet INTEGER NOT NULL,
    segment INTEGER NOT NULL,
    prize INTEGER NOT NULL,
    free INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    gift_id TEXT NOT NULL,
    stars INTEGER NOT NULL,
    coins INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS crash_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bet INTEGER NOT NULL,
    cp INTEGER NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    started REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    mult INTEGER NOT NULL DEFAULT 0,
    prize INTEGER NOT NULL DEFAULT 0
);
"""


class DB:
    """Один коннект + общий lock: все изменения баланса атомарны. Баланс — в звёздах ⭐ (1:1)."""

    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        cols = [r[1] for r in await (await self.conn.execute("PRAGMA table_info(spins)")).fetchall()]
        if "game" not in cols:
            await self.conn.execute("ALTER TABLE spins ADD COLUMN game TEXT NOT NULL DEFAULT 'wheel'")
        await self.conn.commit()
        await self._migrate_to_stars()

    async def _migrate_to_stars(self):
        """Один раз: старые монеты (1 ⭐ = 2 монеты) переводим в звёзды 1:1, округляя вверх в пользу игрока."""
        cur = await self.conn.execute("SELECT value FROM meta WHERE key='stars_migrated'")
        if await cur.fetchone():
            return
        # Новая БД (нет игроков) тоже помечается как мигрированная
        await self.conn.execute("UPDATE users SET balance=(balance+1)/2, total_won=(total_won+1)/2")
        await self.conn.execute("UPDATE spins SET bet=(bet+1)/2, prize=(prize+1)/2 WHERE game IN ('wheel','slots')")
        await self.conn.execute("INSERT INTO meta (key, value) VALUES ('stars_migrated', ?)", (str(int(time.time())),))
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def ensure_user(self, uid: int, name: str = "", username: str = ""):
        await self.conn.execute(
            "INSERT INTO users (id, name, username, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (uid, name, username or "", int(time.time())),
        )
        await self.conn.commit()

    async def get_user(self, uid: int):
        cur = await self.conn.execute("SELECT * FROM users WHERE id=?", (uid,))
        return await cur.fetchone()

    async def add_payment(self, charge_id: str, uid: int, stars: int) -> bool:
        """Зачисляет оплату 1:1. False — если этот платёж уже был (защита от дублей)."""
        async with self.lock:
            cur = await self.conn.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,))
            if await cur.fetchone():
                return False
            await self.conn.execute(
                "INSERT INTO payments (charge_id, user_id, stars, coins, created_at) VALUES (?,?,?,?,?)",
                (charge_id, uid, stars, stars, int(time.time())),
            )
            await self.conn.execute(
                "UPDATE users SET balance=balance+?, deposited_stars=deposited_stars+? WHERE id=?",
                (stars, stars, uid),
            )
            await self.conn.commit()
            return True

    async def get_payment(self, charge_id: str):
        cur = await self.conn.execute("SELECT * FROM payments WHERE charge_id=?", (charge_id,))
        return await cur.fetchone()

    async def mark_refunded(self, charge_id: str, uid: int, stars: int):
        async with self.lock:
            await self.conn.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))
            await self.conn.execute(
                "UPDATE users SET balance=MAX(balance-?,0), deposited_stars=MAX(deposited_stars-?,0) WHERE id=?",
                (stars, stars, uid),
            )
            await self.conn.commit()

    async def spin(self, uid: int, bet: int, free: bool, pick, free_cooldown: int, game: str = "wheel"):
        """pick() -> (код результата, выигрыш). Возвращает (ok, error, код, выигрыш, баланс)."""
        async with self.lock:
            u = await self.get_user(uid)
            now = int(time.time())
            if free:
                if now - u["last_free_spin"] < free_cooldown:
                    return False, "free_not_ready", None, 0, u["balance"]
            elif u["balance"] < bet:
                return False, "no_money", None, 0, u["balance"]
            seg, prize = pick()
            delta = prize - (0 if free else bet)
            await self.conn.execute(
                "UPDATE users SET balance=balance+?, total_won=total_won+?, spins=spins+1"
                + (", last_free_spin=?" if free else "")
                + " WHERE id=?",
                (delta, prize, now, uid) if free else (delta, prize, uid),
            )
            await self.conn.execute(
                "INSERT INTO spins (user_id, bet, segment, prize, free, created_at, game) VALUES (?,?,?,?,?,?,?)",
                (uid, bet, seg, prize, int(free), now, game),
            )
            await self.conn.commit()
            u = await self.get_user(uid)
            return True, None, seg, prize, u["balance"]

    # ---------- краш ----------
    async def crash_active(self, uid: int):
        cur = await self.conn.execute("SELECT * FROM crash_rounds WHERE user_id=? AND status='running' ORDER BY id DESC LIMIT 1", (uid,))
        return await cur.fetchone()

    async def crash_start(self, uid: int, bet: int, cp: int, auto: int):
        """Списывает ставку и открывает раунд. Возвращает (ok, error, round_id, started, balance)."""
        async with self.lock:
            cur = await self.conn.execute("SELECT 1 FROM crash_rounds WHERE user_id=? AND status='running'", (uid,))
            if await cur.fetchone():
                return False, "round_active", None, None, None
            u = await self.get_user(uid)
            if u["balance"] < bet:
                return False, "no_money", None, None, u["balance"]
            now = time.time()
            await self.conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (bet, uid))
            cur = await self.conn.execute(
                "INSERT INTO crash_rounds (user_id, bet, cp, auto, started) VALUES (?,?,?,?,?)", (uid, bet, cp, auto, now))
            await self.conn.commit()
            u = await self.get_user(uid)
            return True, None, cur.lastrowid, now, u["balance"]

    async def crash_settle(self, rid: int, uid: int, won: bool, mult: int, prize: int):
        """Закрывает раунд ровно один раз. Возвращает (закрыт_ли_сейчас, баланс)."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE crash_rounds SET status=?, mult=?, prize=? WHERE id=? AND status='running'",
                ("won" if won else "lost", mult, prize, rid))
            if cur.rowcount == 0:
                u = await self.get_user(uid)
                return False, u["balance"]
            r = await (await self.conn.execute("SELECT bet FROM crash_rounds WHERE id=?", (rid,))).fetchone()
            await self.conn.execute(
                "UPDATE users SET balance=balance+?, total_won=total_won+?, spins=spins+1 WHERE id=?", (prize, prize, uid))
            await self.conn.execute(
                "INSERT INTO spins (user_id, bet, segment, prize, free, created_at, game) VALUES (?,?,?,?,0,?,'crash')",
                (uid, r["bet"], mult, prize, int(time.time())))
            await self.conn.commit()
            u = await self.get_user(uid)
            return True, u["balance"]

    async def history(self, uid: int, limit: int = 15):
        cur = await self.conn.execute(
            "SELECT bet, segment, prize, free, created_at, game FROM spins WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def top(self, limit: int = 20):
        cur = await self.conn.execute(
            "SELECT name, username, total_won, spins FROM users WHERE spins>0 ORDER BY total_won DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def reserve_withdraw(self, uid: int, gift_id: str, stars: int, cost: int):
        """Списывает баланс заранее; если подарок не ушёл — finish_withdraw вернёт его."""
        async with self.lock:
            u = await self.get_user(uid)
            if u["balance"] < cost:
                return None
            await self.conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, uid))
            cur = await self.conn.execute(
                "INSERT INTO withdrawals (user_id, gift_id, stars, coins, status, created_at) VALUES (?,?,?,?,?,?)",
                (uid, gift_id, stars, cost, "pending", int(time.time())),
            )
            await self.conn.commit()
            return cur.lastrowid

    async def finish_withdraw(self, wid: int, uid: int, ok: bool, stars: int, cost: int):
        async with self.lock:
            if ok:
                await self.conn.execute("UPDATE withdrawals SET status='sent' WHERE id=?", (wid,))
                await self.conn.execute(
                    "UPDATE users SET withdrawn_stars=withdrawn_stars+? WHERE id=?", (stars, uid)
                )
            else:
                await self.conn.execute("UPDATE withdrawals SET status='failed' WHERE id=?", (wid,))
                await self.conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (cost, uid))
            await self.conn.commit()

    async def stats(self):
        cur = await self.conn.execute(
            "SELECT COUNT(*) users, COALESCE(SUM(balance),0) bal, COALESCE(SUM(deposited_stars),0) dep, "
            "COALESCE(SUM(withdrawn_stars),0) wd, COALESCE(SUM(spins),0) spins FROM users"
        )
        return dict(await cur.fetchone())

    async def add_balance(self, uid: int, stars: int):
        async with self.lock:
            await self.conn.execute("UPDATE users SET balance=MAX(balance+?,0) WHERE id=?", (stars, uid))
            await self.conn.commit()
