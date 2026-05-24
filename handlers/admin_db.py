from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

import aiosqlite
from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from db.connection import get_db
from db.p2p import get_order_by_code, get_order_by_id
from db.referrals import add_referral_adjustment, get_referral_balance
from db.users import (
    get_referrals_count,
    get_user,
    get_user_by_btc_wallet,
    get_user_by_username,
    get_user_commission,
    is_user_active,
    set_user_active,
    set_user_commission,
)


class AdminDBStates(StatesGroup):
    waiting_search = State()


class AdminDBMessageState(StatesGroup):
    waiting_text = State()


class AdminDBCommissionState(StatesGroup):
    waiting_percent = State()


class AdminDBReferralAdjustState(StatesGroup):
    waiting_amount = State()


async def _is_admin_message(message: types.Message) -> bool:
    user = await get_user(message.from_user.id)
    return bool(user and user.get("role") == "Admin")


async def _is_admin_callback(callback: types.CallbackQuery) -> bool:
    user = await get_user(callback.from_user.id)
    if user and user.get("role") == "Admin":
        return True

    try:
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
    except Exception:
        pass
    return False


def _role_select_kb(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton("Admin", callback_data=f"db_user:{user_id}:role:set:Admin"),
        types.InlineKeyboardButton("User", callback_data=f"db_user:{user_id}:role:set:User"),
    )

    kb.add(
        types.InlineKeyboardButton("Operator", callback_data=f"db_user:{user_id}:role:set:Operator"),
        types.InlineKeyboardButton("Shop", callback_data=f"db_user:{user_id}:role:set:Shop"),
    )

    kb.add(
        types.InlineKeyboardButton("MasterCard", callback_data=f"db_user:{user_id}:role:set:MasterCard"),
    )

    return kb

def _user_actions_kb(user: dict, last_order_ref: Optional[dict] = None) -> types.InlineKeyboardMarkup:
    uid_raw = user.get("telegram_id")
    try:
        uid = int(uid_raw)
    except Exception:
        uid = None

    active = bool(user.get("is_active", 1))

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            "🚫 Заблокировать" if active else "✅ Разблокировать",
            callback_data=f"db_user:{uid_raw}:toggle",
        ),
        types.InlineKeyboardButton(
            "✉️ SMS",
            callback_data=f"support:from_admin:{uid_raw}",
        ),
    )

    # Кнопка "Открыть последнюю заявку" (если найдена)
    if last_order_ref and uid is not None:
        kind = str(last_order_ref.get("kind") or "")
        value = str(last_order_ref.get("value") or "")
        if kind in ("id", "code") and value:
            kb.add(
                types.InlineKeyboardButton(
                    "📌 Открыть последнюю заявку",
                    callback_data=f"db_user:{uid}:last_order:{kind}:{value}",
                )
            )

    kb.add(
        types.InlineKeyboardButton("💸 Комиссия", callback_data=f"db_user:{uid_raw}:commission"),
        types.InlineKeyboardButton("🔧 Роль", callback_data=f"db_user:{uid_raw}:role"),
    )
    kb.add(
        types.InlineKeyboardButton("💰 Реф. счёт", callback_data=f"db_user:{uid_raw}:ref:set"),
        types.InlineKeyboardButton("📄 Финал по заявке", callback_data=f"db_user:{uid_raw}:order:final"),
    )
    return kb


def _fmt_date(val: Optional[str]) -> str:
    if not val or val == "-":
        return "-"
    s = str(val).strip()
    try:
        s2 = s.replace("Z", "").replace("T", " ")
        dt = datetime.fromisoformat(s2)
        return f"{dt.day:02d}-{dt.month:02d}-{dt.year}"
    except Exception:
        return s


