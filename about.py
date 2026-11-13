from html import escape


def about_text(me) -> str:
    return (
        "<i><b>ABOUT LEXI ANIME FINDER</b>\n"
        "<b>HOW IT WORKS</b>\nFind saved anime posts by title, alternate name, partial name or a small typo.\n\n"
        "<b>SEARCH NOTES</b>\nSearch is case-insensitive and uses fuzzy matching to help with incomplete or misspelled titles.\n\n"
        "<b>ABOUT ME</b>\nLexi Anime Finder is made for anime lovers.\n\n"
        "<b>INFORMATION</b>\n"
        f"Bot: @{escape(me.username or 'LexiAnimeFinderBot')}\nVersion: 1.0</i>"
    )
