from html import escape


def about_text(me) -> str:
    """Build the public About page using Telegram-safe HTML and real line breaks."""
    full_name = escape(str(getattr(me, "full_name", "Search Bot")))
    username = getattr(me, "username", None)

    bot_link = (
        f'<a href="https://t.me/{escape(str(username))}">{full_name}</a>'
        if username
        else full_name
    )
    anime_link = '<a href="https://t.me/lexianime">Lexi Anime</a>'

    return (
        "<b>✨ ᴀʙᴏᴜᴛ ᴍᴇ</b>\n\n"
        "<i>Lexi Anime Finder is a simple companion for discovering your favourite "
        "<b>anime posts</b>. Search for an anime by its title or an alternate name "
        "to find it if it is available. Search understands common title variations "
        "and helps you find the anime you are looking for.</i>\n\n"
        "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\n"
        f"<b>➤ ɴᴀᴍᴇ : {bot_link}</b>\n"
        "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʏᴜᴛᴀ</b>\n"
        "<b>➤ ᴏᴡɴᴇʀ : 𝓚𝔂𝓸𝓴𝓸</b>\n"
        f"<b>➤ ᴍᴀᴅᴇ ғᴏʀ : {anime_link}</b>\n"
        "<b>➤ ᴠᴇʀsɪᴏɴ : ✧ ᴠ 0.1 ⚡</b>"
    )
