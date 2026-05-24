from aiogram import types
from aiogram.dispatcher.middlewares import BaseMiddleware
from app.db.users import add_user, update_last_active


class UserMiddleware(BaseMiddleware):
    """Middleware для регистрации пользователей.
    Метод on_pre_process_update удалён как неиспользуемый.
    """
    pass
