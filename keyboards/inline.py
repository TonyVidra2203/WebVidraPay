# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------

from typing import Final, Optional, Sequence, Tuple

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


# -----------------------------------------------------------------------------
# Раздел: Константы callback-данных
# -----------------------------------------------------------------------------

class Callback:
    """
    Идентификаторы callback-данных для инлайн-кнопок.
    """

    EXPRESS_CARD: Final[str] = "express_card"
    P2P_ASSISTANT: Final[str] = "p2p_assistant"
    P2P_ASSISTANT_LTC: Final[str] = "p2p_assistant_ltc"
    P2P_ASSISTANT_USDT: Final[str] = "p2p_assistant_usdt"
    P2P_ASSISTANT_XMR: Final[str] = "p2p_assistant_xmr"
    EXCHANGE_SWAP: Final[str] = "exchange_swap"

    CANCEL_BUY: Final[str] = "cancel_buy"
    CONFIRM_ORDER: Final[str] = "confirm_order"
    PAY_SBP: Final[str] = "pay_sbp"
    PAY_CARD: Final[str] = "pay_card"
    PAY_PAYCORE: Final[str] = "pay_paycore"

    # Новая кнопка в стартовом меню (перенос Paycore-уведомления)
    BINANCE_NEW: Final[str] = "binance_new"
    BINANCE_VERIFY_YES: Final[str] = "binance_verify_yes"
    BINANCE_CANCEL: Final[str] = "binance_cancel"

    BINANCE_ASSET_BTC: Final[str] = "binance_asset_btc"
    BINANCE_ASSET_LTC: Final[str] = "binance_asset_ltc"
    BINANCE_ASSET_USDT: Final[str] = "binance_asset_usdt"
    BINANCE_ASSET_XMR: Final[str] = "binance_asset_xmr"

    # Казино
    CASINO_TOPUP_CRYPTO: Final[str] = "casino_topup_crypto"

    PERSONAL_ACCOUNT: Final[str] = "personal_account"
    SHOW_WEB_PASSWORD: Final[str] = "show_web_password"
    FAST_BUY: Final[str] = "fast_buy"
    P2P_MARKET: Final[str] = "p2p_market"
    RULES: Final[str] = "rules"
    TECH_SUPPORT: Final[str] = "tech_support"
    ADMIN_DB_SEARCH: Final[str] = "admin_db_search"

    # ✅ Долги админов
    ADMIN_DEBT: Final[str] = "admin_debt"
    ADMIN_DEBT_ADD: Final[str] = "admin_debt_add"
    ADMIN_DEBT_SUB: Final[str] = "admin_debt_sub"
    ADMIN_DEBT_HISTORY: Final[str] = "admin_debt_history"
    ADMIN_DEBT_BACK: Final[str] = "admin_debt_back"

    # Возврат в главное меню
    MAIN_MENU: Final[str] = "main_menu"

    # Операторские действия
    OPERATOR_ACCEPT: Final[str] = "operator_accept:"
    OPERATOR_REJECT: Final[str] = "operator_reject:"

    # Бухгалтерия
    ACCOUNTING_EXPENSES: Final[str] = "accounting_expenses"
    ACCOUNTING_EXPENSES_ADD: Final[str] = "accounting_expenses_add"
    ACCOUNTING_EXPENSES_DEL: Final[str] = "accounting_expenses_del"
    ACCOUNTING_EXPENSES_DELETE: Final[str] = "accounting_expenses_delete_"
    ACCOUNTING_WITHDRAWALS: Final[str] = "accounting_withdrawals"
    ACCOUNTING_SET_RESERVE: Final[str] = "accounting_set_reserve"
    ACCOUNTING_RESET: Final[str] = "accounting_reset"

    # Брелок
    BRELOK: Final[str] = "brelok"

    # Заглушка для неактивных кнопок/лейблов
    NOOP: Final[str] = "noop"

# -----------------------------------------------------------------------------
# Раздел: Клавиатуры — «Бухгалтерия»
# -----------------------------------------------------------------------------

def accounting_expenses_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура раздела «Бухгалтерия».
    Верхний ряд: [Расходы] [Вывод] [Сброс]
    Нижний ряд: [Установить резерв]
    """
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(
        InlineKeyboardButton("Расходы", callback_data=Callback.ACCOUNTING_EXPENSES),
        InlineKeyboardButton("Вывод", callback_data=Callback.ACCOUNTING_WITHDRAWALS),
        InlineKeyboardButton("Сброс", callback_data=Callback.ACCOUNTING_RESET),
    )
    kb.add(
        InlineKeyboardButton(
            "Установить резерв",
            callback_data=Callback.ACCOUNTING_SET_RESERVE,
        ),
    )
    return kb


def expenses_actions_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура действий с расходами: добавить/удалить.
    """
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("➕ Добавить", callback_data=Callback.ACCOUNTING_EXPENSES_ADD),
        InlineKeyboardButton("➖ Удалить", callback_data=Callback.ACCOUNTING_EXPENSES_DEL),
    )
    return kb


