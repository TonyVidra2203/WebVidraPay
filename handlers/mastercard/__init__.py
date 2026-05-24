from aiogram import Dispatcher

from handlers.mastercard.cards import register_mastercard_card_handlers
from handlers.mastercard.limits import register_mastercard_limit_handlers
from handlers.mastercard.menu import register_mastercard_menu_handlers


def register_mastercard_handlers(dp: Dispatcher) -> None:
    """Регистрирует все обработчики раздела MasterCard."""
    register_mastercard_card_handlers(dp)
    register_mastercard_limit_handlers(dp)
    register_mastercard_menu_handlers(dp)