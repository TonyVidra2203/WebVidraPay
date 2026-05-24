# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import ClientSession
from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.exceptions import InvalidQueryID

from db.cards import get_all_cards, get_card_balance
from db.connection import get_db
from db.expenses import add_expense, delete_expense, get_expenses
from db.settings import get_setting, get_usdt_rub_rate_manual, set_setting
from db.users import get_user
from db.withdrawals import add_withdrawal
from keyboards.inline import (
    Callback,
    accounting_expenses_keyboard,
    expenses_actions_keyboard,
    expenses_delete_keyboard,
)
from utils.helpers import HTTP_TIMEOUT, get_binance_ticker_price


# -----------------------------------------------------------------------------
# Раздел: Состояния FSM
# -----------------------------------------------------------------------------
class AddExpenseStates(StatesGroup):
    """Состояния для добавления расхода."""
    waiting_title = State()
    waiting_amount = State()


class SetReserveStates(StatesGroup):
    """Состояния для установки ручного порога резерва."""
    waiting_amount = State()


# -----------------------------------------------------------------------------
# Раздел: Настройки и константы
# -----------------------------------------------------------------------------
RESERVE_KEY = "EXCHANGE_RESERVE_RUB"


async def get_exchange_reserve_rub() -> float:
    """Возвращает ручной порог резерва (в ₽)."""
    raw = await get_setting(RESERVE_KEY)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def set_exchange_reserve_rub(amount: float) -> None:
    """Сохраняет ручной порог резерва (в ₽)."""
    await set_setting(RESERVE_KEY, f"{amount:.2f}")


# -----------------------------------------------------------------------------
# Раздел: Агрегаты по картам
# -----------------------------------------------------------------------------
async def get_cards_total_rub() -> float:
    """Возвращает суммарный баланс по всем картам из DAO."""
    total = 0.0
    try:
        cards = await get_all_cards()
        for c in cards:
            card_id = int(c["card_id"])
            try:
                bal = await get_card_balance(card_id)
                if bal:
                    total += float(bal)
            except Exception:
                continue
    except Exception:
        return 0.0
    return total


# -----------------------------------------------------------------------------
# Раздел: Резерв Binance
# -----------------------------------------------------------------------------
async def _build_reserve_block_and_total() -> Tuple[str, float]:
    """
    Возвращает текстовый блок по активам Binance и итог в рублях.
    При ошибке возвращает ("—", 0.0).
    """
    from config.settings import settings as cfg_settings

    api_key = cfg_settings.binance_api_key
    api_secret = cfg_settings.binance_api_secret
    base_url = cfg_settings.binance_base_url

    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}/api/v3/account?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(
                url,
                headers=headers,
                proxy=cfg_settings.binance_proxy or None,
            ) as resp:
                if resp.status != 200:
                    return "—", 0.0
                data = await resp.json()
    except Exception:
        return "—", 0.0

    balances = data.get("balances", []) or []
    assets: List[Dict[str, Any]] = []
    for b in balances:
        asset = (b.get("asset") or "").upper()
        if not asset:
            continue
        try:
            free = float(b.get("free") or 0)
            locked = float(b.get("locked") or 0)
        except (TypeError, ValueError):
            continue
        total = free + locked
        if total <= 0:
            continue
        assets.append({"asset": asset, "total": total})

    if not assets:
        return "—", 0.0

    usdt_rub = await get_usdt_rub_rate_manual() or 0.0
    if usdt_rub <= 0:
        return "—", 0.0

    threshold_rub = 1000.0
    lines: List[str] = []
    total_rub_sum = 0.0

    assets.sort(key=lambda x: x["asset"] != "USDT")

    for item in assets:
        asset = item["asset"]
        total_qty = float(item["total"])

        if asset == "USDT":
            usdt_equiv = total_qty
        else:
            symbol = f"{asset}USDT"
            price = await get_binance_ticker_price(symbol, base_url, HTTP_TIMEOUT)
            if not price:
                continue
            usdt_equiv = total_qty * price

        rub_equiv = usdt_equiv * usdt_rub
        if rub_equiv < threshold_rub:
            continue

        total_rub_sum += rub_equiv
        qty_str = f"{total_qty:.8f}".rstrip("0").rstrip(".")
        rub_str = f"{rub_equiv:,.0f}".replace(",", " ")
        lines.append(f"• {asset}: {qty_str} (≈ {rub_str} ₽)")

    if not lines:
        return "—", 0.0

    total_rub_str = f"{total_rub_sum:,.0f}".replace(",", " ")
    text = "\n".join(lines) + f"\n\nИТОГО: {total_rub_str} ₽"
    return text, total_rub_sum


