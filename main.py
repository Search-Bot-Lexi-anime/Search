import os
import io
import re
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from pymongo import MongoClient
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("search-bot")

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_NAME = os.getenv("DATABASE_NAME", "Search")
MONGODB_URI = os.getenv("MONGODB_URI")
DATA_CHANNEL = os.getenv("DATA_CHANNEL")
OWNER_IDS = {part.strip() for part in os.getenv("OWNER_ID", "").split(",") if part.strip().isdigit()}
DATA_CHANNEL_ID = int(DATA_CHANNEL) if DATA_CHANNEL and DATA_CHANNEL.isdigit() else None

if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")
if not MONGODB_URI:
    raise RuntimeError("Missing MONGODB_URI environment variable")
if not DATA_CHANNEL_ID:
    raise RuntimeError("Missing or invalid DATA_CHANNEL environment variable")
if not OWNER_IDS:
    raise RuntimeError("Set OWNER_ID to comma-separated Discord user IDs")

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
db = mongo[DATABASE_NAME]
anime_collection = db["anime"]
users_collection = db["users"]
anime_collection.create_index("key")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
health_app = Flask(__name__)

@health_app.get("/")
def health():
    return {"status": "ok", "bot": "Search"}, 200

def run_health_server():
    health_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), use_reloader=False)

def is_owner(user_id: int) -> bool:
    return str(user_id) in OWNER_IDS

def ui_text(value: str) -> str:
    return f"*{value}*"

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

def find_anime(query: str):
    q = clean_query(query)
    if not q:
        return None
    best_doc, best_score = None, 0
    for doc in anime_collection.find({}, {"key": 1, "id": 1}):
        aliases = [x.strip() for x in doc.get("key", "").split(",") if x.strip()]
        for alias in aliases:
            candidate = clean_query(alias)
            score = max(fuzz.WRatio(q, candidate), fuzz.partial_ratio(q, candidate), fuzz.token_set_ratio(q, candidate))
            if candidate == q:
                return doc
            if score > best_score:
                best_doc, best_score = doc, score
    return best_doc if best_score >= 72 else None

def register_user(user_id: int):
    existing = users_collection.find_one({"_id": "all_users"}, {"id": 1})
    ids = [x for x in (existing or {}).get("id", "").split(",") if x]
    value = str(user_id)
    if value not in ids:
        ids.append(value)
        users_collection.update_one({"_id": "all_users"}, {"$set": {"id": ",".join(ids)}}, upsert=True)

def user_ids():
    doc = users_collection.find_one({"_id": "all_users"}, {"id": 1}) or {}
    return [int(x) for x in doc.get("id", "").split(",") if x.isdigit()]

