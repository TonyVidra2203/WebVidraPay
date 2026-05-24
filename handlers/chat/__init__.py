from aiogram import Dispatcher

from .operator import register_operator_handlers
from .instruction import register_instruction_handlers
from .referral import register_referral

def register_chat_handlers(dp: Dispatcher):
    register_operator_handlers(dp)
    register_instruction_handlers(dp)
    register_referral(dp)
