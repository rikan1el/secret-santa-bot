import asyncio
import html
import json
import logging
import os
import random
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import (ADMIN_ID, BOT_TOKEN, DEFAULT_GAME, MAX_PARTICIPANTS,
                    MIN_PARTICIPANTS, PAIRS, REMINDER_CHECK_HOURS,
                    REMINDER_LEAD_HOURS, TIMEZONE, as_datetime)

BASE_DIR = Path(__file__).resolve().parent
PARTICIPANTS_FILE = BASE_DIR / "participants.json"
ASSIGNMENTS_FILE = BASE_DIR / "assignments.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
STATE_FILE = BASE_DIR / "state.json"
REMINDERS_FILE = BASE_DIR / "reminders.json"

logger = logging.getLogger(__name__)
dispatcher = Dispatcher()
bot: Bot | None = None
lock = asyncio.Lock()
TZ = ZoneInfo(TIMEZONE)


def setup_logging():
    """Логи с понятным форматом — видны в `pm2 logs`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # aiogram пишет строку на каждый апдейт — это шум, оставляем только предупреждения.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def now() -> datetime:
    """Текущее время в часовом поясе игры (naive, как и as_datetime)."""
    return datetime.now(TZ).replace(tzinfo=None)

# ===========================================================================
# Хранение данных (JSON)
# ===========================================================================

# Гарантии от одновременной записи:
#  1. Все операции "прочитал -> изменил -> записал" выполняются под одним
#     asyncio.Lock. Обработчики aiogram работают в одном событийном цикле, так
#     что два обработчика не могут одновременно изменить словарь в памяти.
#  2. Сама запись атомарна: данные пишутся во временный файл в том же каталоге,
#     затем flush + fsync (данные на диске, а не только в кэше ОС) и os.replace.
#     Читатель либо видит старый файл целиком, либо новый — недописанных данных
#     нет. Поэтому внезапное падение процесса не портит данные.
#  3. ЗАПУСКАЙТЕ ТОЛЬКО ОДИН ЭКЗЕМПЛЯР БОТА. asyncio.Lock защищает внутри
#     процесса, но не между процессами. Вторая копия с тем же каталогом данных
#     может перетереть изменения первой.


def load_json(path, default):
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        corrupt = path.with_name(path.name + ".corrupt")
        os.replace(path, corrupt)
        logger.error("Файл %s повреждён и переименован в %s: %s", path, corrupt, error)
        return default


def save_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def valid_participant(p) -> bool:
    return (isinstance(p, dict) and isinstance(p.get("id"), int)
            and not isinstance(p.get("id"), bool))


raw = load_json(PARTICIPANTS_FILE, {})
if not isinstance(raw, dict):
    logger.error("participants.json содержит не объект — игнорирую")
    raw = {}
participants = [p for p in raw.get("participants", []) if valid_participant(p)]
raw_pairs = raw.get("pairs") if isinstance(raw.get("pairs"), list) else []
pairs = [list(p) for p in raw_pairs if isinstance(p, (list, tuple)) and len(p) == 2]
if not pairs:
    pairs = [list(p) for p in PAIRS]

raw_settings = load_json(SETTINGS_FILE, {})
settings = {**DEFAULT_GAME, **(raw_settings if isinstance(raw_settings, dict) else {})}

raw_state = load_json(STATE_FILE, {})
if not isinstance(raw_state, dict):
    logger.error("state.json содержит не объект — игнорирую")
    raw_state = {}
state = {
    "started": False,
    "drawn": False,
    "chat_id": None,
    "santa_message_id": None,
    "undelivered": [],
    **raw_state,
}

raw_assignments = load_json(ASSIGNMENTS_FILE, {})
assignments = raw_assignments if isinstance(raw_assignments, dict) else {}
if assignments:
    state["drawn"] = True

raw_reminders = load_json(REMINDERS_FILE, {})
reminders_sent = {**{"registration": False, "profile": False, "profile_all": False,
                     "exchange": False, "draw": False, "auto_draw": False},
                  **(raw_reminders if isinstance(raw_reminders, dict) else {})}


def persist():
    save_json(PARTICIPANTS_FILE, {"participants": participants, "pairs": pairs})
    save_json(SETTINGS_FILE, settings)
    save_json(STATE_FILE, state)


def persist_reminders():
    save_json(REMINDERS_FILE, reminders_sent)


# ===========================================================================
# Вспомогательные функции
# ===========================================================================

def is_admin(obj) -> bool:
    user = getattr(obj, "from_user", None)
    return bool(user and user.id == ADMIN_ID)


def find_participant(user_id):
    return next((p for p in participants if p["id"] == user_id), None)


def find_by_pair_key(key):
    key = str(key).strip().lstrip("@")
    if key.isdecimal():
        return find_participant(int(key))
    return next((p for p in participants
                 if (p.get("username") or "").lower() == key.lower()), None)


def has_profile(p) -> bool:
    return bool((p.get("profile") or {}).get("interests_hobbies"))


def recipient_of(user_id):
    recipient_id = assignments.get(str(user_id))
    return find_participant(int(recipient_id)) if recipient_id else None


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def linkify(value) -> str:
    """Экранирует текст и делает URL кликабельными."""
    return re.sub(
        r"(https?://[^\s<>\"']+)",
        lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>',
        esc(value),
    )


PROFILE_SECTIONS = [
    ("interests_hobbies", "🎁", "Интересы и хобби"),
    ("categories", "🏷️", "Любимые категории"),
    ("wishes", "💡", "Что хотелось бы"),
    ("specific_ideas", "✨", "Конкретные идеи"),
    ("avoid", "🚫", "Что не стоит дарить"),
    ("disliked", "🙅", "Не нравятся категории"),
    ("comments", "📝", "Дополнительно"),
]


def profile_text(p, preview=False) -> str:
    """Красиво оформленная анкета в HTML. Ссылки внутри текста кликабельны."""
    pf = p.get("profile") or {}
    budget = f"{settings.get('budget_min', '')}–{settings.get('budget_max', '')} ₽"
    username = (p.get("username") or "").strip()

    head = ("🎅 <b>Ты стал Тайным Сантой!</b>" if not preview
            else "🎅 <b>Предпросмотр анкеты</b>")
    parts = [
        head,
        "",
        "Тебе предстоит подготовить подарок для:" if not preview else "Так вас увидят:",
        "",
        f"👤 <b>{esc(p.get('name', '—'))}</b>",
    ]
    if username:
        parts.append(f"   @{esc(username)}")
    link = f"https://t.me/{username or p['id']}"
    parts.append(f'   <a href="{link}">Открыть профиль</a>')
    parts.append("━━━━━━━━━━━━━━━")

    for key, emoji, title in PROFILE_SECTIONS:
        if pf.get(key):
            parts += ["", f"{emoji} <b>{title}</b>", esc(pf[key])]
    if pf.get("links"):
        parts += ["", "🔗 <b>Ссылки</b>", linkify(pf["links"])]

    parts += [
        "━━━━━━━━━━━━━━━",
        f"💰 <b>Бюджет:</b> {esc(budget)}",
        f"📅 <b>Обмен подарками:</b> {esc(settings.get('exchange_date', '—'))}",
    ]
    if not preview:
        parts += ["", "🔒 Твоё имя остаётся в секрете — получатель не узнает, кто Санта."]
    return "\n".join(parts)


def pairs_flat() -> dict[int, set[int]]:
    """Возвращает взаимные запреты: {id: {id_партнёров}}."""
    forbidden: dict[int, set[int]] = {}
    for pair in pairs:
        ids = [p["id"] for p in (find_by_pair_key(k) for k in pair) if p]
        for a in ids:
            for b in ids:
                if a != b:
                    forbidden.setdefault(a, set()).add(b)
    return forbidden


def unresolved_pairs() -> list[str]:
    """Пары, где хотя бы один участник не найден среди участников игры."""
    bad = []
    for a, b in pairs:
        pa, pb = find_by_pair_key(a), find_by_pair_key(b)
        if not pa or not pb:
            unknown = a if not pa else b
            bad.append(f"{a} ↔ {b} (не найден: {unknown})")
    return bad


def is_registration_open() -> bool:
    if state["drawn"]:
        return False
    if len(participants) >= MAX_PARTICIPANTS:
        return False
    deadline = as_datetime(settings.get("registration_deadline", ""))
    return not deadline or now() <= deadline


# ===========================================================================
# Регистрация (/santa)
# ===========================================================================

def participate_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎅 Участвовать", callback_data="register")],
        [InlineKeyboardButton(text="📋 Моя анкета", callback_data="my")],
    ])


def santa_text(closed=False):
    names = ", ".join(p["name"] for p in participants) or "пока никого"
    lines = [
        "🎄 <b>Тайный Санта</b>",
        "",
        esc(settings.get("announcement", "")),
        "",
        f"👥 Участников: <b>{len(participants)}</b> из {MAX_PARTICIPANTS}",
        f"({esc(names)})",
        "",
        f"Регистрация до: {esc(settings.get('registration_deadline', '—'))}",
        f"Бюджет: {settings.get('budget_min', '')}–{settings.get('budget_max', '')} ₽",
        "",
    ]
    if closed:
        lines.append("🔒 Регистрация закрыта, жеребьёвка проведена.")
    else:
        lines.append("Участие добровольное — нажмите кнопку ниже.")
    return "\n".join(lines)


async def refresh_santa_message(closed=False):
    """Обновляет групповое сообщение регистрации со счётчиком участников."""
    chat_id = state.get("chat_id")
    message_id = state.get("santa_message_id")
    if not chat_id or not message_id:
        return
    markup = None if closed else participate_keyboard()
    try:
        await bot.edit_message_text(santa_text(closed), chat_id=chat_id,
                                    message_id=message_id, parse_mode="HTML",
                                    reply_markup=markup)
    except Exception as error:
        logger.warning("Не удалось обновить сообщение регистрации: %s", error)


@dispatcher.message(Command("santa"))
async def santa(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    state["started"] = True
    state["chat_id"] = message.chat.id
    sent = await message.answer(santa_text(),
                                parse_mode="HTML", reply_markup=participate_keyboard())
    state["santa_message_id"] = getattr(sent, "message_id", None)
    persist()
    logger.info("Регистрация открыта в чате %s (админ %s)", message.chat.id, message.from_user.id)


@dispatcher.callback_query(F.data == "register")
async def register_callback(callback: CallbackQuery):
    async with lock:
        if not is_registration_open():
            return await callback.answer(
                "Регистрация закрыта: жеребьёвка проведена, места заняты или прошёл дедлайн.",
                show_alert=True)
        user = callback.from_user
        if find_participant(user.id):
            return await callback.answer(
                f"Вы уже участвуете. Участников: {len(participants)}/{MAX_PARTICIPANTS}.",
                show_alert=True)
        participants.append({
            "id": user.id,
            "name": user.full_name,
            "username": user.username or "",
            "profile": {},
        })
        persist()
        count = len(participants)
        logger.info("Зарегистрирован участник: %s (id=%s), всего %d/%d",
                    user.full_name, user.id, count, MAX_PARTICIPANTS)

    # Обновляем сообщение в группе: счётчик и список участников.
    await refresh_santa_message()

    # Закрываем регистрацию автоматически, если набрался максимум.
    if count >= MAX_PARTICIPANTS:
        await refresh_santa_message(closed=True)

    text = (f"🎅 Вы зарегистрированы! Участников: {count}/{MAX_PARTICIPANTS}.\n\n"
            "Заполните анкету до жеребьёвки: /profile")
    try:
        await bot.send_message(user.id, text)
        await callback.answer(f"✅ Вы в игре! Участников: {count}/{MAX_PARTICIPANTS}")
    except Exception:
        await callback.answer(
            f"✅ Вы в игре! Участников: {count}/{MAX_PARTICIPANTS}.\n"
            "Напишите боту в личку: /profile", show_alert=True)


@dispatcher.callback_query(F.data == "my")
async def my_callback(callback: CallbackQuery):
    async with lock:
        if not state["drawn"]:
            return await callback.answer(
                "Жеребьёвка ещё не проведена. Заполните анкету: /profile", show_alert=True)
        target = recipient_of(callback.from_user.id)
    # Результат уходит ТОЛЬКО в личку, чтобы получатель не раскрылся в группе.
    try:
        await bot.send_message(
            callback.from_user.id,
            profile_text(target) if target else "Вас нет в распределении.",
            parse_mode="HTML")
        await callback.answer("Отправил вам в личные сообщения ✅")
    except Exception:
        await callback.answer(
            "Напишите боту в личку и нажмите /start, чтобы получить результат.",
            show_alert=True)


# ===========================================================================
# Личные команды
# ===========================================================================

@dispatcher.message(Command("start"), F.chat.type == "private")
async def start(message: Message):
    user = message.from_user
    async with lock:
        p = find_participant(user.id)
        if p is None:
            return await message.answer(
                "Привет! Я бот Тайного Санты. Чтобы участвовать, нажмите кнопку "
                "«Участвовать» в сообщении администратора в групповом чате."
            )
        if state["drawn"]:
            target = recipient_of(user.id)
            if target:
                return await message.answer(profile_text(target), parse_mode="HTML")
            return await message.answer("Жеребьёвка уже проведена.")
        profile_ready = has_profile(p)
        preview = "\n".join([
            f"Имя: {p['name']}",
            f"Интересы: {(p.get('profile') or {}).get('interests_hobbies') or '—'}",
        ])
    if profile_ready:
        await message.answer("Вы зарегистрированы.\n\nВаша анкета:\n" + preview +
                             "\n\nИзменить: /profile")
    else:
        await message.answer("Вы зарегистрированы. Заполните анкету: /profile")


@dispatcher.message(Command("my"), F.chat.type == "private")
async def my(message: Message):
    async with lock:
        target = recipient_of(message.from_user.id)
        if not state["drawn"] or not target:
            return await message.answer("Жеребьёвка ещё не проведена.")
    await message.answer(profile_text(target), parse_mode="HTML")


@dispatcher.message(Command("list"), F.chat.type == "private")
async def list_participants(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    async with lock:
        lines = [f"{i}. {p['name']} (@{p['username']})"
                 f"{' — анкета есть' if has_profile(p) else ' — анкеты нет'}"
                 for i, p in enumerate(participants, 1)]
        text = "Участники:\n" + ("\n".join(lines) if lines else "Пока нет участников.")
        text += "\nРегистрация: " + ("открыта" if is_registration_open() else "закрыта")
        text += "\nЖеребьёвка: " + ("проведена" if state["drawn"] else "не проведена")
    await message.answer(text)


# ===========================================================================
# Анкета
# ===========================================================================

QUESTIONS = [
    ("interests_hobbies", "Чем вы увлекаетесь? Хобби и интересы.", False),
    ("categories", "Какие категории товаров любите?", True),
    ("wishes", "Что было бы интересно получить?", True),
    ("specific_ideas", "Конкретные идеи подарков.", True),
    ("links", "Ссылки на желаемые товары.", True),
    ("avoid", "Что точно не стоит дарить?", True),
    ("disliked", "Какие категории подарков не нравятся?", True),
    ("comments", "Дополнительные комментарии.", True),
]

# Незавершённые анкеты в памяти (не требуют сохранения до подтверждения).
user_states: dict[int, dict] = {}


def ask(index) -> str:
    key, text, optional = QUESTIONS[index]
    return (f"Вопрос {index + 1}/{len(QUESTIONS)}\n\n{text}"
            + (" (можно пропустить)" if optional else ""))


def profile_buttons(index) -> InlineKeyboardMarkup | None:
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data="pf:back"))
    if QUESTIONS[index][2]:
        nav.append(InlineKeyboardButton(text="Пропустить", callback_data="pf:skip"))
    return InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None


def profile_allowed(user_id) -> tuple[bool, str]:
    p = find_participant(user_id)
    if p is None:
        return False, "Сначала зарегистрируйтесь через кнопку «Участвовать»."
    if state["drawn"]:
        return False, "Жеребьёвка уже проведена, менять анкету нельзя."
    deadline = as_datetime(settings.get("profile_deadline", ""))
    if deadline and now() > deadline:
        return False, "Дедлайн заполнения анкеты прошёл."
    return True, ""


def preview_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data="pf:save"),
        InlineKeyboardButton(text="✏️ Начать заново", callback_data="pf:restart"),
    ]])


def fake_participant(user_id, answers):
    p = find_participant(user_id) or {"id": user_id, "name": "—", "username": ""}
    return {**p, "profile": answers}


def save_draft_answer(user_id, key, value):
    """Сохраняет каждый ответ анкеты сразу, до её подтверждения."""
    p = find_participant(user_id)
    if p is None:
        return
    p.setdefault("draft_profile", {})[key] = value
    persist()


def draft_of(p):
    return dict(p.get("draft_profile") or p.get("profile") or {})


def profile_hub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить анкету", callback_data="pf:edit")],
        [InlineKeyboardButton(text="🔁 Заполнить заново", callback_data="pf:restart")],
    ])


async def start_questionnaire(user_id, chat_id, draft):
    user_states[user_id] = {"index": 0, "answers": draft}
    await bot.send_message(chat_id, "Заполняем анкету. На каждом шаге можно вернуться назад.\n\n"
                           + ask(0), reply_markup=profile_buttons(0))


@dispatcher.message(Command("profile"), F.chat.type == "private")
async def profile(message: Message):
    async with lock:
        ok, problem = profile_allowed(message.from_user.id)
        if not ok:
            return await message.answer(problem)
        p = find_participant(message.from_user.id)
        draft = draft_of(p)
        existing = has_profile(p)
    if existing:
        # Просмотр своей анкеты + выбор действия.
        await message.answer(profile_text(p, preview=True),
                             parse_mode="HTML", reply_markup=profile_hub_keyboard())
    else:
        await start_questionnaire(message.from_user.id, message.chat.id, draft)


@dispatcher.callback_query(F.data == "pf:edit")
async def profile_edit(callback: CallbackQuery):
    await callback.answer()
    async with lock:
        ok, problem = profile_allowed(callback.from_user.id)
        if not ok:
            return await callback.message.answer(problem)
        draft = draft_of(find_participant(callback.from_user.id))
    await start_questionnaire(callback.from_user.id, callback.message.chat.id, draft)


@dispatcher.callback_query(F.data.in_({"pf:skip", "pf:back"}))
async def profile_nav(callback: CallbackQuery):
    if state["drawn"]:
        return await callback.answer("Жеребьёвка проведена, менять анкету нельзя.", show_alert=True)
    await callback.answer()
    st = user_states.get(callback.from_user.id)
    if not st:
        return await callback.message.answer("Начните заново: /profile")
    if callback.data == "pf:skip":
        key = QUESTIONS[st["index"]][0]
        if not st["answers"].get(key):
            st["answers"][key] = ""
            save_draft_answer(callback.from_user.id, key, "")
        st["index"] += 1
    elif callback.data == "pf:back":
        st["index"] = max(0, st["index"] - 1)
    if st["index"] >= len(QUESTIONS):
        return await show_preview(callback.from_user.id, callback.message.chat.id, st["answers"])
    await callback.message.answer(ask(st["index"]),
                                  reply_markup=profile_buttons(st["index"]))


async def show_preview(user_id, chat_id, answers):
    await bot.send_message(
        chat_id,
        profile_text(fake_participant(user_id, answers), preview=True),
        reply_markup=preview_keyboard(),
        parse_mode="HTML",
    )


@dispatcher.callback_query(F.data == "pf:save")
async def profile_save(callback: CallbackQuery):
    await callback.answer()
    async with lock:
        ok, problem = profile_allowed(callback.from_user.id)
        if not ok:
            return await callback.message.answer(problem)
        st = user_states.get(callback.from_user.id)
        if not st:
            return await callback.message.answer("Начните заново: /profile")
        p = find_participant(callback.from_user.id)
        p["profile"] = st["answers"]
        p.pop("draft_profile", None)
        persist()
    user_states.pop(callback.from_user.id, None)
    logger.info("Анкета сохранена: %s (id=%s)", p["name"], p["id"])
    await callback.message.answer("Анкета сохранена ✅\nПосмотреть её: /profile")


@dispatcher.callback_query(F.data == "pf:restart")
async def profile_restart(callback: CallbackQuery):
    await callback.answer()
    async with lock:
        ok, problem = profile_allowed(callback.from_user.id)
        if not ok:
            return await callback.message.answer(problem)
        p = find_participant(callback.from_user.id)
        if p and p.get("draft_profile"):
            p["draft_profile"] = {}
            persist()
    user_states[callback.from_user.id] = {"index": 0, "answers": {}}
    await callback.message.answer("Начинаем заново.\n\n" + ask(0),
                                  reply_markup=profile_buttons(0))


# ===========================================================================
# Админ-панель
# ===========================================================================

SETTINGS_LABELS = {
    "budget_min": "Минимальная стоимость, ₽",
    "budget_max": "Максимальная стоимость, ₽",
    "draw_date": "Дата жеребьёвки",
    "draw_time": "Время жеребьёвки",
    "exchange_date": "Дата обмена подарками",
    "registration_deadline": "Дедлайн регистрации",
    "profile_deadline": "Дедлайн заполнения анкет",
    "announcement": "Текст объявления",
}


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки игры", callback_data="adm:settings")],
        [InlineKeyboardButton(text="👥 Участники", callback_data="adm:list")],
        [InlineKeyboardButton(text="🎲 Жеребьёвка", callback_data="adm:draw")],
        [InlineKeyboardButton(text="🔁 Переотправить результаты", callback_data="adm:retry")],
        [InlineKeyboardButton(text="🔄 Сброс", callback_data="adm:reset")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="💑 Пары", callback_data="adm:pairs")],
    ])


def settings_menu():
    rows = [[InlineKeyboardButton(text=f"{label}: {settings.get(key, '—')}",
                                  callback_data=f"adm:set:{key}")]
            for key, label in SETTINGS_LABELS.items()]
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to(target):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data=target)]])


async def send_list(chat_id):
    lines = [f"{i}. {p['name']} (@{p['username']}) "
             f"{'— анкета есть' if has_profile(p) else '— анкеты нет'}"
             for i, p in enumerate(participants, 1)]
    text = "Участники:\n" + ("\n".join(lines) if lines else "Пока нет участников.")
    text += "\nРегистрация: " + ("открыта" if is_registration_open() else "закрыта")
    text += "\nЖеребьёвка: " + ("проведена" if state["drawn"] else "не проведена")
    await bot.send_message(chat_id, text)


@dispatcher.message(Command("admin"), F.chat.type == "private")
async def admin(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    await message.answer("Панель администратора:", reply_markup=admin_menu())


@dispatcher.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Только администратор.", show_alert=True)
    data = callback.data.split(":")
    action = data[1]
    await callback.answer()

    if action == "main":
        return await callback.message.edit_text("Панель администратора:", reply_markup=admin_menu())
    if action == "settings":
        return await callback.message.edit_text("Настройки игры:", reply_markup=settings_menu())
    if action == "set":
        key = data[2]
        admin_input.pending = key  # следующий текст админа — значение настройки
        return await callback.message.edit_text(
            f"{SETTINGS_LABELS[key]}\n\nТекущее: {settings.get(key, '—')}\n\n"
            "Отправьте новое значение.", reply_markup=back_to("adm:settings"))
    if action == "list":
        return await send_list(callback.message.chat.id)
    if action == "draw":
        return await perform_draw(callback.message.chat.id)
    if action == "reset":
        return await reset_draw(callback.message.chat.id)
    if action == "broadcast":
        admin_input.broadcast = True
        return await callback.message.edit_text(
            "Отправьте текст объявления — он уйдёт всем участникам.",
            reply_markup=back_to("adm:main"))
    if action == "retry":
        async with lock:
            if not state["drawn"]:
                return await callback.message.answer("Жеребьёвка ещё не проведена.")
            pending = state.get("undelivered") or []
        if not pending:
            return await callback.message.answer("Недоставленных сообщений нет.")
        async with lock:
            failed = await deliver_results()
            state["undelivered"] = failed
            persist()
            undelivered_names = failed_text(failed) if failed else ""
        logger.info("Переотправка результатов: успешно %d, не доставлено %d",
                    len(pending) - len(failed), len(failed))
        text = f"Переотправлено {len(pending) - len(failed)} из {len(pending)}."
        if not failed:
            return await callback.message.answer(text + "\n\nВсем участникам доставлено ✅")
        text += "\n\nВсё ещё не доставлено:\n" + undelivered_names
        text += "\n\nПопросите этих участников нажать /start у бота."
        return await callback.message.answer(text, reply_markup=retry_keyboard(failed))
    if action == "pairs":
        rows = []
        body = []
        for i, (a, b) in enumerate(pairs, 1):
            body.append(f"{i}. {a} ↔ {b}")
            rows.append([InlineKeyboardButton(text=f"❌ {a} ↔ {b}",
                                              callback_data=f"adm:delpair:{i - 1}")])
        rows.append([InlineKeyboardButton(text="➕ Добавить пару", callback_data="adm:addpair")])
        rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:main")])
        return await callback.message.edit_text(
            "💑 Участники одной пары не дарят друг другу.\n\n"
            + ("\n".join(body) if body else "Пар пока нет."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    if action == "addpair":
        admin_input.pair = True
        return await callback.message.edit_text(
            "Отправьте пару в формате: @user1 @user2", reply_markup=back_to("adm:pairs"))
    if action == "delpair":
        index = int(data[2])
        if 0 <= index < len(pairs):
            removed = pairs.pop(index)
            persist()
            logger.info("Удалена пара: %s", removed)
        return await callback.message.edit_text(
            "Пара удалена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💑 К парам", callback_data="adm:pairs")]]))


class AdminInput:
    """Небольшой объект-приёмник текстовых ответов администратора."""

    def __init__(self):
        self.pending = None
        self.broadcast = False
        self.pair = False


admin_input = AdminInput()


@dispatcher.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def text_router(message: Message):
    """Единый обработчик текста: сначала анкета, затем ввод администратора."""
    st = user_states.get(message.from_user.id)
    if st and st["index"] < len(QUESTIONS):
        if state["drawn"]:
            user_states.pop(message.from_user.id, None)
            return await message.answer("Жеребьёвка проведена, менять анкету нельзя.")
        key = QUESTIONS[st["index"]][0]
        st["answers"][key] = message.text.strip()
        save_draft_answer(message.from_user.id, key, message.text.strip())
        st["index"] += 1
        if st["index"] >= len(QUESTIONS):
            return await show_preview(message.from_user.id, message.chat.id, st["answers"])
        return await message.answer(ask(st["index"]),
                                    reply_markup=profile_buttons(st["index"]))

    if message.from_user.id != ADMIN_ID:
        return

    if admin_input.pending:
        key = admin_input.pending
        admin_input.pending = None
        return await save_setting(message, key)
    if admin_input.pair:
        admin_input.pair = False
        parts = message.text.replace(",", " ").split()
        if len(parts) != 2:
            return await message.answer("Формат: @user1 @user2")
        pairs.append(parts)
        persist()
        logger.info("Добавлена пара: %s ↔ %s", parts[0], parts[1])
        await message.answer(f"Пара добавлена: {parts[0]} ↔ {parts[1]}",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                 InlineKeyboardButton(text="💑 К парам", callback_data="adm:pairs")]]))
        return
    if admin_input.broadcast:
        admin_input.broadcast = False
        delivered, failed = await broadcast(message.text)
        text = f"Отправлено {delivered} из {len(participants)}."
        if failed:
            text += "\nНе доставлено:\n" + "\n".join(f"• {f}" for f in failed)
        return await message.answer(text)


async def save_setting(message: Message, key):
    raw_value = message.text.strip()
    if key in ("budget_min", "budget_max"):
        if not raw_value.isdecimal():
            return await message.answer("Введите целое число в рублях.")
        value = int(raw_value)
        if key == "budget_min" and value > settings["budget_max"]:
            return await message.answer("Минимум не может превышать максимум.")
        if key == "budget_max" and value < settings["budget_min"]:
            return await message.answer("Максимум не может быть меньше минимума.")
    else:
        value = raw_value
    settings[key] = value
    persist()
    logger.info("Настройка изменена: %s = %s", key, value)
    await message.answer(f"Сохранено: {SETTINGS_LABELS[key]} = {value}",
                         reply_markup=settings_menu())


async def broadcast(text) -> tuple[int, list[str]]:
    delivered, failed = 0, []
    for p in participants:
        try:
            await bot.send_message(p["id"], f"📢 {text}")
            delivered += 1
        except Exception:
            failed.append(f"{p['name']} (@{p.get('username') or p['id']})")
    logger.info("Рассылка завершена: доставлено %d/%d, ошибок %d",
                delivered, len(participants), len(failed))
    return delivered, failed


# ===========================================================================
# Жеребьёвка
# ===========================================================================

def build_assignment():
    """Случайная дерранжировка без самоназначений и без пар."""
    forbidden = pairs_flat()
    ids = [p["id"] for p in participants]
    order = ids.copy()
    for _ in range(5000):
        target = ids.copy()
        random.shuffle(target)
        if all(a != b and b not in forbidden.get(a, ())
               for a, b in zip(order, target)):
            result = {str(a): b for a, b in zip(order, target)}
            # Проверка корректности перед сохранением.
            if (len(result) == len(ids)
                    and set(map(int, result)) == set(ids)
                    and set(result.values()) == set(ids)
                    and all(int(a) != b for a, b in result.items())):
                return result
    return None


async def deliver_results():
    """Рассылает каждому его получателя. Возвращает список недоставленных id."""
    failed = []
    for p in participants:
        recipient = find_participant(int(assignments[str(p["id"])]))
        if not recipient:
            failed.append(p["id"])
            continue
        try:
            await bot.send_message(p["id"], profile_text(recipient), parse_mode="HTML")
        except Exception as error:
            logger.warning("Не доставлено %s: %s", p["id"], error)
            failed.append(p["id"])
    return failed


def failed_text(failed_ids):
    names = []
    for user_id in failed_ids:
        p = find_participant(user_id)
        names.append(f"{p['name']} (@{p.get('username') or user_id})" if p else str(user_id))
    return "\n".join(f"• {n}" for n in names)


def retry_keyboard(failed_ids):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Переотправить недоставленные",
                             callback_data="adm:retry")]]) if failed_ids else None


async def post_group_draw():
    """Публикует в группе сообщение о прошедшей жеребьёвке."""
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    text = (
        "🎉 <b>Жеребьёвка прошла!</b>\n\n"
        "Каждый участник получил в личку своего Тайного Санту — кому готовить подарок.\n\n"
        "📬 Не пришло сообщение? Откройте бота и нажмите /start, затем /my."
    )
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        logger.info("Опубликован пост о жеребьёвке в чате %s", chat_id)
    except Exception as error:
        logger.warning("Не удалось опубликовать пост о жеребьёвке в чате %s: %s", chat_id, error)


async def perform_draw(admin_chat):
    async with lock:
        if state["drawn"]:
            return await bot.send_message(
                admin_chat,
                "Жеребьёвка уже проведена. Чтобы повторить, сначала выполните /reset.",
            )
        if len(participants) < MIN_PARTICIPANTS:
            return await bot.send_message(admin_chat,
                                          f"Нужно минимум {MIN_PARTICIPANTS} участника.")
        bad_pairs = unresolved_pairs()
        if bad_pairs:
            logger.warning("Жеребьёвка отменена: неразрешённые пары: %s", bad_pairs)
            return await bot.send_message(
                admin_chat,
                "⚠️ Жеребьёвка отменена: не все пары указывают на участников игры. "
                "Иначе партнёры могут попасть друг к другу.\n\nНе найдены:\n"
                + "\n".join(f"• {b}" for b in bad_pairs)
                + "\n\nИсправьте пары в /admin → 💑 Пары или удалите лишние.",
            )
        result = build_assignment()
        if result is None:
            logger.warning("Не удалось построить распределение с учётом пар")
            return await bot.send_message(
                admin_chat,
                "Не удалось распределить участников с учётом пар. "
                "Проверьте пары в /admin → 💑 Пары.",
            )

        logger.info("Жеребьёвка: старт, участников %d", len(participants))
        # "Транзакция": результат сохраняется одной атомарной записью —
        # либо обновлён весь файл, либо ничего.
        save_json(ASSIGNMENTS_FILE, result)
        assignments.clear()
        assignments.update(result)
        state["drawn"] = True
        state["undelivered"] = []
        persist()

        # Закрываем групповое сообщение регистрации.
        await refresh_santa_message(closed=True)

        failed = await deliver_results()
        state["undelivered"] = failed
        persist()
        logger.info("Жеребьёвка: результаты доставлены %d/%d, ошибок %d",
                    len(participants) - len(failed), len(participants), len(failed))

        # Публикуем пост в группе только после рассылки.
        await post_group_draw()

        text = (f"Жеребьёвка завершена. Доставлено "
                f"{len(participants) - len(failed)} из {len(participants)}.")
        if failed:
            text += ("\n\n⚠️ Не удалось доставить:\n" + failed_text(failed)
                     + "\n\nПроверьте работу ниже. Повторно жеребьёвка не запускается.")
        await bot.send_message(admin_chat, text, reply_markup=retry_keyboard(failed))


@dispatcher.message(Command("draw"), F.chat.type == "private")
async def draw(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    await perform_draw(message.chat.id)


async def reset_draw(chat_id):
    async with lock:
        if not state["drawn"]:
            return await bot.send_message(chat_id, "Жеребьёвка ещё не проводилась.")
        save_json(ASSIGNMENTS_FILE, {})
        assignments.clear()
        state["drawn"] = False
        # Возвращаем напоминания в исходное состояние — после сброса игра снова активна.
        for key in reminders_sent:
            reminders_sent[key] = False
        persist_reminders()
        persist()
        # Возвращаем групповое сообщение в активное состояние.
        await refresh_santa_message(closed=False)
        failed = await notify_cancel()
        logger.info("Жеребьёвка сброшена; уведомлений не доставлено: %d", len(failed))
        text = "Жеребьёвка сброшена, участники уведомлены. Список участников сохранён."
        if failed:
            text += "\nНе удалось уведомить: " + ", ".join(failed)
        await bot.send_message(chat_id, text)


async def notify_cancel():
    failed = []
    for p in participants:
        try:
            await bot.send_message(
                p["id"],
                "❌ Жеребьёвка отменена администратором. Ожидайте нового распределения.")
        except Exception:
            failed.append(p["name"])
    return failed


@dispatcher.message(Command("reset"), F.chat.type == "private")
async def reset(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    await reset_draw(message.chat.id)


# ===========================================================================
# Напоминания
# ===========================================================================

def hours_until(value: str) -> float | None:
    dt = as_datetime(value)
    if dt is None:
        return None
    return (dt - now()).total_seconds() / 3600


async def remind(key, text, only_missing_profile=False, parse_mode=None):
    if reminders_sent.get(key):
        return
    targets = ([p for p in participants if not has_profile(p)]
               if only_missing_profile else participants)
    sent_count = 0
    for p in targets:
        try:
            await bot.send_message(p["id"], text, parse_mode=parse_mode)
            sent_count += 1
        except Exception as error:
            logger.debug("Напоминание '%s' не доставлено %s: %s", key, p["id"], error)
    reminders_sent[key] = True
    persist_reminders()
    logger.info("Напоминание '%s' отправлено: %d из %d", key, sent_count, len(targets))


async def reminder_tick():
    # Напоминание об обмене актуально и после жеребьёвки, поэтому проверяем его всегда.
    exch = hours_until(settings.get("exchange_date", ""))
    if exch is not None and 0 < exch <= REMINDER_LEAD_HOURS:
        await remind("exchange",
                     f"📅 <b>Уже завтра обмен подарками!</b>\n"
                     f"Дата: {esc(settings.get('exchange_date'))}.\n\n"
                     "Не забудьте подготовить и принести подарок. Ваш получатель ждёт 🎁",
                     parse_mode="HTML")
    if state["drawn"]:
        return
    reg = hours_until(settings.get("registration_deadline", ""))
    prof = hours_until(settings.get("profile_deadline", ""))
    draw = hours_until(f"{settings.get('draw_date', '')} {settings.get('draw_time', '')}")

    if reg is not None and 0 < reg <= REMINDER_LEAD_HOURS:
        await remind("registration",
                     f"⏳ Дедлайн регистрации приближается: {settings.get('registration_deadline')}.")
    if prof is not None and 0 < prof <= REMINDER_LEAD_HOURS:
        await remind("profile", "📝 Не забудьте заполнить анкету: /profile",
                     only_missing_profile=True)
    if draw is not None and 0 < draw <= REMINDER_LEAD_HOURS:
        await remind("draw", "🎲 Жеребьёвка скоро! Убедитесь, что анкета заполнена: /profile")
        if not reminders_sent.get("profile_all"):
            missing = [p["name"] for p in participants if not has_profile(p)]
            reminders_sent["profile_all"] = True
            persist_reminders()
            if missing:
                await bot.send_message(ADMIN_ID, "Перед жеребьёвкой нет анкеты у: "
                                       + ", ".join(missing))

    if (draw is not None and -24 < draw <= 0 and state["started"]
            and not state["drawn"] and len(participants) >= MIN_PARTICIPANTS
            and not reminders_sent.get("auto_draw")):
        reminders_sent["auto_draw"] = True
        persist_reminders()
        await perform_draw(ADMIN_ID)


@dispatcher.message(Command("health"), F.chat.type == "private")
async def health(message: Message):
    if not is_admin(message):
        return await message.answer("Команда доступна только администратору.")
    problems = []
    checks = []

    # 1. Файлы читаются (повреждённые уже переименованы загрузчиком при старте).
    for path, data in ((PARTICIPANTS_FILE, participants), (ASSIGNMENTS_FILE, assignments),
                       (SETTINGS_FILE, settings), (STATE_FILE, state)):
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
                checks.append(f"✅ {path.name}")
            except Exception as error:
                checks.append(f"❌ {path.name}")
                problems.append(f"{path.name} не читается: {error}")
        else:
            checks.append(f"➖ {path.name} (создастся при первом изменении)")

    # 2. Участники уникальны.
    ids = [p["id"] for p in participants]
    if len(ids) != len(set(ids)):
        problems.append("В participants.json есть дубли Telegram ID")

    # 3. Пары валидны: обе стороны найдены среди участников.
    for a, b in pairs:
        pa, pb = find_by_pair_key(a), find_by_pair_key(b)
        if not pa or not pb:
            problems.append(f"Пара {a} ↔ {b}: участник не найден "
                            f"({a if not pa else b})")
        elif pa["id"] == pb["id"]:
            problems.append(f"Пара {a} ↔ {b} указывает на одного человека")

    # 4. Жеребьёвка корректна.
    if state["drawn"] and assignments:
        keys = set(map(int, assignments))
        if keys != set(ids):
            problems.append("В жеребьёвке участвуют не все участники")
        if sorted(assignments.values()) != sorted(ids):
            problems.append("Кто-то получает подарок дважды или не получает вовсе")
        if any(int(a) == b for a, b in assignments.items()):
            problems.append("Найдено самоназначение")
        forbidden = pairs_flat()
        if any(b in forbidden.get(int(a), ()) for a, b in assignments.items()):
            problems.append("Партнёры из одной пары назначены друг другу")
        checks.append("✅ Жеребьёвка корректна")
    else:
        checks.append("➖ Жеребьёвка не проведена")

    # 5. Настройки.
    try:
        budget_min, budget_max = int(settings.get("budget_min", 0)), int(settings.get("budget_max", 0))
        if budget_min > budget_max:
            problems.append("Минимальный бюджет больше максимального")
    except (TypeError, ValueError):
        problems.append("Бюджет задан некорректно")
    if len(participants) < MIN_PARTICIPANTS and state["started"]:
        problems.append(f"Участников меньше минимума ({MIN_PARTICIPANTS})")

    header = "🩺 Проверка: всё в порядке" if not problems else f"🩺 Найдено проблем: {len(problems)}"
    parts = [header, "", *checks]
    if problems:
        parts += ["", "⚠️ Проблемы:", *[f"• {p}" for p in problems]]
    undelivered = state.get("undelivered") or []
    if undelivered:
        parts += ["", "📭 Не доставлено результатов:"] + [f"• {n}" for n in failed_text(undelivered).splitlines()]
    await message.answer("\n".join(parts))


# ===========================================================================
# Запуск
# ===========================================================================

async def main():
    global bot
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_БОТА" or not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise ValueError("Укажите BOT_TOKEN и числовой ADMIN_ID в config.py")

    bot = Bot(BOT_TOKEN)
    logger.info("Старт бота: админ=%s, участников=%d, жеребьёвка=%s, TZ=%s",
                ADMIN_ID, len(participants),
                "проведена" if state["drawn"] else "нет", TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(reminder_tick, IntervalTrigger(hours=REMINDER_CHECK_HOURS))
    scheduler.start()
    logger.info("Планировщик напоминаний запущен (проверка каждые %d ч)", REMINDER_CHECK_HOURS)
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
