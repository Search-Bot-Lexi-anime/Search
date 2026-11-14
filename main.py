import asyncio
import logging
import os
import re
import threading
from html import escape, unescape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from flask import Flask
from pymongo import MongoClient
from rapidfuzz import fuzz
from about import about_text

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("search-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_NAME = os.getenv("DATABASE_NAME", "Search")
MONGODB_URI = os.getenv("MONGODB_URI")
DATA_CHANNEL_RAW = os.getenv("DATA_CHANNEL")
def load_owner_ids() -> set[str]:
    owner_ids: set[str] = set()
    for key, value in os.environ.items():
        if key in ("OWNER_ID", "OWNER_IDS") or (key.startswith("OWNER_ID_") and key[len("OWNER_ID_"):].isdigit()):
            owner_ids.update(part.strip() for part in value.split(",") if part.strip().isdigit())
    return owner_ids

OWNER_IDS = load_owner_ids()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")
if not MONGODB_URI:
    raise RuntimeError("Missing MONGODB_URI environment variable")
if not DATA_CHANNEL_RAW:
    raise RuntimeError("Missing DATA_CHANNEL environment variable")
if not OWNER_IDS:
    raise RuntimeError("Set OWNER_ID to comma-separated Telegram user IDs")
try:
    DATA_CHANNEL_ID = int(DATA_CHANNEL_RAW)
except ValueError as exc:
    raise RuntimeError("DATA_CHANNEL must be a numeric Telegram channel ID") from exc

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=10000)
db = mongo[DATABASE_NAME]
anime_collection = db["anime"]
users_collection = db["users"]
bot_settings_collection = db["bot_settings"]
owner_ui_collection = db["owner_ui"]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
health_app = Flask(__name__)
runtime_state = {"database": "connecting", "telegram": "starting"}
bot_profile = None

@health_app.get("/")
@health_app.get("/health")
def health():
    ready = runtime_state["database"] == "connected" and runtime_state["telegram"] == "connected"
    return {"status": "ok" if ready else "starting", "bot": "Search", "framework": "aiogram", **runtime_state}, (200 if ready else 503)

def run_health_server():
    health_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), use_reloader=False)

def is_owner(user_id: int) -> bool:
    return str(user_id) in OWNER_IDS

def is_private_chat(chat) -> bool:
    return bool(chat and chat.type == "private")

async def delete_command_message(message: Message):
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        log.debug("Could not delete group management command")

def ui(value: str) -> str:
    # Italicize only plain text. Preserve every message that already contains formatting.
    if re.search(r"</?(?:b|strong|i|em|u|s|strike|code|pre|a|blockquote|tg-spoiler)(?:\s|>)", value, re.I):
        return value
    return f"<i>{value}</i>"

def small_caps(value: str) -> str:
    table = str.maketrans({
        "a":"ᴀ","b":"ʙ","c":"ᴄ","d":"ᴅ","e":"ᴇ","f":"ғ","g":"ɢ","h":"ʜ",
        "i":"ɪ","j":"ᴊ","k":"ᴋ","l":"ʟ","m":"ᴍ","n":"ɴ","o":"ᴏ","p":"ᴘ",
        "q":"ǫ","r":"ʀ","s":"s","t":"ᴛ","u":"ᴜ","v":"ᴠ","w":"ᴡ","x":"x",
        "y":"ʏ","z":"ᴢ"
    })
    return value.lower().translate(table)

def clean_query(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())

def parse_names(value: str) -> list[str]:
    names, seen = [], set()
    for item in re.split(r"[,;\n]+", value):
        name = item.strip()
        normalized = clean_query(name)
        if normalized and normalized not in seen:
            names.append(name)
            seen.add(normalized)
    return names

def register_user(user_id: int):
    # Store new registrations individually; continue reading the legacy all_users record.
    users_collection.update_one(
        {"_id": f"user:{user_id}"},
        {"$set": {"active": True}},
        upsert=True,
    )

def migrate_legacy_users():
    legacy = users_collection.find_one({"_id": "all_users"}, {"id": 1}) or {}
    legacy_ids = [value for value in legacy.get("id", "").split(",") if value.isdigit()]
    for value in legacy_ids:
        users_collection.update_one(
            {"_id": f"user:{value}"},
            {"$setOnInsert": {"active": True}},
            upsert=True,
        )
    if legacy.get("id"):
        users_collection.update_one({"_id": "all_users"}, {"$unset": {"id": ""}})


def get_user_ids() -> list[int]:
    legacy = users_collection.find_one({"_id": "all_users"}, {"id": 1}) or {}
    ids = {int(value) for value in legacy.get("id", "").split(",") if value.isdigit()}
    active_docs = list(users_collection.find(
        {"_id": {"$regex": "^user:"}, "active": True}, {"_id": 1}
    ))
    inactive_docs = list(users_collection.find(
        {"_id": {"$regex": "^user:"}, "active": False}, {"_id": 1}
    ))
    ids.update(
        int(doc["_id"].split(":", 1)[1])
        for doc in active_docs
        if doc.get("_id", "").split(":")[-1].isdigit()
    )
    inactive = {
        int(doc["_id"].split(":", 1)[1])
        for doc in inactive_docs
        if doc.get("_id", "").split(":")[-1].isdigit()
    }
    return sorted(ids - inactive)

def deactivate_user(user_id: int):
    users_collection.update_one(
        {"_id": f"user:{user_id}"},
        {"$set": {"active": False}},
        upsert=True,
    )

def load_search_catalog() -> list[dict]:
    # One MongoDB aggregation fetches anime links and the shared custom-button config.
    return list(anime_collection.aggregate([
        {"$lookup": {
            "from": "bot_settings",
            "pipeline": [
                {"$match": {"_id": RESULT_BUTTONS_ID}},
                {"$project": {
                    "_id": 0,
                    "fixed_button_text": 1,
                    "custom_enabled": 1,
                    "custom_button_text": 1,
                    "custom_button_url": 1,
                }},
            ],
            "as": "_result_cfg",
        }},
        {"$set": {
            "_result_cfg": {
                "$ifNull": [{"$arrayElemAt": ["$_result_cfg", 0]}, {}]
            }
        }},
        {"$project": {"key": 1, "id": 1, "link": 1, "_result_cfg": 1}},
    ]))


def match_anime(query: str, catalog: list[dict]):
    q = clean_query(query)
    if not q:
        return None
    best_doc, best_score = None, -1
    q_words = q.split()
    for doc in catalog:
        for alias in doc.get("key", "").split(","):
            candidate = clean_query(alias)
            if not candidate:
                continue
            if candidate == q:
                return doc
            c_words = candidate.split()
            score = max(
                fuzz.WRatio(q, candidate),
                fuzz.ratio(q, candidate),
                fuzz.partial_ratio(q, candidate),
                fuzz.token_sort_ratio(q, candidate),
                fuzz.token_set_ratio(q, candidate),
            )
            matched_words = sum(
                1 for q_word in q_words
                if any(c_word.startswith(q_word) or fuzz.ratio(q_word, c_word) >= 82
                       for c_word in c_words)
            )
            coverage = matched_words / len(q_words)
            if coverage == 1:
                score = max(score, 94 if len(q_words) > 1 else 90)
            elif coverage >= 0.5:
                score = max(score, 82 + 10 * coverage)
            if candidate.startswith(q):
                score = max(score, min(98, 88 + len(q) * 1.2))
            if score > best_score:
                best_doc, best_score = doc, score
    threshold = 58 if len(q) >= 7 else 66 if len(q) >= 4 else 82
    return best_doc if best_score >= threshold else None


async def find_anime(query: str):
    catalog = await asyncio.to_thread(load_search_catalog)
    return match_anime(query, catalog)


async def find_animes(queries: list[str]) -> dict[str, dict | None]:
    # All alias duplicate checks share one database round trip off the event loop.
    catalog = await asyncio.to_thread(load_search_catalog)
    return {query: match_anime(query, catalog) for query in queries}

START_SETTINGS_ID = "start_message"
RESULT_BUTTONS_ID = "result_buttons"
DEFAULT_FIXED_BUTTON_TEXT = os.getenv("FIXED_BUTTON_TEXT", "· ᴡᴀᴛᴄʜ ɴᴏᴡ ·")

def get_result_button_settings() -> dict:
    saved = bot_settings_collection.find_one({"_id": RESULT_BUTTONS_ID}) or {}
    return {"fixed_button_text": DEFAULT_FIXED_BUTTON_TEXT, "custom_enabled": False,
            "custom_button_text": "", "custom_button_url": "", **saved}