# -----------------------------------------------------------------------------
# Раздел: Сброс данных
# -----------------------------------------------------------------------------
async def _reset_accounting_and_cards(user_id: int) -> None:
    """
    Полный сброс: расходы, p2p-сделки, обнуление балансов карт и порога резерва.
    """
    db = await get_db()

    try:
        await db.execute("DELETE FROM expenses;")
        await db.commit()
    except Exception:
        pass

    try:
        await db.execute("DELETE FROM completed_p2p_orders;")
        await db.commit()
    except Exception:
        pass

    try:
        cards = await get_all_cards()
        for c in cards:
            card_id = int(c["card_id"])
            try:
                bal = await get_card_balance(card_id)
            except Exception:
                continue
            if bal and bal > 0:
                try:
                    await add_withdrawal(user_id, card_id, bal)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        await set_exchange_reserve_rub(0.0)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Раздел: Отчёт «Бухгалтерия»
# -----------------------------------------------------------------------------
async def send_accounting_report(
    chat: types.Message | types.CallbackQuery,
    del_msg: Optional[types.Message] = None,
) -> None:
    """Формирует и отправляет сводный отчёт по бухгалтерии."""
    if del_msg:
        try:
            await del_msg.delete()
        except Exception:
            pass

    try:
        db = await get_db()
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(total_rub - rub_amount), 0) AS commission_sum,
                COALESCE(SUM(total_rub), 0)              AS total_volume
            FROM completed_p2p_orders;
            """
        )
        row = await cursor.fetchone()
        total_orders = row[0] or 0
        total_volume = row[2] or 0.0
    except Exception as e:
        text = f"❗️ Не смог сформировать отчёт: {e}"
        if isinstance(chat, types.Message):
            await chat.answer(text, reply_markup=accounting_expenses_keyboard())
        else:
            await chat.message.answer(text, reply_markup=accounting_expenses_keyboard())
        return

    expenses = await get_expenses()
    total_expenses = (
        sum(float(exp.get("amount", 0) or 0) for exp in expenses)
        if expenses
        else 0.0
    )

    _reserve_text, exchange_total_rub = await _build_reserve_block_and_total()
    reserve_threshold_rub = await get_exchange_reserve_rub()
    cards_total_rub = await get_cards_total_rub()

    profit = max(
        0.0,
        (exchange_total_rub + cards_total_rub) - reserve_threshold_rub - total_expenses,
    )

    report = (
        "📊 Бухгалтерия\n"
        "———————————\n"
        "Справочно:\n"
        f"• Всего завершённых сделок: {total_orders}\n"
        f"• Общий оборот: {total_volume:.2f} ₽\n"
        f"• Расходы: {total_expenses:.2f} ₽\n"
        f"• Резерв: {reserve_threshold_rub:.2f} ₽\n"
        "———————————\n"
        "Итого:\n"
        f"На Binance: {exchange_total_rub:.2f} ₽\n"
        f"На Картах: {cards_total_rub:.2f} ₽\n\n"
        f"💰Прибыль:  {profit:.2f} ₽"
    )

    if isinstance(chat, types.Message):
        await chat.answer(report, reply_markup=accounting_expenses_keyboard())
    else:
        await chat.message.answer(report, reply_markup=accounting_expenses_keyboard())


# -----------------------------------------------------------------------------
# Раздел: Хендлеры «Бухгалтерия»
# -----------------------------------------------------------------------------
async def admin_accounting(message: types.Message, state: FSMContext) -> None:
    """Точка входа в раздел «Бухгалтерия» для админов."""
    user = await get_user(message.from_user.id)
    if not user or user.get("role") != "Admin":
        return
    await send_accounting_report(message)


async def show_expenses(callback: types.CallbackQuery) -> None:
    """Показывает список расходов и действия с ними."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    expenses = await get_expenses()
    if not expenses:
        text = "Расходов нету."
    else:
        lines: List[str] = []
        for exp in expenses:
            title = exp.get("title", "")
            amount = float(exp.get("amount", 0) or 0)
            lines.append(f"{title} — {amount:.2f} ₽")
        text = "\n".join(lines)

    await callback.message.answer(text, reply_markup=expenses_actions_keyboard())


async def start_add_expense(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Старт добавления расхода."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Введите назначение расхода:")
    await state.set_state(AddExpenseStates.waiting_title.state)


async def process_expense_title(message: types.Message, state: FSMContext) -> None:
    """Принимает назначение расхода и просит сумму."""
    await state.update_data(title=message.text)
    await message.answer("Введите сумму расхода (только число):")
    await state.set_state(AddExpenseStates.waiting_amount.state)


