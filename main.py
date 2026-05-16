import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
DATA_FILE = Path("data.json")

DEFAULT_DATA = {
    "polls": [
        {
            "id": 1,
            "question": "Как прошла тренировка сегодня? 💪",
            "options": ["Огонь! 🔥", "Хорошо 👍", "Средне 😐", "Плохо 😔", "Не ходил ❌"],
            "active": True,
        },
        {
            "id": 2,
            "question": "Выполнил план на сегодня? ✅",
            "options": ["Да, полностью!", "Почти всё", "Частично", "Не выполнил"],
            "active": True,
        },
    ],
    "settings": {
        "group_id": None,
        "send_time": "21:00",
        "current_poll_index": 0,
    },
    "checklists": [
        {
            "id": 1,
            "title": "План тренировки 💪",
            "items": ["Отжимания 3x20 🤸", "Подтягивания 3x10 🏋️", "Планка 3x60сек ⏱", "Пресс 3x30 🔥"],
            "active": True,
        }
    ],
    "checklist_settings": {
        "send_time": "08:00",
        "current_index": 0,
    },
    "daily_state": {},
}

scheduler: Optional[AsyncIOScheduler] = None
router = Router()


# ---------- Storage ----------

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # добавить новые ключи если их нет (для старых data.json)
        data.setdefault("checklists", DEFAULT_DATA["checklists"])
        data.setdefault("checklist_settings", DEFAULT_DATA["checklist_settings"])
        data.setdefault("daily_state", {})
        return data
    data = {k: v for k, v in DEFAULT_DATA.items()}
    save_data(data)
    return data


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def today() -> str:
    return datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")


# ---------- FSM ----------

class AddPoll(StatesGroup):
    question = State()
    options = State()


class SetTime(StatesGroup):
    time = State()


class AddChecklist(StatesGroup):
    title = State()
    items = State()


class SetCheckTime(StatesGroup):
    time = State()


# ---------- Helpers ----------

def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


