"""
Дилемма-бот: автогенерация постов "выбери одно из двух" с картинками
в едином стиле, публикация в канал по расписанию из общей очереди.

Команды:
  /generate N          — сгенерировать N постов в очередь
  /slots 09:00,20:00    — задать свои слоты публикации (через запятую)
  /clear_queue          — удалить ВСЕ посты из очереди
  /clear_queue N        — удалить N самых старых постов из очереди
"""

import logging
import threading
from datetime import datetime, time as time_
from io import BytesIO

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    PollAnswerHandler, ContextTypes,
)

import config
import storage
import generator

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dilemmabot")

TZ = pytz.timezone(config.TIMEZONE)

_poll_index = {}
_publish_lock = threading.Lock()

MAX_BATCH_SIZE = 200


def _current_slots():
    settings = storage.load_settings()
    custom = settings.get("post_slots")
    if custom:
        return custom
    return config.POST_SLOTS


def _make_post(category_id=None):
    content = generator.generate_full_post(category_id=category_id)
    return {
        "category": content["category"],
        "category_label": content["category_label"],
        "option_a": content["option_a"],
        "option_b": content["option_b"],
        "question": content["question"],
        "image_bytes_hex": content["image_bytes"].hex(),
    }


async def generate_batch(context: ContextTypes.DEFAULT_TYPE, chat_id: int, count: int):
    count = max(1, min(count, MAX_BATCH_SIZE))
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ Генерирую {count} постов... это может занять несколько минут.",
    )

    made = 0
    failed = 0
    new_posts = []

    for i in range(count):
        try:
            post = _make_post()
            new_posts.append(post)
            storage.add_to_history(post["option_a"], post["option_b"], post["category"])
            made += 1
        except Exception as e:
            failed += 1
            log.exception(f"Ошибка генерации поста {i+1}/{count}: {e}")

        if (i + 1) % 5 == 0 or (i + 1) == count:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=f"⏳ Сгенерировано {i+1}/{count} (ошибок: {failed})...",
                )
            except Exception:
                pass

    if new_posts:
        storage.add_posts_to_queue(new_posts)

    queued_total = storage.count_queued()
    slots = _current_slots()
    days = queued_total // len(slots) if slots else 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Готово! Добавлено в очередь: {made}, ошибок: {failed}\n"
            f"Всего в очереди сейчас: {queued_total} постов (хватит на ~{days} дней "
            f"при {len(slots)} постах в день)"
        ),
    )


async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /generate N\nНапример: /generate 30 — сгенерирует 30 постов в очередь."
        )
        return

    count = int(args[0])
    await generate_batch(context, update.effective_chat.id, count)


async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return

    args = context.args
    if not args:
        current = _current_slots()
        await update.message.reply_text(
            f"Текущие слоты публикации: {', '.join(current)}\n\n"
            f"Чтобы изменить: /slots 09:00,13:00,20:00\n"
            f"Чтобы сбросить на стандартные ({', '.join(config.POST_SLOTS)}): /slots reset"
        )
        return

    raw = " ".join(args)

    if raw.strip().lower() == "reset":
        settings = storage.load_settings()
        settings["post_slots"] = None
        storage.save_settings(settings)
        await update.message.reply_text(f"Слоты сброшены на стандартные: {', '.join(config.POST_SLOTS)}")
        await _reload_schedule(context.application)
        return

    candidates = [s.strip() for s in raw.split(",") if s.strip()]
    valid = []
    for s in candidates:
        try:
            h, m = s.split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
            else:
                raise ValueError
        except Exception:
            await update.message.reply_text(f"⚠️ Неверный формат времени: '{s}'. Используй ЧЧ:ММ, например 09:00.")
            return

    if not valid:
        await update.message.reply_text("Не удалось распознать ни одного времени.")
        return

    settings = storage.load_settings()
    settings["post_slots"] = sorted(set(valid))
    storage.save_settings(settings)
    await update.message.reply_text(f"✅ Новые слоты публикации: {', '.join(settings['post_slots'])}")
    await _reload_schedule(context.application)


async def _reload_schedule(application: Application):
    jq = application.job_queue
    for job in jq.jobs():
        if job.name and job.name.startswith("publish_"):
            job.schedule_removal()

    for slot in _current_slots():
        h, m = map(int, slot.split(":"))
        jq.run_daily(
            _make_publish_job(slot),
            time=time_(hour=h, minute=m, tzinfo=TZ),
            name=f"publish_{slot}",
        )
    log.info(f"Расписание обновлено: {_current_slots()}")