async def _render_user_card(
    message_or_cb: types.Message | types.CallbackQuery,
    user: dict,
) -> None:
    def _ru_status(raw: Optional[str]) -> str:
        if not raw:
            return "—"
        s = str(raw).strip().lower()
        mapping = {
            "completed": "✅ Завершена",
            "pending": "⏳ В ожидании",
            "processing": "⏳ В обработке",
            "paid": "💸 Оплачена",
            "canceled": "🚫 Отменена",
            "cancelled": "🚫 Отменена",
            "failed": "❌ Ошибка",
            "error": "❌ Ошибка",
            "expired": "⌛️ Истекла",
            "emergency": "⚠️ Аварийный режим",
        }
        return mapping.get(s, raw)

    async def _get_last_order_info(*, user_id: Optional[int], username: str) -> tuple[str, Optional[dict]]:
        """
        Возвращает:
        - строку для карточки пользователя
        - ссылку на заявку для кнопки (kind: id/code, value: ...)
        """
        try:
            db = await get_db()
            db.row_factory = aiosqlite.Row

            # 1) p2p_orders по user_id (самый точный источник)
            if isinstance(user_id, int):
                async with db.execute(
                    """
                    SELECT order_id, status, created_at
                      FROM p2p_orders
                     WHERE user_id = ?
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    (int(user_id),),
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        oid = row.get("order_id")
                        st = _ru_status(row.get("status"))
                        dt = _fmt_date(row.get("created_at") or "-")
                        display = f"#{oid} — {st} — {dt}"
                        ref = {"kind": "id", "value": str(oid)}
                        return display, ref

            # 2) Фоллбек: completed_p2p_orders по user_link=username
            uname = (username or "").strip().lstrip("@")
            if uname:
                async with db.execute(
                    """
                    SELECT order_code, created_at
                      FROM completed_p2p_orders
                     WHERE user_link = ?
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    (uname,),
                ) as cur2:
                    row2 = await cur2.fetchone()
                    if row2:
                        oc = row2.get("order_code")
                        dt = _fmt_date(row2.get("created_at") or "-")
                        display = f"{oc} — ✅ Завершена — {dt}"
                        ref = {"kind": "code", "value": str(oc)}
                        return display, ref

        except Exception:
            pass

        return "—", None

    user_id_raw = user.get("telegram_id", "-")
    username = user.get("username") or "-"
    role = user.get("role") or "-"
    created_at = _fmt_date(user.get("created_at") or "-")
    last_active = _fmt_date(user.get("last_active") or "-")
    ref = user.get("referrer_id") or "-"
    is_active = "✅" if user.get("is_active", 1) else "❌"
    btc_wallet = user.get("btc_wallet") or "—"

    uid_int: Optional[int]
    try:
        uid_int = int(user_id_raw) if (isinstance(user_id_raw, int) or str(user_id_raw).isdigit()) else None
    except Exception:
        uid_int = None

    # Последняя заявка (и ссылка для кнопки)
    last_order_line = "—"
    last_order_ref: Optional[dict] = None
    try:
        last_order_line, last_order_ref = await _get_last_order_info(
            user_id=uid_int,
            username=str(username),
        )
    except Exception:
        last_order_line, last_order_ref = "—", None

    commission_str = "—"
    try:
        if uid_int is not None:
            commission = await get_user_commission(uid_int)
            if commission is not None:
                commission_str = f"{float(commission):.2f}%"
    except Exception:
        pass

    referrals_count_str = "—"
    ref_balance_str = "—"
    try:
        if uid_int is not None:
            referrals_count = await get_referrals_count(uid_int)
            ref_balance = await get_referral_balance(uid_int)
            referrals_count_str = str(int(referrals_count or 0))
            ref_balance_str = f"{float(ref_balance or 0.0):.2f} RUB"
    except Exception:
        pass

    text = (
        f"<b>🗂 Карточка пользователя</b>\n\n"
        f"ID: <code>{user_id_raw}</code>\n"
        f"Username: @{username}\n"
        f"BTC-кошелёк: <code>{btc_wallet}</code>\n"
        f"Роль: {role}\n"
        f"Статус: {is_active}\n"
        f"Регистрация: {created_at}\n"
        f"Последняя активность: {last_active}\n"
        f"Последняя заявка: <b>{last_order_line}</b>\n"
        f"Реферер: {ref}\n"
        f"Комиссия: {commission_str}\n"
        f"Рефералов: {referrals_count_str}\n"
        f"Реф. счёт: {ref_balance_str}\n"
    )

    kb = _user_actions_kb(user, last_order_ref=last_order_ref)
    if isinstance(message_or_cb, types.Message):
        await message_or_cb.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_cb.message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _render_order_card(
    message_or_cb: types.Message | types.CallbackQuery,
    order: dict,
) -> None:
    def _ru_status(raw: Optional[str]) -> str:
        if not raw:
            return "—"
        s = str(raw).strip().lower()
        mapping = {
            "completed": "✅ Завершена",
            "pending": "⏳ В ожидании",
            "processing": "⏳ В обработке",
            "paid": "💸 Оплачена",
            "canceled": "🚫 Отменена",
            "cancelled": "🚫 Отменена",
            "failed": "❌ Ошибка",
            "error": "❌ Ошибка",
            "expired": "⌛️ Истекла",
            "emergency": "⚠️ Аварийный режим",
        }
        return mapping.get(s, raw)

    def _extract_txid(link_val: str) -> str:
        s = (link_val or "").strip()
        if not s or s == "—":
            return "—"

        # mempool.space: /tx/<id>
        if "/tx/" in s:
            h = s.split("/tx/", 1)[1].strip().strip("/")
            if "?" in h:
                h = h.split("?", 1)[0]
            return h or "—"

        # blockchair: /transaction/<id> | tronscan: #/transaction/<id>
        if "/transaction/" in s:
            h = s.split("/transaction/", 1)[1].strip().strip("/")
            if "?" in h:
                h = h.split("?", 1)[0]
            return h or "—"

        # если вдруг уже чистый txid
        if " " not in s and len(s) >= 16 and "http" not in s.lower():
            return s

        return "—"

    def _fmt_money_rub(v: Optional[Union[int, float, str]]) -> str:
        if v in (None, "", "—"):
            return "—"
        try:
            return f"{float(v):.2f} ₽"
        except (TypeError, ValueError):
            return "—"

    def _fmt_float(v: Optional[Union[int, float, str]], ndp: int = 8) -> str:
        if v in (None, "", "—"):
            return "—"
        try:
            s = f"{float(v):.{int(ndp)}f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s
        except (TypeError, ValueError):
            return "—"

    def _pm_label(pm: str) -> str:
        p = (pm or "").lower().strip()
        if p == "akkula":
            return "Akkula"
        if p == "paycore":
            return "Обычная (Paycore)"
        return pm or "—"

    order_id = order.get("order_id") or order.get("id") or "—"
    status = _ru_status(order.get("status"))
    created_at = _fmt_date(order.get("created_at") or "-")

    user_id = order.get("user_id") or "—"
    user_link = order.get("user_link") or "—"

    payment_method = str(order.get("payment_method") or "").strip()
    is_akkula = payment_method.lower() == "akkula"

    total_rub_str = _fmt_money_rub(order.get("total_rub"))
    crypto_amount_str = _fmt_float(order.get("btc_amount"), 8)

    wallet = str(order.get("wallet") or "—")

    tx_to = (order.get("tx_to") or "").strip()
    tx_link_str = tx_to if tx_to else "—"
    txid = _extract_txid(tx_to)

    # --- Akkula-данные (если есть) ---
    akk_partner_order_id = "—"
    akk_order_id = "—"
    akk_tx_hash = "—"
    akk_amount_usdt = "—"

    if is_akkula:
        try:
            from db.akkula_orders import get_akkula_order_by_p2p_order_id

            try:
                oid = int(order_id)
            except Exception:
                oid = None

            if oid is not None:
                akk = await get_akkula_order_by_p2p_order_id(oid)
                if akk:
                    akk_partner_order_id = str(akk.get("partner_order_id") or "—")
                    akk_order_id = str(akk.get("order_id") or "—")
                    akk_tx_hash = str(akk.get("tx_hash") or "—")
                    if akk.get("amount_usdt") is not None:
                        akk_amount_usdt = f"{float(akk.get('amount_usdt')):.2f}"
        except Exception:
            pass

    # --- Обычные поля (оператор/реквизиты) ---
    operator_id = order.get("operator_id") or "—"
    operator_username = order.get("operator_username") or "—"
    bank_name = str(order.get("bank_name") or "—").strip() or "—"
    bank_card = str(order.get("bank_card") or "—").strip() or "—"

    header = "🧾 <b>Заявка Akkula</b>" if is_akkula else "🧾 <b>Заявка</b>"

    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 ID: <code>{order_id}</code>",
        f"📌 Статус: <b>{status}</b>",
        f"🕒 Создана: {created_at}",
        "",
        "👤 <b>Пользователь</b>",
        f"• ID: <code>{user_id}</code>",
        f"• Ссылка/юзер: {user_link}",
        "",
        "💰 <b>Суммы</b>",
        f"• RUB: <b>{total_rub_str}</b>",
        f"• Crypto: <b>{crypto_amount_str}</b>",
        f"• Метод: <b>{_pm_label(payment_method)}</b>",
        "",
        "🏷 <b>Получатель</b>",
        f"• Кошелёк: <code>{wallet}</code>",
        "",
        "🔎 <b>Транзакция</b>",
        f"• TX id: <code>{txid}</code>",
        f"• TX link: {tx_link_str}",
    ]

    if is_akkula:
        # Для Akkula НЕ показываем реквизиты/оператора (по требованию)
        lines.extend(
            [
                "",
                "🧩 <b>Akkula</b>",
                f"• partner_order_id: <code>{akk_partner_order_id}</code>",
                f"• akkula order_id: <code>{akk_order_id}</code>",
                f"• tx_hash: <code>{akk_tx_hash}</code>",
                f"• amount_usdt: <b>{akk_amount_usdt}</b>",
            ]
        )
    else:
        # Для обычных показываем реквизиты и кто оформлял
        lines.extend(
            [
                "",
                "🧑‍💼 <b>Оформление</b>",
                f"• Оператор ID: <code>{operator_id}</code>",
                f"• Оператор: <b>{operator_username}</b>",
                "",
                "🏦 <b>Реквизиты оплаты</b>",
                f"• Банк: <b>{bank_name}</b>",
                f"• Реквизит (карта/СБП): <code>{bank_card}</code>",
            ]
        )

    text = "\n".join(lines)

    if isinstance(message_or_cb, types.Message):
        await message_or_cb.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await message_or_cb.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