def button_label_text(value: str) -> str:
    # Telegram inline keyboard labels are plain text and cannot render HTML.
    plain = unescape(str(value or ""))
    plain = re.sub(r"<[^>]*>", "", plain)
    return plain.strip()[:64]

def result_keyboard_for_anime(anime: dict) -> InlineKeyboardMarkup | None:
    cfg = anime.get("_result_cfg") or {}
    rows = []
    fixed_url = (anime.get("link") or "").strip()
    fixed_label = button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
    if fixed_label and re.fullmatch(r"https?://[^\s]+", fixed_url, re.I):
        rows.append([InlineKeyboardButton(text=fixed_label, url=fixed_url)])

    # The optional custom button is global and occupies its own second row.
    if cfg.get("custom_enabled"):
        custom_label = button_label_text(cfg.get("custom_button_text"))
        custom_url = (cfg.get("custom_button_url") or "").strip()
        if custom_label and re.fullmatch(r"https?://[^\s]+", custom_url, re.I):
            rows.append([InlineKeyboardButton(text=custom_label, url=custom_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def clear_owner_interaction_messages(session: dict, input_message: Message | None = None):
    chat_id = session.get("settings_chat_id") or (input_message.chat.id if input_message else None)
    ids = list(session.get("input_message_ids", [])) + list(session.get("prompt_message_ids", []))
    ids += [session.get("prompt_message_id"), session.get("last_input_message_id")]
    if input_message is not None:
        ids.append(input_message.message_id)
    for message_id in {x for x in ids if x}:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            log.debug("Could not delete owner interaction message")
    session["input_message_ids"], session["prompt_message_ids"] = [], []
    session.pop("prompt_message_id", None)
    session.pop("last_input_message_id", None)

async def edit_owner_panel(session: dict, text: str, reply_markup=None) -> Message:
    markup = reply_markup or InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
    ])
    return await bot.edit_message_text(
        chat_id=session["settings_chat_id"],
        message_id=session["settings_message_id"],
        text=ui(text),
        reply_markup=markup,
        link_preview_options={"is_disabled": True},
    )

async def send_owner_prompt(user_id: int, session: dict, text: str) -> Message:
    if session.get("settings_message_id"):
        return await edit_owner_panel(session, text)
    prompt = await bot.send_message(chat_id=session["settings_chat_id"], text=ui(text))
    session["settings_message_id"] = prompt.message_id
    session.setdefault("prompt_message_ids", []).clear()
    await track_owner_ui(user_id, prompt, "panel")
    return prompt

async def delete_pending_data_post(session: dict):
    if session.get("committing"):
        return
    message_id = session.get("new_id")
    if not message_id:
        return
    session.pop("new_id", None)
    try:
        await bot.delete_message(chat_id=DATA_CHANNEL_ID, message_id=int(message_id))
    except Exception:
        log.debug("Could not delete cancelled temporary data-channel post")


async def clear_pending_for_panel(user_id: int, panel_message_id: int):
    session = pending.get(user_id)
    broadcast = pending_broadcast.get(user_id)
    pending_matches = bool(session and session.get("settings_message_id") == panel_message_id)
    broadcast_matches = bool(broadcast and broadcast.get("panel_message_id") == panel_message_id)
    if pending_matches or broadcast_matches:
        await cancel_owner_process(user_id)
DEFAULT_START = {
    "_id": START_SETTINGS_ID,
    "text": "<b><i>Welcome, {mention}!</i></b>\n<i>I'm {bot_name}, your anime search companion. Search an anime name to find it if it is available.</i>\n\n<i>Made for <a href=\"https://t.me/lexianime\">Lexi Anime</a> fans ✨</i>",
    "media_type": None, "media_file_id": None,
    **{f"button{i}_{field}": "" for i in range(1, 5) for field in ("text", "url")},
}
DEFAULT_CAPTION = DEFAULT_START["text"]
DEFAULT_START["caption_custom"] = False

def get_start_settings() -> dict:
    saved = bot_settings_collection.find_one({"_id": START_SETTINGS_ID}) or {}
    return {**DEFAULT_START, **saved}

async def render_start_text(template: str, user) -> str:
    global bot_profile
    if bot_profile is None:
        bot_profile = await bot.get_me()
    me = bot_profile
    user_name = escape(user.full_name if user else "there")
    user_id = user.id if user else 0
    mention = f'<a href="tg://user?id={user_id}">{user_name}</a>' if user_id else user_name
    bot_name = escape(me.full_name)
    anime = '<a href="https://t.me/lexianime">Lexi Anime</a>'
    for key, value in {
        "{bot_name}": bot_name, "{bot_mention}": bot_name,
        "{user_name}": user_name, "{mention}": mention, "{anime}": anime,
    }.items():
        template = template.replace(key, value)
    return template

def caption_variables_help() -> str:
    variables = ("{bot_name}", "{user_name}", "{mention}", "{anime}")
    return "<i>Available variables (tap to copy):</i>\n" + "\n".join(
        f"<code>{escape(item)}</code>" for item in variables
    )

def start_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    rows = []
    for first, second in ((1, 2), (3, 4)):
        row = []
        for idx in (first, second):
            label = (cfg.get(f"button{idx}_text") or "").strip()
            url = (cfg.get(f"button{idx}_url") or "").strip()
            if label and re.fullmatch(r"https?://[^\s]+", url, re.I):
                row.append(InlineKeyboardButton(text=label[:64], url=url))
        if row:
            rows.append(row)
    if cfg.get("about_enabled", True):
        rows.append([InlineKeyboardButton(text=small_caps("about"), callback_data="start:about")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def start_settings_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("caption"), callback_data="startcfg:caption"),
         InlineKeyboardButton(text=small_caps("media"), callback_data="startcfg:media")],
        [InlineKeyboardButton(text=small_caps("buttons"), callback_data="startcfg:buttons"),
         InlineKeyboardButton(text=small_caps("about"), callback_data="startcfg:about_toggle")],
        [InlineKeyboardButton(text=small_caps("custom button"), callback_data="resultcfg:home")],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="startcfg:settings_home")],
    ])

def start_buttons_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    def button(idx: int) -> InlineKeyboardButton:
        enabled = bool((cfg.get(f"button{idx}_text") or "").strip() and (cfg.get(f"button{idx}_url") or "").strip())
        return InlineKeyboardButton(
            text=small_caps(f"{idx} {'ON ✅' if enabled else 'OFF ❌'}"),
            callback_data=f"startcfg:button:{idx}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(1), button(2)],
        [button(3), button(4)],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="startcfg:home")],
    ])

def start_buttons_text(cfg: dict) -> str:
    statuses = "\n".join(
        f"<i>Button {idx}: {'ON ✅' if cfg.get(f'button{idx}_text') and cfg.get(f'button{idx}_url') else 'OFF ❌'}</i>"
        for idx in range(1, 5)
    )
    return ui("<b><u>START BUTTONS</u></b>\n\n<i>Customize buttons shown with your start message.</i>\n\n" + statuses)

def result_buttons_text(cfg: dict) -> str:
    custom_state = "Enabled ✅" if cfg.get("custom_enabled") else "Disabled ❌"
    label = escape(button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT))
    return ("<b><u>BUTTONS</u></b>\n\n"
            "<i>Customize Results Post Button Using the buttons below.</i>\n\n"
            f"<i>Fixed Button: </i>{label}\n"
            f"<i>Custom Button: {custom_state}</i>")

def fixed_button_text(cfg: dict) -> str:
    label = escape(button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT))
    return ("<b><u>FIXED BUTTON</u></b>\n\n"
            "<i>Customize Fixed Button Label Text As You Want.</i>\n\n"
            f"<i>Current Text: {label}</i>")

def result_buttons_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    custom_status = "custom button " + ("✅" if cfg.get("custom_enabled") else "❌")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("fixed button"), callback_data="resultcfg:fixed"),
         InlineKeyboardButton(text=small_caps(custom_status), callback_data="resultcfg:custom_toggle")],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="resultcfg:back")],
    ])

def fixed_button_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("edit"), callback_data="resultcfg:fixed_edit"),
         InlineKeyboardButton(text=small_caps("see"), callback_data="resultcfg:fixed_see")],
        [InlineKeyboardButton(text=small_caps("default"), callback_data="resultcfg:fixed_default")],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="resultcfg:home")],
    ])

