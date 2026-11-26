# NFT Wheel (wheel_bot_v2) — Telegram-бот + мини-апп: колесо, слоты, кейсы, краш

- `bot.py` — aiogram 3 (polling) + aiohttp: раздаёт мини-апп `/` и API `/api/*` (авторизация по Telegram initData).
- `games.py` — вся игровая логика без Telegram/БД: таблицы шансов, выбор результата, математика краша. Результат только на сервере.
- `db.py` — SQLite (`$DATA_DIR/wheel.db`, на Railway volume `/data`).
- `webapp/index.html` — мини-апп одним файлом (колесо, слоты, кейсы, краш, пополнение, вывод, топ).
- `tests/test_all.py` — тесты (`python -m unittest discover -s tests`): RTP, честность краша, БД, миграция, API.
- Валюта: баланс = Telegram Stars 1:1 (монет больше нет). При первом запуске `db.py` один раз переводит старые монеты в ⭐ (2 монеты → 1 ⭐, округление вверх); флаг в таблице `meta`.
- Пополнение: Telegram Stars (XTR), invoice link → `openInvoice`; payload `dep:<user_id>:<stars>` проверяется в pre_checkout; зачисление в `successful_payment` (защита от дублей по charge_id).
- Вывод: `sendGift` с баланса звёзд бота; баланс списывается заранее и возвращается, если подарок не ушёл; под глобальным lock.
- Бот: @upspinbot_bot (UpSpin).
- RTP (возврат игрокам, считается автоматически и показывается в приложении): колесо ≈86%, слоты ≈86%, кейсы ≈86%, краш 90%. Выигрыш в целых ⭐: дробная часть разыгрывается случайно (`prize_for`), матожидание точное.
- Краш: P(точка краха ≥ x) = 0.9/x, 10% раундов — мгновенный краш; точка хранится в `crash_rounds`, клиент её не знает; множитель m(t)=exp(0.06·t), вывод и автовывод считаются по серверному времени; закрытие раунда атомарно, один раз.
- `spins.game`: 'wheel' | 'slots' | 'case' | 'crash'. segment: колесо — индекс сектора; слоты — a*100+b*10+c; кейс — case*100+item; краш — множитель×100 (при проигрыше — точка краха).
- Не редактировать файлы через PowerShell Get-Content/Set-Content — ломает UTF-8.

## Переменные окружения
BOT_TOKEN (обязательно), ADMIN_ID, DATA_DIR=/data, WITHDRAW_ENABLED=1, WITHDRAW_FEE=0, MIN_DEPOSIT_FOR_WITHDRAW=20, FREE_SPIN_BET=2 (⭐-эквивалент бесплатного спина), WEBAPP_URL (по умолчанию из RAILWAY_PUBLIC_DOMAIN).
COINS_PER_STAR больше не используется.

## Деплой
Railway: проект `nft-wheel-bot`, сервис `nft-wheel-bot`, домен https://nft-wheel-bot-production.up.railway.app, деплой `railway up --detach` из этой папки.

## Админ-команды
/stats, /refund <charge_id>, /give <user_id> <звёзды>.

## Тест локально
Локальный Python 3.12.0 ломает aiogram 3.34 — тестировать на `py -3.14` в venv.
