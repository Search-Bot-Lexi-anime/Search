import asyncio
import logging
import os
import re
import threading
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from flask import Flask
from pymongo import MongoClient
from rapidfuzz import fuzz

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
    "text": "<b>Welcome to Lexi Anime Finder!</b>\n<i>Search your favourite anime by sending its name below.</i>",
    "media_type": None, "media_file_id": None,
    "button1_text": "", "button1_url": "",
    "button2_text": "", "button2_url": "",
    "button3_text": "", "button3_url": "",
    "button4_text": "", "button4_url": "",
}

def get_start_settings() -> dict:
    saved = bot_settings_collection.find_one({"_id": START_SETTINGS_ID}) or {}
    return {**DEFAULT_START, **saved}

def start_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    rows = []
    for idx in range(1, 5):
        label, url = cfg.get(f"button{idx}_text"), cfg.get(f"button{idx}_url")
        if label and url and re.match(r"^https?://[^\s]+$", url, re.I):
            rows.append([InlineKeyboardButton(text=small_caps(label)[:64], url=url)])
    rows.append([InlineKeyboardButton(text=small_caps("about"), callback_data="start:about")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def start_settings_keyboard() -> InlineKeyboardMarkup:
    items = [("edit text", "caption"), ("set photo / gif", "media"),
             ("button 1", "button1"), ("button 2", "button2"),
             ("button 3", "button3"), ("button 4", "button4"),
             ("remove media", "remove_media"), ("reset defaults", "reset")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps(label), callback_data=f"startcfg:{action}")]
        for label, action in items
    ])

def about_text(me) -> str:
    return ui(
        "<b>ABOUT LEXI ANIME FINDER</b>\n"
        "<b>HOW IT WORKS</b>\nFind saved anime posts by title, alternate name, partial name or a small typo.\n\n"
        "<b>SEARCH NOTES</b>\nSearch is case-insensitive and uses fuzzy matching to help with incomplete or misspelled titles.\n\n"
        "<b>ABOUT ME</b>\nLexi Anime Finder is made for anime lovers.\n\n"
        "<b>INFORMATION</b>\n"
        f"Bot: @{escape(me.username or 'LexiAnimeFinderBot')}\nVersion: 1.0"
    )

