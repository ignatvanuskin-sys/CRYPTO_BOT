from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def rules_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принимаю правила", callback_data="accept_rules")]
    ])

def force_close_confirm():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, точно закрыть", callback_data="force_close_confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="force_close_cancel")]
    ])
