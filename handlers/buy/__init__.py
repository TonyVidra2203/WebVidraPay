from aiogram import Dispatcher

from .casino import register_casino_handlers
from .p2p import register_p2p_handlers


def register_buy_handlers(dp: Dispatcher):
    register_p2p_handlers(dp)
    register_casino_handlers(dp)