async def send_start_page(message: Message):
    cfg = get_start_settings()
    text = cfg.get("text") or DEFAULT_START["text"]
    markup = start_keyboard(cfg)
    media_type, media_id = cfg.get("media_type"), cfg.get("media_file_id")
    if media_type == "photo" and media_id:
        await message.answer_photo(media_id, caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif media_type == "animation" and media_id:
        await message.answer_animation(media_id, caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML,
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
        callback_data=f"anime:{doc['id']}",
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

@dp.message(Command("setstart"))
async def setstart_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return await message.reply(ui("This command is available to owners only."))
    cfg=get_start_settings()
    await message.reply(ui("<b>START MESSAGE SETTINGS</b>\\nChoose an item to edit.\\n"
        f"Current media: {escape(str(cfg.get('media_type') or 'not set'))}\\nAbout layout is built in."),
        reply_markup=start_settings_keyboard())

@dp.callback_query(F.data.startswith("startcfg:"))
async def startcfg_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.",show_alert=True)
    action=callback.data.split(":",1)[1]
    if action=="reset":
        bot_settings_collection.replace_one({"_id":START_SETTINGS_ID},DEFAULT_START.copy(),upsert=True)
        await callback.answer("Reset")
        return await callback.message.edit_text(ui("<b>START SETTINGS RESET</b>"),reply_markup=start_settings_keyboard())
    if action=="remove_media":
        bot_settings_collection.update_one({"_id":START_SETTINGS_ID},{"$set":{"media_type":None,"media_file_id":None}},upsert=True)
        await callback.answer("Media removed")
        return await callback.message.edit_text(ui("<b>START MESSAGE SETTINGS</b>\\nMedia removed."),reply_markup=start_settings_keyboard())
    pending[callback.from_user.id]={"stage":"start_setting","action":action}
    prompts={"caption":"Send new Start text (HTML supported).","media":"Send a photo or GIF.",
      "button1":"Send Button Name | https://example.com (or REMOVE).","button2":"Send Button Name | https://example.com (or REMOVE).",
      "button3":"Send Button Name | https://example.com (or REMOVE).","button4":"Send Button Name | https://example.com (or REMOVE)."}
    await callback.answer(); await callback.message.answer(ui(prompts[action]))

@dp.callback_query(F.data=="start:about")
async def start_about_callback(callback: CallbackQuery):
    await callback.answer(); body=about_text(await bot.get_me())
    markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=small_caps("back"),callback_data="start:back")]])
    if callback.message.photo or callback.message.animation:
        await callback.message.edit_caption(caption=body,reply_markup=markup,parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(body,reply_markup=markup,parse_mode=ParseMode.HTML,link_preview_options={"is_disabled":True})

@dp.callback_query(F.data=="start:back")
async def start_back_callback(callback: CallbackQuery):
    await callback.answer(); cfg=get_start_settings()
    body=cfg.get("text") or DEFAULT_START["text"]; markup=start_keyboard(cfg)
    if callback.message.photo or callback.message.animation:
        await callback.message.edit_caption(caption=body,reply_markup=markup,parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(body,reply_markup=markup,parse_mode=ParseMode.HTML)

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
    await message.answer(ui(f"<b>SAVED ANIME</b> — {len(docs)} records\nSelect a title to view its details."), reply_markup=anime_list_markup(docs, 0))

@dp.callback_query(F.data.startswith("page:"))
async def page_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    page = int(callback.data.split(":", 1)[1])
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
    await callback.message.edit_reply_markup(reply_markup=anime_list_markup(docs, page))
    await callback.answer()

@dp.callback_query(F.data.startswith("anime:"))
async def anime_detail_callback(callback: CallbackQuery):
    if not callback.from_user or not is_owner(callback.from_user.id):
        return await callback.answer("Owner only.", show_alert=True)
    message_id = callback.data.split(":", 1)[1]
    doc = anime_collection.find_one({"id": message_id}, {"key": 1, "id": 1})
    if not doc:
        return await callback.answer("Record not found.", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=small_caps("replace"), callback_data=f"replace:{message_id}")
    ]])
    await callback.message.answer(
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

    if is_owner(user_id) and user_id in pending and pending[user_id].get("stage") == "start_setting":
        session=pending[user_id]; action=session["action"]
        if action=="caption":
            value=message.text or message.caption
            if not value: return await message.reply(ui("Please send Start text."))
            bot_settings_collection.update_one({"_id":START_SETTINGS_ID},{"$set":{"text":value}},upsert=True)
        elif action=="media":
            if message.photo: mt,mid="photo",message.photo[-1].file_id
            elif message.animation: mt,mid="animation",message.animation.file_id
            else: return await message.reply(ui("Please send a photo or GIF."))
            bot_settings_collection.update_one({"_id":START_SETTINGS_ID},{"$set":{"media_type":mt,"media_file_id":mid}},upsert=True)
        elif action.startswith("button"):
            idx=action[-1]
            if message.text and message.text.strip().lower() in {"remove","none"}: label,url="",""
            elif message.text and "|" in message.text:
                label,url=[x.strip() for x in message.text.split("|",1)]
                if not label or len(label)>64 or not re.match(r"^https?://[^\s]+$",url,re.I):
                    return await message.reply(ui("Invalid. Send Button Name | https://example.com"))
            else: return await message.reply(ui("Send Button Name | https://example.com (or REMOVE)."))
            bot_settings_collection.update_one({"_id":START_SETTINGS_ID},{"$set":{f"button{idx}_text":label,f"button{idx}_url":url}},upsert=True)
        pending.pop(user_id,None)
        return await message.reply(ui("<b>Start settings updated.</b> Use /start to preview."))

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
                        f"<b>ANIME SETTINGS</b>\\n"
                        f"<b>NAMES</b>\\n{escape(match.get('key', ''))}\\n\\n"
                        f"<b>MANAGEMENT</b>\nUse the button below to replace the saved post."
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
            me = await bot.get_me()
            log.info("Telegram Bot API authenticated as @%s (id=%s)", me.username, me.id)
            await bot.set_my_commands([
                BotCommand(command="start", description="Open the bot home"),
                BotCommand(command="add", description="Add anime record"),
                BotCommand(command="show", description="Show anime records"),
                BotCommand(command="broadcast", description="Broadcast a message"),
                BotCommand(command="setstart", description="Configure Start message"),
            ])
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
