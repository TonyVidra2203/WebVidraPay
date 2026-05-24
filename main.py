# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import asyncio
import logging
import time

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils.exceptions import BotBlocked, NetworkError, TelegramAPIError

from config.settings import settings
from db.admin_debts import init_admin_debts_db
from db.cards import init_cards_table
from db.connection import close_db
from db.p2p import init_p2p_db
from db.settings import init_settings_table
from db.sms_events import init_sms_events_db
from db.transactions import init_orders_db
from db.users import init_users_db
from handlers import register_all
from middlewares.block_inactive import BlockInactiveUsersMiddleware
from middlewares.rate_limit import RateLimitMiddleware


# -----------------------------------------------------------------------------
# Раздел: Логирование
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("aiogram.executor").setLevel(logging.ERROR)


# -----------------------------------------------------------------------------
# Раздел: Хендлеры ошибок
# -----------------------------------------------------------------------------
async def handle_bot_blocked(update: types.Update, exception: BotBlocked) -> bool:
    """Игнорирует BotBlocked и не прерывает обработку остальных апдейтов."""
    logging.warning("Бот заблокирован пользователем: %s", exception)
    return True


# -----------------------------------------------------------------------------
# Раздел: Жизненный цикл бота (startup/shutdown)
# -----------------------------------------------------------------------------
async def on_startup(dispatcher: Dispatcher) -> None:
    """Инициализация БД, таблиц и сервисов перед стартом polling."""
    await init_users_db()
    await init_cards_table()
    await init_p2p_db()
    await init_orders_db()
    await init_settings_table()
    await init_sms_events_db()
    await init_admin_debts_db()

    logging.info("Bot started")


async def on_shutdown(dispatcher: Dispatcher) -> None:
    """Корректное завершение работы: закрытие FSM-хранилища и соединений с БД."""
    # Пытаемся корректно закрыть FSM-хранилище
    try:
        await dispatcher.storage.close()
        await dispatcher.storage.wait_closed()
    except Exception:
        logging.exception("Ошибка при закрытии FSM-хранилища")

    # Пытаемся корректно закрыть соединение с БД
    try:
        await close_db()
    except Exception:
        logging.exception("Ошибка при закрытии соединения с БД")

    logging.info("Bot shutdown complete")


async def handle_unexpected_error(update: types.Update, exception: Exception) -> bool:
    """
    Общий обработчик всех неожиданных ошибок в хендлерах.
    Логирует ошибку и не даёт ей прервать работу бота.
    """
    logging.exception(
        "Неожиданная ошибка при обработке апдейта %r: %r",
        update,
        exception,
    )
    # Возвращаем True, чтобы aiogram не пробрасывал исключение дальше
    return True


# -----------------------------------------------------------------------------
# Раздел: Точка входа и устойчивый поллинг
# -----------------------------------------------------------------------------
def main() -> None:
    """Запускает устойчивый polling с экспоненциальным бэкоффом."""
    logging.info("Starting polling")

    backoff = 1
    while True:
        try:
            bot = Bot(token=settings.bot_token, parse_mode="HTML")
            storage = MemoryStorage()
            dp = Dispatcher(bot, storage=storage)

            dp.middleware.setup(BlockInactiveUsersMiddleware())

            dp.middleware.setup(RateLimitMiddleware())

            register_all(dp)

            dp.register_errors_handler(handle_bot_blocked, exception=BotBlocked)
            dp.register_errors_handler(handle_unexpected_error)

            executor.start_polling(
                dp,
                skip_updates=True,
                on_startup=on_startup,
                on_shutdown=on_shutdown,
                timeout=60,
                reset_webhook=True,
            )

            backoff = 1
            break

        except (NetworkError, TelegramAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error("Polling network error: %r. Retry in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

        except Exception:
            logging.exception("Unexpected polling error. Retry in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()