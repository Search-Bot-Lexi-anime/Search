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
setstart_panels: dict[int, tuple[int, int]] = {}
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
RESULT_BUTTONS_ID = "result_buttons"
DEFAULT_FIXED_BUTTON_TEXT = os.getenv("FIXED_BUTTON_TEXT", "· ᴡᴀᴛᴄʜ ɴᴏᴡ ·")

def get_result_button_settings() -> dict:
    saved = bot_settings_collection.find_one({"_id": RESULT_BUTTONS_ID}) or {}
    return {"fixed_button_text": DEFAULT_FIXED_BUTTON_TEXT, "custom_enabled": False,
            "custom_button_text": "", "custom_button_url": "", **saved}

def button_label_text(value: str) -> str:
    # Telegram inline keyboard labels are plain text and cannot render HTML.
    return unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()[:64]

def result_keyboard_from_source(source_markup, cfg: dict):
    fixed_url = None
    if source_markup and getattr(source_markup, "inline_keyboard", None):
        for row in source_markup.inline_keyboard:
            for button in row:
                if getattr(button, "url", None):
                    fixed_url = button.url
                    break
            if fixed_url:
                break
    rows = []
    label = button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
    if fixed_url and label:
        rows.append([InlineKeyboardButton(text=label, url=fixed_url)])
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

async def send_owner_prompt(user_id: int, session: dict, text: str) -> Message:
    prompt = await bot.send_message(chat_id=session["settings_chat_id"], text=ui(text))
    session.setdefault("prompt_message_ids", []).append(prompt.message_id)
    session["prompt_message_id"] = prompt.message_id
    await track_owner_ui(user_id, prompt)
    return prompt

async def clear_pending_for_panel(user_id: int, panel_message_id: int):
    session = pending.get(user_id)
    if session and session.get("settings_message_id") == panel_message_id:
        pending.pop(user_id, None)
        await clear_owner_interaction_messages(session)
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
        [InlineKeyboardButton(text=small_caps("custom button"), callback_data="resultcfg:home")],
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
    return ui("<b><i><u>START BUTTONS</u></i></b>\n\n<i>Customize buttons shown with your start message.</i>\n\n" + statuses)

def result_buttons_text(cfg: dict) -> str:
    custom_state = "Enabled ✅" if cfg.get("custom_enabled") else "Disabled ❌"
    label = escape(button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT))
    return ("<b><u>CUSTOM BUTTON</u></b>\n\n"
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
        [InlineKeyboardButton(text=small_caps("back"), callback_data="resultcfg:back")],
    ])

def fixed_button_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps("edit"), callback_data="resultcfg:fixed_edit"),
         InlineKeyboardButton(text=small_caps("see"), callback_data="resultcfg:fixed_see")],
        [InlineKeyboardButton(text=small_caps("default"), callback_data="resultcfg:fixed_default")],
        [InlineKeyboardButton(text=small_caps("back"), callback_data="resultcfg:home")],
    ])

