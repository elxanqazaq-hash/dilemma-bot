import json
import random
import re

from google import genai
from google.genai import types

from config import GEMINI_KEY, TEXT_MODEL, IMAGE_MODEL, IMAGE_STYLE_PROMPT
import storage

client = genai.Client(api_key=GEMINI_KEY)


def pick_category():
    data = storage.load_categories()
    mode = data.get("mode", "weighted")
    cats = {k: v for k, v in data["categories"].items() if v.get("enabled", True)}

    if not cats:
        raise ValueError("Нет ни одной включённой категории.")

    if mode == "random":
        return random.choice(list(cats.keys()))

    if mode == "balanced":
        history = storage.load_history()["pairs"]
        last_used = {}
        for i, p in enumerate(history):
            last_used[p["category"]] = i
        never_used = [c for c in cats if c not in last_used]
        if never_used:
            return random.choice(never_used)
        return min(cats.keys(), key=lambda c: last_used.get(c, -1))

    names = list(cats.keys())
    weights = [max(cats[c].get("weight", 1), 0.01) for c in names]
    return random.choices(names, weights=weights, k=1)[0]


def _extract_json(text):
    cleaned = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"Не удалось найти JSON в ответе модели: {text[:200]}")
    return json.loads(match.group(0))


def generate_dilemma(category_id, category_label):
    recent_pairs = storage.get_recent_pairs_text(limit=60)
    recent_text = "\n".join(f"- {p}" for p in recent_pairs) if recent_pairs else "(пока пусто)"

    prompt = f"""Ты придумываешь дилеммы для Telegram-канала в формате "выбери одно из двух".
Категория: {category_label}

Придумай ОДНУ новую дилемму строго в рамках этой категории. Тон подбирай под категорию:
может быть смешным, абсурдным, серьёзным или философским — главное, чтобы было интересно
и вызывало желание проголосовать.

ВАЖНО: question должен быть КОРОТКИМ — максимум 5-6 слов, без описания ситуации.
Примеры хороших вопросов: "Что выберешь?", "Кем бы стал?", "Куда отправишься?".
НЕ пиши длинные вопросы вида "Какую способность для управления судьбой ты выберешь?".

Уже использованные дилеммы (НЕ повторяй их и не делай слишком похожие):
{recent_text}

Ответь СТРОГО в формате JSON, без markdown-обёртки, без преамбулы:
{{
  "option_a": "короткое название варианта A (2-4 слова, на русском)",
  "option_b": "короткое название варианта B (2-4 слова, на русском)",
  "question": "короткий вопрос, максимум 5-6 слов"
}}"""

    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
    )
    data = _extract_json(response.text)
    data["category"] = category_id
    data["category_label"] = category_label
    return data


def generate_image(option_a, option_b):
    prompt = IMAGE_STYLE_PROMPT.format(option_a=option_a, option_b=option_b)

    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return part.inline_data.data

    raise RuntimeError("Gemini не вернул изображение в ответе")


def generate_full_post(category_id=None):
    data = storage.load_categories()
    if category_id is None:
        category_id = pick_category()
    category_label = data["categories"][category_id]["label"]

    dilemma = generate_dilemma(category_id, category_label)
    image_bytes = generate_image(dilemma["option_a"], dilemma["option_b"])

    return {
        "category": category_id,
        "category_label": category_label,
        "option_a": dilemma["option_a"],
        "option_b": dilemma["option_b"],
        "question": dilemma.get("question", "Что бы ты выбрал?"),
        "image_bytes": image_bytes,
    }
