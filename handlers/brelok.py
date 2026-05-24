# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import hmac
import re
import time
import datetime as dt
from typing import Any, Optional, Tuple

from aiohttp import ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config.settings import settings as cfg
from db.user_breloks import (
    clear_pin,
    get_by_owner,
    get_pin_hash,
    list_history_by_owner,
    set_pin_hash,
    update_wallet_with_asset,
)
from db.settings import get_usdt_rub_rate_manual
from handlers.common import send_welcome
from keyboards.inline import Callback
from utils.helpers import HTTP_TIMEOUT
from utils.security import hash_pin


# -----------------------------------------------------------------------------
# Раздел: Константы и колбэки
# -----------------------------------------------------------------------------
BRELOK_NOT_ASSIGNED = (
    "Брелок для внесения наличных средств\n\n"
    "У нас есть персональный брелок обменника — это уникальный платежный "
    "идентификатор, закреплённый только за одним клиентом.\n\n"
    "Что он даёт:\n\n"
    "• вы пополняете свой криптокошелёк через любой банкомат РФ\n"
    "• без использования карты\n"
    "• без риска блокировок со стороны банка\n"
    "• пополнение идёт автоматически — без оператора\n\n"
    "Сейчас доступны монеты: BTC, LTC, USDT (TRC20), TON.\n\n"
    "Стоимость брелока — 20 000 ₽.\n\n"
    "Чтобы получить свой брелок — напишите в техподдержку."
)

CB_HISTORY = "brelok_history"
CB_UPDATE_ADDR = "brelok_update_addr"
CB_CANCEL = "brelok_cancel"

CB_PIN = "brelok_pin"
CB_PIN_SET = "brelok_pin_set"
CB_PIN_CHANGE = "brelok_pin_change"
CB_PIN_DELETE = "brelok_pin_delete"
CB_PIN_BACK = "brelok_pin_back"

# Выбор монеты/сети
CB_WALLET_ASSET_BTC = "brelok_wallet_asset_btc"
CB_WALLET_ASSET_LTC = "brelok_wallet_asset_ltc"
CB_WALLET_ASSET_USDT_TRC20 = "brelok_wallet_asset_usdt_trc20"
CB_WALLET_ASSET_TON = "brelok_wallet_asset_ton"

_PIN_REGEX = re.compile(r"^\d{4,6}$")


# -----------------------------------------------------------------------------
# Раздел: Машины состояний
# -----------------------------------------------------------------------------
class BrelokStates(StatesGroup):
    """Состояния для управления брелоком."""
    choosing_wallet_asset = State()
    waiting_wallet_address = State()


class PinStates(StatesGroup):
    """Состояния для установки/смены PIN-кода."""
    waiting_new_pin = State()
    waiting_new_pin_confirm = State()


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции форматирования
# -----------------------------------------------------------------------------
def _spaced_int(n: Optional[int]) -> str:
    """Возвращает целое число с разделителями тысяч пробелами."""
    if n is None:
        return "0"
    s = str(int(n))
    parts: list[str] = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    return " ".join(reversed(parts))


def _fmt_rub(n: Optional[int]) -> str:
    """Форматирует сумму в рублях без пробела перед символом ₽."""
    return f"{_spaced_int(int(n or 0))}₽"


def _last4(number: Optional[int], fallback: Optional[str] = None) -> str:
    """Возвращает последние 4 цифры номера или подстановку."""
    if number is None:
        return (fallback or "")[-4:] if fallback else "—"
    return str(number)[-4:]


def _likely_btc_address(addr: str) -> bool:
    """Простейшая эвристика валидности BTC-адреса по длине и префиксу."""
    addr = (addr or "").strip()
    return 14 <= len(addr) <= 120 and addr.startswith(("1", "3", "bc1", "tb1", "BC1"))


def _likely_ltc_address(addr: str) -> bool:
    """Грубая проверка LTC-адреса."""
    addr = (addr or "").strip()
    return 26 <= len(addr) <= 120 and addr.startswith(("L", "M", "ltc1", "LTC1"))


def _likely_trc20_address(addr: str) -> bool:
    """Грубая проверка TRC20 (USDT TRON) — адреса начинаются на T."""
    addr = (addr or "").strip()
    return 30 <= len(addr) <= 120 and addr.startswith(("T",))


def _likely_ton_address(addr: str) -> bool:
    """Грубая проверка TON-адреса."""
    addr = (addr or "").strip()
    # допускаем обычный base64url-вид и ton://...
    if addr.startswith("ton://"):
        return True
    return 40 <= len(addr) <= 120 and addr.startswith(("UQ", "EQ", "kQ", "0Q"))