async def admin_db_menu(message: types.Message, state: FSMContext) -> None:
    if not await _is_admin_message(message):
        return

    await message.answer(
        "Введите username (без @), Telegram ID, BTC-кошелёк, order_code (например: OP-00844) или номер заявки:"
    )
    await AdminDBStates.waiting_search.set()



async def admin_db_search(message: types.Message, state: FSMContext) -> None:
    if not await _is_admin_message(message):
        await state.finish()
        return

    query = (message.text or "").strip()
    user = None
    order = None

    q_norm = query.strip()
    q_nohash = q_norm.replace("#", "").strip()

    # 1) Сначала пробуем интерпретировать как заявку (ID / order_code)
    if q_nohash.isdigit():
        order = await get_order_by_id(int(q_nohash))

    if not order:
        order = await get_order_by_code(q_norm)

    if order:
        await _render_order_card(message, order)
        await state.finish()
        return

    # 2) Если не заявка — обычный поиск пользователя
    if q_nohash.isdigit():
        user = await get_user(int(q_nohash))

    if not user:
        user = await get_user_by_username(q_norm.replace("@", ""))

    if not user:
        user = await get_user_by_btc_wallet(q_norm)

    if not user:
        await message.answer("Пользователь или заявка не найдены.")
        await state.finish()
        return

    await _render_user_card(message, user)
    await state.finish()