async def cmd_clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return

    args = context.args
    if args and args[0].isdigit():
        n = int(args[0])
        removed = storage.clear_queue(count=n)
        await update.message.reply_text(f"🗑 Удалено {removed} постов из очереди (самые старые).")
    else:
        removed = storage.clear_queue(count=None)
        await update.message.reply_text(f"🗑 Очередь полностью очищена. Удалено постов: {removed}.")


async def _publish_post(context: ContextTypes.DEFAULT_TYPE, post: dict, source: str):
    try:
        image_bytes = bytes.fromhex(post["image_bytes_hex"])
        caption = f"#{post['category']}"

        await context.bot.send_photo(
            chat_id=config.CHANNEL,
            photo=BytesIO(image_bytes),
            caption=caption,
        )
        poll_message = await context.bot.send_poll(
            chat_id=config.CHANNEL,
            question=post["question"],
            options=[post["option_a"], post["option_b"]],
            is_anonymous=True,
        )

        stat_entry = {
            "poll_id": poll_message.poll.id,
            "category": post["category"],
            "option_a": post["option_a"],
            "option_b": post["option_b"],
            "option_a_votes": 0,
            "option_b_votes": 0,
            "posted_at": datetime.now(TZ).isoformat(),
        }
        storage.add_stat_entry(stat_entry)
        _poll_index[poll_message.poll.id] = stat_entry

        log.info(f"[{source}] Опубликован пост: {post['option_a']} vs {post['option_b']}")
        return True

    except Exception as e:
        log.exception(f"[{source}] Ошибка публикации поста id={post.get('id')}: {e}")
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text=f"🔴 Ошибка публикации поста: {e}",
        )
        return False


async def publish_from_schedule(context: ContextTypes.DEFAULT_TYPE, slot_time: str):
    settings = storage.load_settings()
    if settings.get("paused"):
        log.info(f"Бот на паузе — пропускаю публикацию слота {slot_time}")
        return

    with _publish_lock:
        post = storage.pop_next_queued_post()

    if post is None:
        log.warning(f"Очередь пуста на момент слота {slot_time}")
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text=(
                f"⚠️ Слот {slot_time}: очередь постов пуста, публиковать нечего.\n"
                f"Сгенерируй новую пачку: /generate 30"
            ),
        )
        return

    await _publish_post(context, post, source=f"slot:{slot_time}")


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id

    stats = storage.load_stats()
    entry = next((p for p in stats["posts"] if p["poll_id"] == poll_id), None)
    if entry is None:
        return

    if answer.option_ids:
        chosen = answer.option_ids[0]
        if chosen == 0:
            entry["option_a_votes"] += 1
        else:
            entry["option_b_votes"] += 1
        storage.update_stat_votes(poll_id, entry["option_a_votes"], entry["option_b_votes"])


def _main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Опубликовать сейчас", callback_data="menu:publishnow")],
        [InlineKeyboardButton("📦 Статус очереди", callback_data="menu:queue")],
        [InlineKeyboardButton("📊 Статистика по категориям", callback_data="menu:stats")],
        [InlineKeyboardButton("⚙️ Категории", callback_data="menu:categories")],
        [InlineKeyboardButton("⏸️ Пауза/Возобновить", callback_data="menu:pause")],
    ])


HELP_TEXT = (
    "Меню дилемма-бота:\n\n"
    "/generate N — сгенерировать N постов в очередь\n"
    "/slots 09:00,20:00 — задать своё расписание публикации\n"
    "/clear_queue — очистить всю очередь\n"
    "/clear_queue N — удалить N старых постов из очереди"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=_main_menu())


