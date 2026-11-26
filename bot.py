import asyncio
import logging
import math
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import games
from db import DB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wheel")

# ---------- настройки (всё через переменные окружения) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL") or (f"https://{_domain}/" if _domain else "")

# Баланс в игре = Telegram Stars один к одному.
WITHDRAW_ENABLED = os.getenv("WITHDRAW_ENABLED", "1") == "1"
WITHDRAW_FEE = float(os.getenv("WITHDRAW_FEE", "0"))         # 0.1 = комиссия 10% на вывод
MIN_DEPOSIT_FOR_WITHDRAW = int(os.getenv("MIN_DEPOSIT_FOR_WITHDRAW", "20"))  # ⭐ всего пополнено
MIN_STARS, MAX_STARS = 1, 10000
BETS = games.BETS
FREE_SPIN_BET = int(os.getenv("FREE_SPIN_BET", "2"))         # «ставка» бесплатного спина, ⭐
FREE_COOLDOWN = 24 * 3600
MIN_SPIN_INTERVAL = 0.35                                     # защита от флуда запросами

WEBAPP_DIR = Path(__file__).parent / "webapp"

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
db = DB(str(DATA_DIR / "wheel.db"))
withdraw_lock = asyncio.Lock()
_last_action: dict[int, float] = {}


def gift_cost(stars: int) -> int:
    return math.ceil(stars * (1 + WITHDRAW_FEE))


def play_kb():
    if not WEBAPP_URL:
        return None
    base = WEBAPP_URL
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Колесо", web_app=WebAppInfo(url=base)),
         InlineKeyboardButton(text="🎰 Слоты", web_app=WebAppInfo(url=base + "?game=slots"))],
        [InlineKeyboardButton(text="📦 Кейсы", web_app=WebAppInfo(url=base + "?game=cases")),
         InlineKeyboardButton(text="🚀 Краш", web_app=WebAppInfo(url=base + "?game=crash"))],
    ])


def rtp_line() -> str:
    return (f"колесо {games.wheel_rtp() * 100:.0f}%, слоты {games.slots_rtp() * 100:.0f}%, "
            f"кейсы {sum(games.case_rtp(c) for c in games.CASES) / len(games.CASES) * 100:.0f}%, "
            f"краш {games.crash_rtp() * 100:.0f}%")


# ---------- бот ----------
@dp.message(CommandStart())
async def start(m: Message):
    await db.ensure_user(m.from_user.id, m.from_user.full_name, m.from_user.username)
    await m.answer(
        "🎡 <b>NFT Wheel</b>\n\n"
        "Колесо 🎡, слоты 🎰, кейсы 📦 и краш 🚀 — прямо в Telegram.\n"
        "• Баланс в звёздах: <b>1 ⭐ = 1 ⭐</b>, без пересчёта в монеты\n"
        "• Бесплатный спин раз в 24 часа\n"
        "• Выигрыш можно вывести подарками Telegram, которые конвертируются в ⭐\n\n"
        "Шансы и возврат каждой игры открыты в приложении. Только 18+. /terms",
        reply_markup=play_kb(),
    )


@dp.message(Command("balance"))
async def balance(m: Message):
    await db.ensure_user(m.from_user.id, m.from_user.full_name, m.from_user.username)
    u = await db.get_user(m.from_user.id)
    await m.answer(f"Баланс: <b>{u['balance']} ⭐</b>", reply_markup=play_kb())


@dp.message(Command("terms"))
async def terms(m: Message):
    await m.answer(
        "<b>Условия</b>\n"
        "1. Только для лиц 18+.\n"
        "2. Баланс — звёзды Telegram один к одному. Результат каждой игры определяется случайно на сервере. "
        f"Средний возврат игрокам: {rtp_line()}. Игра рассчитана на проигрыш в среднем — играй на то, что не жалко.\n"
        f"3. Вывод — подарками Telegram, после пополнения минимум на {MIN_DEPOSIT_FOR_WITHDRAW} ⭐.\n"
        "4. Вопросы по оплате: /paysupport текст проблемы"
    )


@dp.message(Command("paysupport"))
async def paysupport(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Опиши проблему одним сообщением после команды, например:\n"
                              "<code>/paysupport оплатил 50 ⭐, баланс не пополнился, ID платежа …</code>")
    await m.answer("Принято, администратор ответит. Спасибо!")
    if ADMIN_ID:
        who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
        await bot.send_message(ADMIN_ID, f"🆘 /paysupport от {who} (<code>{m.from_user.id}</code>):\n{text[:1500]}")


