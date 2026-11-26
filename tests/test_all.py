import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123:ABC")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import games  # noqa: E402
from db import DB  # noqa: E402


class GameMath(unittest.TestCase):
    def test_rtp_in_expected_range(self):
        self.assertTrue(0.82 <= games.wheel_rtp() <= 0.90, games.wheel_rtp())
        self.assertTrue(0.82 <= games.slots_rtp() <= 0.90, games.slots_rtp())
        for c in games.CASES:
            self.assertTrue(0.82 <= games.case_rtp(c) <= 0.90, (c["name"], games.case_rtp(c)))
        self.assertAlmostEqual(games.crash_rtp(), 0.9)

    def test_wheel_harsher_than_before(self):
        total = sum(s[3] for s in games.SEGMENTS)
        loss = sum(s[3] for s in games.SEGMENTS if s[0] < 1) / total
        big = sum(s[3] for s in games.SEGMENTS if s[0] >= 5) / total
        self.assertGreater(loss, 0.59)            # раньше проигрышных исходов было 59%
        self.assertLess(big, 0.040)               # раньше иксов x5+ было 4.0%
        self.assertLessEqual(max(s[3] for s in games.SEGMENTS if s[0] == 50), 0.05)

    def test_slots_top_prizes_reduced(self):
        top3 = {s[0]: s[2] for s in games.SLOT_SYMBOLS}
        self.assertLessEqual(top3["👑"], 300)     # раньше x2000
        self.assertLessEqual(top3["7️⃣"], 120)    # раньше x500

    def test_prize_rounding_is_unbiased(self):
        random.seed(1)
        old = games.rng
        games.rng = random.Random(7)
        try:
            n = 200000
            mean = sum(games.prize_for(0.5, 5) for _ in range(n)) / n
            self.assertAlmostEqual(mean, 2.5, delta=0.02)
            self.assertEqual(games.prize_for(2, 10), 20)
        finally:
            games.rng = old

    def test_crash_returns_ninety_percent_for_any_target(self):
        old = games.rng
        games.rng = random.Random(42)
        try:
            n = 400000
            cps = [games.new_crash_point() for _ in range(n)]
        finally:
            games.rng = old
        self.assertAlmostEqual(sum(1 for c in cps if c == 0) / n, 0.10, delta=0.004)   # мгновенный краш = край казино
        for target in (101, 150, 300, 1000):
            wins = sum(1 for c in cps if c >= target)
            self.assertAlmostEqual(wins / n * target / 100, 0.9, delta=0.025, msg=target)
        self.assertTrue(all(c <= games.CRASH_MAX_X100 for c in cps))

    def test_crash_outcome_logic(self):
        self.assertEqual(games.crash_outcome(0, 0, 0), ("lose", 0))
        cp = 250
        t_end = games.time_to_x100(cp + 1)
        self.assertIsNone(games.crash_outcome(cp, 0, t_end - 0.01))
        self.assertEqual(games.crash_outcome(cp, 0, t_end + 0.01), ("lose", cp))
        self.assertEqual(games.crash_outcome(cp, 200, games.time_to_x100(200) + 0.01), ("win", 200))
        self.assertIsNone(games.crash_outcome(cp, 200, games.time_to_x100(200) - 0.01))
        self.assertEqual(games.crash_outcome(cp, 300, t_end + 0.01), ("lose", cp))    # автовыход выше точки краша
        self.assertLessEqual(games.mult_x100_at(t_end - 0.001), cp)
        self.assertEqual(games.mult_x100_at(0), 100)

    def test_case_pick_codes(self):
        for i, c in enumerate(games.CASES):
            code, prize = games.pick_case(i)
            self.assertEqual(code // 100, i)
            self.assertIn(prize, [it[2] for it in c["items"]])


class Database(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mkdtemp() + "/t.db"
        self.db = DB(self.path)
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()

    async def test_migration_coins_to_stars_once(self):
        await self.db.close()
        os.remove(self.path)
        con = sqlite3.connect(self.path)
        con.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, username TEXT, balance INTEGER NOT NULL DEFAULT 0,
              deposited_stars INTEGER NOT NULL DEFAULT 0, withdrawn_stars INTEGER NOT NULL DEFAULT 0,
              total_won INTEGER NOT NULL DEFAULT 0, spins INTEGER NOT NULL DEFAULT 0, last_free_spin INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL);
            CREATE TABLE spins (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bet INTEGER NOT NULL,
              segment INTEGER NOT NULL, prize INTEGER NOT NULL, free INTEGER NOT NULL, created_at INTEGER NOT NULL);
            INSERT INTO users (id, balance, total_won, created_at) VALUES (1, 101, 41, 0), (2, 100, 0, 0);
            INSERT INTO spins (user_id, bet, segment, prize, free, created_at) VALUES (1, 10, 0, 25, 0, 0);
        """)
        con.commit(); con.close()
        self.db = DB(self.path)
        await self.db.connect()
        self.assertEqual((await self.db.get_user(1))["balance"], 51)     # 101 монета -> 51 ⭐ (округление вверх)
        self.assertEqual((await self.db.get_user(2))["balance"], 50)
        self.assertEqual((await self.db.history(1))[0]["bet"], 5)
        await self.db.close()
        self.db = DB(self.path)
        await self.db.connect()                                          # второй запуск ничего не меняет
        self.assertEqual((await self.db.get_user(1))["balance"], 51)

    async def test_payment_idempotent_and_refund(self):
        await self.db.ensure_user(1, "a", "a")
        self.assertTrue(await self.db.add_payment("c1", 1, 50))
        self.assertFalse(await self.db.add_payment("c1", 1, 50))
        self.assertEqual((await self.db.get_user(1))["balance"], 50)
        await self.db.mark_refunded("c1", 1, 50)
        u = await self.db.get_user(1)
        self.assertEqual((u["balance"], u["deposited_stars"]), (0, 0))

    async def test_spin_and_free(self):
        await self.db.ensure_user(1, "a", "a")
        await self.db.add_balance(1, 20)
        ok, e, seg, prize, bal = await self.db.spin(1, 10, False, lambda: (3, 25), 86400)
        self.assertEqual((ok, prize, bal), (True, 25, 35))
        ok, e, *_ = await self.db.spin(1, 100, False, lambda: (0, 0), 86400)
        self.assertEqual((ok, e), (False, "no_money"))
        ok, *_ , bal = await self.db.spin(1, 2, True, lambda: (0, 1), 86400)
        self.assertTrue(ok)
        ok, e, *_ = await self.db.spin(1, 2, True, lambda: (0, 1), 86400)
        self.assertEqual((ok, e), (False, "free_not_ready"))

    async def test_crash_round_settles_once(self):
        await self.db.ensure_user(1, "a", "a")
        await self.db.add_balance(1, 100)
        ok, e, rid, started, bal = await self.db.crash_start(1, 10, 500, 0)
        self.assertEqual((ok, bal), (True, 90))
        ok2, e2, *_ = await self.db.crash_start(1, 10, 500, 0)
        self.assertEqual((ok2, e2), (False, "round_active"))             # второй раунд параллельно нельзя
        r = await asyncio.gather(self.db.crash_settle(rid, 1, True, 200, 20), self.db.crash_settle(rid, 1, True, 200, 20))
        self.assertEqual(sorted(x[0] for x in r), [False, True])          # выплата ровно один раз
        self.assertEqual((await self.db.get_user(1))["balance"], 110)
        self.assertEqual((await self.db.history(1))[0]["game"], "crash")

    async def test_withdraw_reserve_and_refund_on_failure(self):
        await self.db.ensure_user(1, "a", "a")
        await self.db.add_balance(1, 30)
        wid = await self.db.reserve_withdraw(1, "g", 25, 25)
        self.assertEqual((await self.db.get_user(1))["balance"], 5)
        self.assertIsNone(await self.db.reserve_withdraw(1, "g", 25, 25))
        await self.db.finish_withdraw(wid, 1, False, 25, 25)
        self.assertEqual((await self.db.get_user(1))["balance"], 30)


def init_data(uid, name="Test"):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
    d["hash"] = hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)


class Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        from aiohttp.test_utils import TestClient, TestServer
        self.bot = bot
        bot._last_action.clear()
        bot.db = DB(tempfile.mkdtemp() + "/a.db")
        await bot.db.connect()
        self.client = TestClient(TestServer(bot.build_app()))
        await self.client.start_server()
        self.h = {"X-Init-Data": init_data(77)}

    async def asyncTearDown(self):
        await self.client.close()
        await self.bot.db.close()

    async def post(self, path, data=None, h=None):
        r = await self.client.post(path, json=data or {}, headers=h or self.h)
        return r.status, await r.json()

    async def fund(self, n):
        await self.bot.db.ensure_user(77, "T", "t")
        await self.bot.db.add_balance(77, n)

    async def test_auth_required(self):
        r = await self.client.post("/api/me", json={})
        self.assertEqual(r.status, 401)
        r = await self.client.post("/api/me", json={}, headers={"X-Init-Data": "hash=bad"})
        self.assertEqual(r.status, 401)

    async def test_me_config_in_stars(self):
        st, me = await self.post("/api/me")
        self.assertEqual(st, 200)
        self.assertNotIn("rate", me["cfg"])
        self.assertEqual(len(me["cfg"]["cases"]), 3)
        self.assertEqual(me["cfg"]["crash"]["rtp"], 90.0)

    async def test_wheel_slots_case_charge_exact(self):
        await self.fund(100)
        st, r = await self.post("/api/spin", {"bet": 10})
        self.assertEqual(st, 200)
        self.assertEqual(r["balance"], 100 - 10 + r["prize"])
        self.bot._last_action.clear()
        st, r = await self.post("/api/case", {"case": 0})
        self.assertEqual(st, 200)
        self.assertEqual(r["price"], 10)
        self.bot._last_action.clear()
        st, r = await self.post("/api/slots", {"bet": 7})
        self.assertEqual((st, r["error"]), (400, "bad_bet"))
        self.bot._last_action.clear()
        st, r = await self.post("/api/case", {"case": 9})
        self.assertEqual((st, r["error"]), (400, "bad_case"))

    async def test_free_spin_bet_not_in_bets_works(self):
        await self.fund(0)
        st, r = await self.post("/api/spin", {"free": True})
        self.assertEqual(st, 200)
        self.assertEqual(r["bet"], self.bot.FREE_SPIN_BET)
        self.assertEqual(r["balance"], r["prize"])
        self.bot._last_action.clear()
        st, r = await self.post("/api/spin", {"free": True})
        self.assertEqual(r["error"], "free_not_ready")

    async def test_throttle(self):
        await self.fund(100)
        await self.post("/api/spin", {"bet": 5})
        st, r = await self.post("/api/spin", {"bet": 5})
        self.assertEqual((st, r["error"]), (429, "too_fast"))

    async def test_crash_win_flow(self):
        await self.fund(100)
        orig = self.bot.games.new_crash_point
        self.bot.games.new_crash_point = lambda: 10000
        try:
            st, r = await self.post("/api/crash/start", {"bet": 10})
            self.assertEqual((st, r["balance"]), (200, 90))
            self.bot._last_action.clear()
            st, r = await self.post("/api/crash/start", {"bet": 10})
            self.assertEqual((st, r["error"]), (400, "round_active"))
            await asyncio.sleep(0.5)
            st, r = await self.post("/api/crash/cashout")
            self.assertEqual((st, r["status"]), (200, "won"))
            self.assertGreaterEqual(r["mult"], 100)
            self.assertEqual(r["balance"], 90 + r["prize"])
            st, r2 = await self.post("/api/crash/cashout")
            self.assertEqual((st, r2["error"]), (400, "no_round"))        # повторный вывод невозможен
        finally:
            self.bot.games.new_crash_point = orig

    async def test_crash_instant_bust_and_cannot_cash(self):
        await self.fund(100)
        orig = self.bot.games.new_crash_point
        self.bot.games.new_crash_point = lambda: 0
        try:
            await self.post("/api/crash/start", {"bet": 10})
            st, r = await self.post("/api/crash/cashout")
            self.assertEqual((r["status"], r["prize"], r["balance"]), ("lost", 0, 90))
        finally:
            self.bot.games.new_crash_point = orig

    async def test_crash_auto_cashout_validation(self):
        await self.fund(100)
        st, r = await self.post("/api/crash/start", {"bet": 10, "auto": 0.5})
        self.assertEqual((st, r["error"]), (400, "bad_auto"))

    def test_payload_parsing(self):
        p = self.bot.parse_payload
        self.assertEqual(p("dep:5:50"), (5, 50))
        for bad in ("x:5:50", "dep:5", "dep:a:b", ""):
            self.assertIsNone(p(bad))


if __name__ == "__main__":
    unittest.main()