def bot_mention_text() -> str:
    # bot_profile is populated during startup before owner settings can be opened.
    if bot_profile and bot_profile.username:
        return f'<a href="tg://resolve?domain={escape(bot_profile.username)}">{escape(bot_profile.full_name)}</a>'
    return escape(bot_profile.full_name) if bot_profile else "Bot"


def settings_home_text() -> str:
    return ("<b><u>SETTINGS</u></b>\n\n"
            f"<i>✰ Name: {bot_mention_text()}</i>\n\n"
            "<i>Access/Manage Bot using the following options below.</i>")
def settings_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✚ " + small_caps("add"), callback_data="settings:add"),
         InlineKeyboardButton(text=small_caps("added"), callback_data="settings:show")],
        [InlineKeyboardButton(text=small_caps("start"), callback_data="settings:start"),
         InlineKeyboardButton(text=small_caps("buttons"), callback_data="settings:results")],
        [InlineKeyboardButton(text=small_caps("broadcast"), callback_data="settings:broadcast"),
         InlineKeyboardButton(text=small_caps("p-broadcast"), callback_data="settings:pbroadcast")],
    ])

def start_settings_text(cfg: dict) -> str:
    caption_state = "Added ✅" if cfg.get("caption_custom") else "Default ✨"
    media_state = "Added ✅" if cfg.get("media_type") and cfg.get("media_file_id") else "None ❌"
    about_state = "Enabled ✅" if cfg.get("about_enabled", True) else "Disabled ❌"
    return ("<b><u>SETTINGS</u></b>\n\n"
            "<i>Customize start message using the following options below.</i>\n\n"
            f"<i>Caption: {caption_state}</i>\n"
            f"<i>Media: {media_state}</i>\n"
            f"<i>About: {about_state}</i>")
def caption_settings_keyboard(has_caption: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            text=small_caps("edit") if has_caption else "✚ " + small_caps("add"),
            callback_data="startcfg:caption_edit",
        ),
        InlineKeyboardButton(text=small_caps("show"), callback_data="startcfg:caption_show"),
    ]]
    rows.extend([
        [InlineKeyboardButton(text=small_caps("default"), callback_data="startcfg:caption_default")],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="startcfg:home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def media_settings_keyboard(has_media: bool) -> InlineKeyboardMarkup:
    if has_media:
        rows = [[
            InlineKeyboardButton(text=small_caps("edit"), callback_data="startcfg:media_edit"),
            InlineKeyboardButton(text=small_caps("remove"), callback_data="startcfg:media_remove"),
        ]]
    else:
        rows = [[InlineKeyboardButton(text="✚ " + small_caps("add"), callback_data="startcfg:media_edit")]]
    rows.append([InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="startcfg:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_start_page(message: Message):
    cfg = await asyncio.to_thread(get_start_settings)
    body = await render_start_text(cfg.get("text") or DEFAULT_START["text"], message.from_user)
    markup = start_keyboard(cfg)
    media_type, media_id = cfg.get("media_type"), cfg.get("media_file_id")
    if media_type == "photo" and media_id:
        return await bot.send_photo(chat_id=message.chat.id, photo=media_id, caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif media_type == "animation" and media_id:
        return await bot.send_animation(chat_id=message.chat.id, animation=media_id, caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await bot.send_message(chat_id=message.chat.id, text=body, reply_markup=markup, parse_mode=ParseMode.HTML,
                                   link_preview_options={"is_disabled": True})


pending: dict[int, dict] = {}
pending_broadcast: dict[int, dict] = {}
owner_ui_state: dict[int, dict] = {}

async def cancel_broadcast_wait(user_id: int, notify: bool = False):
    session = pending_broadcast.pop(user_id, None)
    if session:
        task = session.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
        if notify and session.get("panel_message_id"):
            try:
                await bot.edit_message_text(
                    chat_id=session["chat_id"],
                    message_id=session["panel_message_id"],
                    text=ui("❌ Broadcast cancelled."),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
                    ]),
                    link_preview_options={"is_disabled": True},
                )
            except Exception:
                log.debug("Could not update cancelled broadcast panel")

async def cancel_owner_process(user_id: int, notify_broadcast: bool = False):
    session = pending.pop(user_id, None)
    if session:
        await delete_pending_data_post(session)
        await clear_owner_interaction_messages(session)
    await cancel_broadcast_wait(user_id, notify=notify_broadcast)

async def begin_owner_command(user_id: int):
    session = pending.get(user_id)
    await cancel_owner_process(user_id)
    return session

async def finish_owner_command(user_id: int, previous_session: dict | None, keep: tuple[int, int]):
    if previous_session:
        await clear_owner_interaction_messages(previous_session)
    await cleanup_owner_ui(user_id, keep=keep)

async def track_owner_ui(user_id: int, message: Message, kind: str = "prompt"):
    state = owner_ui_state.setdefault(user_id, {"prompts": [], "panels": []})
    ref = {"chat_id": message.chat.id, "message_id": message.message_id}
    doc_id = str(user_id)
    saved = await asyncio.to_thread(owner_ui_collection.find_one, {"_id": doc_id}) or {}
    if kind == "panel":
        previous = saved.get("panel")
        stale = list(saved.get("previous_panels", []))
        if previous and previous != ref and previous not in stale:
            stale.append(previous)
        await asyncio.to_thread(
            owner_ui_collection.update_one,
            {"_id": doc_id},
            {"$set": {"panel": ref, "previous_panels": stale}},
            upsert=True,
        )
        state["panels"] = [(message.chat.id, message.message_id)]
    else:
        prompts = list(saved.get("prompts", []))
        if ref not in prompts:
            prompts.append(ref)
        await asyncio.to_thread(
            owner_ui_collection.update_one,
            {"_id": doc_id},
            {"$set": {"prompts": prompts}},
            upsert=True,
        )
        state["prompts"] = [(item["chat_id"], item["message_id"]) for item in prompts]


async def open_owner_panel(user_id: int, chat_id: int, text: str, reply_markup=None) -> Message:
    state = owner_ui_state.setdefault(user_id, {"prompts": [], "panels": []})
    saved = await asyncio.to_thread(owner_ui_collection.find_one, {"_id": str(user_id)}) or {}
    panel_ref = saved.get("panel")
    if not panel_ref and state.get("panels"):
        old_chat_id, old_message_id = state["panels"][-1]
        panel_ref = {"chat_id": old_chat_id, "message_id": old_message_id}
    if panel_ref and panel_ref.get("chat_id") == chat_id:
        try:
            panel = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_ref["message_id"],
                text=text,
                reply_markup=reply_markup,
                link_preview_options={"is_disabled": True},
            )
            await track_owner_ui(user_id, panel, "panel")
            return panel
        except Exception:
            log.debug("Could not edit the current owner panel; creating a fresh one")
    panel = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        link_preview_options={"is_disabled": True},
    )
    await track_owner_ui(user_id, panel, "panel")
    return panel

async def cleanup_owner_ui(user_id: int, keep: tuple[int, int] | None = None):
    state = owner_ui_state.setdefault(user_id, {"prompts": [], "panels": []})
    saved = await asyncio.to_thread(owner_ui_collection.find_one, {"_id": str(user_id)}) or {}
    refs = []
    refs.extend(saved.get("prompts", []))
    refs.extend(saved.get("previous_panels", []))
    if saved.get("panel"):
        refs.append(saved["panel"])
    refs.extend(
        {"chat_id": chat_id, "message_id": message_id}
        for chat_id, message_id in state.get("prompts", []) + state.get("panels", [])
    )
    unique, seen = [], set()
    for ref in refs:
        key = (ref.get("chat_id"), ref.get("message_id"))
        if key[0] is not None and key[1] is not None and key not in seen:
            seen.add(key)
            unique.append(ref)

    failed_prompts, failed_panels = [], []
    kept_panel = None
    saved_prompts = saved.get("prompts", [])
    for ref in unique:
        key = (ref["chat_id"], ref["message_id"])
        if keep == key:
            kept_panel = ref
            continue
        try:
            await bot.delete_message(chat_id=key[0], message_id=key[1])
        except Exception:
            log.debug("Could not delete previous owner prompt/panel")
            (failed_prompts if ref in saved_prompts else failed_panels).append(ref)

    await asyncio.to_thread(
        owner_ui_collection.update_one,
        {"_id": str(user_id)},
        {"$set": {
            "panel": kept_panel,
            "prompts": failed_prompts,
            "previous_panels": failed_panels,
        }},
        upsert=True,
    )
    state["prompts"] = [(item["chat_id"], item["message_id"]) for item in failed_prompts]
    state["panels"] = [(kept_panel["chat_id"], kept_panel["message_id"])] if kept_panel else []

async def start_broadcast_wait(user_id: int, chat_id: int, pin: bool, panel_message_id: int):
    await cancel_owner_process(user_id)
    session = {"chat_id": chat_id, "pin": pin, "panel_message_id": panel_message_id}
    pending_broadcast[user_id] = session

    async def expire():
        try:
            await asyncio.sleep(30)
            if pending_broadcast.get(user_id) is session:
                pending_broadcast.pop(user_id, None)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=panel_message_id,
                        text=ui("❌ Broadcast cancelled due to 30 seconds of inactivity."),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
                        ]),
                        link_preview_options={"is_disabled": True},
                    )
                except Exception:
                    log.debug("Could not update expired broadcast panel")
        except asyncio.CancelledError:
            pass

    session["task"] = asyncio.create_task(expire())

