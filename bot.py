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


def _today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _new_daily_posts_for_today():
    return {"date": _today_str(), "posts": []}


def _make_post_draft(slot_time, category_id=None):
    content = generator.generate_full_post(category_id=category_id)
    return {
        "slot_time": slot_time,
        "status": "pending",
        "regeneration_attempts": 0,
        "category": content["category"],
        "category_label": content["category_label"],
        "option_a": content["option_a"],
        "option_b": content["option_b"],
        "question": content["question"],
        "image_bytes_hex": content["image_bytes"].hex(),
    }


async def generate_daily_posts(context: ContextTypes.DEFAULT_TYPE):
    settings = storage.load_settings()
    if settings.get("paused"):
        log.info("Бот на паузе — пропускаю генерацию постов дня")
        return

    daily = _new_daily_posts_for_today()
    for slot in config.POST_SLOTS:
        try:
            draft = _make_post_draft(slot)
            daily["posts"].append(draft)
            storage.add_to_history(draft["option_a"], draft["option_b"], draft["category"])
        except Exception as e:
            log.exception(f"Ошибка генерации поста на слот {slot}: {e}")

    storage.save_daily_posts(daily)

    if settings.get("auto_post_no_moderation"):
        for post in daily["posts"]:
            post["status"] = "approved"
        storage.save_daily_posts(daily)
        log.info("Автопостинг без модерации — черновики одобрены автоматически")
        return

    for i, post in enumerate(daily["posts"]):
        await _send_draft_for_review(context, i, post)


async def _send_draft_for_review(context, index, post):
    caption = (
        f"🗂 Черновик на {post['slot_time']}\n"
        f"Категория: {post['category_label']}\n\n"
        f"❓ {post['question']}\n"
        f"🅰️ {post['option_a']}\n"
        f"🆚\n"
        f"🅱️ {post['option_b']}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{index}"),
        InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen:{index}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{index}"),
    ]])
    image_bytes = bytes.fromhex(post["image_bytes_hex"])
    await context.bot.send_photo(
        chat_id=config.OWNER_ID,
        photo=BytesIO(image_bytes),
        caption=caption,
        reply_markup=keyboard,
    )


async def on_moderation_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, index_str = query.data.split(":")
    index = int(index_str)

    daily = storage.load_daily_posts()
    if daily.get("date") != _today_str() or index >= len(daily["posts"]):
        await query.edit_message_caption(caption="⚠️ Этот черновик устарел.")
        return

    post = daily["posts"][index]

    if action == "approve":
        post["status"] = "approved"
        storage.save_daily_posts(daily)
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ Одобрено, ждёт публикации.")

    elif action == "reject":
        post["status"] = "rejected"
        storage.save_daily_posts(daily)
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ Отклонено, генерирую замену...")
        await _regenerate_post(context, index)

    elif action == "regen":
        await query.edit_message_caption(caption=query.message.caption + "\n\n🔄 Перегенерирую...")
        await _regenerate_post(context, index)


async def _regenerate_post(context, index):
    daily = storage.load_daily_posts()
    post = daily["posts"][index]

    post["regeneration_attempts"] += 1
    if post["regeneration_attempts"] > config.MAX_REGENERATIONS_ON_REJECT:
        post["status"] = "rejected"
        storage.save_daily_posts(daily)
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text=f"⚠️ Слот {post['slot_time']} — превышен лимит перегенераций. Слот будет пропущен.",
        )
        return

    try:
        new_draft = _make_post_draft(post["slot_time"])
        new_draft["regeneration_attempts"] = post["regeneration_attempts"]
        daily["posts"][index] = new_draft
        storage.save_daily_posts(daily)
        storage.add_to_history(new_draft["option_a"], new_draft["option_b"], new_draft["category"])
        await _send_draft_for_review(context, index, new_draft)
    except Exception as e:
        log.exception(f"Ошибка перегенерации поста {index}: {e}")
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text=f"⚠️ Ошибка при перегенерации слота {post['slot_time']}: {e}",
        )


