# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import logging
from typing import Optional, Union

from aiogram import Bot, types
from aiogram.utils import exceptions as tg_exc


# -----------------------------------------------------------------------------
# Раздел: Логирование
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Раздел: Безопасные операции с сообщениями
# -----------------------------------------------------------------------------
async def safe_delete(
    bot: Bot,
    chat_id: Union[int, str, None],
    message_id: Optional[int],
) -> bool:
    """
    Безопасное удаление сообщения.

    - Игнорирует пустые chat_id/message_id.
    - Не падает при MessageToDeleteNotFound / MessageCantBeDeleted.
    - Не засоряет логи ошибок.
    Возвращает True, если удаление прошло успешно, иначе False.
    """
    if not chat_id or not message_id:
        return False

    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except (tg_exc.MessageToDeleteNotFound, tg_exc.MessageCantBeDeleted):
        logger.debug(
            "safe_delete: message not found or can't be deleted (chat=%s, msg=%s)",
            chat_id,
            message_id,
        )
        return False
    except tg_exc.ChatNotFound:
        logger.debug("safe_delete: chat not found (chat=%s)", chat_id)
        return False
    except tg_exc.BotBlocked:
        logger.info("safe_delete: bot is blocked by user (chat=%s)", chat_id)
        return False
    except Exception as e:
        logger.debug(
            "safe_delete: delete failed (chat=%s, msg=%s): %r", chat_id, message_id, e
        )
        return False


async def safe_edit(message: types.Message, reply_markup: Optional[types.InlineKeyboardMarkup] = None) -> None:
    """Безопасно редактирует клавиатуру у сообщения."""
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except Exception:
        logger.exception("safe_edit: edit_markup failed")


async def bot_send(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
) -> types.Message:
    """Отправка сообщения с защитой от типовых ошибок."""
    return await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
