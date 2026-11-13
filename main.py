import asyncio
import logging
import os
import re
import threading
from html import escape, unescape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
health_app = Flask(__name__)
runtime_state = {"database": "connecting", "telegram": "starting"}
commands_panels: dict[int, tuple[int, int]] = {}
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

def ui(value: str) -> str:
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
    users_collection.update_one(
        {"_id": "all_users"},
        [{"$set": {
            "id": {
                "$let": {
                    "vars": {"old": {"$ifNull": ["$id", ""]}},
                    "in": {
                        "$cond": [
                            {"$in": [str(user_id), {"$split": ["$$old", ","]}]},
                            "$$old",
                            {"$cond": [{"$eq": ["$$old", ""]}, str(user_id), {"$concat": ["$$old", ",", str(user_id)]}]}
                        ]
                    }
                }
            }
        }}],
        upsert=True,
    )

def get_user_ids() -> list[int]:
    doc = users_collection.find_one({"_id": "all_users"}, {"id": 1}) or {}
    return [int(x) for x in doc.get("id", "").split(",") if x.isdigit()]

def find_anime(query: str):
    q = clean_query(query)
    if not q:
        return None
    best_doc, best_score = None, 0
    for doc in anime_collection.find({}, {"key": 1, "id": 1}):
        for alias in doc.get("key", "").split(","):
            candidate = clean_query(alias)
            if not candidate:
                continue
            if candidate == q:
                return doc
            q_words, c_words = q.split(), candidate.split()
            if candidate.startswith(q) or any(word.startswith(q) for word in c_words):
                score = 100 if len(q) >= 3 else 90
            elif all(any(word.startswith(qw) for word in c_words) for qw in q_words):
                score = 96 if len(q) >= 5 else 86
            else:
                score = max(fuzz.WRatio(q, candidate), fuzz.partial_ratio(q, candidate),
                            fuzz.token_set_ratio(q, candidate), fuzz.ratio(q, candidate))
            if score > best_score:
                best_doc, best_score = doc, score
    threshold = 58 if len(q) >= 7 else 66 if len(q) >= 4 else 82
    return best_doc if best_score >= threshold else None