async def _safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "not modified" not in str(e).lower():
            raise


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]

    if action == "backmain":
        await _safe_edit(query, HELP_TEXT, reply_markup=_main_menu())
    elif action == "stats":
        await _show_stats(query)
    elif action == "categories":
        await _show_categories(query)
    elif action == "togglecat":
        cat_id = parts[2]
        data = storage.load_categories()
        if cat_id in data["categories"]:
            data["categories"][cat_id]["enabled"] = not data["categories"][cat_id].get("enabled", True)
            storage.save_categories(data)
        await _show_categories(query)
    elif action == "queue":
        await _show_queue_status(query)
    elif action == "pause":
        settings = storage.load_settings()
        settings["paused"] = not settings.get("paused", False)
        storage.save_settings(settings)
        state = "⏸️ На паузе" if settings["paused"] else "▶️ Активен"
        await _safe_edit(query, f"Статус бота: {state}", reply_markup=_main_menu())
    elif action == "publishnow":
        with _publish_lock:
            post = storage.pop_next_queued_post()

        if post is None:
            await context.bot.send_message(
                chat_id=config.OWNER_ID,
                text="⚠️ Очередь пуста. Сгенерируй пачку: /generate 30",
            )
        else:
            ok = await _publish_post(context, post, source="manual")
            if ok:
                await context.bot.send_message(
                    chat_id=config.OWNER_ID,
                    text=f"✅ Опубликовано: {post['option_a']} vs {post['option_b']}",
                    reply_markup=_main_menu(),
                )


async def _show_queue_status(query):
    queued = storage.count_queued()
    slots = _current_slots()
    days_left = queued // len(slots) if slots else 0
    text = (
        f"📦 В очереди: {queued} постов\n"
        f"При {len(slots)} постах в день хватит примерно на {days_left} дней.\n\n"
        f"Слоты публикации: {', '.join(slots)} ({config.TIMEZONE})\n\n"
        f"Добавить ещё: /generate N\n"
        f"Изменить расписание: /slots ЧЧ:ММ,ЧЧ:ММ\n"
        f"Очистить очередь: /clear_queue [N]"
    )
    await _safe_edit(query, text, reply_markup=_main_menu())


async def _show_stats(query):
    stats = storage.load_stats()
    by_category = {}
    for p in stats["posts"]:
        cat = p["category"]
        total = p["option_a_votes"] + p["option_b_votes"]
        by_category.setdefault(cat, {"posts": 0, "votes": 0})
        by_category[cat]["posts"] += 1
        by_category[cat]["votes"] += total

    if not by_category:
        await _safe_edit(query, "Пока нет статистики — ни один пост ещё не набрал голосов.", reply_markup=_main_menu())
        return

    categories = storage.load_categories()["categories"]
    lines = ["📊 Статистика по категориям (постов / среднее голосов):\n"]
    ranked = sorted(by_category.items(), key=lambda kv: kv[1]["votes"] / kv[1]["posts"], reverse=True)
    for cat_id, d in ranked:
        label = categories.get(cat_id, {}).get("label", cat_id)
        avg = d["votes"] / d["posts"]
        lines.append(f"{label}: {d['posts']} постов, в среднем {avg:.1f} голосов")

    await _safe_edit(query, "\n".join(lines), reply_markup=_main_menu())


def _categories_keyboard(data, queued_counts):
    rows = []
    for cat_id, c in data["categories"].items():
        mark = "✅" if c.get("enabled", True) else "⬜️"
        n = queued_counts.get(cat_id, 0)
        label = f"{mark} {c['label']} ({n} в очереди)"
        rows.append([InlineKeyboardButton(label, callback_data=f"menu:togglecat:{cat_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:backmain")])
    return InlineKeyboardMarkup(rows)


async def _show_categories(query):
    data = storage.load_categories()
    queued_counts = storage.count_queued_by_category()
    text = f"⚙️ Категории (режим выбора: {data['mode']}):\n\nЖми, чтобы включить/выключить категорию."
    await _safe_edit(query, text, reply_markup=_categories_keyboard(data, queued_counts))


def _make_publish_job(slot_time):
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await publish_from_schedule(context, slot_time)
    return _job


def setup_schedule(application: Application):
    jq = application.job_queue
    for slot in _current_slots():
        h, m = map(int, slot.split(":"))
        jq.run_daily(
            _make_publish_job(slot),
            time=time_(hour=h, minute=m, tzinfo=TZ),
            name=f"publish_{slot}",
        )


def main():
    if not config.BOT_TOKEN or not config.CHANNEL or not config.OWNER_ID or not config.GEMINI_KEY:
        raise RuntimeError(
            "Не заполнен bot.env — нужны BOT_TOKEN, CHANNEL, OWNER_ID, GEMINI_KEY"
        )

    application = Application.builder().token(config.BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start))
    application.add_handler(CommandHandler("generate", cmd_generate))
    application.add_handler(CommandHandler("slots", cmd_slots))
    application.add_handler(CommandHandler("clear_queue", cmd_clear_queue))
    application.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    application.add_handler(PollAnswerHandler(on_poll_answer))

    setup_schedule(application)

    log.info("Дилемма-бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