def parse_payload(payload: str):
    """dep:<user_id>:<stars> -> (user_id, stars) или None."""
    try:
        kind, uid, stars = payload.split(":")
        if kind != "dep":
            return None
        return int(uid), int(stars)
    except ValueError:
        return None


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    p = parse_payload(q.invoice_payload)
    ok = bool(p and p[0] == q.from_user.id and q.currency == "XTR"
              and q.total_amount == p[1] and MIN_STARS <= p[1] <= MAX_STARS)
    await q.answer(ok=ok, error_message=None if ok else "Счёт недействителен, создай новый в приложении")


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    stars = sp.total_amount
    await db.ensure_user(m.from_user.id, m.from_user.full_name, m.from_user.username)
    if await db.add_payment(sp.telegram_payment_charge_id, m.from_user.id, stars):
        u = await db.get_user(m.from_user.id)
        await m.answer(f"✅ +{stars} ⭐ зачислено!\nБаланс: <b>{u['balance']} ⭐</b>", reply_markup=play_kb())
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"💰 {m.from_user.full_name} пополнил {stars} ⭐\n<code>{sp.telegram_payment_charge_id}</code>")


# ---------- админка ----------
def is_admin(m: Message) -> bool:
    return bool(ADMIN_ID) and m.from_user.id == ADMIN_ID


@dp.message(Command("stats"))
async def stats(m: Message):
    if not is_admin(m):
        return
    s = await db.stats()
    try:
        bal = (await bot.get_my_star_balance()).amount
    except Exception:
        bal = "?"
    await m.answer(
        f"👥 Игроков: {s['users']}\n🎲 Игр: {s['spins']}\n"
        f"⭐ Пополнено: {s['dep']}\n🎁 Выведено: {s['wd']} ⭐\n"
        f"💼 На балансах игроков: {s['bal']} ⭐\n"
        f"🏦 Баланс бота: {bal} ⭐"
    )


@dp.message(Command("refund"))
async def refund(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    charge = (command.args or "").strip()
    p = await db.get_payment(charge)
    if not p or p["refunded"]:
        return await m.answer("Платёж не найден или уже возвращён.")
    try:
        await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=charge)
    except Exception as e:
        return await m.answer(f"❌ Telegram не вернул платёж: {e}")
    await db.mark_refunded(charge, p["user_id"], p["stars"])
    await m.answer(f"↩️ Вернул {p['stars']} ⭐ пользователю {p['user_id']}")


@dp.message(Command("give"))
async def give(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        uid, stars = map(int, (command.args or "").split())
    except ValueError:
        return await m.answer("Формат: /give user_id звёзды")
    await db.add_balance(uid, stars)
    await m.answer("Готово.")


# ---------- API мини-аппа ----------
def auth(request: web.Request):
    init = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, init)
    except ValueError:
        raise web.HTTPUnauthorized(text="bad init data")
    if time.time() - data.auth_date.timestamp() > 24 * 3600:
        raise web.HTTPUnauthorized(text="init data expired")
    return data.user


def err(code: str, status: int = 400, **extra):
    return web.json_response({"error": code, **extra}, status=status)


def throttle(uid: int):
    """Не чаще одного игрового запроса в MIN_SPIN_INTERVAL секунд на игрока."""
    now = time.monotonic()
    if now - _last_action.get(uid, 0) < MIN_SPIN_INTERVAL:
        raise web.HTTPTooManyRequests(text='{"error": "too_fast"}', content_type="application/json")
    _last_action[uid] = now


async def body(request):
    try:
        return await request.json()
    except Exception:
        return {}


async def touch(u):
    """Единый способ обновить игрока (раньше имя перезаписывалось то полным, то коротким)."""
    await db.ensure_user(u.id, f"{u.first_name} {u.last_name or ''}".strip(), u.username)


