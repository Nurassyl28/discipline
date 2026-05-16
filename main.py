import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import pytz
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
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
        {
            "id": 3,
            "question": "Как самочувствие после зала? 🏋️",
            "options": ["Энергия зашкаливает!", "Норм", "Немного устал", "Очень устал", "Не ходил"],
            "active": True,
        },
    ],
    "settings": {
        "group_id": None,
        "send_time": "09:00",
        "current_poll_index": 0,
    },
}

scheduler: Optional[AsyncIOScheduler] = None
router = Router()


# ---------- Storage ----------

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    data = dict(DEFAULT_DATA)
    save_data(data)
    return data


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- FSM ----------

class AddPoll(StatesGroup):
    question = State()
    options = State()


class SetTime(StatesGroup):
    time = State()


# ---------- Helpers ----------

def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True  # если ADMIN_IDS не задан — разрешить всем (для первоначальной настройки)
    return user_id in ADMIN_IDS


async def guard(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return False
    return True


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


def reschedule(bot: Bot, hour: int, minute: int):
    global scheduler
    if scheduler:
        tz = pytz.timezone(TIMEZONE)
        scheduler.add_job(
            send_daily_poll,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[bot],
            id="daily_poll",
            replace_existing=True,
        )


# ---------- Handlers ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для ежедневных опросов.\n\n"
        "📋 <b>Команды:</b>\n"
        "/addpoll — добавить новый опрос\n"
        "/listpolls — список всех опросов\n"
        "/delpoll &lt;id&gt; — удалить опрос\n"
        "/setgroup — установить эту группу для рассылки\n"
        "/settime — изменить время рассылки\n"
        "/sendnow — отправить опрос прямо сейчас\n"
        "/status — текущие настройки",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not await guard(message):
        return

    data = load_data()
    s = data["settings"]
    polls = data["polls"]
    active = [p for p in polls if p.get("active", True)]
    group_id = s.get("group_id") or "не задана"
    send_time = s.get("send_time", "09:00")
    next_idx = (s.get("current_poll_index", 0) % len(active)) + 1 if active else "—"

    await message.answer(
        f"📊 <b>Статус бота:</b>\n\n"
        f"👥 Группа: <code>{group_id}</code>\n"
        f"⏰ Время рассылки: <b>{send_time}</b> ({TIMEZONE})\n"
        f"📋 Опросов: {len(polls)} (активных: {len(active)})\n"
        f"🔄 Следующий: #{next_idx}",
        parse_mode="HTML",
    )


@router.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    if not await guard(message):
        return

    data = load_data()
    data["settings"]["group_id"] = message.chat.id
    save_data(data)

    await message.answer(
        f"✅ Группа установлена!\n"
        f"ID: <code>{message.chat.id}</code>\n"
        f"Опросы будут отправляться сюда.",
        parse_mode="HTML",
    )


@router.message(Command("settime"))
async def cmd_settime(message: Message, state: FSMContext):
    if not await guard(message):
        return

    data = load_data()
    current = data["settings"].get("send_time", "09:00")
    await state.set_state(SetTime.time)
    await message.answer(
        f"⏰ Текущее время рассылки: <b>{current}</b>\n\n"
        f"Введи новое время в формате ЧЧ:ММ\n"
        f"Например: <code>09:00</code> или <code>18:30</code>",
        parse_mode="HTML",
    )


@router.message(SetTime.time)
async def process_settime(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        hour, minute = map(int, time_str.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        return await message.answer(
            "❌ Неверный формат. Введи время как ЧЧ:ММ, например <code>09:00</code>",
            parse_mode="HTML",
        )

    data = load_data()
    data["settings"]["send_time"] = f"{hour:02d}:{minute:02d}"
    save_data(data)
    reschedule(message.bot, hour, minute)

    await state.clear()
    await message.answer(
        f"✅ Время рассылки изменено: <b>{hour:02d}:{minute:02d}</b> ({TIMEZONE})",
        parse_mode="HTML",
    )


@router.message(Command("addpoll"))
async def cmd_addpoll(message: Message, state: FSMContext):
    if not await guard(message):
        return

    await state.set_state(AddPoll.question)
    await message.answer("📝 Введи <b>вопрос</b> для опроса:", parse_mode="HTML")


@router.message(AddPoll.question)
async def process_question(message: Message, state: FSMContext):
    if len(message.text) > 300:
        return await message.answer("❌ Вопрос слишком длинный (макс. 300 символов).")

    await state.update_data(question=message.text.strip())
    await state.set_state(AddPoll.options)
    await message.answer(
        "📋 Введи <b>варианты ответов</b> — каждый с новой строки.\n\n"
        "Пример:\n"
        "<code>Отлично! 🔥\n"
        "Хорошо 👍\n"
        "Средне 😐\n"
        "Плохо 😔\n"
        "Не ходил ❌</code>",
        parse_mode="HTML",
    )


@router.message(AddPoll.options)
async def process_options(message: Message, state: FSMContext):
    options = [o.strip() for o in message.text.strip().split("\n") if o.strip()]

    if len(options) < 2:
        return await message.answer("❌ Нужно минимум 2 варианта ответа!")
    if len(options) > 10:
        return await message.answer("❌ Максимум 10 вариантов ответа!")
    if any(len(o) > 100 for o in options):
        return await message.answer("❌ Один из вариантов слишком длинный (макс. 100 символов).")

    fsm_data = await state.get_data()
    question = fsm_data["question"]

    data = load_data()
    poll_id = max((p["id"] for p in data["polls"]), default=0) + 1
    data["polls"].append({"id": poll_id, "question": question, "options": options, "active": True})
    save_data(data)
    await state.clear()

    opts_text = "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(options))
    await message.answer(
        f"✅ Опрос #{poll_id} добавлен!\n\n"
        f"❓ <b>{question}</b>\n\n"
        f"Варианты:\n{opts_text}",
        parse_mode="HTML",
    )


@router.message(Command("listpolls"))
async def cmd_listpolls(message: Message):
    if not await guard(message):
        return

    data = load_data()
    polls = data["polls"]

    if not polls:
        return await message.answer("📭 Опросов пока нет. Добавь через /addpoll")

    lines = ["📋 <b>Список опросов:</b>\n"]
    for poll in polls:
        status = "✅" if poll.get("active", True) else "⏸"
        opts = "\n".join(f"    • {o}" for o in poll["options"])
        lines.append(f"{status} <b>#{poll['id']}</b>: {poll['question']}\n{opts}\n")

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
        polls_list = "\n".join(f"  #{p['id']}: {p['question']}" for p in data["polls"])
        return await message.answer(
            f"Укажи ID опроса:\n<code>/delpoll &lt;id&gt;</code>\n\nОпросы:\n{polls_list}",
            parse_mode="HTML",
        )

    try:
        poll_id = int(args[1])
    except ValueError:
        return await message.answer(
            "❌ ID должен быть числом. Например: <code>/delpoll 1</code>", parse_mode="HTML"
        )

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
        await message.answer("✅ Опрос отправлен в группу!")
    except Exception as e:
        logger.error(f"Ошибка при отправке: {e}")
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
    send_time = data["settings"].get("send_time", "09:00")
    hour, minute = map(int, send_time.split(":"))
    tz = pytz.timezone(TIMEZONE)

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        send_daily_poll,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[bot],
        id="daily_poll",
        replace_existing=True,
    )
    scheduler.start()

    logger.info(f"Бот запущен. Рассылка каждый день в {send_time} ({TIMEZONE})")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
