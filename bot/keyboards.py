from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from bot.emojis import ENVELOPE_ID


def contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Поделиться номером", request_contact=True, icon_custom_emoji_id=ENVELOPE_ID)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