def _wallet_asset_label(wallet_asset: str, wallet_network: Optional[str]) -> str:
    """Человекочитаемый ярлык монеты/сети."""
    asset = (wallet_asset or "BTC").upper()
    network = (wallet_network or "").lower()

    if asset == "BTC":
        return "BTC (on-chain)"
    if asset == "LTC":
        return "LTC"
    if asset == "USDT":
        if network == "trc20":
            return "USDT (TRC20)"
        return "USDT"
    if asset == "TON":
        return "TON"
    return asset


def _validate_wallet(asset: str, addr: str) -> bool:
    """Проверка адреса в зависимости от выбранной монеты."""
    asset = asset.upper()
    if asset == "BTC":
        return _likely_btc_address(addr)
    if asset == "LTC":
        return _likely_ltc_address(addr)
    if asset == "USDT":
        return _likely_trc20_address(addr)
    if asset == "TON":
        return _likely_ton_address(addr)
    return False


# -----------------------------------------------------------------------------
# Раздел: Работа с Binance (резерв)
# -----------------------------------------------------------------------------
async def _get_binance_usdt_balance_rub() -> int:
    """Возвращает резерв в RUB = (USDT free+locked) × ручной курс USDT→RUB."""
    rate = await get_usdt_rub_rate_manual() or 0.0
    if rate <= 0:
        return 0

    try:
        api_key = cfg.binance_api_key
        api_secret = cfg.binance_api_secret
        base_url = cfg.binance_base_url
    except Exception:
        return 0

    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(
        api_secret.encode(), query_string.encode(), hashlib.sha256
    ).hexdigest()
    url = f"{base_url}/api/v3/account?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}

    total_usdt = 0.0
    try:
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return 0
                data = await resp.json()
                for b in data.get("balances", []):
                    if (b.get("asset") or "").upper() == "USDT":
                        free = float(b.get("free") or 0)
                        locked = float(b.get("locked") or 0)
                        total_usdt = free + locked
                        break
    except Exception:
        return 0

    return int(round(total_usdt * rate))


# -----------------------------------------------------------------------------
# Раздел: Бизнес-логика и тексты
# -----------------------------------------------------------------------------
def _brelok_view_text_from_row(
    row: dict[str, Any],
    last4_hint: Optional[str],
    reserve_rub: int,
) -> str:
    """Формирует текст карточки брелока для пользователя."""
    number = row.get("brelok_number") or row.get("number")
    last4 = _last4(number, last4_hint)
    bank = row.get("bank") or "—"

    total_topup = int(row.get("total_topup") or 0)

    # Адрес кошелька (может быть пустым)
    wallet = row.get("wallet") or row.get("legacy_btc_address") or None

    if wallet:
        asset = (row.get("wallet_asset") or "BTC").upper()
        network = (row.get("wallet_network") or "").lower()

        # Красиво собираем подпись монета(сеть)
        if asset == "USDT" and network == "trc20":
            wallet_label = "USDT(TRC20)"
        elif asset == "BTC":
            wallet_label = "BTC"
        elif asset == "LTC":
            wallet_label = "LTC"
        elif asset == "TON":
            wallet_label = "TON"
        else:
            wallet_label = f"{asset}({network.upper()})" if network else asset

        wallet_title = f"Кошелёк для пополнения {wallet_label}:"
        wallet_value = wallet
    else:
        wallet_title = "Кошелёк для пополнения:"
        wallet_value = "-"

    return (
        f"Брелок №{last4}\n"
        f"___________\n"
        f"Банк: {bank}\n"
        f"Сумма пополнений: {_fmt_rub(total_topup)}\n"
        f"Резерв: {_fmt_rub(reserve_rub)}\n"
        f"____________\n"
        f"{wallet_title}\n\n"   # ← ВОТ ЭТОТ ОТСТУП, как в твоём примере
        f"{wallet_value}"
    )


async def _get_user_brelok_row(
    user_id: int,
) -> Tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    """
    Возвращает (строка брелока, подсказка last4_hint, legacy-кошелёк) по user_id.
    """
    row = await get_by_owner(user_id)
    if not row:
        return None, None, None

    last4_hint = None
    if row.get("last4_hint"):
        last4_hint = str(row["last4_hint"])

    legacy_wallet = row.get("legacy_btc_address")
    return row, last4_hint, legacy_wallet


