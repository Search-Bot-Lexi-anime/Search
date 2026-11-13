from html import escape

from aiogram.enums import ParseMode


def about_text(me) -> str:
    return (
        "<i>✦ <b>ᴀʙᴏᴜᴛ Lexi Anime Finder</b> ✦\n\n"
        "<b>Lexi Anime Finder</b> helps you find anime posts quickly. Send an anime title or alternate name, and the bot will search its saved collection and return the matching post.\n\n"
        "<b>✦ Hᴏᴡ ɪᴛ ᴡᴏʀᴋs</b>\nSend an anime name in the bot's chat. Search supports alternate titles and approximate matching. When a match is found, the saved anime post is delivered to you.\n\n"
        "<b>✦ sᴇᴀʀᴄʜ ɴᴏᴛᴇs</b>\nYou can search using an anime's title or saved alternate names. If no matching title is found, try another spelling or title.\n\n"
        "<b>✨ ᴀʙᴏᴜᴛ ᴍᴇ</b>\n\n"
        "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\n"
        f"<b>➤ ɴᴀᴍᴇ : {escape(me.full_name)}</b>\n"
        "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʟᴇxɪ ᴀɴɪᴍᴇ</b>\n"
        "<b>➤ ᴏᴡɴᴇʀ : ʙᴏᴛ ᴏᴡɴᴇʀ</b>\n"
        "<b>➤ ᴍᴀᴅᴇ ғᴏʀ : ᴀɴɪᴍᴇ ʟᴏᴠᴇʀs</b>\n"
        "<b>➤ ᴠᴇʀsɪᴏɴ : ✧ ᴠ 1.0 ⚡</b></i>"
    )