def start_settings_text(cfg: dict) -> str:
    caption_state = "Added ✅" if cfg.get("caption_custom") else "Default ✨"
    media_state = "Added ✅" if cfg.get("media_type") and cfg.get("media_file_id") else "None ❌"
    about_state = "Enabled ✅" if cfg.get("about_enabled", True) else "Disabled ❌"
    return ("<b><i>START MESSAGE</i></b>\n\n"
            "<i>Customize your bot's start message using the following options.</i>\n\n"
            f"<i>Caption: {caption_state}</i>\n"
            f"<i>Media: {media_state}</i>\n"
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
        if notify:
            await bot.send_message(session["chat_id"], ui("✨ Broadcast cancelled."))

async def cancel_owner_process(user_id: int, notify_broadcast: bool = False):
    session = pending.pop(user_id, None)
    if session:
        await clear_owner_interaction_messages(session)
    await cancel_broadcast_wait(user_id, notify=notify_broadcast)

async def begin_owner_command(user_id: int):
    session = pending.pop(user_id, None)
    await cancel_broadcast_wait(user_id)
    return session

async def finish_owner_command(user_id: int, previous_session: dict | None, keep: tuple[int, int]):
    if previous_session:
        await clear_owner_interaction_messages(previous_session)
    await cleanup_owner_ui(user_id, keep=keep)

async def track_owner_ui(user_id: int, message: Message, kind: str = "prompt"):
    state = owner_ui_state.setdefault(user_id, {"prompts": [], "panels": []})
    state["panels" if kind == "panel" else "prompts"].append((message.chat.id, message.message_id))

async def cleanup_owner_ui(user_id: int, keep: tuple[int, int] | None = None):
    state = owner_ui_state.setdefault(user_id, {"prompts": [], "panels": []})
    old_items = state["prompts"] + state["panels"]
    state["prompts"], state["panels"] = [], []
    for chat_id, message_id in old_items:
        if keep == (chat_id, message_id):
            state["panels"].append((chat_id, message_id))
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            log.debug("Could not delete previous owner prompt/panel")

async def start_broadcast_wait(user_id: int, chat_id: int, pin: bool):
    await cancel_owner_process(user_id)
    session = {"chat_id": chat_id, "pin": pin}
    pending_broadcast[user_id] = session

    async def expire():
        try:
            await asyncio.sleep(30)
            if pending_broadcast.get(user_id) is session:
                pending_broadcast.pop(user_id, None)
                await cleanup_owner_ui(user_id)
                await bot.send_message(chat_id, ui("✨ Broadcast cancelled due to 30 seconds of inactivity."))
        except asyncio.CancelledError:
            pass

    session["task"] = asyncio.create_task(expire())

async def copy_to_data_channel(message: Message, fixed_url: str) -> int:
    copied = await bot.copy_message(
        chat_id=DATA_CHANNEL_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    label = button_label_text(get_result_button_settings().get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
    await bot.edit_message_reply_markup(
        chat_id=DATA_CHANNEL_ID,
        message_id=copied.message_id,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, url=fixed_url)]
        ]),
    )
    return copied.message_id

async def deliver_saved(chat_id: int, message_id: int, reply_to: Message | None = None):
    # The fixed URL is stored only in the canonical channel post's inline keyboard.
    kwargs = {"chat_id": chat_id, "from_chat_id": DATA_CHANNEL_ID, "message_id": message_id}
    if reply_to is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=reply_to.message_id,
            allow_sending_without_reply=True,
        )
    forwarded = await bot.forward_message(**kwargs)
    markup = result_keyboard_from_source(forwarded.reply_markup, get_result_button_settings())
    if markup:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=forwarded.message_id, reply_markup=markup
        )

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
    owner_id = message.from_user.id if message.from_user and is_owner(message.from_user.id) else None
    previous_session = None
    if message.from_user:
        if owner_id is not None:
            previous_session = await begin_owner_command(owner_id)
        else:
            await cancel_broadcast_wait(message.from_user.id, notify=True)
        register_user(message.from_user.id)
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
    owner_id = message.from_user.id
    previous_session = await begin_owner_command(owner_id)
    cfg = get_start_settings()

    # Send the fresh panel first; only after it exists, remove the previous panel and command.
    # Use send_message directly: never inherit/reuse the incoming command as a reply target.
    new_panel = await bot.send_message(
        chat_id=message.chat.id,
        text=start_settings_text(cfg),
        reply_markup=start_settings_keyboard(cfg),
    )

    old_panel = commands_panels.get(owner_id)
    commands_panels[owner_id] = (new_panel.chat.id, new_panel.message_id)
    await track_owner_ui(owner_id, new_panel, "panel")
    await finish_owner_command(owner_id, previous_session, (new_panel.chat.id, new_panel.message_id))

    # The fresh panel is already visible. Now clean up the command and previous panel.
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        log.debug("Could not delete /commands request")

    if old_panel:
        try:
            await bot.delete_message(chat_id=old_panel[0], message_id=old_panel[1])
        except Exception:
            log.debug("Could not delete previous /commands panel")