def parse_int(b, key) -> int:
    try:
        return int(b.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def case_cfg():
    out = []
    for c in games.CASES:
        total = sum(i[3] for i in c["items"])
        out.append({
            "name": c["name"], "emoji": c["emoji"], "price": c["price"],
            "rtp": round(games.case_rtp(c) * 100, 1),
            "items": [{"emoji": i[0], "name": i[1], "value": i[2], "chance": round(i[3] / total * 100, 2)} for i in c["items"]],
        })
    return out


async def api_me(request):
    u = auth(request)
    await touch(u)
    row = await db.get_user(u.id)
    total = sum(s[3] for s in games.SEGMENTS)
    return web.json_response({
        "id": u.id,
        "name": row["name"],
        "balance": row["balance"],
        "deposited": row["deposited_stars"],
        "spins": row["spins"],
        "total_won": row["total_won"],
        "free_in": max(0, FREE_COOLDOWN - (int(time.time()) - row["last_free_spin"])),
        "history": await db.history(u.id),
        "cfg": {
            "bets": BETS,
            "free_bet": FREE_SPIN_BET,
            "min_stars": MIN_STARS,
            "max_stars": MAX_STARS,
            "withdraw": WITHDRAW_ENABLED,
            "min_dep_wd": MIN_DEPOSIT_FOR_WITHDRAW,
            "rtp": round(games.wheel_rtp() * 100, 1),
            "segments": [
                {"mult": s[0], "emoji": s[1], "label": s[2], "chance": round(s[3] / total * 100, 2)}
                for s in games.SEGMENTS
            ],
            "slots_rtp": round(games.slots_rtp() * 100, 1),
            "slots": [{"emoji": s[0], "x3": s[2], "x2": s[3]} for s in games.SLOT_SYMBOLS],
            "cases": case_cfg(),
            "crash": {"rtp": round(games.crash_rtp() * 100, 1), "k": games.CRASH_K, "max_x": games.CRASH_MAX_X100 / 100},
        },
    })


async def api_slots(request):
    u = auth(request)
    throttle(u.id)
    bet = parse_int(await body(request), "bet")
    if bet not in BETS:
        return err("bad_bet")
    await touch(u)
    ok, e, code, prize, bal = await db.spin(u.id, bet, False, lambda: games.pick_slots(bet), FREE_COOLDOWN, game="slots")
    if not ok:
        return err(e, balance=bal)
    reels = [code // 100, code // 10 % 10, code % 10]
    return web.json_response({"reels": reels, "prize": prize, "balance": bal, "bet": bet})


async def api_spin(request):
    u = auth(request)
    throttle(u.id)
    b = await body(request)
    free = bool(b.get("free"))
    bet = FREE_SPIN_BET if free else parse_int(b, "bet")
    if not free and bet not in BETS:
        return err("bad_bet")
    await touch(u)
    ok, e, seg, prize, bal = await db.spin(u.id, bet, free, lambda: games.pick_wheel(bet), FREE_COOLDOWN)
    if not ok:
        return err(e, balance=bal)
    return web.json_response({"segment": seg, "prize": prize, "balance": bal, "bet": bet, "free": free})


async def api_case(request):
    u = auth(request)
    throttle(u.id)
    idx = parse_int(await body(request), "case")
    if not 0 <= idx < len(games.CASES):
        return err("bad_case")
    price = games.CASES[idx]["price"]
    await touch(u)
    ok, e, code, prize, bal = await db.spin(u.id, price, False, lambda: games.pick_case(idx), FREE_COOLDOWN, game="case")
    if not ok:
        return err(e, balance=bal)
    return web.json_response({"case": idx, "item": code % 100, "prize": prize, "balance": bal, "price": price})


# ----- краш -----
async def crash_sync(uid: int):
    """Закрывает раунд, если он уже закончился. Возвращает словарь состояния для клиента."""
    r = await db.crash_active(uid)
    if not r:
        return {"status": "none"}
    elapsed = time.time() - r["started"]
    out = games.crash_outcome(r["cp"], r["auto"], elapsed)
    if out is None:
        return {"status": "running", "round": r["id"], "bet": r["bet"], "elapsed": elapsed,
                "mult": games.mult_x100_at(elapsed), "auto": r["auto"]}
    kind, x100 = out
    won = kind == "win"
    prize = games.crash_prize(r["bet"], x100) if won else 0
    _, bal = await db.crash_settle(r["id"], uid, won, x100, prize)
    return {"status": "won" if won else "lost", "round": r["id"], "bet": r["bet"], "mult": x100,
            "crash": r["cp"], "prize": prize, "balance": bal}


async def api_crash_state(request):
    u = auth(request)
    return web.json_response(await crash_sync(u.id))


async def api_crash_start(request):
    u = auth(request)
    throttle(u.id)
    b = await body(request)
    bet = parse_int(b, "bet")
    if bet not in BETS:
        return err("bad_bet")
    try:
        auto = int(round(float(b.get("auto", 0) or 0) * 100))
    except (TypeError, ValueError):
        auto = 0
    if auto and not 101 <= auto <= games.CRASH_MAX_X100:
        return err("bad_auto")
    await touch(u)
    st = await crash_sync(u.id)                 # закрыть прошлый раунд, если он уже закончился
    if st["status"] == "running":
        return err("round_active", state=st)
    ok, e, rid, started, bal = await db.crash_start(u.id, bet, games.new_crash_point(), auto)
    if not ok:
        return err(e, balance=bal)
    return web.json_response({"status": "running", "round": rid, "bet": bet, "elapsed": 0.0, "mult": 100,
                              "auto": auto, "balance": bal, "k": games.CRASH_K})


async def api_crash_cashout(request):
    u = auth(request)
    st = await crash_sync(u.id)
    if st["status"] == "none":
        return err("no_round")
    if st["status"] != "running":               # уже закончился: краш или сработал автовывод
        return web.json_response(st)
    r = await db.crash_active(u.id)
    x100 = min(games.mult_x100_at(time.time() - r["started"]), r["cp"])
    prize = games.crash_prize(r["bet"], x100)
    closed, bal = await db.crash_settle(r["id"], u.id, True, x100, prize)
    if not closed:
        return web.json_response(await crash_sync(u.id))
    return web.json_response({"status": "won", "round": r["id"], "bet": r["bet"], "mult": x100,
                              "crash": r["cp"], "prize": prize, "balance": bal})


async def api_invoice(request):
    u = auth(request)
    b = await body(request)
    stars = parse_int(b, "stars")
    if not MIN_STARS <= stars <= MAX_STARS:
        return err("bad_amount")
    link = await bot.create_invoice_link(
        title=f"Пополнение {stars} ⭐",
        description=f"Пополнение баланса NFT Wheel на {stars} ⭐ (1 ⭐ = 1 ⭐)",
        payload=f"dep:{u.id}:{stars}",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение", amount=stars)],
    )
    return web.json_response({"link": link})


_gifts_cache = {"t": 0, "items": []}


async def load_gifts():
    if time.time() - _gifts_cache["t"] < 600:
        return _gifts_cache["items"]
    res = await bot.get_available_gifts()
    items = []
    for g in res.gifts:
        if g.remaining_count == 0:  # лимитированный и уже распродан
            continue
        items.append({
            "id": g.id,
            "stars": g.star_count,
            "emoji": g.sticker.emoji if g.sticker else "🎁",
            "cost": gift_cost(g.star_count),
        })
    items.sort(key=lambda x: x["stars"])
    # по одному подарку на каждую цену, чтобы список не был огромным
    seen, uniq = set(), []
    for it in items:
        if it["stars"] not in seen:
            seen.add(it["stars"])
            uniq.append(it)
    _gifts_cache.update(t=time.time(), items=uniq)
    return uniq


async def api_gifts(request):
    auth(request)
    if not WITHDRAW_ENABLED:
        return web.json_response({"gifts": []})
    return web.json_response({"gifts": await load_gifts()})


async def api_withdraw(request):
    u = auth(request)
    if not WITHDRAW_ENABLED:
        return err("disabled")
    b = await body(request)
    async with withdraw_lock:       # проверка банка и отправка подарка идут по одному — банк не уйдёт в минус
        gift = next((g for g in await load_gifts() if g["id"] == str(b.get("gift_id"))), None)
        if not gift:
            return err("no_gift")
        row = await db.get_user(u.id)
        if not row or row["deposited_stars"] < MIN_DEPOSIT_FOR_WITHDRAW:
            return err("need_deposit")
        try:
            if (await bot.get_my_star_balance()).amount < gift["stars"]:
                return err("bank_empty")
        except Exception:
            pass
        wid = await db.reserve_withdraw(u.id, gift["id"], gift["stars"], gift["cost"])
        if not wid:
            return err("no_money")
        try:
            await bot.send_gift(gift_id=gift["id"], user_id=u.id, text="Выигрыш из NFT Wheel 🎡")
            ok = True
        except Exception as e:
            log.warning("send_gift failed: %s", e)
            ok = False
        await db.finish_withdraw(wid, u.id, ok, gift["stars"], gift["cost"])
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"🎁 Вывод {gift['stars']} ⭐ для {u.id}: {'✅' if ok else '❌'}")
    row = await db.get_user(u.id)
    if not ok:
        return err("send_failed", 500, balance=row["balance"])
    return web.json_response({"ok": True, "balance": row["balance"], "stars": gift["stars"]})


async def api_top(request):
    auth(request)
    return web.json_response({"top": await db.top()})


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/me", api_me)
    app.router.add_post("/api/spin", api_spin)
    app.router.add_post("/api/slots", api_slots)
    app.router.add_post("/api/case", api_case)
    app.router.add_post("/api/crash/state", api_crash_state)
    app.router.add_post("/api/crash/start", api_crash_start)
    app.router.add_post("/api/crash/cashout", api_crash_cashout)
    app.router.add_post("/api/invoice", api_invoice)
    app.router.add_post("/api/gifts", api_gifts)
    app.router.add_post("/api/withdraw", api_withdraw)
    app.router.add_post("/api/top", api_top)
    return app


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    await db.connect()
    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web on :%s, webapp url: %s, RTP: %s", PORT, WEBAPP_URL or "(не задан)", rtp_line())

    await bot.set_my_commands([
        BotCommand(command="start", description="Играть"),
        BotCommand(command="balance", description="Баланс"),
        BotCommand(command="terms", description="Условия"),
        BotCommand(command="paysupport", description="Помощь с оплатой"),
    ])
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎡 Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
