import os
import threading
import logging

import discord
from discord import app_commands
from flask import Flask
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("search-bot")

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or os.getenv("TOKEN")
DATABASE_NAME = os.getenv("DATABASE_NAME", "Search")
MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DATA_CHANNEL_ID = os.getenv("DATA_CHANNEL_ID")
OWNER_IDS = {
    value.strip()
    for value in (os.getenv("OWNERS_ID") or os.getenv("OWNER_ID") or "").split(",")
    if value.strip()
}

if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN (or DISCORD_BOT_TOKEN/TOKEN) environment variable")
if not MONGODB_URI:
    raise RuntimeError("Missing MONGODB_URI environment variable")

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
db = mongo[DATABASE_NAME]

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

health_app = Flask(__name__)

@health_app.get("/")
def health():
    return {"status": "ok", "bot": "Search"}, 200

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)

@tree.command(name="add", description="Add command (feature setup pending)")
async def add(interaction: discord.Interaction):
    await interaction.response.send_message(
        "The /add command is registered. Its functionality will be added later.",
        ephemeral=True,
    )

@tree.command(name="show", description="Show command (feature setup pending)")
async def show(interaction: discord.Interaction):
    await interaction.response.send_message(
        "The /show command is registered. Its functionality will be added later.",
        ephemeral=True,
    )

@bot.event
async def on_ready():
    try:
        mongo.admin.command("ping")
        log.info("MongoDB connection successful; database=%s", DATABASE_NAME)
    except Exception:
        log.exception("MongoDB connection failed")
        raise
    await tree.sync()
    log.info("Logged in as %s; slash commands synced", bot.user)

threading.Thread(target=run_health_server, daemon=True).start()
bot.run(TOKEN)