async def admin_db_show_history(callback: types.CallbackQuery) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    user = await get_user(user_id)
    if not user:
        await callback.message.answer("Пользователь не найден.")
        return

    username = user.get("username") or ""
    db = await get_db()
    db.row_factory = aiosqlite.Row

    async with db.execute(
        """
        SELECT order_code, total_rub, created_at, bank_name
          FROM completed_p2p_orders
         WHERE user_link = ?
         ORDER BY created_at DESC
         LIMIT 10
        """,
        (username,),
    ) as cur:
        rows = await cur.fetchall()
        orders = [dict(r) for r in rows] if rows else []

    if not orders:
        await callback.message.answer("История пуста.")
    else:
        lines = [
            f"{o.get('order_code', '—')} — {float(o.get('total_rub', 0)):.2f} ₽ — "
            f"{o.get('created_at', '—')} — {o.get('bank_name', '—')}"
            for o in orders
        ]
        await callback.message.answer("Последние 10 заявок:\n" + "\n".join(lines))

    await _render_user_card(callback, user)


async def admin_db_toggle_active(callback: types.CallbackQuery) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    user = await get_user(user_id)
    if not user:
        await callback.message.answer("Пользователь не найден.")
        return

    current = await is_user_active(user_id)
    await set_user_active(user_id, not current)
    await _render_user_card(callback, {**user, "is_active": 0 if current else 1})


