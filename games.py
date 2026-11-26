"""Чистая игровая логика (без Telegram и БД): таблицы шансов, выбор результата, математика краша.
Все суммы — в звёздах ⭐ (1 ⭐ = 1 единица баланса). Результат определяется только на сервере."""
import math
import random
from itertools import product

rng = random.SystemRandom()

BETS = [5, 10, 25, 50]

# ---------- колесо ----------
# (множитель, эмодзи, подпись, вес). Средний возврат (RTP) считается автоматически.
SEGMENTS = [
    (0, "💨", "Пусто", 37),
    (2.5, "💎", "x2.5", 7),
    (0.5, "🍀", "x0.5", 25),
    (10, "👑", "x10", 0.7),
    (1, "🪙", "x1", 16),
    (5, "🔮", "x5", 2.7),
    (1.5, "⭐", "x1.5", 11.5),
    (50, "🚀", "x50", 0.05),
]


def prize_for(mult: float, bet: int) -> int:
    """Выигрыш в целых звёздах. Дробная часть разыгрывается случайно, поэтому матожидание точное."""
    x = round(mult * bet, 6)
    whole = int(x)
    return whole + (1 if rng.random() < x - whole else 0)


def wheel_rtp() -> float:
    total = sum(s[3] for s in SEGMENTS)
    return sum(s[0] * s[3] for s in SEGMENTS) / total


def pick_wheel(bet: int):
    idx = rng.choices(range(len(SEGMENTS)), weights=[s[3] for s in SEGMENTS])[0]
    return idx, prize_for(SEGMENTS[idx][0], bet)


# ---------- слоты ----------
# (эмодзи, вес на барабане, множитель за 3 в ряд, множитель за 2 одинаковых)
SLOT_SYMBOLS = [
    ("🍒", 30, 5, 0.5),
    ("🍋", 25, 8, 1),
    ("🍇", 20, 12, 1),
    ("🔔", 12, 20, 1.5),
    ("💎", 8, 40, 2),
    ("7️⃣", 4, 120, 3),
    ("👑", 1, 300, 5),
]


def slot_mult(reels) -> float:
    for i, s in enumerate(SLOT_SYMBOLS):
        if reels.count(i) == 3:
            return s[2]
    for i, s in enumerate(SLOT_SYMBOLS):
        if reels.count(i) == 2:
            return s[3]
    return 0


def slots_rtp() -> float:
    total = sum(s[1] for s in SLOT_SYMBOLS)
    r = 0.0
    for c in product(range(len(SLOT_SYMBOLS)), repeat=3):
        p = 1.0
        for i in c:
            p *= SLOT_SYMBOLS[i][1] / total
        r += p * slot_mult(list(c))
    return r


def pick_slots(bet: int):
    """Барабаны кодируются в одно число: a*100 + b*10 + c."""
    w = [s[1] for s in SLOT_SYMBOLS]
    reels = [rng.choices(range(len(SLOT_SYMBOLS)), weights=w)[0] for _ in range(3)]
    return reels[0] * 100 + reels[1] * 10 + reels[2], prize_for(slot_mult(reels), bet)


# ---------- кейсы ----------
# Каждый кейс: название, эмодзи, цена в ⭐, предметы (эмодзи, название, выигрыш в ⭐, вес).
CASES = [
    {"name": "Лёгкий", "emoji": "📦", "price": 10, "items": [
        ("💨", "Пусто", 0, 21), ("🧸", "Мишка", 5, 30), ("🌹", "Роза", 10, 20),
        ("🎂", "Торт", 15, 12), ("🏆", "Кубок", 20, 8), ("💍", "Кольцо", 50, 2.2)]},
    {"name": "Средний", "emoji": "🎁", "price": 25, "items": [
        ("💨", "Пусто", 0, 20), ("🧸", "Мишка", 10, 30), ("🌹", "Роза", 25, 18),
        ("🎂", "Торт", 50, 9), ("🏆", "Кубок", 100, 3.2), ("💍", "Кольцо", 250, 0.7),
        ("💎", "Алмаз", 1000, 0.05)]},
    {"name": "Рискованный", "emoji": "💎", "price": 100, "items": [
        ("💨", "Пусто", 0, 10), ("🌹", "Роза", 50, 22), ("🏆", "Кубок", 100, 10),
        ("💍", "Кольцо", 250, 4), ("💎", "Алмаз", 1000, 0.7), ("👑", "Корона", 5000, 0.04)]},
]


def case_rtp(case: dict) -> float:
    total = sum(i[3] for i in case["items"])
    return sum(i[2] * i[3] for i in case["items"]) / total / case["price"]


def pick_case(case_idx: int):
    """Возвращает (код, выигрыш); код = case_idx*100 + item_idx."""
    case = CASES[case_idx]
    idx = rng.choices(range(len(case["items"])), weights=[i[3] for i in case["items"]])[0]
    return case_idx * 100 + idx, case["items"][idx][2]


# ---------- краш ----------
CRASH_EDGE = 0.10           # преимущество казино: RTP краша = 90% при любой точке выхода
CRASH_MAX_X100 = 10000      # потолок 100.00x
CRASH_K = 0.06              # рост множителя: m(t) = exp(K * t), t в секундах


def crash_rtp() -> float:
    return 1 - CRASH_EDGE


def new_crash_point() -> int:
    """Точка краша в сотых долях (x100). 0 — мгновенный краш на старте (вероятность = преимуществу казино).
    Иначе P(точка >= x) = 0.9 / x, поэтому возврат при любой цели выхода ровно 90%."""
    u = (rng.randrange(10 ** 9) + 1) / 10 ** 9
    cp = math.floor(100 * (1 - CRASH_EDGE) / u)
    if cp < 100:
        return 0
    return min(cp, CRASH_MAX_X100)


def mult_x100_at(elapsed: float) -> int:
    return int(math.floor(100 * math.exp(CRASH_K * max(0.0, elapsed)) + 1e-9))


def time_to_x100(x100: int) -> float:
    return math.log(max(x100, 100) / 100) / CRASH_K


def crash_outcome(cp: int, auto: int, elapsed: float):
    """Что произошло к моменту elapsed. None — раунд ещё идёт.
    ('lose', cp) — краш; ('win', x100) — сработал автовывод."""
    if cp <= 0:
        return ("lose", 0)
    t_crash = time_to_x100(cp + 1)           # момент, когда множитель впервые превысит точку краша
    if auto and auto <= cp:
        if elapsed >= time_to_x100(auto):
            return ("win", auto)
    if elapsed >= t_crash:
        return ("lose", cp)
    return None


def crash_prize(bet: int, x100: int) -> int:
    return prize_for(x100 / 100, bet)