async def _show_brelok_card_message(bot: Bot, chat_id: int, user_id: int) -> None:
    """Показывает карточку брелока пользователю или уведомление, если брелок не назначен."""
    row, last4_hint, legacy_wallet = await _get_user_brelok_row(user_id)
    if not row:
        await bot.send_message(chat_id, BRELOK_NOT_ASSIGNED, reply_markup=kb_main_menu())
        return

    reserve_rub = await _get_binance_usdt_balance_rub()
    text = _brelok_view_text_from_row(row, last4_hint=last4_hint, reserve_rub=reserve_rub)
    wallet = row.get("wallet") or legacy_wallet
    wallet_asset = (row.get("wallet_asset") or "BTC").upper()
    await bot.send_message(chat_id, text, reply_markup=kb_main(wallet, wallet_asset))


async def _send_pin_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    """
    Показывает PIN-код брелока пользователю.

    PIN хранится в открытом виде и задаётся администратором при создании брелока.
    Пользователь здесь только смотрит PIN, менять его нельзя.
    """
    stored = await get_pin_hash(user_id)

    if stored:
        text = f"🔐 Пин-код вашего брелока: {stored}"
    else:
        text = (
            "🔐 Пин-код для вашего брелока пока не задан.\n"
            "Свяжитесь с техподдержкой или администратором."
        )

    await bot.send_message(chat_id, text)


# -----------------------------------------------------------------------------
# Раздел: Клавиатуры
# -----------------------------------------------------------------------------
def kb_main(wallet: Optional[str], wallet_asset: str) -> InlineKeyboardMarkup:
    """Главное меню управления брелоком.

    • Сверху ОДНА кнопка «Добавить кошелек».
    """
    kb = InlineKeyboardMarkup(row_width=1)

    # Одна кнопка в самом верху
    btn_update = InlineKeyboardButton("📝 Добавить / Изменить кошелек", callback_data=CB_UPDATE_ADDR)
    kb.row(btn_update)

    # Ниже — история и пин-код
    kb.row(
        InlineKeyboardButton("📜 История", callback_data=CB_HISTORY),
        InlineKeyboardButton("🔐 Пин-код", callback_data=CB_PIN),
    )

    # Внизу — возврат в главное меню бота
    kb.add(InlineKeyboardButton("🏠 Главное меню", callback_data=Callback.MAIN_MENU))
    return kb


def kb_main_menu() -> InlineKeyboardMarkup:
    """Кнопка возврата в главное меню бота."""
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton("🏠 Главное меню", callback_data=Callback.MAIN_MENU)
    )


def kb_cancel() -> InlineKeyboardMarkup:
    """Кнопка отмены текущего действия."""
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton("🚫 Отмена", callback_data=CB_CANCEL)
    )


def kb_pin_menu(has_pin: bool) -> InlineKeyboardMarkup:
    """Клавиатура управления PIN-кодом."""
    kb = InlineKeyboardMarkup(row_width=1)
    if has_pin:
        kb.add(
            InlineKeyboardButton("✏️ Изменить PIN", callback_data=CB_PIN_CHANGE),
            InlineKeyboardButton("🗑️ Удалить PIN", callback_data=CB_PIN_DELETE),
        )
    else:
        kb.add(InlineKeyboardButton("➕ Установить PIN", callback_data=CB_PIN_SET))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data=CB_PIN_BACK))
    return kb