def expenses_delete_keyboard(
    expenses: Sequence[Tuple[int, str, float]],
) -> InlineKeyboardMarkup:
    """
    Клавиатура удаления конкретных расходов.

    :param expenses: последовательность кортежей (id, название, сумма)
    """
    kb = InlineKeyboardMarkup(row_width=1)
    for exp_id, title, amount in expenses:
        kb.add(
            InlineKeyboardButton(
                f"Удалить: {title} ({amount:.2f})",
                callback_data=f"{Callback.ACCOUNTING_EXPENSES_DELETE}{exp_id}",
            )
        )
    return kb


# -----------------------------------------------------------------------------
# Раздел: Клавиатуры — Магазин / Главное меню
# -----------------------------------------------------------------------------

def buy_keyboard() -> InlineKeyboardMarkup:
    """
    Главная инлайн-клавиатура магазина.
    """
    kb = InlineKeyboardMarkup()

    kb.add(
        InlineKeyboardButton(
            "🔥Web-версия — Скидка 20%🔥",
            url="https://webvidra.com",
        )
    )

    kb.add(
        InlineKeyboardButton(
            "Купить BTC (Bitcoin)",
            callback_data=Callback.P2P_ASSISTANT,
        )
    )
    kb.add(
        InlineKeyboardButton(
            "Купить LTC (Litecoin)",
            callback_data=Callback.P2P_ASSISTANT_LTC,
        )
    )
    kb.add(
        InlineKeyboardButton(
            "Купить USDT (TRC20)",
            callback_data=Callback.P2P_ASSISTANT_USDT,
        )
    )
    kb.add(
        InlineKeyboardButton(
            "Купить XMR (Monero)",
            callback_data=Callback.P2P_ASSISTANT_XMR,
        )
    )

    kb.row(
        InlineKeyboardButton("📜 Правила", callback_data=Callback.RULES),
        InlineKeyboardButton("🛠️ Тех. поддержка", callback_data=Callback.TECH_SUPPORT),
    )
    kb.add(
        InlineKeyboardButton(
            "👤 Личный кабинет",
            callback_data=Callback.PERSONAL_ACCOUNT,
        )
    )
    kb.add(
        InlineKeyboardButton(
            "📢 Телеграм Канал",
            url="https://t.me/VidraObmenChannel",
        )
    )
    return kb


def cancel_buy_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура отмены покупки.
    """
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY)
    )


def confirmation_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения/отмены заявки.
    """
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=Callback.CONFIRM_ORDER),
        InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY),
    )


def payment_method_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора метода оплаты.
    """
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("СБП", callback_data=Callback.PAY_SBP),
        InlineKeyboardButton("СБП (трансгран)", callback_data="pay_sbp_transgr"),
    )
    kb.row(
        InlineKeyboardButton("На карту", callback_data=Callback.PAY_CARD),
        InlineKeyboardButton("Paycore (ссылка)", callback_data=Callback.PAY_PAYCORE),
    )
    return kb


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные элементы
# -----------------------------------------------------------------------------

def _label_button(text: str) -> InlineKeyboardButton:
    """
    Кнопка-лейбл (неактивная): отображает текст, callback ведёт в заглушку.
    """
    return InlineKeyboardButton(text, callback_data=Callback.NOOP)


# -----------------------------------------------------------------------------
# Раздел: Клавиатуры — Карточка заявки для админов/операторов
# -----------------------------------------------------------------------------

def operator_keyboard(
    user_id: int,
    order_id: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
    operator_name: Optional[str] = None,
    viewer_admin_id: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура карточки заявки для админов.

    Поведение:
    - Если заявка свободна → кнопка «✅ Принять».
    - Если занята текущим админом → лейбл «✅ Принята вами».
    - Если занята другим админом → лейбл «✅ Взял: <имя>».

    Старые вызовы с 1–2 позиционными аргументами остаются совместимы;
    новые параметры передаются по имени.
    """
    kb = InlineKeyboardMarkup(row_width=1)

    if operator_id is None:
        # Свободна — кнопка «Принять»
        callback_suffix = f"{user_id}" if order_id is None else f"{user_id}:{order_id}"
        kb.add(
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"{Callback.OPERATOR_ACCEPT}{callback_suffix}",
            )
        )
        return kb

    # Уже занята — показываем соответствующий лейбл
    if viewer_admin_id is not None and operator_id == viewer_admin_id:
        kb.add(_label_button("✅ Принята вами"))
    else:
        name = operator_name or "оператор"
        kb.add(_label_button(f"✅ Взял: {name}"))

    return kb


# -----------------------------------------------------------------------------
# Раздел: Reply-клавиатуры
# -----------------------------------------------------------------------------

def shop_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Reply-клавиатура магазина.
    """
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.add(KeyboardButton("🆕 Создать заявку"))
    kb.row(
        KeyboardButton("💬 Чат с поддержкой"),
        KeyboardButton("✅ Завершенные заявки"),
    )
    return kb