async def guard(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return False
    return True


def build_checklist_keyboard(checklist_id: int, items: list, checks: dict) -> InlineKeyboardMarkup:
    buttons = []
    for i, item in enumerate(items):
        done_by = checks.get(item, [])
        count = len(done_by)
        label = f"✅ {item}  ({count})" if count else f"☐ {item}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"chk:{checklist_id}:{i}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- Poll sender ----------

async def send_daily_poll(bot: Bot):
    data = load_data()
    group_id = data["settings"].get("group_id")
    polls = [p for p in data["polls"] if p.get("active", True)]

    if not group_id:
        logger.warning("Group ID не задан — опрос не отправлен.")
        return
    if not polls:
        logger.warning("Нет активных опросов.")
        return

    idx = data["settings"].get("current_poll_index", 0) % len(polls)
    poll = polls[idx]

    await bot.send_poll(
        chat_id=group_id,
        question=poll["question"],
        options=poll["options"],
        is_anonymous=False,
    )
    logger.info(f"Опрос #{poll['id']} отправлен в группу {group_id}")

    data["settings"]["current_poll_index"] = (idx + 1) % len(polls)
    save_data(data)


# ---------- Checklist sender ----------

async def send_daily_checklist(bot: Bot):
    data = load_data()
    group_id = data["settings"].get("group_id")
    checklists = [c for c in data["checklists"] if c.get("active", True)]

    if not group_id:
        logger.warning("Group ID не задан — чеклист не отправлен.")
        return
    if not checklists:
        logger.warning("Нет активных чеклистов.")
        return

    idx = data["checklist_settings"].get("current_index", 0) % len(checklists)
    checklist = checklists[idx]
    date = today()

    data["daily_state"][date] = {
        "checklist_id": checklist["id"],
        "checks": {},
    }

    keyboard = build_checklist_keyboard(checklist["id"], checklist["items"], {})
    msg = await bot.send_message(
        chat_id=group_id,
        text=f"📋 <b>{checklist['title']}</b>\n\nОтмечай что выполнил 👇",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    data["daily_state"][date]["message_id"] = msg.message_id
    data["checklist_settings"]["current_index"] = (idx + 1) % len(checklists)
    save_data(data)
    logger.info(f"Чеклист #{checklist['id']} отправлен в группу {group_id}")


# ---------- Schedulers ----------

def reschedule_poll(bot: Bot, hour: int, minute: int):
    if scheduler:
        tz = pytz.timezone(TIMEZONE)
        scheduler.add_job(
            send_daily_poll,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[bot], id="daily_poll", replace_existing=True,
        )


def reschedule_checklist(bot: Bot, hour: int, minute: int):
    if scheduler:
        tz = pytz.timezone(TIMEZONE)
        scheduler.add_job(
            send_daily_checklist,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[bot], id="daily_checklist", replace_existing=True,
        )


# ---------- Callback: чеклист ----------

@router.callback_query(F.data.startswith("chk:"))
async def handle_check(callback: CallbackQuery):
    _, checklist_id_str, item_idx_str = callback.data.split(":")
    checklist_id = int(checklist_id_str)
    item_idx = int(item_idx_str)
    user_id = callback.from_user.id
    date = today()

    data = load_data()

    checklist = next((c for c in data["checklists"] if c["id"] == checklist_id), None)
    if not checklist:
        return await callback.answer("Чеклист не найден.")

    item = checklist["items"][item_idx]

    day = data["daily_state"].setdefault(date, {"checklist_id": checklist_id, "checks": {}})
    checks = day.setdefault("checks", {})
    done_by = checks.setdefault(item, [])

    if user_id in done_by:
        done_by.remove(user_id)
        await callback.answer("Снято ✗")
    else:
        done_by.append(user_id)
        await callback.answer("Отмечено ✅")

    save_data(data)

    keyboard = build_checklist_keyboard(checklist_id, checklist["items"], checks)
    await callback.message.edit_reply_markup(reply_markup=keyboard)


# ---------- /start ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для ежедневных опросов и чеклистов.\n\n"
        "📊 <b>Опросы:</b>\n"
        "/addpoll — добавить опрос\n"
        "/listpolls — список опросов\n"
        "/delpoll &lt;id&gt; — удалить опрос\n"
        "/settime — время рассылки опросов\n"
        "/sendnow — отправить опрос сейчас\n\n"
        "✅ <b>Чеклисты:</b>\n"
        "/addchecklist — добавить чеклист\n"
        "/listchecklists — список чеклистов\n"
        "/delchecklist &lt;id&gt; — удалить чеклист\n"
        "/setchecktime — время рассылки чеклистов\n"
        "/checknow — отправить чеклист сейчас\n\n"
        "⚙️ <b>Настройки:</b>\n"
        "/setgroup — установить эту группу для рассылки\n"
        "/status — текущие настройки",
        parse_mode="HTML",
    )


# ---------- /status ----------

@router.message(Command("status"))
async def cmd_status(message: Message):
    if not await guard(message):
        return

    data = load_data()
    s = data["settings"]
    cs = data["checklist_settings"]
    polls = data["polls"]
    checklists = data["checklists"]
    active_polls = [p for p in polls if p.get("active", True)]
    active_cl = [c for c in checklists if c.get("active", True)]
    group_id = s.get("group_id") or "не задана"

    await message.answer(
        f"📊 <b>Статус бота:</b>\n\n"
        f"👥 Группа: <code>{group_id}</code>\n\n"
        f"📋 Опросы: {len(active_polls)} активных\n"
        f"⏰ Время опросов: <b>{s.get('send_time', '21:00')}</b>\n\n"
        f"✅ Чеклисты: {len(active_cl)} активных\n"
        f"⏰ Время чеклистов: <b>{cs.get('send_time', '08:00')}</b>\n"
        f"🌍 Часовой пояс: {TIMEZONE}",
        parse_mode="HTML",
    )


# ---------- /setgroup ----------

@router.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    if not await guard(message):
        return

    data = load_data()
    data["settings"]["group_id"] = message.chat.id
    save_data(data)

    await message.answer(
        f"✅ Группа установлена!\n"
        f"ID: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


# ---------- /settime ----------

@router.message(Command("settime"))
async def cmd_settime(message: Message, state: FSMContext):
    if not await guard(message):
        return

    data = load_data()
    current = data["settings"].get("send_time", "21:00")
    await state.set_state(SetTime.time)
    await message.answer(
        f"⏰ Текущее время опросов: <b>{current}</b>\n\n"
        f"Введи новое время (ЧЧ:ММ), например <code>21:00</code>",
        parse_mode="HTML",
    )


@router.message(SetTime.time)
async def process_settime(message: Message, state: FSMContext):
    try:
        hour, minute = map(int, message.text.strip().split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        return await message.answer("❌ Формат: ЧЧ:ММ, например <code>21:00</code>", parse_mode="HTML")

    data = load_data()
    data["settings"]["send_time"] = f"{hour:02d}:{minute:02d}"
    save_data(data)
    reschedule_poll(message.bot, hour, minute)

    await state.clear()
    await message.answer(f"✅ Время опросов: <b>{hour:02d}:{minute:02d}</b> ({TIMEZONE})", parse_mode="HTML")


# ---------- /setchecktime ----------

@router.message(Command("setchecktime"))
async def cmd_setchecktime(message: Message, state: FSMContext):
    if not await guard(message):
        return

    data = load_data()
    current = data["checklist_settings"].get("send_time", "08:00")
    await state.set_state(SetCheckTime.time)
    await message.answer(
        f"⏰ Текущее время чеклистов: <b>{current}</b>\n\n"
        f"Введи новое время (ЧЧ:ММ), например <code>08:00</code>",
        parse_mode="HTML",
    )


@router.message(SetCheckTime.time)
async def process_setchecktime(message: Message, state: FSMContext):
    try:
        hour, minute = map(int, message.text.strip().split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        return await message.answer("❌ Формат: ЧЧ:ММ, например <code>08:00</code>", parse_mode="HTML")

    data = load_data()
    data["checklist_settings"]["send_time"] = f"{hour:02d}:{minute:02d}"
    save_data(data)
    reschedule_checklist(message.bot, hour, minute)

    await state.clear()
    await message.answer(f"✅ Время чеклистов: <b>{hour:02d}:{minute:02d}</b> ({TIMEZONE})", parse_mode="HTML")


# ---------- Опросы ----------

@router.message(Command("addpoll"))
async def cmd_addpoll(message: Message, state: FSMContext):
    if not await guard(message):
        return
    await state.set_state(AddPoll.question)
    await message.answer("📝 Введи <b>вопрос</b> для опроса:", parse_mode="HTML")


@router.message(AddPoll.question)
async def process_poll_question(message: Message, state: FSMContext):
    if len(message.text) > 300:
        return await message.answer("❌ Вопрос слишком длинный (макс. 300 символов).")
    await state.update_data(question=message.text.strip())
    await state.set_state(AddPoll.options)
    await message.answer(
        "📋 Введи <b>варианты ответов</b> — каждый с новой строки.\n\n"
        "Пример:\n<code>Отлично! 🔥\nХорошо 👍\nСредне 😐\nПлохо 😔</code>",
        parse_mode="HTML",
    )


@router.message(AddPoll.options)
async def process_poll_options(message: Message, state: FSMContext):
    options = [o.strip() for o in message.text.strip().split("\n") if o.strip()]
    if len(options) < 2:
        return await message.answer("❌ Нужно минимум 2 варианта!")
    if len(options) > 10:
        return await message.answer("❌ Максимум 10 вариантов!")
    if any(len(o) > 100 for o in options):
        return await message.answer("❌ Вариант слишком длинный (макс. 100 символов).")

    fsm_data = await state.get_data()
    data = load_data()
    poll_id = max((p["id"] for p in data["polls"]), default=0) + 1
    data["polls"].append({"id": poll_id, "question": fsm_data["question"], "options": options, "active": True})
    save_data(data)
    await state.clear()

    opts_text = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(options))
    await message.answer(
        f"✅ Опрос #{poll_id} добавлен!\n\n❓ <b>{fsm_data['question']}</b>\n\n{opts_text}",
        parse_mode="HTML",
    )


@router.message(Command("listpolls"))
async def cmd_listpolls(message: Message):
    if not await guard(message):
        return
    data = load_data()
    if not data["polls"]:
        return await message.answer("📭 Опросов нет. Добавь через /addpoll")

    lines = ["📋 <b>Список опросов:</b>\n"]
    for p in data["polls"]:
        status = "✅" if p.get("active", True) else "⏸"
        opts = "\n".join(f"    • {o}" for o in p["options"])
        lines.append(f"{status} <b>#{p['id']}</b>: {p['question']}\n{opts}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("delpoll"))
async def cmd_delpoll(message: Message):
    if not await guard(message):
        return
    args = message.text.split()
    if len(args) < 2:
        data = load_data()
        if not data["polls"]:
            return await message.answer("📭 Опросов нет.")
        lst = "\n".join(f"  #{p['id']}: {p['question']}" for p in data["polls"])
        return await message.answer(f"Укажи ID:\n<code>/delpoll &lt;id&gt;</code>\n\n{lst}", parse_mode="HTML")

    try:
        poll_id = int(args[1])
    except ValueError:
        return await message.answer("❌ Пример: <code>/delpoll 1</code>", parse_mode="HTML")

    data = load_data()
    before = len(data["polls"])
    data["polls"] = [p for p in data["polls"] if p["id"] != poll_id]
    if len(data["polls"]) == before:
        return await message.answer(f"❌ Опрос #{poll_id} не найден.")

    active = [p for p in data["polls"] if p.get("active", True)]
    data["settings"]["current_poll_index"] = (
        data["settings"].get("current_poll_index", 0) % len(active) if active else 0
    )
    save_data(data)
    await message.answer(f"🗑 Опрос #{poll_id} удалён.")


@router.message(Command("sendnow"))
async def cmd_sendnow(message: Message):
    if not await guard(message):
        return
    try:
        await send_daily_poll(message.bot)
        await message.answer("✅ Опрос отправлен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------- Чеклисты ----------

@router.message(Command("addchecklist"))
async def cmd_addchecklist(message: Message, state: FSMContext):
    if not await guard(message):
        return
    await state.set_state(AddChecklist.title)
    await message.answer("📝 Введи <b>название</b> чеклиста:\n\nНапример: <code>Тренировка на сегодня 💪</code>", parse_mode="HTML")


@router.message(AddChecklist.title)
async def process_cl_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddChecklist.items)
    await message.answer(
        "📋 Введи <b>пункты</b> чеклиста — каждый с новой строки.\n\n"
        "Пример:\n"
        "<code>Отжимания 3x20 🤸\n"
        "Подтягивания 3x10 🏋️\n"
        "Планка 60сек ⏱\n"
        "Пресс 3x30 🔥</code>",
        parse_mode="HTML",
    )


@router.message(AddChecklist.items)
async def process_cl_items(message: Message, state: FSMContext):
    items = [i.strip() for i in message.text.strip().split("\n") if i.strip()]
    if len(items) < 1:
        return await message.answer("❌ Нужен хотя бы 1 пункт!")
    if len(items) > 20:
        return await message.answer("❌ Максимум 20 пунктов!")

    fsm_data = await state.get_data()
    data = load_data()
    cl_id = max((c["id"] for c in data["checklists"]), default=0) + 1
    data["checklists"].append({"id": cl_id, "title": fsm_data["title"], "items": items, "active": True})
    save_data(data)
    await state.clear()

    items_text = "\n".join(f"  ☐ {it}" for it in items)
    await message.answer(
        f"✅ Чеклист #{cl_id} добавлен!\n\n"
        f"📋 <b>{fsm_data['title']}</b>\n{items_text}",
        parse_mode="HTML",
    )


@router.message(Command("listchecklists"))
async def cmd_listchecklists(message: Message):
    if not await guard(message):
        return
    data = load_data()
    if not data["checklists"]:
        return await message.answer("📭 Чеклистов нет. Добавь через /addchecklist")

    lines = ["✅ <b>Список чеклистов:</b>\n"]
    for c in data["checklists"]:
        status = "✅" if c.get("active", True) else "⏸"
        items_text = "\n".join(f"    ☐ {it}" for it in c["items"])
        lines.append(f"{status} <b>#{c['id']}</b>: {c['title']}\n{items_text}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("delchecklist"))
async def cmd_delchecklist(message: Message):
    if not await guard(message):
        return
    args = message.text.split()
    if len(args) < 2:
        data = load_data()
        if not data["checklists"]:
            return await message.answer("📭 Чеклистов нет.")
        lst = "\n".join(f"  #{c['id']}: {c['title']}" for c in data["checklists"])
        return await message.answer(f"Укажи ID:\n<code>/delchecklist &lt;id&gt;</code>\n\n{lst}", parse_mode="HTML")

    try:
        cl_id = int(args[1])
    except ValueError:
        return await message.answer("❌ Пример: <code>/delchecklist 1</code>", parse_mode="HTML")

    data = load_data()
    before = len(data["checklists"])
    data["checklists"] = [c for c in data["checklists"] if c["id"] != cl_id]
    if len(data["checklists"]) == before:
        return await message.answer(f"❌ Чеклист #{cl_id} не найден.")

    active = [c for c in data["checklists"] if c.get("active", True)]
    data["checklist_settings"]["current_index"] = (
        data["checklist_settings"].get("current_index", 0) % len(active) if active else 0
    )
    save_data(data)
    await message.answer(f"🗑 Чеклист #{cl_id} удалён.")


@router.message(Command("checknow"))
async def cmd_checknow(message: Message):
    if not await guard(message):
        return
    try:
        await send_daily_checklist(message.bot)
        await message.answer("✅ Чеклист отправлен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------- Entry point ----------

async def main():
    global scheduler

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Добавь его в файл .env")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    data = load_data()
    tz = pytz.timezone(TIMEZONE)

    poll_time = data["settings"].get("send_time", "21:00")
    ph, pm = map(int, poll_time.split(":"))

    check_time = data["checklist_settings"].get("send_time", "08:00")
    ch, cm = map(int, check_time.split(":"))

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(send_daily_poll, CronTrigger(hour=ph, minute=pm, timezone=tz),
                      args=[bot], id="daily_poll", replace_existing=True)
    scheduler.add_job(send_daily_checklist, CronTrigger(hour=ch, minute=cm, timezone=tz),
                      args=[bot], id="daily_checklist", replace_existing=True)
    scheduler.start()

    logger.info(f"Бот запущен. Чеклист в {check_time}, опрос в {poll_time} ({TIMEZONE})")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