async def deliver_saved(chat_id: int, anime: dict, reply_to: Message | None = None):
    # One Telegram copy request attaches the anime's fixed link and optional custom button.
    kwargs = {
        "chat_id": chat_id,
        "from_chat_id": DATA_CHANNEL_ID,
        "message_id": int(anime["id"]),
    }
    keyboard = result_keyboard_for_anime(anime)
    if keyboard is not None:
        kwargs["reply_markup"] = keyboard
    if reply_to is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=reply_to.message_id,
            allow_sending_without_reply=True,
        )
    await bot.copy_message(**kwargs)

def anime_list_markup(docs: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * 10
    rows = [[InlineKeyboardButton(
        text=small_caps(doc.get("key", "anime").split(",")[0].strip())[:64],
        callback_data=f"anime:{doc['id']}:{page}",
    )] for doc in docs[start:start + 10]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=small_caps("previous"), callback_data=f"page:{page-1}"))
    if start + 10 < len(docs):
        nav.append(InlineKeyboardButton(text=small_caps("next"), callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def start_handler(message: Message):
    owner_id = message.from_user.id if (
        message.from_user and is_owner(message.from_user.id) and is_private_chat(message.chat)
    ) else None
    previous_session = None
    if message.from_user:
        if owner_id is not None:
            previous_session = await begin_owner_command(owner_id)
        elif not is_owner(message.from_user.id):
            await cancel_broadcast_wait(message.from_user.id, notify=True)
        if is_private_chat(message.chat):
            await asyncio.to_thread(register_user, message.from_user.id)
    panel = await send_start_page(message)
    if owner_id is not None:
        await track_owner_ui(owner_id, panel, "panel")
        await finish_owner_command(owner_id, previous_session, (panel.chat.id, panel.message_id))
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            log.debug("Could not delete /start request")

@dp.message(Command("settings"))
@dp.message(Command("commands"))
async def commands_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    owner_id = message.from_user.id
    previous_session = await begin_owner_command(owner_id)
    # Send the fresh panel first; only after it exists, remove the previous panel and command.
    # Use send_message directly: never inherit/reuse the incoming command as a reply target.
    new_panel = await bot.send_message(
        chat_id=message.chat.id,
        text=settings_home_text(),
        reply_markup=settings_home_keyboard(),
        link_preview_options={"is_disabled": True},
    )

    await track_owner_ui(owner_id, new_panel, "panel")
    await finish_owner_command(owner_id, previous_session, (new_panel.chat.id, new_panel.message_id))

    # The fresh panel is already visible. Now clean up the command and previous panel.
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        log.debug("Could not delete /commands request")


@dp.message(Command("setstart"))
async def setstart_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    user_id = message.from_user.id
    previous_session = await begin_owner_command(user_id)
    cfg = await asyncio.to_thread(get_start_settings)
    panel = await open_owner_panel(
        user_id,
        message.chat.id,
        start_settings_text(cfg),
        start_settings_keyboard(cfg),
    )
    await finish_owner_command(user_id, previous_session, (panel.chat.id, panel.message_id))
    await delete_command_message(message)

async def refresh_start_settings_message(chat_id: int, message_id: int):
    cfg = await asyncio.to_thread(get_start_settings)
    await bot.edit_message_text(
        start_settings_text(cfg),
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=start_settings_keyboard(cfg),
        link_preview_options={"is_disabled": True},
    )

async def refresh_start_settings(callback: CallbackQuery):
    await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("settings:"))
async def settings_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)

    action = callback.data.split(":", 1)[1]
    chat_id, panel_id = callback.message.chat.id, callback.message.message_id
    await clear_pending_for_panel(callback.from_user.id, panel_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(chat_id, panel_id))

    if action == "home":
        await callback.message.edit_text(
            settings_home_text(), reply_markup=settings_home_keyboard(),
            link_preview_options={"is_disabled": True},
        )
        return await callback.answer()

    if action == "start":
        cfg = await asyncio.to_thread(get_start_settings)
        await callback.message.edit_text(
            start_settings_text(cfg), reply_markup=start_settings_keyboard(cfg),
            link_preview_options={"is_disabled": True},
        )
        return await callback.answer()

    if action == "results":
        cfg = await asyncio.to_thread(get_result_button_settings)
        await callback.message.edit_text(result_buttons_text(cfg), reply_markup=result_buttons_keyboard(cfg))
        return await callback.answer()

    if action == "add":
        pending[callback.from_user.id] = {
            "stage": "names", "settings_chat_id": chat_id, "settings_message_id": panel_id,
            "input_message_ids": [], "prompt_message_ids": [],
        }
        await callback.answer()
        await send_owner_prompt(
            callback.from_user.id, pending[callback.from_user.id],
            "<b><i>ADD ANIME</i></b>\n\n<i>Send all alternate anime names, separated by commas or new lines. Matching will connect every alias to the saved post.</i>",
        )
        return

    if action == "show":
        docs = await asyncio.to_thread(lambda: list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1)))
        if not docs:
            await callback.message.edit_text(
                ui("<b><i>SAVED ANIME</i></b>\n\n<i>No anime records have been added yet.</i>"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
                ]),
            )
            return await callback.answer()
        total_pages = max(1, (len(docs) + 9) // 10)
        await callback.message.edit_text(
            ui(f"<b><i>SAVED ANIME</i></b> — {len(docs)} records\n<i>Page 1 of {total_pages} · Showing 1–{min(10, len(docs))}</i>\n<i>Select a title to view its details.</i>"),
            reply_markup=anime_list_markup(docs, 0),
        )
        return await callback.answer()

    if action in ("broadcast", "pbroadcast"):
        pin = action == "pbroadcast"
        await start_broadcast_wait(callback.from_user.id, chat_id, pin=pin, panel_message_id=panel_id)
        await callback.message.edit_text(
            ui("<b><i>PIN BROADCAST</i></b>\n\n<i>Send the message to broadcast and pin. You have 30 seconds.</i>" if pin
               else "<b><i>BROADCAST</i></b>\n\n<i>Send the message to broadcast. You have 30 seconds.</i>"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
            ]),
        )
        return await callback.answer()

    await callback.answer("Unknown setting.", show_alert=True)


