import json
import os
import threading
from datetime import datetime
from config import DEFAULT_CATEGORIES, DEFAULT_CATEGORY_MODE

STATE_DIR = "state"
HISTORY_DIR = "history"

CATEGORIES_FILE = os.path.join(STATE_DIR, "categories.json")
HISTORY_FILE = os.path.join(HISTORY_DIR, "used_pairs.json")
QUEUE_FILE = os.path.join(STATE_DIR, "queue.json")
STATS_FILE = os.path.join(STATE_DIR, "stats.json")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")

_lock = threading.Lock()


def _ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def load_categories():
    _ensure_dirs()
    with _lock:
        data = _read_json(CATEGORIES_FILE, None)
        if data is None:
            data = {
                "mode": DEFAULT_CATEGORY_MODE,
                "categories": {k: dict(v) for k, v in DEFAULT_CATEGORIES.items()},
            }
            _write_json(CATEGORIES_FILE, data)
        return data


def save_categories(data):
    with _lock:
        _write_json(CATEGORIES_FILE, data)


def load_history():
    _ensure_dirs()
    with _lock:
        return _read_json(HISTORY_FILE, {"pairs": []})


def add_to_history(option_a, option_b, category):
    with _lock:
        data = _read_json(HISTORY_FILE, {"pairs": []})
        data["pairs"].append({
            "option_a": option_a,
            "option_b": option_b,
            "category": category,
            "added_at": datetime.now().isoformat(),
        })
        data["pairs"] = data["pairs"][-500:]
        _write_json(HISTORY_FILE, data)


def get_recent_pairs_text(limit=60):
    data = load_history()
    recent = data["pairs"][-limit:]
    return [f"{p['option_a']} vs {p['option_b']}" for p in recent]


def load_queue():
    _ensure_dirs()
    with _lock:
        return _read_json(QUEUE_FILE, {"next_id": 1, "posts": []})


def save_queue(data):
    with _lock:
        _write_json(QUEUE_FILE, data)


def add_posts_to_queue(new_posts):
    with _lock:
        data = _read_json(QUEUE_FILE, {"next_id": 1, "posts": []})
        for p in new_posts:
            p["id"] = data["next_id"]
            p["status"] = "queued"
            p["created_at"] = datetime.now().isoformat()
            data["next_id"] += 1
            data["posts"].append(p)
        _write_json(QUEUE_FILE, data)
        return data


def pop_next_queued_post():
    with _lock:
        data = _read_json(QUEUE_FILE, {"next_id": 1, "posts": []})
        for p in data["posts"]:
            if p["status"] == "queued":
                p["status"] = "published"
                _write_json(QUEUE_FILE, data)
                return p
        return None


def get_post_by_id(post_id):
    data = load_queue()
    for p in data["posts"]:
        if p["id"] == post_id:
            return p
    return None


def mark_post_published(post_id):
    with _lock:
        data = _read_json(QUEUE_FILE, {"next_id": 1, "posts": []})
        for p in data["posts"]:
            if p["id"] == post_id:
                p["status"] = "published"
                break
        _write_json(QUEUE_FILE, data)


def remove_post_from_queue(post_id):
    with _lock:
        data = _read_json(QUEUE_FILE, {"next_id": 1, "posts": []})
        data["posts"] = [p for p in data["posts"] if p["id"] != post_id]
        _write_json(QUEUE_FILE, data)


def count_queued():
    data = load_queue()
    return sum(1 for p in data["posts"] if p["status"] == "queued")


def load_stats():
    _ensure_dirs()
    with _lock:
        return _read_json(STATS_FILE, {"posts": []})


def add_stat_entry(entry):
    with _lock:
        data = _read_json(STATS_FILE, {"posts": []})
        data["posts"].append(entry)
        _write_json(STATS_FILE, data)


def update_stat_votes(poll_id, option_a_votes, option_b_votes):
    with _lock:
        data = _read_json(STATS_FILE, {"posts": []})
        for p in data["posts"]:
            if p.get("poll_id") == poll_id:
                p["option_a_votes"] = option_a_votes
                p["option_b_votes"] = option_b_votes
                break
        _write_json(STATS_FILE, data)


def load_settings():
    _ensure_dirs()
    with _lock:
        return _read_json(SETTINGS_FILE, {
            "paused": False,
            "auto_post_no_moderation": False,
        })


def save_settings(data):
    with _lock:
        _write_json(SETTINGS_FILE, data)
