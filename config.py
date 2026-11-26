from datetime import datetime

# --- Основные настройки ---------------------------------------------------
BOT_TOKEN = "43243245:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
ADMIN_ID = 43243245  # Числовой Telegram ID администратора.

MAX_PARTICIPANTS = 6
MIN_PARTICIPANTS = 3

# Часовой пояс заданий (все даты в config и в админ-панели трактуются в нём).
TIMEZONE = "Europe/Moscow"

# --- Параметры игры по умолчанию (можно менять из панели администратора) ---
DEFAULT_GAME = {
    "budget_min": 500,
    "budget_max": 1000,
    "exchange_date": "13.01.2027",
    "registration_deadline": "22.11.2026 23:59",
    "profile_deadline": "22.11.2026 23:59",
    "draw_date": "15.01.2027",
    "draw_time": "18:00",
    "announcement": "Обмен подарками в нашей компании! Заполните анкету и участвуйте.",
}

# --- Пары ----------------------------------------------------------------
# Участники одной пары НИКОГДА не дарят подарки друг другу.
# Указывайте Telegram username (можно с @) или числовой ID.
# Пары хранятся в файле participants.json и правятся через /admin → "Пары".
PAIRS = [
]

# --- Напоминания ----------------------------------------------------------
# Как часто (в часах) планировщик проверяет, не пора ли что-то напомнить.
REMINDER_CHECK_HOURS = 6
# За сколько часов до события отправлять напоминание.
REMINDER_LEAD_HOURS = 24
# Сколько раз за один день отправлять напоминание о цветах/дедлайне не нужно —
# напоминание отправляется однократно за событие.

# --- Начальные значения, которые попадут в participants.json при первом запуске.
# Последнее слово всегда за данных в participants.json, поэтому если файл уже
# существует, эти константы НЕ перезаписывают его.
INITIAL_PARTICIPANTS = []  # Формат: [{"id": 123, "name": "Александр", "username": "alex"}]


def as_datetime(value: str) -> datetime | None:
    """Парсит дату/время из настроек. Пустая строка — отсутствует."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