@dp.callback_query(F.data.startswith("startcfg:"))
async def startcfg_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    action = callback.data.split(":", 1)[1]
    cfg = await asyncio.to_thread(get_start_settings)

    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))

    if action == "settings_home":
        await callback.message.edit_text(
            settings_home_text(), reply_markup=settings_home_keyboard(),
            link_preview_options={"is_disabled": True},
        )
        return await callback.answer()

    if action in ("back", "home"):
        return await refresh_start_settings(callback)

    if action == "about_toggle":
        await asyncio.to_thread(
            bot_settings_collection.update_one,
            {"_id": START_SETTINGS_ID},
            {"$set": {"about_enabled": not cfg.get("about_enabled", True)}},
            upsert=True,
        )
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("✨ About setting updated.")

    if action == "buttons":
        await callback.message.edit_text(start_buttons_text(cfg), reply_markup=start_buttons_keyboard(cfg))
        return await callback.answer()

    if action == "caption":
        await callback.message.edit_text(
            ui("<b><u>CAPTION</u></b>\n\n<i>Customize your bot's start message Caption.</i>"),
            reply_markup=caption_settings_keyboard(bool(cfg.get("caption_custom")))
        )
        return await callback.answer()

    if action == "caption_show":
        caption = cfg.get("text") or DEFAULT_CAPTION
        return await callback.answer(re.sub(r"<[^>]*>", "", unescape(caption))[:190], show_alert=True)

    if action == "caption_default":
        await asyncio.to_thread(
            bot_settings_collection.update_one,
            {"_id": START_SETTINGS_ID}, {"$unset": {"text": "", "caption_custom": ""}}
        )
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("✨ Successfully Restored to default.", show_alert=True)

    if action == "caption_edit":
        pending[callback.from_user.id] = {"stage": "start_caption", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                                "<i>Send The New Caption text.</i>\n\n" + caption_variables_help())
        return

    if action == "media":
        has_media = bool(cfg.get("media_type") and cfg.get("media_file_id"))
        await callback.message.edit_text(
            ui("<b><u>MEDIA</u></b>\n\n<i>Include an image or GIF with your start message.</i>"),
            reply_markup=media_settings_keyboard(has_media)
        )
        return await callback.answer()

    if action == "media_edit":
        pending[callback.from_user.id] = {"stage": "start_media", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                                "<i>Only GIFs and images are supported. Send the new start media here.</i>")
        return

    if action == "media_remove":
        await asyncio.to_thread(
            bot_settings_collection.update_one,
            {"_id": START_SETTINGS_ID},
            {"$unset": {"media_type": "", "media_file_id": ""}},
        )
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("✨ Successfully removed your bot start poster.", show_alert=True)

    if action.startswith("button:"):
        idx = int(action.split(":", 1)[1])
        if idx not in (1, 2, 3, 4):
            return await callback.answer("Invalid button.", show_alert=True)
        label = (cfg.get(f"button{idx}_text") or "").strip()
        url = (cfg.get(f"button{idx}_url") or "").strip()
        if label and url:
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": START_SETTINGS_ID},
                {"$unset": {f"button{idx}_text": "", f"button{idx}_url": ""}},
            )
            cfg_now = await asyncio.to_thread(get_start_settings)
            await callback.message.edit_text(start_buttons_text(cfg_now), reply_markup=start_buttons_keyboard(cfg_now))
            return await callback.answer(f"✨ Button {idx} disabled.")
        pending[callback.from_user.id] = {"stage": "start_button_text", "button": idx, "settings_page": "buttons", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                                f"<i>Send text for Start Message Button {idx}.</i>")
        return

    await callback.answer("Unknown setting.", show_alert=True)

@dp.callback_query(F.data.startswith("resultcfg:"))
async def resultcfg_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
    action = callback.data.split(":", 1)[1]
    cfg = await asyncio.to_thread(get_result_button_settings)

    if action == "home":
        await callback.message.edit_text(result_buttons_text(cfg), reply_markup=result_buttons_keyboard(cfg))
        return await callback.answer()
    if action == "back":
        await callback.message.edit_text(
            settings_home_text(), reply_markup=settings_home_keyboard(),
            link_preview_options={"is_disabled": True},
        )
        return await callback.answer()
    if action == "fixed":
        await callback.message.edit_text(fixed_button_text(cfg), reply_markup=fixed_button_keyboard())
        return await callback.answer()
    if action == "fixed_see":
        label = button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
        return await callback.answer(label[:190], show_alert=True)
    if action == "fixed_default":
        await asyncio.to_thread(
            bot_settings_collection.update_one,
            {"_id": RESULT_BUTTONS_ID}, {"$unset": {"fixed_button_text": ""}}, upsert=True
        )
        cfg = await asyncio.to_thread(get_result_button_settings)
        await callback.message.edit_text(fixed_button_text(cfg), reply_markup=fixed_button_keyboard())
        return await callback.answer("Successfully Restored to default ♻️", show_alert=True)
    if action == "fixed_edit":
        pending[callback.from_user.id] = {
            "stage": "fixed_button_text",
            "settings_chat_id": callback.message.chat.id,
            "settings_message_id": callback.message.message_id,
            "input_message_ids": [],
            "prompt_message_ids": [],
        }
        await callback.answer()
        await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                                "Send The New Fixed Button text.")
        return
    if action == "custom_toggle":
        if cfg.get("custom_enabled"):
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": RESULT_BUTTONS_ID}, {"$set": {"custom_enabled": False}}, upsert=True
            )
            cfg = await asyncio.to_thread(get_result_button_settings)
            await callback.message.edit_text(result_buttons_text(cfg), reply_markup=result_buttons_keyboard(cfg))
            return await callback.answer("✨ Custom Button Disabled.")
        pending[callback.from_user.id] = {
            "stage": "custom_button_text",
            "settings_chat_id": callback.message.chat.id,
            "settings_message_id": callback.message.message_id,
            "input_message_ids": [],
            "prompt_message_ids": [],
        }
        await callback.answer()
        await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                                "Send text for Custom Button.")
        return
    await callback.answer("Unknown setting.", show_alert=True)

@dp.callback_query(F.data == "start:about")
async def start_about_callback(callback: CallbackQuery):
    if callback.from_user and is_owner(callback.from_user.id):
        await cancel_owner_process(callback.from_user.id)
    cfg = await asyncio.to_thread(get_start_settings)
    if not cfg.get("about_enabled", True):
        return await callback.answer("About is disabled.", show_alert=True)
    await callback.answer()
    global bot_profile
    if bot_profile is None:
        bot_profile = await bot.get_me()
    body = about_text(bot_profile)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("back"), callback_data="start:back")]
    ])
    if callback.message.photo or callback.message.animation:
        await callback.message.edit_caption(caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML,
                                         link_preview_options={"is_disabled": True})

@dp.callback_query(F.data == "start:back")
async def start_back_callback(callback: CallbackQuery):
    if callback.from_user and is_owner(callback.from_user.id):
        await cancel_owner_process(callback.from_user.id)
    await callback.answer()
    cfg = await asyncio.to_thread(get_start_settings)
    body = await render_start_text(cfg.get("text") or DEFAULT_START["text"], callback.from_user)
    markup = start_keyboard(cfg)
    if callback.message.photo or callback.message.animation:
        await callback.message.edit_caption(caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML,
                                         link_preview_options={"is_disabled": True})

@dp.message(Command("add"))
async def add_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    previous_session = await begin_owner_command(message.from_user.id)
    panel = await open_owner_panel(
        message.from_user.id,
        message.chat.id,
        "<b><i>ADD ANIME</i></b>\n\n<i>Send all alternate anime names, separated by commas or new lines. Matching will connect every alias to the saved post.</i>",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
        ]),
    )
    pending[message.from_user.id] = {
        "stage": "names",
        "settings_chat_id": message.chat.id,
        "settings_message_id": panel.message_id,
        "input_message_ids": [],
        "prompt_message_ids": [],
    }
    await track_owner_ui(message.from_user.id, panel, "panel")
    await finish_owner_command(message.from_user.id, previous_session, (panel.chat.id, panel.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /add command")

@dp.message(Command("show"))
async def show_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    previous_session = await begin_owner_command(message.from_user.id)
    docs = await asyncio.to_thread(lambda: list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1)))
    if not docs:
        prompt = await open_owner_panel(
            message.from_user.id,
            message.chat.id,
            ui("<b><i>SAVED ANIME</i></b>\n\n<i>No anime records have been added yet.</i>"),
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
            ]),
        )
        await finish_owner_command(message.from_user.id, previous_session, (prompt.chat.id, prompt.message_id))
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            log.debug("Could not delete /show command")
        return
    total_pages = max(1, (len(docs) + 9) // 10)
    panel = await open_owner_panel(
        message.from_user.id,
        message.chat.id,
        ui(f"<b><i>SAVED ANIME</i></b> — {len(docs)} records\n<i>Page 1 of {total_pages} · Showing 1–{min(10, len(docs))}</i>\n<i>Select a title to view its details.</i>"),
        anime_list_markup(docs, 0),
    )
    await finish_owner_command(message.from_user.id, previous_session, (panel.chat.id, panel.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /show command")

@dp.callback_query(F.data.startswith("page:"))
async def page_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
    page = int(callback.data.split(":", 1)[1])
    docs = await asyncio.to_thread(lambda: list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1)))
    total_pages = max(1, (len(docs) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    start = page * 10
    end = min(start + 10, len(docs))
    await callback.message.edit_text(
        ui(f"<b><i>SAVED ANIME</i></b> — {len(docs)} records\n<i>Page {page + 1} of {total_pages} · Showing {start + 1 if docs else 0}–{end}</i>\nSelect a title to view its details."),
        reply_markup=anime_list_markup(docs, page),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("anime:"))
async def anime_detail_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
    _, message_id, page_raw = callback.data.split(":", 2)
    page = int(page_raw)
    doc = await asyncio.to_thread(anime_collection.find_one, {"id": message_id}, {"key": 1, "id": 1, "link": 1})
    if not doc:
        return await callback.answer("Record not found.", show_alert=True)
    link_state = "Saved ✅" if doc.get("link") else "Missing ❌"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("edit fixed link"), callback_data=f"anime_link_edit:{message_id}:{page}")],
        [InlineKeyboardButton(text=small_caps("replace"), callback_data=f"replace:{message_id}")],
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data=f"page:{page}")],
    ])
    await callback.message.edit_text(
        ui(f"<b><i>ANIME NAMES</i></b>\n{escape(doc['key'])}\n\n<b><i>FIXED LINK</i></b>\n<i>{link_state}</i>"),
        reply_markup=keyboard,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("anime_link_edit:"))
