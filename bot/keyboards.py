from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

def contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