async def clone_message(source: discord.Message, destination, content_override=None):
    files = []
    async with aiohttp.ClientSession() as session:
        for attachment in source.attachments:
            async with session.get(attachment.url) as response:
                response.raise_for_status()
                data = await response.read()
            files.append(discord.File(io.BytesIO(data), filename=attachment.filename, spoiler=attachment.is_spoiler()))

    view = None
    rows = []
    for row_index, action_row in enumerate(source.components[:5]):
        for component in action_row.children:
            if getattr(component, "type", None) != discord.ComponentType.button:
                continue
            style_value = getattr(component.style, "value", component.style)
            kwargs = {
                "label": component.label,
                "style": discord.ButtonStyle(int(style_value)),
                "disabled": component.disabled,
                "emoji": component.emoji,
                "row": row_index,
            }
            if component.url:
                kwargs["url"] = component.url
            elif component.custom_id:
                kwargs["custom_id"] = component.custom_id
            else:
                continue
            rows.append(kwargs)

    if rows:
        view = discord.ui.View(timeout=None)
        for kwargs in rows:
            try:
                view.add_item(discord.ui.Button(**kwargs))
            except (TypeError, ValueError):
                log.exception("Could not recreate a message button")

    try:
        return await destination.send(
            content=(content_override if content_override is not None else source.content) or None,
            embeds=list(source.embeds) or None,
            files=files or None,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        for file in files:
            try:
                file.close()
            except Exception:
                pass
        raise

def parse_names(value: str):
    names, seen = [], set()
    for item in value.split(","):
        name = item.strip()
        normalized = clean_query(name)
        if name and normalized and normalized not in seen:
            names.append(name)
            seen.add(normalized)
    return names

pending = {}

class ReplaceView(discord.ui.View):
    def __init__(self, doc, owner_id):
        super().__init__(timeout=300)
        self.doc, self.owner_id = doc, owner_id

    @discord.ui.button(label="ʀᴇᴘʟᴀᴄᴇ", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(ui_text("This control is for the owner only."), ephemeral=True)
        pending[self.owner_id] = {"stage": "replace_post", "anime_id": str(self.doc["id"]), "key": self.doc["key"]}
        await interaction.response.send_message(ui_text("Send the new replacement post in this chat. The old post stays active until the new one is stored."), ephemeral=True)

class AnimeSelect(discord.ui.Select):
    def __init__(self, docs, owner_id):
        self.owner_id = owner_id
        options = [discord.SelectOption(label=doc.get("key", "Anime")[:100], value=str(doc["id"])) for doc in docs[:25]]
        super().__init__(placeholder=small_caps("select anime"), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
        doc = anime_collection.find_one({"id": self.values[0]}, {"key": 1, "id": 1})
        if not doc:
            return await interaction.response.send_message(ui_text("Anime record not found."), ephemeral=True)
        details = f"* **ANIME NAMES**\n{doc['key']}\n\n* **SAVED MESSAGE ID**\n{doc['id']}"
        await interaction.response.send_message(details, view=ReplaceView(doc, self.owner_id), ephemeral=True)

class AnimeListView(discord.ui.View):
    def __init__(self, docs, owner_id, page=0):
        super().__init__(timeout=300)
        self.docs, self.owner_id, self.page = docs, owner_id, page
        page_docs = docs[page * 25:(page + 1) * 25]
        if page_docs:
            self.add_item(AnimeSelect(page_docs, owner_id))
        if page > 0:
            self.add_item(self.PrevButton())
        if (page + 1) * 25 < len(docs):
            self.add_item(self.NextButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
            return False
        return True

    class PrevButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="ᴘʀᴇᴠɪᴏᴜs", style=discord.ButtonStyle.secondary)
        async def callback(self, interaction):
            view = self.view
            await interaction.response.edit_message(view=AnimeListView(view.docs, view.owner_id, view.page - 1))

    class NextButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="ɴᴇxᴛ", style=discord.ButtonStyle.secondary)
        async def callback(self, interaction):
            view = self.view
            await interaction.response.edit_message(view=AnimeListView(view.docs, view.owner_id, view.page + 1))

@bot.event
async def on_ready():
    mongo.admin.command("ping")
    await bot.tree.sync()
    log.info("Connected as %s; MongoDB=%s; slash commands synced", bot.user, DATABASE_NAME)

@bot.tree.command(name="add", description="Add an anime name record and its original post")
async def add(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
    pending[interaction.user.id] = {"stage": "names"}
    await interaction.response.send_message(ui_text("Send all alternate anime names in one message, separated by commas. Then send the original post."), ephemeral=True)

@bot.tree.command(name="show", description="Show saved anime records")
async def show(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
    docs = list(anime_collection.find({}, {"key": 1, "id": 1}).sort("key", 1))
    if not docs:
        return await interaction.response.send_message(ui_text("No anime records have been added yet."), ephemeral=True)
    await interaction.response.send_message(f"* **SAVED ANIME** — {len(docs)} records\nSelect an anime to view its details.", view=AnimeListView(docs, interaction.user.id), ephemeral=True)

async def broadcast_source(source: discord.Message, requester: discord.abc.User, content_override=None):
    delivered, failed = 0, 0
    for user_id in user_ids():
        if user_id == requester.id:
            continue
        try:
            target = await bot.fetch_user(user_id)
            await clone_message(source, target, content_override=content_override)
            delivered += 1
        except Exception:
            failed += 1
            log.exception("Broadcast failed for user %s", user_id)
    return delivered, failed

@bot.tree.command(name="broadcast", description="Broadcast a text message to registered users")
@app_commands.describe(text="Message content to broadcast")
async def broadcast(interaction: discord.Interaction, text: Optional[str] = None):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
    if not text:
        return await interaction.response.send_message(ui_text("Use /broadcast with message text, or reply to a message with the /broadcast chat command."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    delivered, failed = 0, 0
    for user_id in user_ids():
        if user_id == interaction.user.id:
            continue
        try:
            target = await bot.fetch_user(user_id)
            await target.send(content=text, allowed_mentions=discord.AllowedMentions.none())
            delivered += 1
        except Exception:
            failed += 1
    await interaction.followup.send(ui_text(f"Broadcast complete. Delivered: {delivered}. Failed: {failed}."), ephemeral=True)

@bot.tree.context_menu(name="Broadcast Message")
async def broadcast_message_context(interaction: discord.Interaction, message: discord.Message):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message(ui_text("Owner only."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    delivered, failed = await broadcast_source(message, interaction.user)
    await interaction.followup.send(ui_text(f"Broadcast complete. Delivered: {delivered}. Failed: {failed}."), ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if is_owner(message.author.id) and message.content.strip().lower().startswith("/broadcast"):
        source = None
        if message.reference and message.reference.message_id:
            try:
                source = await message.channel.fetch_message(message.reference.message_id)
            except Exception:
                source = None
        broadcast_content = None
        if source is None:
            remainder = message.content.strip()[len("/broadcast"):].strip()
            if remainder or message.attachments or message.embeds:
                source = message
                broadcast_content = remainder if remainder else None
        if source is None:
            await message.reply(ui_text("Reply to a message with /broadcast, or add text after /broadcast."), mention_author=False)
            return
        delivered, failed = await broadcast_source(source, message.author, content_override=broadcast_content)
        await message.reply(ui_text(f"Broadcast complete. Delivered: {delivered}. Failed: {failed}."), mention_author=False)
        return

    if is_owner(message.author.id) and message.author.id in pending:
        session = pending[message.author.id]
        if session["stage"] == "names":
            names = parse_names(message.content)
            if not names:
                return await message.reply(ui_text("Send at least one anime name."), mention_author=False)
            session["key"] = ", ".join(names)
            session["stage"] = "post"
            await message.reply(ui_text("Names received. Now send the original message to save in the Data Channel."), mention_author=False)
            return
        if session["stage"] in ("post", "replace_post"):
            try:
                data_channel = bot.get_channel(DATA_CHANNEL_ID) or await bot.fetch_channel(DATA_CHANNEL_ID)
                saved = await clone_message(message, data_channel)
                if session["stage"] == "post":
                    duplicate = False
                    for record in anime_collection.find({}, {"key": 1}):
                        old_names = {clean_query(x) for x in record.get("key", "").split(",")}
                        if any(clean_query(x) in old_names for x in session["key"].split(",") ):
                            duplicate = True
                            break
                    if duplicate:
                        await saved.delete()
                        await message.reply(ui_text("One or more names already exist. Use /show and Replace instead."), mention_author=False)
                    else:
                        anime_collection.insert_one({"key": session["key"], "id": str(saved.id)})
                        await message.reply(ui_text("Anime record saved successfully."), mention_author=False)
                else:
                    anime_collection.update_one({"id": session["anime_id"]}, {"$set": {"id": str(saved.id)}})
                    await message.reply(ui_text("Replacement saved. The new message ID is now active."), mention_author=False)
            except Exception:
                log.exception("Could not save anime post")
                await message.reply(ui_text("Could not save the post. The existing database record was not changed."), mention_author=False)
            finally:
                pending.pop(message.author.id, None)
            return

    register_user(message.author.id)
    if message.content and message.content.startswith(("/", "!")):
        return
    doc = find_anime(message.content or "")
    if doc:
        try:
            data_channel = bot.get_channel(DATA_CHANNEL_ID) or await bot.fetch_channel(DATA_CHANNEL_ID)
            original = await data_channel.fetch_message(int(doc["id"]))
            await clone_message(original, message.channel)
        except Exception:
            log.exception("Anime result delivery failed")
    await bot.process_commands(message)

threading.Thread(target=run_health_server, daemon=True).start()
bot.run(TOKEN)