@dp.message(Command("setstart"))
async def setstart_handler(message: Message):
    await commands_handler(message)

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

    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
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
        return await callback.answer("✨ About setting updated.")

    if action == "buttons":
        await callback.message.edit_text(start_buttons_text(cfg), reply_markup=start_buttons_keyboard(cfg))
        return await callback.answer()

    if action == "caption":
        await callback.message.edit_text(
            ui("<b><i><u>CAPTION</u></i></b>\n\n<i>Customize your bot's start message Caption.</i>"),
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
        prompt = await bot.send_message(chat_id=callback.message.chat.id, text=
            ui("<i>Send The New Caption text.</i>\n\n" + caption_variables_help())
        )
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await track_owner_ui(callback.from_user.id, prompt)
        return

    if action == "media":
        has_media = bool(cfg.get("media_type") and cfg.get("media_file_id"))
        await callback.message.edit_text(
            ui("<b><i><u>MEDIA</u></i></b>\n\n<i>Include an image or GIF with your start message.</i>"),
            reply_markup=media_settings_keyboard(has_media)
        )
        return await callback.answer()

    if action == "media_edit":
        pending[callback.from_user.id] = {"stage": "start_media", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        prompt = await bot.send_message(chat_id=callback.message.chat.id, text=ui("<i>Only GIFs and images are supported. Send the new start media here.</i>"))
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await track_owner_ui(callback.from_user.id, prompt)
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
            return await callback.answer(f"✨ Button {idx} disabled.")
        pending[callback.from_user.id] = {"stage": "start_button_text", "button": idx, "settings_page": "buttons", "settings_chat_id": callback.message.chat.id, "settings_message_id": callback.message.message_id, "input_message_ids": [], "prompt_message_ids": []}
        await callback.answer()
        prompt = await bot.send_message(chat_id=callback.message.chat.id, text=ui(f"<i>Send text for Start Message Button {idx}.</i>"))
        pending[callback.from_user.id]["prompt_message_id"] = prompt.message_id
        pending[callback.from_user.id]["prompt_message_ids"].append(prompt.message_id)
        await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
        await track_owner_ui(callback.from_user.id, prompt)
        return

    await callback.answer("Unknown setting.", show_alert=True)

@dp.callback_query(F.data.startswith("resultcfg:"))
async def resultcfg_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
    action = callback.data.split(":", 1)[1]
    cfg = get_result_button_settings()

    if action == "home":
        await callback.message.edit_text(result_buttons_text(cfg), reply_markup=result_buttons_keyboard(cfg))
        return await callback.answer()
    if action == "back":
        await refresh_start_settings_message(callback.message.chat.id, callback.message.message_id)
        return await callback.answer()
    if action == "fixed":
        await callback.message.edit_text(fixed_button_text(cfg), reply_markup=fixed_button_keyboard())
        return await callback.answer()
    if action == "fixed_see":
        label = button_label_text(cfg.get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
        return await callback.answer(label[:190], show_alert=True)
    if action == "fixed_default":
        bot_settings_collection.update_one(
            {"_id": RESULT_BUTTONS_ID}, {"$unset": {"fixed_button_text": ""}}, upsert=True
        )
        cfg = get_result_button_settings()
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
            bot_settings_collection.update_one(
                {"_id": RESULT_BUTTONS_ID}, {"$set": {"custom_enabled": False}}, upsert=True
            )
            cfg = get_result_button_settings()
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
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    previous_session = await begin_owner_command(message.from_user.id)
    pending[message.from_user.id] = {
        "stage": "names",
        "settings_chat_id": message.chat.id,
        "input_message_ids": [],
        "prompt_message_ids": [],
    }
    prompt = await send_owner_prompt(
        message.from_user.id, pending[message.from_user.id],
        "<b><i>ADD ANIME</i></b>\n\n<i>Send all alternate anime names, separated by commas or new lines. Matching will connect every alias to the saved post.</i>"
    )
    await finish_owner_command(message.from_user.id, previous_session, (prompt.chat.id, prompt.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /add command")

@dp.message(Command("show"))
async def show_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    previous_session = await begin_owner_command(message.from_user.id)
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
    if not docs:
        prompt = await bot.send_message(chat_id=message.chat.id, text=ui("No anime records have been added yet."))
        await track_owner_ui(message.from_user.id, prompt)
        await finish_owner_command(message.from_user.id, previous_session, (prompt.chat.id, prompt.message_id))
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            log.debug("Could not delete /show command")
        return
    total_pages = max(1, (len(docs) + 9) // 10)
    panel = await bot.send_message(
        chat_id=message.chat.id,
        text=ui(f"<b><i>SAVED ANIME</i></b> — {len(docs)} records\n<i>Page 1 of {total_pages} · Showing 1–{min(10, len(docs))}</i>\n<i>Select a title to view its details.</i>"),
        reply_markup=anime_list_markup(docs, 0),
    )
    await track_owner_ui(message.from_user.id, panel, "panel")
    await finish_owner_command(message.from_user.id, previous_session, (panel.chat.id, panel.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /show command")

@dp.callback_query(F.data.startswith("page:"))
async def page_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
    page = int(callback.data.split(":", 1)[1])
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
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
    await clear_pending_for_panel(callback.from_user.id, callback.message.message_id)
    await cleanup_owner_ui(callback.from_user.id, keep=(callback.message.chat.id, callback.message.message_id))
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
        ui(f"<b><i>ANIME NAMES</i></b>\n{escape(doc['key'])}\n\n<b><i>STATUS</i></b>\n<i>Saved and ready to replace.</i>"),
        reply_markup=keyboard,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("replace:"))
async def replace_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    previous_session = await begin_owner_command(callback.from_user.id)
    old_id = callback.data.split(":", 1)[1]
    doc = anime_collection.find_one({"id": old_id}, {"key": 1, "id": 1})
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
    previous_session = await begin_owner_command(message.from_user.id)
    if message.reply_to_message:
        delivered, failed, _, _ = await broadcast_message(message.reply_to_message)
        result = await bot.send_message(chat_id=message.chat.id, text=ui(f"✨ Broadcast complete. Delivered: {delivered}. Failed: {failed}."))
        await track_owner_ui(message.from_user.id, result)
        await finish_owner_command(message.from_user.id, previous_session, (result.chat.id, result.message_id))
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            log.debug("Could not delete /broadcast command")
        return
    await start_broadcast_wait(message.from_user.id, message.chat.id, pin=False)
    prompt = await bot.send_message(chat_id=message.chat.id, text=ui("Now send the message to broadcast. Its media, formatting and inline buttons will be copied. You have 30 seconds."))
    await track_owner_ui(message.from_user.id, prompt)
    await finish_owner_command(message.from_user.id, previous_session, (prompt.chat.id, prompt.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /broadcast command")

@dp.message(Command("pbroadcast"))
async def pin_broadcast_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await bot.send_message(chat_id=message.chat.id, text=ui("This command is available to owners only."))
    previous_session = await begin_owner_command(message.from_user.id)
    if message.reply_to_message:
        delivered, failed, pinned, pin_failed = await broadcast_message(message.reply_to_message, pin=True)
        result = await bot.send_message(chat_id=message.chat.id, text=ui(
            f"✨ Pin broadcast complete. Delivered: {delivered}. Delivery failed: {failed}. "
            f"Pinned: {pinned}. Pin failed: {pin_failed}."
        ))
        await track_owner_ui(message.from_user.id, result)
        await finish_owner_command(message.from_user.id, previous_session, (result.chat.id, result.message_id))
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            log.debug("Could not delete /pbroadcast command")
        return
    await start_broadcast_wait(message.from_user.id, message.chat.id, pin=True)
    prompt = await bot.send_message(chat_id=message.chat.id, text=ui("Now send the message to broadcast. Its media, formatting and inline buttons will be copied. You have 30 seconds."))
    await track_owner_ui(message.from_user.id, prompt)
    await finish_owner_command(message.from_user.id, previous_session, (prompt.chat.id, prompt.message_id))
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        log.debug("Could not delete /pbroadcast command")

async def broadcast_message(source: Message, pin: bool = False) -> tuple[int, int, int, int]:
    delivered = failed = pinned = pin_failed = 0
    for user_id in get_user_ids():
        try:
            copied = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
                reply_markup=source.reply_markup,
            )
            delivered += 1
        except Exception:
            failed += 1
            log.exception("Broadcast failed for Telegram user %s", user_id)
            continue

        if pin:
            try:
                await bot.pin_chat_message(
                    chat_id=user_id,
                    message_id=copied.message_id,
                    disable_notification=True,
                )
                pinned += 1
            except Exception:
                pin_failed += 1
                log.warning("Broadcast delivered but pin failed for Telegram user %s", user_id, exc_info=True)

    return delivered, failed, pinned, pin_failed

@dp.message()
async def all_messages_handler(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = message.from_user.id
    register_user(user_id)

    # Unknown commands must never be consumed as input for an active owner workflow.
    if is_owner(user_id) and message.text and message.text.startswith("/") and (user_id in pending or user_id in pending_broadcast):
        await cancel_owner_process(user_id, notify_broadcast=True)
        await cleanup_owner_ui(user_id)
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
                return await bot.send_message(chat_id=message.chat.id, text=ui("<b>INVALID CAPTION</b>\nSend text only."))
            try:
                await bot_settings_collection.update_one(
                    {"_id": START_SETTINGS_ID}, {"$set": {"text": value, "caption_custom": True}}, upsert=True
                )
            except Exception:
                log.exception("Start caption save failed")
                return await bot.send_message(chat_id=message.chat.id, text=ui("<b>SAVE FAILED</b>\nCaption could not be saved."))
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await bot.send_message(chat_id=message.chat.id, text=ui("<i>✨ Successfully Updated New Start Message Caption.</i>"))

        if stage == "start_media":
            if message.photo:
                media_type, media_id = "photo", message.photo[-1].file_id
            elif message.animation:
                media_type, media_id = "animation", message.animation.file_id
            else:
                return await bot.send_message(chat_id=message.chat.id, text=ui("<b>INVALID MEDIA</b>\nPlease send a photo or GIF."))
            bot_settings_collection.update_one(
                {"_id": START_SETTINGS_ID},
                {"$set": {"media_type": media_type, "media_file_id": media_id}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await bot.send_message(chat_id=message.chat.id, text=ui("<i>✨ Start Message media added successfully.</i>"))

        if stage == "start_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await bot.send_message(chat_id=message.chat.id, text=ui("<b>INVALID BUTTON TEXT</b>\nSend a label between 1 and 64 characters."))
            session["label"] = label
            session["stage"] = "start_button_url"
            await clear_setup_messages(session, message)
            prompt = await bot.send_message(chat_id=message.chat.id, text=ui(f"<i>Now send the URL for Start Message Button {session['button']}.</i>"))
            session["prompt_message_id"] = prompt.message_id
            session.setdefault("prompt_message_ids", []).append(prompt.message_id)
            return

        if stage == "start_button_url":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await bot.send_message(chat_id=message.chat.id, text=ui("<b>INVALID LINK</b>\nSend a valid http:// or https:// URL."))
            idx, label = session["button"], session["label"]
            bot_settings_collection.update_one(
                {"_id": START_SETTINGS_ID},
                {"$set": {f"button{idx}_text": label, f"button{idx}_url": url}},
                upsert=True
            )
            pending.pop(user_id, None)
            await clear_setup_messages(session, message)
            await refresh_start_panel(session)
            return await bot.send_message(chat_id=message.chat.id, text=ui(f"<i>✨ Start Message Button {idx} added successfully.</i>"))


    if is_owner(user_id) and user_id in pending:
        session = pending[user_id]
        session["last_input_message_id"] = message.message_id
        session.setdefault("input_message_ids", []).append(message.message_id)
        stage = session["stage"]

        if stage == "fixed_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await bot.send_message(chat_id=message.chat.id, text=ui("<i>Send a button label between 1 and 64 characters.</i>"))
            bot_settings_collection.update_one(
                {"_id": RESULT_BUTTONS_ID}, {"$set": {"fixed_button_text": label}}, upsert=True
            )
            await clear_owner_interaction_messages(session, message)
            pending.pop(user_id, None)
            cfg = get_result_button_settings()
            await bot.edit_message_text(
                fixed_button_text(cfg), chat_id=session["settings_chat_id"],
                message_id=session["settings_message_id"], reply_markup=fixed_button_keyboard()
            )
            notice = await bot.send_message(chat_id=message.chat.id, text=ui("✨ Successfully Updated New Fixed Button Text."))
            await track_owner_ui(user_id, notice)
            return

        if stage == "custom_button_text":
            label = (message.text or "").strip()
            if not label or len(label) > 64:
                return await bot.send_message(chat_id=message.chat.id, text=ui("<i>Send a custom button label between 1 and 64 characters.</i>"))
            session["custom_label"] = label
            session["stage"] = "custom_button_url"
            await clear_owner_interaction_messages(session, message)
            await send_owner_prompt(user_id, session, "Now Send Link For Custom Button.")
            return

        if stage == "custom_button_url":
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await bot.send_message(chat_id=message.chat.id, text=ui("<i>Send a valid http:// or https:// link.</i>"))
            bot_settings_collection.update_one(
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
            cfg = get_result_button_settings()
            await bot.edit_message_text(
                result_buttons_text(cfg), chat_id=session["settings_chat_id"],
                message_id=session["settings_message_id"], reply_markup=result_buttons_keyboard(cfg)
            )
            notice = await bot.send_message(chat_id=message.chat.id, text=ui("✨ Start Message Button Added Successfully.."))
            await track_owner_ui(user_id, notice)
            return

        if stage == "names":
            names = parse_names(message.text or message.caption or "")
            if not names:
                return await bot.send_message(chat_id=message.chat.id, text=ui("<i>Send at least one anime name.</i>"))
            session["key"] = ", ".join(names)
            # Use the same fuzzy alias matcher as public search to prevent near-duplicate records.
            duplicates = [name for name in names if find_anime(name)]
            if duplicates:
                await clear_owner_interaction_messages(session, message)
                pending.pop(user_id, None)
                notice = await bot.send_message(chat_id=message.chat.id, text=ui("<i>One or more names already match a saved anime. Use /show to inspect existing records.</i>"))
                await track_owner_ui(user_id, notice)
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
                notice = await bot.send_message(chat_id=message.chat.id, text=ui("<i>Could not save the post. Please try again.</i>"))
                await track_owner_ui(user_id, notice)
            return

        if stage in ("fixed_url", "replace_fixed_url"):
            url = (message.text or "").strip()
            if not re.fullmatch(r"https?://[^\s]+", url, re.I):
                return await bot.send_message(chat_id=message.chat.id, text=ui("<i>Send a valid http:// or https:// link for the Fixed Button.</i>"))
            try:
                label = button_label_text(get_result_button_settings().get("fixed_button_text") or DEFAULT_FIXED_BUTTON_TEXT)
                await bot.edit_message_reply_markup(
                    chat_id=DATA_CHANNEL_ID,
                    message_id=session["new_id"],
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=label, url=url)]
                    ]),
                )
                if stage == "fixed_url":
                    anime_collection.insert_one({"key": session["key"], "id": str(session["new_id"])})
                    success_text = "✨ Anime record saved successfully."
                else:
                    result = anime_collection.update_one(
                        {"id": session["anime_id"]}, {"$set": {"id": str(session["new_id"])}}
                    )
                    if not result.matched_count:
                        raise RuntimeError("Anime record to replace was not found")
                    try:
                        await bot.delete_message(DATA_CHANNEL_ID, int(session["anime_id"]))
                    except Exception:
                        log.debug("Could not delete replaced canonical post")
                    success_text = "✨ Replacement saved successfully."
                await clear_owner_interaction_messages(session, message)
                pending.pop(user_id, None)
                notice = await bot.send_message(chat_id=message.chat.id, text=ui(success_text))
                await track_owner_ui(user_id, notice)
            except Exception:
                log.exception("Anime post/fixed button save failed")
                try:
                    await bot.delete_message(DATA_CHANNEL_ID, session["new_id"])
                except Exception:
                    pass
                await clear_owner_interaction_messages(session, message)
                pending.pop(user_id, None)
                notice = await bot.send_message(chat_id=message.chat.id, text=ui("<i>Could not save the post and Fixed Button. The existing record was left unchanged.</i>"))
                await track_owner_ui(user_id, notice)
            return

    if is_owner(user_id) and message.text and message.text.startswith("/"):
        await cancel_owner_process(user_id, notify_broadcast=True)
        await cleanup_owner_ui(user_id)
        return

    if is_owner(user_id) and user_id in pending_broadcast and not (message.text or "").startswith("/"):
        session = pending_broadcast.pop(user_id)
        task = session.get("task")
        if task:
            task.cancel()
        delivered, failed, pinned, pin_failed = await broadcast_message(message, pin=session["pin"])
        if session["pin"]:
            result = await bot.send_message(chat_id=message.chat.id, text=ui(
                f"✨ Pin broadcast complete. Delivered: {delivered}. Delivery failed: {failed}. "
                f"Pinned: {pinned}. Pin failed: {pin_failed}."
            ))
        else:
            result = await bot.send_message(chat_id=message.chat.id, text=ui(f"✨ Broadcast complete. Delivered: {delivered}. Failed: {failed}."))
        await cleanup_owner_ui(user_id, keep=(result.chat.id, result.message_id))
        await track_owner_ui(user_id, result)
        return

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
                await bot.send_message(chat_id=message.chat.id, text=
                    ui(
                        f"<b><i>ANIME SETTINGS</i></b>\n"
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
                BotCommand(command="settings", description=small_caps("Open bot settings")),
                BotCommand(command="commands", description=small_caps("Open bot settings")),
                BotCommand(command="setstart", description=small_caps("Open bot settings")),
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