START_SETTINGS_ID = "start_message"
DEFAULT_START = {
    "_id": START_SETTINGS_ID,
    "text": "<b><i>Welcome, {mention}!</i></b>\n<i>I'm {bot_mention}, your anime search companion. Search an anime name to find it if it is available.</i>\n\n<i>Made for <a href=\"https://t.me/lexianime\">Lexi Anime</a> fans ✨</i>",
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
    bot_mention = f'<a href="https://t.me/{escape(me.username)}">{bot_name}</a>' if me.username else bot_name
    anime = '<a href="https://t.me/lexianime">Lexi Anime</a>'
    for key, value in {
        "{bot_name}": bot_name, "{bot_mention}": bot_mention,
        "{user_name}": user_name, "{mention}": mention, "{anime}": anime,
    }.items():
        template = template.replace(key, value)
    return template

def caption_variables_help() -> str:
    variables = ("{bot_name}", "{bot_mention}", "{user_name}", "{mention}", "{anime}")
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

def start_settings_text(cfg: dict) -> str:
    caption_state = "✅" if cfg.get("caption_custom") else "❌"
    media_state = "✅" if cfg.get("media_type") and cfg.get("media_file_id") else "❌"
    about_state = "Enabled ✅" if cfg.get("about_enabled", True) else "Disabled ❌"
    return ui("<b><u>START MESSAGE</u></b>\n\n"
              "<i>Customize your bot's start message using the following options.</i>\n\n"
              f"<i>Caption: {'Added ✅' if caption_state == '✅' else 'Default ✨'}</i>\n"
              f"<i>Media: {'Added ✅' if media_state == '✅' else 'None ❌'}</i>\n"
              f"<i>About: {about_state}</i>")

def caption_settings_keyboard(has_caption: bool) -> InlineKeyboardMarkup:
    if has_caption:
        rows = [[
            InlineKeyboardButton(text=small_caps("edit"), callback_data="startcfg:caption_edit"),
            InlineKeyboardButton(text=small_caps("show"), callback_data="startcfg:caption_show"),
        ]]
    else:
        rows = [[InlineKeyboardButton(text="✚ " + small_caps("add"), callback_data="startcfg:caption_edit")]]
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
    rows.append([InlineKeyboardButton(text=small_caps("back"), callback_data="startcfg:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_start_page(message: Message):
    cfg = get_start_settings()
    body = await render_start_text(cfg.get("text") or DEFAULT_START["text"], message.from_user)
    markup = start_keyboard(cfg)
    media_type, media_id = cfg.get("media_type"), cfg.get("media_file_id")
    if media_type == "photo" and media_id:
        await message.answer_photo(media_id, caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif media_type == "animation" and media_id:
        await message.answer_animation(media_id, caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await message.answer(body, reply_markup=markup, parse_mode=ParseMode.HTML,
                             link_preview_options={"is_disabled": True})


pending: dict[int, dict] = {}
pending_broadcast: set[int] = set()

async def copy_to_data_channel(message: Message) -> int:
    copied = await bot.copy_message(
        chat_id=DATA_CHANNEL_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=message.reply_markup,
    )
    return copied.message_id

async def deliver_saved(chat_id: int, message_id: int, reply_to: Message | None = None):
    kwargs = {"chat_id": chat_id, "from_chat_id": DATA_CHANNEL_ID, "message_id": message_id}
    if reply_to is not None:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to.message_id, allow_sending_without_reply=True)
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
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user: register_user(message.from_user.id)
    await send_start_page(message)

@dp.message(Command("commands"))
async def commands_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    owner_id = message.from_user.id
    cfg = get_start_settings()

    # Send the fresh panel first; only after it exists, remove the previous panel and command.
    new_panel = await message.answer(
        start_settings_text(cfg),
        reply_markup=start_settings_keyboard(cfg),
    )

    old_panel = commands_panels.get(owner_id)
    commands_panels[owner_id] = (new_panel.chat.id, new_panel.message_id)

    if old_panel:
        try:
            await bot.delete_message(chat_id=old_panel[0], message_id=old_panel[1])
        except Exception:
            log.debug("Could not delete previous /commands panel")

    try:
        await message.delete()
    except Exception:
        log.debug("Could not delete /commands request")


@dp.message(Command("setstart"))
async def setstart_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.reply(ui("<b>OWNER ACCESS ONLY</b>"))
    cfg = get_start_settings()
    await message.reply(start_settings_text(cfg), reply_markup=start_settings_keyboard(cfg))

async def refresh_start_settings_message(chat_id: int, message_id: int):
    cfg = get_start_settings()
    await bot.edit_message_text(
        start_settings_text(cfg),
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=start_settings_keyboard(cfg),
    )

async def refresh_start_settings(callback: CallbackQuery):
    await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("startcfg:"))
async def startcfg_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    action = callback.data.split(":", 1)[1]
    cfg = get_start_settings()

    session = pending.get(callback.from_user.id)
    if session and session.get("settings_message_id") == callback.message.message_id:
        pending.pop(callback.from_user.id, None)
        message_ids = set(session.get("prompt_message_ids", [])) | set(session.get("input_message_ids", []))
        message_ids.update(message_id for message_id in (session.get("prompt_message_id"), session.get("last_input_message_id")) if message_id)
        for message_id in message_ids:
            try:
                await bot.delete_message(session["settings_chat_id"], message_id)
            except Exception:
                log.debug("Could not clear previous /setstart interaction message")

    if action in ("back", "home"):
        return await refresh_start_settings(callback)

    if action == "about_toggle":
        bot_settings_collection.update_one({"_id": START_SETTINGS_ID},
            {"$set": {"about_enabled": not cfg.get("about_enabled", True)}}, upsert=True)
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("About setting updated.")

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
        bot_settings_collection.update_one(
            {"_id": START_SETTINGS_ID}, {"$unset": {"text": "", "caption_custom": ""}}
        )
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("✨ Successfully Restored to default.", show_alert=True)

    if action == "caption_edit":
        pending[callback.from_user.id] = {"stage": "start_caption", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        prompt = await callback.message.answer(
            ui("<i>Send The New Caption text.</i>\n\n" + caption_variables_help())
        )
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
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
        prompt = await callback.message.answer(ui("<i>Only GIFs and images are supported. Send the new start media here.</i>"))
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
        return

    if action == "media_remove":
        bot_settings_collection.update_one(
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
            bot_settings_collection.update_one(
                {"_id": START_SETTINGS_ID},
                {"$unset": {f"button{idx}_text": "", f"button{idx}_url": ""}},
            )
            await callback.message.edit_text(start_buttons_text(get_start_settings()), reply_markup=start_buttons_keyboard(get_start_settings()))
            return await callback.answer(f"Button {idx} disabled.")
        pending[callback.from_user.id] = {"stage": "start_button_text", "button": idx, "settings_page": "buttons", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        prompt = await callback.message.answer(ui(f"<i>Send text for Start Message Button {idx}.</i>"))
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
        return

    await callback.answer("Unknown setting.", show_alert=True)

@dp.callback_query(F.data == "start:about")
async def start_about_callback(callback: CallbackQuery):
    if not get_start_settings().get("about_enabled", True):
        return await callback.answer("About is disabled.", show_alert=True)
    await callback.answer()
    global bot_profile
    if bot_profile is None:
        bot_profile = await bot.get_me()
    body = about_text(bot_profile)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❮❮ " + small_caps("back"), callback_data="start:back")]
    ])
    if callback.message.photo or callback.message.animation:
        await callback.message.edit_caption(caption=body, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(body, reply_markup=markup, parse_mode=ParseMode.HTML,
                                         link_preview_options={"is_disabled": True})

@dp.callback_query(F.data == "start:back")
async def start_back_callback(callback: CallbackQuery):
    await callback.answer()
    cfg = get_start_settings()
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
        return await message.reply(ui("This command is available to owners only."))
    pending[message.from_user.id] = {"stage": "names"}
    await message.reply(ui("Send all alternate anime names, separated by commas or new lines. Then send the original post."))

@dp.message(Command("show"))
async def show_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.reply(ui("This command is available to owners only."))
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
    if not docs:
        return await message.reply(ui("No anime records have been added yet."))
    total_pages = max(1, (len(docs) + 9) // 10)
    await message.answer(
        ui(f"<b>SAVED ANIME</b> — {len(docs)} records\n<i>Page 1 of {total_pages} · Showing 1–{min(10, len(docs))}</i>\nSelect a title to view its details."),
        reply_markup=anime_list_markup(docs, 0),
    )

@dp.callback_query(F.data.startswith("page:"))
async def page_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    page = int(callback.data.split(":", 1)[1])
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
    total_pages = max(1, (len(docs) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    start = page * 10
    end = min(start + 10, len(docs))
    await callback.message.edit_text(
        ui(f"<b>SAVED ANIME</b> — {len(docs)} records\n<i>Page {page + 1} of {total_pages} · Showing {start + 1 if docs else 0}–{end}</i>\nSelect a title to view its details."),
        reply_markup=anime_list_markup(docs, page),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("anime:"))
async def anime_detail_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    _, message_id, page_raw = callback.data.split(":", 2)
    page = int(page_raw)
    doc = anime_collection.find_one({"id": message_id}, {"key": 1, "id": 1})
    if not doc:
        return await callback.answer("Record not found.", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("replace"), callback_data=f"replace:{message_id}")],
        [InlineKeyboardButton(text=small_caps("back"), callback_data=f"page:{page}")],
    ])
    await callback.message.edit_text(
        ui(f"<b>ANIME NAMES</b>\n{escape(doc['key'])}\n\n<b>STATUS</b>\nSaved and ready to replace."),
        reply_markup=keyboard,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("replace:"))
async def replace_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    old_id = callback.data.split(":", 1)[1]
    doc = anime_collection.find_one({"id": old_id}, {"key": 1, "id": 1})
    if not doc:
        return await callback.answer("Record not found.", show_alert=True)
    pending[callback.from_user.id] = {"stage": "replace_post", "anime_id": old_id}
    await callback.message.answer(ui("Send the new original post. The old message ID will be replaced only after the new post is copied successfully."))
    await callback.answer()

@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.reply(ui("This command is available to owners only."))
    if message.reply_to_message:
        delivered, failed = await broadcast_message(message.reply_to_message)
        return await message.reply(ui(f"Broadcast complete. Delivered: {delivered}. Failed: {failed}."))
    pending_broadcast.add(message.from_user.id)
    await message.reply(ui("Now send the message to broadcast. Its media, formatting and inline buttons will be copied."))

async def broadcast_message(source: Message) -> tuple[int, int]:
    delivered = failed = 0
    for user_id in get_user_ids():
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
                reply_markup=source.reply_markup,
            )
            delivered += 1
        except Exception:
            failed += 1
            log.exception("Broadcast failed for Telegram user %s", user_id)
    return delivered, failed

@dp.message()
async def all_messages_handler(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = message.from_user.id
    register_user(user_id)

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
                cfg_now = get_start_settings()
                await bot.edit_message_text(start_buttons_text(cfg_now), chat_id=session["settings_chat_id"],
                    message_id=session["settings_message_id"], reply_markup=start_buttons_keyboard(cfg_now))
            else:
                await refresh_start_settings_message(session["settings_chat_id"], session["settings_message_id"])
        except Exception:
            log.exception("Could not refresh /setstart panel after saving")
    if is_owner(user_id) and user_id in pending and pending[user_id].get("stage") in (
        "start_caption", "start_media", "start_button_text", "start_button_url"
    ):
        session = pending[user_id]
        session["last_input_message_id"] = message.message_id
        session.setdefault("input_message_ids", []).append(message.message_id)
        stage = session["stage"]
        if stage == "start_caption":
            value = message.text or message.caption
            if not value:
                return await message.reply(ui("<b>INVALID CAPTION</b>\nSend text only."))
            try:
                await bot_settings_collection.update_one(
                    {"_id": START_SETTINGS_ID}, {"$set": {"text": value, "caption_custom": True}}, upsert=True
                )
            except Exception:
                log.exception("Start caption save failed")
                return await message.reply(ui("<b>SAVE FAILED</b>\nCaption could not be saved."))
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await message.reply(ui("<i>✨ Successfully Updated New Start Message Caption.</i>"))

        if stage == "start_media":
            if message.photo:
                media_type, media_id = "photo", message.photo[-1].file_id
            elif message.animation:
                media_type, media_id = "animation", message.animation.file_id
            else:
                return await message.reply(ui("<b>INVALID MEDIA</b>\nPlease send a photo or GIF."))
            bot_settings_collection.update_one(
                {"_id": START_SETTINGS_ID},
                {"$set": {"media_type": media_type, "media_file_id": media_id}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await message.reply(ui("<i>✨ Start Message media added successfully.</i>"))

        if stage == "start_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await message.reply(ui("<b>INVALID BUTTON TEXT</b>\nSend a label between 1 and 64 characters."))
            session["label"] = label
            session["stage"] = "start_button_url"
            await clear_setup_messages(session, message)
            prompt = await message.answer(ui(f"<i>Now send the URL for Start Message Button {session['button']}.</i>"))
            session["prompt_message_id"] = prompt.message_id
            session.setdefault("prompt_message_ids", []).append(prompt.message_id)
            return

        if stage == "start_button_url":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await message.reply(ui("<b>INVALID LINK</b>\nSend a valid http:// or https:// URL."))
            idx, label = session["button"], session["label"]
            bot_settings_collection.update_one(
                {"_id": START_SETTINGS_ID},
                {"$set": {f"button{idx}_text": label, f"button{idx}_url": url}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await message.reply(ui(f"<i>✨ Start Message Button {idx} added successfully.</i>"))


    if is_owner(user_id) and user_id in pending:
        session = pending[user_id]
        if session["stage"] == "names":
            names = parse_names(message.text or message.caption or "")
            if not names:
                return await message.reply(ui("Send at least one anime name."))
            session["key"] = ", ".join(names)
            session["stage"] = "post"
            return await message.reply(ui("Names received. Now send the original post with its media and buttons."))
        if session["stage"] in ("post", "replace_post"):
            try:
                new_id = await copy_to_data_channel(message)
                if session["stage"] == "post":
                    existing_names = set()
                    for record in anime_collection.find({}, {"key": 1}):
                        existing_names.update(clean_query(x) for x in record.get("key", "").split(","))
                    if any(clean_query(x) in existing_names for x in session["key"].split(",")):
                        await bot.delete_message(DATA_CHANNEL_ID, new_id)
                        await message.reply(ui("One or more names already exist. Use /show and Replace to update an existing record."))
                    else:
                        anime_collection.insert_one({"key": session["key"], "id": str(new_id)})
                        await message.reply(ui("Anime record saved successfully."))
                else:
                    result = anime_collection.update_one({"id": session["anime_id"]}, {"$set": {"id": str(new_id)}})
                    if result.matched_count:
                        await message.reply(ui("Replacement saved successfully."))
                    else:
                        await bot.delete_message(DATA_CHANNEL_ID, new_id)
                        await message.reply(ui("Original record was not found; existing data was left unchanged."))
            except Exception:
                log.exception("Anime post save/replace failed")
                await message.reply(ui("Could not save the post. The existing database record was not changed."))
            finally:
                pending.pop(user_id, None)
            return

    if is_owner(user_id) and user_id in pending_broadcast:
        pending_broadcast.discard(user_id)
        delivered, failed = await broadcast_message(message)
        return await message.reply(ui(f"Broadcast complete. Delivered: {delivered}. Failed: {failed}."))

    if message.text and not message.text.startswith("/"):
        match = find_anime(message.text)
        if match:
            # Owners receive the management/settings panel instead of the public saved post.
            if is_owner(user_id):
                settings_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=small_caps("replace"),
                        callback_data=f"replace:{match['id']}",
                    )
                ]])
                await message.reply(
                    ui(
                        f"<b>ANIME SETTINGS</b>\n"
                        f"<b>NAMES</b>\n{escape(match.get('key', ''))}\n\n"
                        f"<b>MANAGEMENT</b>\nUse the button below to replace the assigned anime message."
                    ),
                    reply_markup=settings_keyboard,
                )
                return
            try:
                await deliver_saved(
                    chat_id=message.chat.id,
                    message_id=int(match["id"]),
                    reply_to=message,
                )
            except Exception:
                log.exception("Could not deliver saved anime message")

async def main():
    log.info("Search bot process started; waiting for MongoDB connection")
    while True:
        try:
            await asyncio.to_thread(mongo.admin.command, "ping")
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
                BotCommand(command="add", description=small_caps("Add anime record")),
                BotCommand(command="show", description=small_caps("Show anime records")),
                BotCommand(command="broadcast", description=small_caps("Broadcast a message")),
                BotCommand(command="setstart", description=small_caps("Configure start message")),
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