def kb_wallet_asset_menu() -> InlineKeyboardMarkup:
    """Меню выбора монеты/сети для кошелька брелока."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("BTC", callback_data=CB_WALLET_ASSET_BTC),
        InlineKeyboardButton("LTC", callback_data=CB_WALLET_ASSET_LTC),
    )
    kb.add(
        InlineKeyboardButton("USDT (TRC20)", callback_data=CB_WALLET_ASSET_USDT_TRC20),
        InlineKeyboardButton("TON", callback_data=CB_WALLET_ASSET_TON),
    )
    kb.add(InlineKeyboardButton("⬅️ Отмена", callback_data=CB_CANCEL))
    return kb


def _mask_pin(stored: Optional[str]) -> str:
    """Возвращает маску PIN-кода или маркер отсутствия."""
    return "••••" if stored else "не установлен"


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — карточка и главное меню
# -----------------------------------------------------------------------------
async def on_brelok_click(call: types.CallbackQuery) -> None:
    """Открывает карточку брелока."""
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_brelok_card_message(call.bot, call.from_user.id, call.from_user.id)


async def on_main_menu_click(call: types.CallbackQuery) -> None:
    """Возврат в главное меню бота."""
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_welcome(call.bot, call.from_user.id)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — история
# -----------------------------------------------------------------------------
async def on_history_click(call: types.CallbackQuery) -> None:
    """Показывает историю пополнений."""
    await call.answer()
    rows = await list_history_by_owner(call.from_user.id, limit=10)
    if not rows:
        await call.message.answer("📜 История пуста.")
        return

    # Берём текущий актив брелока (BTC / LTC / TON / USDT и т.п.)
    asset_global = "BTC"
    try:
        info = await get_by_owner(call.from_user.id)
        if info:
            asset_global = str(info.get("brelok_wallet_asset") or "BTC").upper()
    except Exception:
        asset_global = "BTC"

    lines: list[str] = []
    for r in rows:
        rub = f"{_spaced_int(int(r.get('rub_amount') or 0))}₽"

        raw_ts = (r.get("created_at") or "").replace("T", " ")
        try:
            dt_obj = dt.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S")
            ts_fmt = dt_obj.strftime("%d.%m.%Y - %H:%M")
        except Exception:
            ts_fmt = raw_ts or "?"

        # Если когда-нибудь начнёшь писать asset в history_json — возьмём его.
        asset = str(r.get("asset") or asset_global).upper()

        lines.append(f"• ({ts_fmt}) • {rub}  ({asset})")

    await call.message.answer(
        "📜 История пополнений (последние):\n\n" + "\n".join(lines)
    )


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — выбор монеты/сети и адреса
# -----------------------------------------------------------------------------
async def on_update_addr_click(call: types.CallbackQuery, state: FSMContext) -> None:
    """Старт процесса добавления/изменения кошелька."""
    await call.answer()
    await state.finish()

    # Удаляем старую карточку брелока
    try:
        await call.message.delete()
    except Exception:
        pass

    text = (
        "Выберите монету/сеть для пополнения по брелоку:\n\n"
        "_Минимальная сумма пополнения — 1500 ₽._\n"
        "_Всегда проверяйте монету и сеть перед отправкой._\n"
        "_Неверно указанный адрес может привести к потере средств. ⚠️💰_"
    )

    await call.message.answer(
        text,
        reply_markup=kb_wallet_asset_menu(),
        parse_mode="Markdown",
    )
    await BrelokStates.choosing_wallet_asset.set()


async def on_wallet_asset_chosen(call: types.CallbackQuery, state: FSMContext) -> None:
    """Обработка выбора монеты/сети для кошелька."""
    await call.answer()

    data_map = {
        CB_WALLET_ASSET_BTC: ("BTC", "btc", "BTC (on-chain)"),
        CB_WALLET_ASSET_LTC: ("LTC", "ltc", "LTC"),
        CB_WALLET_ASSET_USDT_TRC20: ("USDT", "trc20", "USDT (TRC20)"),
        CB_WALLET_ASSET_TON: ("TON", "ton", "TON"),
    }
    asset, network, human = data_map[call.data]

    await state.update_data(wallet_asset=asset, wallet_network=network)

    sent = await call.message.answer(
        f"Пришлите адрес для {human}:\n\n"
        f"Проверьте, что сеть совпадает.",
        reply_markup=kb_cancel(),
    )

    # Запоминаем ID сообщения с запросом, чтобы потом удалить его
    await state.update_data(prompt_msg_id=sent.message_id)
    await BrelokStates.waiting_wallet_address.set()


async def on_wallet_address(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    asset = data.get("wallet_asset") or "BTC"
    network = data.get("wallet_network")

    addr = (message.text or "").strip()
    if not _validate_wallet(asset, addr):
        await message.answer(
            f"Адрес выглядит некорректно для {asset.upper()} ({network}).\n"
            f"Попробуйте снова или отмените действие.",
        )
        return

    user_id = message.from_user.id

    # Обновляем в базе
    await update_wallet_with_asset(user_id, addr, asset, network)

    # Удаляем сообщение пользователя + запрос
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Показываем новую карточку брелока
    await state.finish()
    await message.answer(
        f"✅ Кошелёк для {asset.upper()} ({network}) обновлён.",
        reply_markup=ReplyKeyboardRemove(),
    )

    await _show_brelok_card_message(message.bot, message.chat.id, user_id)


async def on_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    """Отмена текущего шага и возврат к карточке брелока."""
    await call.answer("Отменено")
    await state.finish()
    await _show_brelok_card_message(call.bot, call.from_user.id, call.from_user.id)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — PIN-код (меню и операции)
# -----------------------------------------------------------------------------
async def on_pin_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    """Открывает меню управления PIN-кодом."""
    await call.answer()
    await _send_pin_menu(call.bot, call.from_user.id, call.from_user.id)


async def on_pin_set_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """Старт установки нового PIN-кода."""
    await call.answer()
    await call.message.answer(
        "Введите новый PIN (4–6 цифр):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await PinStates.waiting_new_pin.set()


async def on_pin_change_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """Старт смены PIN-кода (идентично установке)."""
    await on_pin_set_start(call, state)


async def on_pin_delete(call: types.CallbackQuery, state: FSMContext) -> None:
    """Удаляет установленный PIN-код."""
    await call.answer()
    row, _, _ = await _get_user_brelok_row(call.from_user.id)
    if not row:
        await call.message.answer(BRELOK_NOT_ASSIGNED)
        return

    await clear_pin(call.from_user.id)
    await call.message.answer("✅ PIN удалён.")
    await _show_brelok_card_message(call.bot, call.from_user.id, call.from_user.id)


async def on_pin_back(call: types.CallbackQuery, state: FSMContext) -> None:
    """Возврат к карточке брелока из меню PIN."""
    await call.answer()
    await _show_brelok_card_message(call.bot, call.from_user.id, call.from_user.id)


async def on_pin_new_entered(message: types.Message, state: FSMContext) -> None:
    """Сохраняет введённый PIN во временном состоянии и запрашивает подтверждение."""
    pin = (message.text or "").strip()
    if not _PIN_REGEX.fullmatch(pin):
        await message.answer("⚠️ PIN должен содержать 4–6 цифр. Попробуйте снова:")
        return

    await state.update_data(new_pin=pin)
    await message.answer("Подтвердите PIN, повторно введя его (4–6 цифр):")
    await PinStates.waiting_new_pin_confirm.set()


async def on_pin_new_confirm(message: types.Message, state: FSMContext) -> None:
    """Проверяет подтверждение PIN и сохраняет его."""
    data = await state.get_data()
    pin_first = data.get("new_pin")
    pin_second = (message.text or "").strip()

    if not (_PIN_REGEX.fullmatch(pin_second) and pin_first == pin_second):
        await message.answer(
            "⚠️ PIN не совпадает. Начните заново: «🔐 Пин-код» → «Установить/Изменить»."
        )
        await state.finish()
        await _send_pin_menu(message.bot, message.chat.id, message.from_user.id)
        return

    row, _, _ = await _get_user_brelok_row(message.from_user.id)
    if not row:
        await state.finish()
        await message.answer(BRELOK_NOT_ASSIGNED)
        return

    hashed = hash_pin(pin_first)
    await set_pin_hash(message.from_user.id, hashed)

    await state.finish()
    await message.answer("✅ PIN сохранён.", reply_markup=ReplyKeyboardRemove())
    await _show_brelok_card_message(message.bot, message.chat.id, message.from_user.id)


# -----------------------------------------------------------------------------
# Раздел: Регистрация хэндлеров
# -----------------------------------------------------------------------------
def register(dp: Dispatcher) -> None:
    """Регистрирует хэндлеры в диспетчере Aiogram."""
    # Карточка
    dp.register_callback_query_handler(on_brelok_click, text=Callback.BRELOK)

    # Главное меню (возврат)
    dp.register_callback_query_handler(on_main_menu_click, text=Callback.MAIN_MENU, state="*")

    # История
    dp.register_callback_query_handler(on_history_click, text=CB_HISTORY, state="*")

    # Кошелёк: выбор монеты/сети и адреса
    dp.register_callback_query_handler(on_update_addr_click, text=CB_UPDATE_ADDR, state="*")

    dp.register_callback_query_handler(
        on_wallet_asset_chosen,
        text=[CB_WALLET_ASSET_BTC, CB_WALLET_ASSET_LTC, CB_WALLET_ASSET_USDT_TRC20, CB_WALLET_ASSET_TON],
        state=BrelokStates.choosing_wallet_asset,
    )
    dp.register_callback_query_handler(on_cancel, text=CB_CANCEL, state="*")

    dp.register_message_handler(
        on_wallet_address,
        state=BrelokStates.waiting_wallet_address,
        content_types=types.ContentTypes.TEXT,
    )

    # PIN: только просмотр PIN-кода, без установки/изменения пользователем
    dp.register_callback_query_handler(on_pin_menu, text=CB_PIN, state="*")