async def admin_db_start_message(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    await state.update_data(target_user_id=user_id)
    await callback.message.answer("Введите текст сообщения для пользователя:")
    await AdminDBMessageState.waiting_text.set()



async def admin_db_send_message(message: types.Message, state: FSMContext) -> None:
    if not await _is_admin_message(message):
        await state.finish()
        return

    data = await state.get_data()
    target_id = data.get("target_user_id")
    if not target_id:
        await state.finish()
        await message.answer("Сессия отправки сообщения не найдена.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пуст. Введите сообщение ещё раз.")
        return

    try:
        await message.bot.send_message(int(target_id), text)
        await message.answer("✅ Сообщение отправлено.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить. Возможно, пользователь не писал боту.")
    await state.finish()



async def admin_db_open_last_order(callback: types.CallbackQuery) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    parts = (callback.data or "").split(":")
    # ожидаем: db_user:<uid>:last_order:<kind>:<value>
    if len(parts) < 5:
        await callback.message.answer("Некорректные данные запроса.")
        return

    kind = parts[3]
    value = parts[4]

    order = None
    try:
        if kind == "id" and value.isdigit():
            order = await get_order_by_id(int(value))
        elif kind == "code":
            order = await get_order_by_code(value)
    except Exception:
        order = None

    if not order:
        await callback.message.answer("Заявка не найдена.")
        return

    await _render_order_card(callback, order)



async def admin_db_order_final(callback: types.CallbackQuery) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer("Функция «Финал по заявке» пока не подключена.", show_alert=True)



async def admin_db_start_commission(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    await state.update_data(target_user_id=user_id)
    await callback.message.answer(
        "Введите персональную комиссию в процентах (например, 1.5). Для сброса введите 0."
    )
    await AdminDBCommissionState.waiting_percent.set()



async def admin_db_set_commission(message: types.Message, state: FSMContext) -> None:
    if not await _is_admin_message(message):
        await state.finish()
        return

    data = await state.get_data()
    user_id = data.get("target_user_id")
    if not user_id:
        await state.finish()
        await message.answer("Сессия установки комиссии не найдена.")
        return

    raw = (message.text or "").replace(",", ".").strip()
    try:
        percent = float(raw)
        if percent < 0 or percent > 50:
            await message.answer("Введите число от 0 до 50.")
            return
    except ValueError:
        await message.answer("Некорректное число. Пример: 1.5")
        return

    await set_user_commission(int(user_id), percent)
    await message.answer(f"✅ Комиссия для пользователя {user_id} установлена: {percent:.2f}%")

    user = await get_user(int(user_id))
    if user:
        await _render_user_card(message, user)

    await state.finish()



async def admin_db_start_role(callback: types.CallbackQuery) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    await callback.message.answer("Выберите роль:", reply_markup=_role_select_kb(user_id))



async def admin_db_set_role(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.message.answer("Некорректные данные роли.")
        return

    try:
        user_id = int(parts[1])
    except Exception:
        await callback.message.answer("Некорректный user_id.")
        return

    role_name = parts[4]
    if role_name not in {"Admin", "User", "Operator", "Shop", "MasterCard"}:
        await callback.message.answer("Некорректная роль.")
        return

    from db.users import set_field

    await set_field(user_id, "role", role_name)

    await callback.message.answer(
        f"✅ Роль пользователя {user_id} установлена: <b>{role_name}</b>",
        parse_mode="HTML",
    )

    user = await get_user(user_id)
    if user:
        await _render_user_card(callback, user)



async def admin_db_start_ref_set(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await _is_admin_callback(callback):
        return

    await callback.answer()
    try:
        user_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.message.answer("Некорректные данные запроса.")
        return

    await state.finish()
    await state.update_data(target_user_id=user_id)
    await callback.message.answer(
        "Введите итоговую сумму реферального счёта в RUB.\n"
        "Пример: 0, 500, 1250.75"
    )
    await AdminDBReferralAdjustState.waiting_amount.set()


async def admin_db_apply_ref_set(message: types.Message, state: FSMContext) -> None:
    if not await _is_admin_message(message):
        await state.finish()
        return

    data = await state.get_data()
    user_id = data.get("target_user_id")

    if not user_id:
        await state.finish()
        await message.answer("Сессия установки реф. счёта не найдена.")
        return

    raw = (message.text or "").replace(",", ".").strip()
    try:
        target_amount = float(raw)
        if target_amount < 0 or target_amount > 10_000_000:
            await message.answer("Введите сумму ≥ 0 и разумную по размеру.")
            return
    except ValueError:
        await message.answer("Некорректная сумма. Пример: 1500 или 1500.50")
        return

    try:
        current_balance = await get_referral_balance(int(user_id))
        delta = round(target_amount - float(current_balance or 0.0), 2)

        if delta != 0:
            await add_referral_adjustment(
                referrer_id=int(user_id),
                admin_id=int(message.from_user.id),
                amount=float(delta),
                reason="admin_set_balance",
            )
    except Exception:
        await state.finish()
        await message.answer("⚠️ Не удалось установить реф. счёт.")
        return

    await message.answer(
        f"✅ Реферальный счёт пользователя {user_id} установлен: {target_amount:.2f} RUB"
    )

    user = await get_user(int(user_id))
    if user:
        await _render_user_card(message, user)

    await state.finish()



def register_admin_db_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(
        admin_db_menu,
        lambda m: m.text == "🗄️ БД",
        state="*",
    )
    dp.register_message_handler(
        admin_db_search,
        state=AdminDBStates.waiting_search,
    )

    dp.register_callback_query_handler(
        admin_db_show_history,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":history"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_db_toggle_active,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":toggle"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_db_start_message,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":message"),
        state="*",
    )
    dp.register_message_handler(
        admin_db_send_message,
        state=AdminDBMessageState.waiting_text,
    )

    dp.register_callback_query_handler(
        admin_db_open_last_order,
        lambda c: (c.data or "").startswith("db_user:") and ":last_order:" in (c.data or ""),
        state="*",
    )

    dp.register_callback_query_handler(
        admin_db_order_final,
        lambda c: (c.data or "").startswith("db_user:") and (c.data or "").endswith(":order:final"),
        state="*",
    )

    dp.register_callback_query_handler(
        admin_db_start_commission,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":commission"),
        state="*",
    )
    dp.register_message_handler(
        admin_db_set_commission,
        state=AdminDBCommissionState.waiting_percent,
    )

    dp.register_callback_query_handler(
        admin_db_start_role,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":role"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_db_set_role,
        lambda c: c.data.startswith("db_user:") and ":role:set:" in c.data,
        state="*",
    )

    dp.register_callback_query_handler(
        admin_db_start_ref_set,
        lambda c: c.data.startswith("db_user:") and c.data.endswith(":ref:set"),
        state="*",
    )
    dp.register_message_handler(
        admin_db_apply_ref_set,
        state=AdminDBReferralAdjustState.waiting_amount,
        content_types=types.ContentTypes.TEXT,
    )
