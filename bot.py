"""
Дилемма-бот: автогенерация постов "выбери одно из двух" с картинками
в едином стиле, публикация в канал по расписанию из общей очереди.

Флоу:
  - Ты сам генерируешь пачку постов вперёд командой /generate N
    (например /generate 30 — сделает 30 постов и положит в очередь)
  - По расписанию (12:00 и 20:00 каждый день) бот берёт САМЫЙ СТАРЫЙ
    пост из очереди (FIFO) и публикует его
  - Модерации нет по умолчанию — посты публикуются автоматически.
    Если очередь пуста на момент слота — просто ничего не публикуется,
    придёт уведомление тебе в личку
  - Кнопка "Опубликовать сейчас" — публикует следующий пост из очереди
    немедленно, не дожидаясь слота

Запуск: nohup python bot.py > bot.log 2>&1 &
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

MAX_BATCH_SIZE = 200  # защита от случайного "сгенерируй 100000"


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
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Готово! Добавлено в очередь: {made}, ошибок: {failed}\n"
            f"Всего в очереди сейчас: {queued_total} постов "
            f"(хватит на ~{queued_total // len(config.POST_SLOTS)} дней при {len(config.POST_SLOTS)} постах в день)"
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


async def _publish_post(context: ContextTypes.DEFAULT_TYPE, post: dict, source: str):
    try:
        image_bytes = bytes.fromhex(post["image_bytes_hex"])
        caption = f"{post['question']}\n\n#{post['category']}"

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


MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📤 Опубликовать сейчас", callback_data="menu:publishnow")],
    [InlineKeyboardButton("📦 Статус очереди", callback_data="menu:queue")],
    [InlineKeyboardButton("📊 Статистика по категориям", callback_data="menu:stats")],
    [InlineKeyboardButton("⚙️ Категории", callback_data="menu:categories")],
    [InlineKeyboardButton("⏸️ Пауза/Возобновить", callback_data="menu:pause")],
])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    await update.message.reply_text(
        "Меню дилемма-бота:\n\nЧтобы сгенерировать пачку постов вперёд — "
        "команда /generate N (например /generate 30).",
        reply_markup=MAIN_MENU,
    )


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "stats":
        await _show_stats(query)
    elif action == "categories":
        await _show_categories(query)
    elif action == "queue":
        await _show_queue_status(query)
    elif action == "pause":
        settings = storage.load_settings()
        settings["paused"] = not settings.get("paused", False)
        storage.save_settings(settings)
        state = "⏸️ На паузе" if settings["paused"] else "▶️ Активен"
        await query.edit_message_text(f"Статус бота: {state}", reply_markup=MAIN_MENU)
    elif action == "publishnow":
        try:
            await query.edit_message_text("📤 Публикую следующий пост из очереди...", reply_markup=MAIN_MENU)
        except Exception:
            pass
        with _publish_lock:
            post = storage.pop_next_queued_post()
        if post is None:
            await context.bot.send_message(
                chat_id=config.OWNER_ID,
                text="⚠️ Очередь пуста. Сгенерируй пачку: /generate 30",
            )
        else:
            await _publish_post(context, post, source="manual")


async def _show_queue_status(query):
    queued = storage.count_queued()
    days_left = queued // len(config.POST_SLOTS) if config.POST_SLOTS else 0
    text = (
        f"📦 В очереди: {queued} постов\n"
        f"При {len(config.POST_SLOTS)} постах в день хватит примерно на {days_left} дней.\n\n"
        f"Слоты публикации: {', '.join(config.POST_SLOTS)} ({config.TIMEZONE})\n\n"
        f"Чтобы добавить ещё: /generate N"
    )
    await query.edit_message_text(text, reply_markup=MAIN_MENU)


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
        await query.edit_message_text("Пока нет статистики — ни один пост ещё не набрал голосов.", reply_markup=MAIN_MENU)
        return

    categories = storage.load_categories()["categories"]
    lines = ["📊 Статистика по категориям (постов / среднее голосов):\n"]
    ranked = sorted(by_category.items(), key=lambda kv: kv[1]["votes"] / kv[1]["posts"], reverse=True)
    for cat_id, d in ranked:
        label = categories.get(cat_id, {}).get("label", cat_id)
        avg = d["votes"] / d["posts"]
        lines.append(f"{label}: {d['posts']} постов, в среднем {avg:.1f} голосов")

    await query.edit_message_text("\n".join(lines), reply_markup=MAIN_MENU)


async def _show_categories(query):
    data = storage.load_categories()
    lines = [f"⚙️ Категории (режим: {data['mode']}):\n"]
    for cat_id, c in data["categories"].items():
        status = "✅" if c.get("enabled", True) else "🚫"
        lines.append(f"{status} {c['label']} — вес {c.get('weight', 1)}")
    lines.append(
        "\nДля изменения весов/режима отредактируй state/categories.json "
        "напрямую в Termux."
    )
    await query.edit_message_text("\n".join(lines), reply_markup=MAIN_MENU)


def _make_publish_job(slot_time):
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await publish_from_schedule(context, slot_time)
    return _job


def setup_schedule(application: Application):
    jq = application.job_queue
    for slot in config.POST_SLOTS:
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
    application.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    application.add_handler(PollAnswerHandler(on_poll_answer))

    setup_schedule(application)

    log.info("Дилемма-бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