async def anime_link_edit_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    _, anime_id, page_raw = callback.data.split(":", 2)
    if not await asyncio.to_thread(anime_collection.find_one, {"id": anime_id}, {"_id": 1}):
        return await callback.answer("Record not found.", show_alert=True)
    previous_session = await begin_owner_command(callback.from_user.id)
    session = {
        "stage": "anime_link",
        "anime_id": anime_id,
        "page": int(page_raw),
        "settings_chat_id": callback.message.chat.id,
        "settings_message_id": callback.message.message_id,
        "input_message_ids": [],
        "prompt_message_ids": [],
    }
    pending[callback.from_user.id] = session
    await send_owner_prompt(callback.from_user.id, session, "Send the Fixed Button URL for this anime.")
    await finish_owner_command(
        callback.from_user.id, previous_session,
        (callback.message.chat.id, callback.message.message_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("replace:"))
async def replace_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    if not is_private_chat(callback.message.chat):
        return await callback.answer("Settings are available only in the owner's private chat.", show_alert=True)
    previous_session = await begin_owner_command(callback.from_user.id)
    old_id = callback.data.split(":", 1)[1]
    doc = await asyncio.to_thread(anime_collection.find_one, {"id": old_id}, {"key": 1, "id": 1})
    if not doc:
        return await callback.answer("Record not found.", show_alert=True)
    pending[callback.from_user.id] = {
        "stage": "replace_post", "anime_id": old_id,
        "settings_chat_id": callback.message.chat.id,
        "settings_message_id": callback.message.message_id,
        "input_message_ids": [], "prompt_message_ids": [],
    }
    await clear_owner_interaction_messages(pending[callback.from_user.id])
    await send_owner_prompt(callback.from_user.id, pending[callback.from_user.id],
                            "Send the replacement anime post. The old record stays active until the new fixed link is saved.")
    await finish_owner_command(callback.from_user.id, previous_session,
                               (callback.message.chat.id, callback.message.message_id))
    await callback.answer()

@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    previous_session = await begin_owner_command(message.from_user.id)
    panel = await open_owner_panel(
        message.from_user.id,
        message.chat.id,
        ui("<b><i>BROADCAST</i></b>\n\n" + ("Broadcasting replied message..." if message.reply_to_message else "Send the message to broadcast. Its media, formatting and inline buttons will be copied. You have 30 seconds.")),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
        ]),
    )
    await finish_owner_command(message.from_user.id, previous_session, (panel.chat.id, panel.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /broadcast command")
    if message.reply_to_message:
        delivered, failed, _, _ = await broadcast_message(message.reply_to_message)
        await bot.edit_message_text(
            chat_id=panel.chat.id, message_id=panel.message_id,
            text=ui(f"✨ Broadcast complete. Delivered: {delivered}. Failed: {failed}."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
            ]),
            link_preview_options={"is_disabled": True},
        )
        return
    await start_broadcast_wait(message.from_user.id, message.chat.id, pin=False, panel_message_id=panel.message_id)

@dp.message(Command("pbroadcast"))
async def pin_broadcast_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    if not is_private_chat(message.chat):
        await delete_command_message(message)
        return
    previous_session = await begin_owner_command(message.from_user.id)
    panel = await open_owner_panel(
        message.from_user.id,
        message.chat.id,
        ui("<b><i>PIN BROADCAST</i></b>\n\n" + ("Broadcasting and pinning replied message..." if message.reply_to_message else "Send the message to broadcast and pin. Its media, formatting and inline buttons will be copied. You have 30 seconds.")),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
        ]),
    )
    await finish_owner_command(message.from_user.id, previous_session, (panel.chat.id, panel.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /pbroadcast command")
    if message.reply_to_message:
        delivered, failed, pinned, pin_failed = await broadcast_message(message.reply_to_message, pin=True)
        await bot.edit_message_text(
            chat_id=panel.chat.id, message_id=panel.message_id,
            text=ui(f"✨ Pin broadcast complete. Delivered: {delivered}. Delivery failed: {failed}. Pinned: {pinned}. Pin failed: {pin_failed}."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
            ]),
            link_preview_options={"is_disabled": True},
        )
        return
    await start_broadcast_wait(message.from_user.id, message.chat.id, pin=True, panel_message_id=panel.message_id)

async def broadcast_message(source: Message, pin: bool = False) -> tuple[int, int, int, int]:
    delivered = failed = pinned = pin_failed = 0
    for user_id in await asyncio.to_thread(get_user_ids):
        copied = None
        for attempt in range(4):
            try:
                copied = await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source.chat.id,
                    message_id=source.message_id,
                    reply_markup=source.reply_markup,
                )
                delivered += 1
                break
            except TelegramRetryAfter as exc:
                if attempt < 3:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    continue
                failed += 1
                log.warning("Broadcast rate limit retries exhausted for Telegram user %s", user_id)
            except TelegramForbiddenError:
                failed += 1
                await asyncio.to_thread(deactivate_user, user_id)
                log.info("Deactivated blocked Telegram user %s", user_id)
                break
            except Exception:
                failed += 1
                log.exception("Broadcast failed for Telegram user %s", user_id)
                break
        if copied is None:
            continue

        if pin:
            for pin_attempt in range(3):
                try:
                    await bot.pin_chat_message(
                        chat_id=user_id,
                        message_id=copied.message_id,
                        disable_notification=True,
                    )
                    pinned += 1
                    break
                except TelegramRetryAfter as exc:
                    if pin_attempt < 2:
                        await asyncio.sleep(float(exc.retry_after) + 0.5)
                        continue
                    pin_failed += 1
                    log.warning("Pin retries exhausted for Telegram user %s", user_id)
                except TelegramForbiddenError:
                    pin_failed += 1
                    await asyncio.to_thread(deactivate_user, user_id)
                    log.info("Deactivated blocked Telegram user %s after pin failure", user_id)
                    break
                except Exception:
                    pin_failed += 1
                    log.warning("Broadcast delivered but pin failed for Telegram user %s", user_id, exc_info=True)
                    break

    return delivered, failed, pinned, pin_failed

