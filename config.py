import os
from dotenv import load_dotenv

load_dotenv("bot.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = os.getenv("CHANNEL")
_owner_id_raw = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(_owner_id_raw) if _owner_id_raw else 0
GEMINI_KEY = os.getenv("GEMINI_KEY")

TEXT_MODEL = "gemini-flash-latest"
IMAGE_MODEL = "gemini-3.1-flash-image-preview"

TIMEZONE = "Asia/Almaty"
GENERATION_TIME = "08:00"
POST_SLOTS = ["12:00", "20:00"]

MAX_REGENERATIONS_ON_REJECT = 3

IMAGE_STYLE_PROMPT = (
    "Flat vector illustration, split-screen composition: left side depicts "
    "'{option_a}', right side depicts '{option_b}', bold minimal 'VS' divider "
    "in the center, soft gradient background in complementary colors, clean "
    "modern flat design style, consistent color palette, no additional text "
    "or labels except the VS divider, high quality, square aspect ratio"
)

DEFAULT_CATEGORIES = {
    "food":        {"label": "🍔 Еда и вкусы",              "enabled": True, "weight": 1},
    "characters":  {"label": "⚔️ Персонажи/архетипы",        "enabled": True, "weight": 1},
    "situations":  {"label": "🏝️ Ситуации и стиль жизни",    "enabled": True, "weight": 1},
    "superpowers": {"label": "🦸 Суперспособности",           "enabled": True, "weight": 1},
    "fantasy":     {"label": "🐉 Фэнтези/мифология",          "enabled": True, "weight": 1},
    "scifi":       {"label": "🚀 Sci-fi и технологии",        "enabled": True, "weight": 1},
    "aesthetics":  {"label": "🏙️ Эстетика/сеттинги",         "enabled": True, "weight": 1},
    "absurd":      {"label": "🤪 Абсурд и нонсенс",           "enabled": True, "weight": 1},
    "philosophy":  {"label": "🧠 Философские дилеммы",        "enabled": True, "weight": 1},
    "daily_fails": {"label": "😅 Бытовые неприятности",       "enabled": True, "weight": 1},
    "money_life":  {"label": "💰 Деньги и образ жизни",       "enabled": True, "weight": 1},
    "gaming":      {"label": "🎮 Поп-культура/геймерское",    "enabled": True, "weight": 1},
    "fears":       {"label": "😱 Страхи/фобии (в шутку)",     "enabled": True, "weight": 1},
    "anime":       {"label": "🎌 Аниме-тематика",             "enabled": True, "weight": 1},
}

DEFAULT_CATEGORY_MODE = "weighted"
