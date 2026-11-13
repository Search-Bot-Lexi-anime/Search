from html import escape


def about_text(me) -> str:
    bot_name = escape(me.full_name)
    bot_link = f'<a href="https://t.me/{escape(me.username)}">{bot_name}</a>' if me.username else bot_name
    anime_link = '<a href="https://t.me/lexianime">Lexi Anime</a>'
    return (
        "<b>✨ ᴀʙᴏᴜᴛ ᴍᴇ</b>\n\n"
        "<i>Lexi Anime Finder is a simple companion for discovering your favourite <b><i>anime posts</i></b>. "
        "Search for an anime by its title or an alternate name to find the anime if it is available. "
        "Search understands common title variations and helps you find the anime you are looking for.</i>\n\n
        "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\n"
        f"<b>➤ ɴᴀᴍᴇ : {bot_link}</b>\n"
        "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʏᴜᴛᴀ</b>\n"
        "<b>➤ ᴏᴡɴᴇʀ : 𝓚𝔂𝓸𝓴𝓸</b>\n"
        f"<b>➤ ᴍᴀᴅᴇ ғᴏʀ : {anime_link}</b>\n"
        "<b>➤ ᴠᴇʀsɪᴏɴ : ✧ ᴠ 0.1 ⚡</b>"
    )