@dp.message()
async def all_messages_handler(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = message.from_user.id

    # A new command cancels an active input flow before the command can be mistaken for user input.
    if is_owner(user_id) and message.text and message.text.startswith("/") and (
        (user_id in pending and pending[user_id].get("settings_chat_id") == message.chat.id) or
        (user_id in pending_broadcast and pending_broadcast[user_id].get("chat_id") == message.chat.id)
    ):
        active_session = pending.get(user_id)
        broadcast_session = pending_broadcast.get(user_id)
        if active_session and active_session.get("settings_message_id"):
            panel_ref = (active_session.get("settings_chat_id", message.chat.id), active_session["settings_message_id"])
        elif broadcast_session and broadcast_session.get("panel_message_id"):
            panel_ref = (broadcast_session.get("chat_id", message.chat.id), broadcast_session["panel_message_id"])
        else:
            panels = owner_ui_state.get(user_id, {}).get("panels", [])
            panel_ref = panels[-1] if panels else None
        await cancel_owner_process(user_id, notify_broadcast=False)
        await cleanup_owner_ui(user_id, keep=panel_ref)
        if panel_ref:
            try:
                await bot.edit_message_text(
                    chat_id=panel_ref[0],
                    message_id=panel_ref[1],
                    text=settings_home_text(),
                    reply_markup=settings_home_keyboard(),
                    link_preview_options={"is_disabled": True},
                )
            except Exception:
                log.debug("Could not restore the owner panel after cancelling an input")
        return

    async def clear_setup_messages(session, input_message: Message | None = None):
        input_ids = list(session.get("input_message_ids", []))
        if input_message is not None and input_message.message_id not in input_ids:
            input_ids.append(input_message.message_id)
        prompt_ids = list(session.get("prompt_message_ids", []))
        if session.get("prompt_message_id") and session["prompt_message_id"] not in prompt_ids:
            prompt_ids.append(session["prompt_message_id"])
        for message_id in input_ids + prompt_ids:
            try:
                await bot.delete_message(session["settings_chat_id"], message_id)
            except Exception:
                log.debug("Could not clear previous /setstart interaction message")

    async def refresh_start_panel(session):
        try:
            if session.get("settings_page") == "buttons":
                cfg_now = await asyncio.to_thread(get_start_settings)
                await bot.edit_message_text(start_buttons_text(cfg_now), chat_id=session["settings_chat_id"],
                    message_id=session["settings_message_id"], reply_markup=start_buttons_keyboard(cfg_now))
            else:
                await refresh_start_settings_message(session["settings_chat_id"], session["settings_message_id"])
        except Exception:
            log.exception("Could not refresh /setstart panel after saving")
    if is_owner(user_id) and user_id in pending and pending[user_id].get("settings_chat_id") == message.chat.id and pending[user_id].get("stage") in (
        "start_caption", "start_media", "start_button_text", "start_button_url"
    ):
        session = pending[user_id]
        session["last_input_message_id"] = message.message_id
        session.setdefault("input_message_ids", []).append(message.message_id)
        stage = session["stage"]
        if stage == "start_caption":
            # Serialize Telegram entities back to HTML so formatting survives MongoDB storage.
            value = message.html_text or message.html_caption
            if not value:
                return await send_owner_prompt(user_id, session, "<b>INVALID CAPTION</b>\nSend text only.")
            try:
                await asyncio.to_thread(
                    bot_settings_collection.update_one,
                    {"_id": START_SETTINGS_ID}, {"$set": {"text": value, "caption_custom": True}}, upsert=True
                )
            except Exception:
                log.exception("Start caption save failed")
                return await send_owner_prompt(user_id, session, "<b>SAVE FAILED</b>\nCaption could not be saved. Please try again.")
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return

        if stage == "start_media":
            if message.photo:
                media_type, media_id = "photo", message.photo[-1].file_id
            elif message.animation:
                media_type, media_id = "animation", message.animation.file_id
            else:
                return await send_owner_prompt(user_id, session, "<b>INVALID MEDIA</b>\nPlease send a photo or GIF.")
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": START_SETTINGS_ID},
                {"$set": {"media_type": media_type, "media_file_id": media_id}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return

        if stage == "start_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await send_owner_prompt(user_id, session, "<b>INVALID BUTTON TEXT</b>\nSend a label between 1 and 64 characters.")
            session["label"] = label
            session["stage"] = "start_button_url"
            await clear_setup_messages(session, message)
            await send_owner_prompt(user_id, session, f"<i>Now send the URL for Start Message Button {session['button']}.</i>")
            return

        if stage == "start_button_url":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await send_owner_prompt(user_id, session, "<b>INVALID LINK</b>\nSend a valid http:// or https:// URL.")
            idx, label = session["button"], session["label"]
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": START_SETTINGS_ID},
                {"$set": {f"button{idx}_text": label, f"button{idx}_url": url}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return


    if is_owner(user_id) and user_id in pending and pending[user_id].get("settings_chat_id") == message.chat.id:
        session = pending[user_id]
        session["last_input_message_id"] = message.message_id
        session.setdefault("input_message_ids", []).append(message.message_id)
        stage = session["stage"]

        if stage == "fixed_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await send_owner_prompt(user_id, session, "<i>Send a button label between 1 and 64 characters.</i>")
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": RESULT_BUTTONS_ID}, {"$set": {"fixed_button_text": label}}, upsert=True
            )
            await clear_owner_interaction_messages(session, message)
            pending.pop(user_id, None)
            cfg = await asyncio.to_thread(get_result_button_settings)
            await edit_owner_panel(session, fixed_button_text(cfg), fixed_button_keyboard())
            return

        if stage == "custom_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await send_owner_prompt(user_id, session, "<i>Send a custom button label between 1 and 64 characters.</i>")
            session["custom_label"] = label
            session["stage"] = "custom_button_url"
            await clear_owner_interaction_messages(session, message)
            await send_owner_prompt(user_id, session, "Now Send Link For Custom Button.")
            return

        if stage == "custom_button_url":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await send_owner_prompt(user_id, session, "<i>Send a valid http:// or https:// link.</i>")
            await asyncio.to_thread(
                bot_settings_collection.update_one,
                {"_id": RESULT_BUTTONS_ID},
                {"$set": {
                    "custom_enabled": True,
                    "custom_button_text": session["custom_label"],
                    "custom_button_url": url,
                }},
                upsert=True,
            )
            await clear_owner_interaction_messages(session, message)
            pending.pop(user_id, None)
            cfg = await asyncio.to_thread(get_result_button_settings)
            await edit_owner_panel(session, result_buttons_text(cfg), result_buttons_keyboard(cfg))
            return

        if stage == "anime_link":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await send_owner_prompt(user_id, session, "<i>Send a valid http:// or https:// link.</i>")
            old_doc = await asyncio.to_thread(
                anime_collection.find_one,
                {"id": session["anime_id"]},
                {"_id": 0, "link": 1},
            )
            if not old_doc:
                return await send_owner_prompt(user_id, session, "<i>Anime record not found. Please reopen its details.</i>")
            old_url = (old_doc.get("link") or "").strip()
            fixed_cfg = await asyncio.to_thread(get_result_button_settings)
            label = button_label_text(fixed_cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=DATA_CHANNEL_ID,
                    message_id=int(session["anime_id"]),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=label, url=url)]
                    ]),
                )
                result = await asyncio.to_thread(
                    anime_collection.update_one,
                    {"id": session["anime_id"]},
                    {"$set": {"link": url}},
                )
                if not result.matched_count:
                    raise RuntimeError("Anime record not found during fixed-link update")
            except Exception:
                log.exception("Could not update anime fixed link")
                try:
                    rollback_markup = (
                        InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text=label, url=old_url)]
                        ])
                        if re.fullmatch(r"https?://[^\s]+", old_url, re.I)
                        else None
                    )
                    await bot.edit_message_reply_markup(
                        chat_id=DATA_CHANNEL_ID,
                        message_id=int(session["anime_id"]),
                        reply_markup=rollback_markup,
                    )
                except Exception:
                    log.exception("Could not roll back canonical post keyboard after failed URL update")
                return await send_owner_prompt(user_id, session, "<i>Could not update the Fixed Button link. Please try again.</i>")
            await clear_owner_interaction_messages(session, message)
            pending.pop(user_id, None)
            await edit_owner_panel(
                session,
                "<b><u>FIXED LINK SAVED</u></b>\n\n<i>This anime's Fixed Button URL has been saved.</i>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❮❮ " + small_caps("back to anime"),
                        callback_data=f"anime:{session['anime_id']}:{session['page']}",
                    )]
                ]),
            )
            return

        if stage == "names":
            names = parse_names(message.text or message.caption or "")
            if not names:
                return await send_owner_prompt(user_id, session, "<i>Send at least one anime name.</i>")
            session["key"] = ", ".join(names)
            # Use the same fuzzy alias matcher as public search to prevent near-duplicate records.
            matches = await find_animes(names)
            duplicates = [name for name in names if matches.get(name)]
            if duplicates:
                await clear_owner_interaction_messages(session, message)
                pending.pop(user_id, None)
                await edit_owner_panel(session, "<i>One or more names already match a saved anime. Use /show to inspect existing records.</i>")
                return
            session["stage"] = "post"
            await clear_owner_interaction_messages(session, message)
            await send_owner_prompt(user_id, session, "Now send the original anime post. The bot will add the Fixed Button automatically.")
            return

        if stage in ("post", "replace_post"):
            try:
                new_id = await bot.copy_message(
                    chat_id=DATA_CHANNEL_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                session["new_id"] = new_id.message_id
                session["stage"] = "replace_fixed_url" if stage == "replace_post" else "fixed_url"
                await clear_owner_interaction_messages(session, message)
                await send_owner_prompt(user_id, session, "Now send the link for Fixed Button.")
            except Exception:
                log.exception("Could not copy anime post to data channel")
                await clear_owner_interaction_messages(session, message)
                pending.pop(user_id, None)
                await edit_owner_panel(session, "<i>Could not save the post. Please try again.</i>")
            return

        if stage in ("fixed_url", "replace_fixed_url"):
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await send_owner_prompt(user_id, session, "<i>Send a valid http:// or https:// link for the Fixed Button.</i>")
            new_message_id = int(session["new_id"])
            session["committing"] = True
            try:
                fixed_cfg = await asyncio.to_thread(get_result_button_settings)
                label = button_label_text(fixed_cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
                await bot.edit_message_reply_markup(
                    chat_id=DATA_CHANNEL_ID,
                    message_id=new_message_id,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=label, url=url)]
                    ]),
                )
                if pending.get(user_id) is not session:
                    session["committing"] = False
                    await delete_pending_data_post(session)
                    return
                if stage == "fixed_url":
                    await asyncio.to_thread(
                        anime_collection.insert_one,
                        {
                            "key": session["key"],
                            "id": str(new_message_id),
                            "link": url,
                        },
                    )
                    success_text = "✨ Anime record and Fixed Button link saved successfully."
                else:
                    result = await asyncio.to_thread(
                        anime_collection.update_one,
                        {"id": session["anime_id"]},
                        {"$set": {"id": str(new_message_id), "link": url}},
                    )
                    if not result.matched_count:
                        raise RuntimeError("Anime record to replace was not found")
                    success_text = "✨ Replacement saved successfully."
            except Exception:
                session["committing"] = False
                log.exception("Anime post/fixed button save failed before database commit")
                await delete_pending_data_post(session)
                await clear_owner_interaction_messages(session, message)
                if pending.get(user_id) is session:
                    pending.pop(user_id, None)
                    try:
                        await edit_owner_panel(session, "<i>Could not save the post and Fixed Button. The existing record was left unchanged.</i>")
                    except Exception:
                        log.exception("Could not refresh owner panel after failed anime save")
                return

            session["committing"] = False
            # The database now points to the new canonical post. Never delete it because
            # a later UI edit failed or the owner cancelled while the database write ran.
            old_anime_id = session.get("anime_id") if stage == "replace_fixed_url" else None
            session.pop("new_id", None)
            if pending.get(user_id) is not session:
                if old_anime_id:
                    try:
                        await bot.delete_message(DATA_CHANNEL_ID, int(old_anime_id))
                    except Exception:
                        log.debug("Could not delete replaced canonical post after successful replacement")
                return
            pending.pop(user_id, None)
            await clear_owner_interaction_messages(session, message)
            if old_anime_id:
                try:
                    await bot.delete_message(DATA_CHANNEL_ID, int(old_anime_id))
                except Exception:
                    log.debug("Could not delete replaced canonical post after successful replacement")
            try:
                await edit_owner_panel(session, success_text)
            except Exception:
                log.exception("Record saved, but the owner panel could not be refreshed")
            return

    if is_owner(user_id) and message.text and message.text.startswith("/") and (
        (user_id in pending and pending[user_id].get("settings_chat_id") == message.chat.id) or
        (user_id in pending_broadcast and pending_broadcast[user_id].get("chat_id") == message.chat.id)
    ):
        active_session = pending.get(user_id)
        broadcast_session = pending_broadcast.get(user_id)
        panel_ref = None
        if active_session and active_session.get("settings_message_id"):
            panel_ref = (active_session.get("settings_chat_id", message.chat.id), active_session["settings_message_id"])
        elif broadcast_session and broadcast_session.get("panel_message_id"):
            panel_ref = (broadcast_session.get("chat_id", message.chat.id), broadcast_session["panel_message_id"])
        else:
            panels = owner_ui_state.get(user_id, {}).get("panels", [])
            if panels:
                panel_ref = panels[-1]
        await cancel_owner_process(user_id, notify_broadcast=False)
        await cleanup_owner_ui(user_id, keep=panel_ref)
        if panel_ref:
            try:
                await bot.edit_message_text(
                    chat_id=panel_ref[0],
                    message_id=panel_ref[1],
                    text=settings_home_text(),
                    reply_markup=settings_home_keyboard(),
                    link_preview_options={"is_disabled": True},
                )
            except Exception:
                log.debug("Could not restore the owner panel after cancelling an input")
        await delete_command_message(message)
        return

    if is_owner(user_id) and user_id in pending_broadcast and pending_broadcast[user_id].get("chat_id") == message.chat.id and not (message.text or "").startswith("/"):
        session = pending_broadcast.pop(user_id)
        task = session.get("task")
        if task:
            task.cancel()
        delivered, failed, pinned, pin_failed = await broadcast_message(message, pin=session["pin"])
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            log.debug("Could not delete broadcast input message")
        if session["pin"]:
            result_text = (
                f"✨ Pin broadcast complete. Delivered: {delivered}. Delivery failed: {failed}. "
                f"Pinned: {pinned}. Pin failed: {pin_failed}."
            )
        else:
            result_text = f"✨ Broadcast complete. Delivered: {delivered}. Failed: {failed}."
        await bot.edit_message_text(
            chat_id=session["chat_id"],
            message_id=session["panel_message_id"],
            text=ui(result_text),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")]
            ]),
            link_preview_options={"is_disabled": True},
        )
        return

    if message.text and not message.text.startswith("/"):
        match = await find_anime(message.text)
        if match:
            if is_owner(user_id) and is_private_chat(message.chat):
                try:
                    await deliver_saved(
                        chat_id=message.chat.id,
                        anime=match,
                        reply_to=message,
                    )
                except Exception:
                    log.exception("Could not deliver saved anime result to owner")
                settings_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=small_caps("replace"), callback_data=f"replace:{match['id']}")],
                    [InlineKeyboardButton(text="❮❮ " + small_caps("back to settings"), callback_data="settings:home")],
                ])
                anime_panel_text = ui(
                    f"<b><i>ANIME SETTINGS</i></b>\n"
                    f"<b>NAMES</b>\n{escape(match.get('key', ''))}\n\n"
                    f"<b>MANAGEMENT</b>\nUse the button below to replace the assigned anime message."
                )
                panel = await open_owner_panel(
                    user_id,
                    message.chat.id,
                    anime_panel_text,
                    settings_keyboard,
                )
                await cleanup_owner_ui(user_id, keep=(panel.chat.id, panel.message_id))
                return
            try:
                await deliver_saved(
                    chat_id=message.chat.id,
                    anime=match,
                    reply_to=message,
                )
            except Exception:
                log.exception("Could not deliver saved anime message")