async def publish_slot(context: ContextTypes.DEFAULT_TYPE, slot_time: str):
    with _publish_lock:
        daily = storage.load_daily_posts()
        if daily.get("date") != _today_str():
            log.warning(f"Нет постов на сегодня для слота {slot_time}")
            return

        index = None
        for i, p in enumerate(daily["posts"]):
            if p["slot_time"] == slot_time:
                index = i
                break
        if index is None:
            return

        post = daily["posts"][index]

        if post["status"] == "published":
            return
        if post["status"] == "rejected":
            log.info(f"Слот {slot_time} отклонён — пропуск")
            return

        if post["status"] == "pending":
            post["status"] = "approved"

        post["status"] = "published"
        storage.save_daily_posts(daily)

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

        log.info(f"Опубликован пост слота {slot_time}: {post['option_a']} vs {post['option_b']}")

    except Exception as e:
        log.exception(f"Ошибка публикации слота {slot_time}: {e}")
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text=f"🔴 Ошибка публикации слота {slot_time}: {e}",
        )


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
    [InlineKeyboardButton("📊 Статистика по категориям", callback_data="menu:stats")],
    [InlineKeyboardButton("⚙️ Категории", callback_data="menu:categories")],
    [InlineKeyboardButton("⏸️ Пауза/Возобновить", callback_data="menu:pause")],
    [InlineKeyboardButton("🤖 Автопостинг без модерации: вкл/выкл", callback_data="menu:automod")],
    [InlineKeyboardButton("🚀 Сгенерировать посты дня сейчас", callback_data="menu:gennow")],
])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    await update.message.reply_text("Меню дилемма-бота:", reply_markup=MAIN_MENU)


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "stats":
        await _show_stats(query)
    elif action == "categories":
        await _show_categories(query)
    elif action == "pause":
        settings = storage.load_settings()
        settings["paused"] = not settings.get("paused", False)
        storage.save_settings(settings)
        state = "⏸️ На паузе" if settings["paused"] else "▶️ Активен"
        await query.edit_message_text(f"Статус бота: {state}", reply_markup=MAIN_MENU)
    elif action == "automod":
        settings = storage.load_settings()
        settings["auto_post_no_moderation"] = not settings.get("auto_post_no_moderation", False)
        storage.save_settings(settings)
        state = "включён" if settings["auto_post_no_moderation"] else "выключен"
        await query.edit_message_text(f"Автопостинг без модерации: {state}", reply_markup=MAIN_MENU)
    elif action == "gennow":
        await query.edit_message_text("Генерирую посты дня...", reply_markup=MAIN_MENU)
        await generate_daily_posts(context)


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
        await query.edit_message_text("Пока нет статистики.", reply_markup=MAIN_MENU)
        return

    categories = storage.load_categories()["categories"]
    lines = ["📊 Статистика по категориям:\n"]
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
    lines.append("\nИзменить веса: nano state/categories.json")
    await query.edit_message_text("\n".join(lines), reply_markup=MAIN_MENU)


def _make_publish_job(slot_time):
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await publish_slot(context, slot_time)
    return _job


async def _job_generate_wrapper(context: ContextTypes.DEFAULT_TYPE):
    await generate_daily_posts(context)


def setup_schedule(application: Application):
    jq = application.job_queue
    gen_h, gen_m = map(int, config.GENERATION_TIME.split(":"))
    jq.run_daily(
        _job_generate_wrapper,
        time=time_(hour=gen_h, minute=gen_m, tzinfo=TZ),
        name="generate_daily_posts",
    )
    for slot in config.POST_SLOTS:
        h, m = map(int, slot.split(":"))
        jq.run_daily(
            _make_publish_job(slot),
            time=time_(hour=h, minute=m, tzinfo=TZ),
            name=f"publish_{slot}",
        )


def main():
    if not config.BOT_TOKEN or not config.CHANNEL or not config.OWNER_ID or not config.GEMINI_KEY:
        raise RuntimeError("Не заполнен bot.env — нужны BOT_TOKEN, CHANNEL, OWNER_ID, GEMINI_KEY")

    application = Application.builder().token(config.BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start))
    application.add_handler(CallbackQueryHandler(on_moderation_button, pattern=r"^(approve|reject|regen):"))
    application.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    application.add_handler(PollAnswerHandler(on_poll_answer))

    setup_schedule(application)

    log.info("Дилемма-бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
