from aiogram import Dispatcher
from keyboards.inline import Callback

from handlers.start import register as register_start
from handlers.chat import register_chat_handlers
from handlers.buy import register_buy_handlers
from handlers.admin import register_admin_handlers
from handlers.profile import register_profile_handlers
from handlers.rules import show_rules
from handlers.support import register_support_handlers
from handlers.accounting import register_accounting_handlers
from handlers.admin_db import register_admin_db_handlers
from handlers.admin_p2p import register_admin_p2p_handlers
from handlers.sms_tg import register as register_sms_tg
from handlers.mastercard import register_mastercard_handlers


def register_all(dp: Dispatcher):
    """Главная точка регистрации всех обработчиков"""

    register_start(dp)
    register_mastercard_handlers(dp)
    register_chat_handlers(dp)
    register_buy_handlers(dp)
    register_admin_handlers(dp)
    register_profile_handlers(dp)
    register_support_handlers(dp)
    register_accounting_handlers(dp)
    register_admin_db_handlers(dp)
    register_admin_p2p_handlers(dp)
    register_sms_tg(dp)

    # правила
    dp.register_callback_query_handler(
        show_rules,
        lambda c: c.data == Callback.RULES,
        state="*"
    )