async def main():
    log.info("Search bot process started; waiting for MongoDB connection")
    while True:
        try:
            await asyncio.to_thread(mongo.admin.command, "ping")
            await asyncio.to_thread(migrate_legacy_users)
            await asyncio.to_thread(anime_collection.create_index, [("id", 1)], name="anime_id_idx")
            await asyncio.to_thread(anime_collection.create_index, [("key", 1)], name="anime_key_idx")
            runtime_state["database"] = "connected"
            log.info("MongoDB connection established; database=%s", DATABASE_NAME)
            break
        except Exception:
            runtime_state["database"] = "connecting"
            log.exception("MongoDB connection failed; retrying in 15 seconds")
            await asyncio.sleep(15)

    while True:
        try:
            global bot_profile
            me = await bot.get_me()
            bot_profile = me
            log.info("Telegram Bot API authenticated as @%s (id=%s)", me.username, me.id)
            await bot.set_my_commands(
                [BotCommand(command="start", description=small_caps("Start the adventure ✨"))],
                scope=BotCommandScopeDefault(),
            )
            owner_commands = [
                BotCommand(command="settings", description=small_caps("Open bot settings")),
                BotCommand(command="add", description=small_caps("Add anime record")),
                BotCommand(command="show", description=small_caps("Show anime records")),
                BotCommand(command="broadcast", description=small_caps("Broadcast a message")),
                BotCommand(command="pbroadcast", description=small_caps("Broadcast and pin a message")),
            ]
            for owner_id in OWNER_IDS:
                await bot.set_my_commands(
                    [BotCommand(command="start", description=small_caps("Start the adventure ✨")), *owner_commands],
                    scope=BotCommandScopeChat(chat_id=int(owner_id)),
                )
            runtime_state["telegram"] = "connected"
            log.info("Aiogram polling starting")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            runtime_state["telegram"] = "reconnecting"
            log.warning("Aiogram polling returned; restarting polling in 3 seconds")
            await asyncio.sleep(3)
        except Exception:
            runtime_state["telegram"] = "reconnecting"
            log.exception("Telegram startup/polling failed; retrying in 15 seconds")
            await asyncio.sleep(15)

threading.Thread(target=run_health_server, daemon=True).start()

if __name__ == "__main__":
    asyncio.run(main())