async def process_expense_amount(message: types.Message, state: FSMContext) -> None:
    """Принимает сумму расхода, сохраняет и обновляет отчёт."""
    try:
        amount = float((message.text or "").replace(" ", "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("Ошибка: введите сумму числом, больше нуля.")
        return

    data = await state.get_data()
    await add_expense(data["title"], amount)
    await message.answer("Расход добавлен.")
    await state.finish()
    await send_accounting_report(message)


async def start_delete_expense(callback: types.CallbackQuery) -> None:
    """Старт удаления расхода."""
    try:
        await callback.answer(cache_time=0)
    except InvalidQueryID:
        pass
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    expenses = await get_expenses()
    if not expenses:
        await callback.message.answer("Удалять нечего: расходов нет.")
        return

    expenses_items: List[Tuple[int, str, float]] = []
    for exp in expenses:
        try:
            exp_id = int(exp.get("id"))
        except Exception:
            continue
        title = str(exp.get("title", ""))
        amount = float(exp.get("amount", 0) or 0)
        expenses_items.append((exp_id, title, amount))

    if not expenses_items:
        await callback.message.answer("Удалять нечего: расходов нет.")
        return

    await callback.message.answer(
        "Выберите расход для удаления:",
        reply_markup=expenses_delete_keyboard(expenses_items),
    )


async def confirm_delete_expense(callback: types.CallbackQuery) -> None:
    """Подтверждает удаление выбранного расхода и обновляет отчёт."""
    try:
        expense_id = int(callback.data.replace(Callback.ACCOUNTING_EXPENSES_DELETE, ""))
        await delete_expense(expense_id)
        try:
            await callback.answer("Удалено!")
        except InvalidQueryID:
            pass
    except Exception:
        try:
            await callback.answer("Ошибка при удалении.", show_alert=True)
        except InvalidQueryID:
            pass
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_accounting_report(callback.message)


async def start_set_reserve(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Старт установки ручного порога резерва."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.set_state(SetReserveStates.waiting_amount.state)
    await callback.message.answer("Введите сумму резерва в рублях (только число). Пример: 150000.50")


async def process_set_reserve_amount(message: types.Message, state: FSMContext) -> None:
    """Сохраняет ручной порог резерва и обновляет отчёт."""
    try:
        txt = (message.text or "").strip().replace(" ", "").replace(",", ".")
        amount = float(txt)
        if amount < 0:
            raise ValueError
    except Exception:
        await message.answer("Ошибка: введите сумму числом (примеры: 0, 1000, 125000.75).")
        return

    await set_exchange_reserve_rub(amount)
    await message.answer(f"Резерв обновлён: {amount:.2f} ₽")
    await state.finish()
    await send_accounting_report(message)


async def reset_accounting(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Сбрасывает бухгалтерию и обнуляет балансы карт."""
    user = await get_user(callback.from_user.id)
    if not user or user.get("role") != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    await _reset_accounting_and_cards(callback.from_user.id)
    await callback.message.answer("♻️ Выполнен полный сброс бухгалтерии и балансов карт.")
    await send_accounting_report(callback.message)


# -----------------------------------------------------------------------------
# Раздел: Регистрация хендлеров
# -----------------------------------------------------------------------------
def register_accounting_handlers(dp: Dispatcher) -> None:
    """Регистрирует обработчики раздела «Бухгалтерия»."""
    dp.register_message_handler(admin_accounting, lambda m: m.text == "🧾 Бухгалтерия", state="*")

    dp.register_callback_query_handler(show_expenses, lambda c: c.data == Callback.ACCOUNTING_EXPENSES, state="*")
    dp.register_callback_query_handler(start_add_expense, lambda c: c.data == Callback.ACCOUNTING_EXPENSES_ADD, state="*")
    dp.register_message_handler(process_expense_title, state=AddExpenseStates.waiting_title)
    dp.register_message_handler(process_expense_amount, state=AddExpenseStates.waiting_amount)
    dp.register_callback_query_handler(start_delete_expense, lambda c: c.data == Callback.ACCOUNTING_EXPENSES_DEL, state="*")
    dp.register_callback_query_handler(
        confirm_delete_expense,
        lambda c: c.data and c.data.startswith(Callback.ACCOUNTING_EXPENSES_DELETE),
        state="*",
    )

    dp.register_callback_query_handler(start_set_reserve, lambda c: c.data == Callback.ACCOUNTING_SET_RESERVE, state="*")
    dp.register_message_handler(process_set_reserve_amount, state=SetReserveStates.waiting_amount)

    dp.register_callback_query_handler(reset_accounting, lambda c: c.data == Callback.ACCOUNTING_RESET, state="*")