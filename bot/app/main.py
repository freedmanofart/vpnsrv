import asyncio
import html
from io import BytesIO
import logging
import os

import httpx
import qrcode
from app.content import CONTENT, link as content_link, platform as get_platform, text as content_text
from app.domain import (
    country_label,
    rotation_payload,
    subscription_payload,
    supports_threexui,
)
from app.logging_config import configure_logging
from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
    BotCommand,
    MenuButtonCommands,
)


configure_logging(os.getenv("LOG_LEVEL", "INFO"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "http://api:8000")
SERVICE_API_TOKEN = os.environ["SERVICE_API_TOKEN"]
TELEGRAM_CHANNEL_URL = content_link("channel")
SUPPORT_URL = content_link("support")
YOOMONEY_PAYMENT_URL = content_link("payment")
TRY_PAYMENT_URL = content_link("try_payment")
PUBLIC_PLAN_CODES = tuple(
    item.strip()
    for item in os.getenv("BOT_PLAN_CODES", "vpn_14d,vpn_30d,vpn_90d").split(",")
    if item.strip()
)


class PromoFlow(StatesGroup):
    waiting_code = State()


def api_client(*, base_url: str = API_URL, timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        headers={"Authorization": f"Bearer {SERVICE_API_TOKEN}"},
    )

router = Router()


# =========================================================
# Keyboards
# =========================================================

def main_menu() -> InlineKeyboardMarkup:
    channel_button = (
        InlineKeyboardButton(text="📣 Наш канал", url=TELEGRAM_CHANNEL_URL)
        if TELEGRAM_CHANNEL_URL
        else InlineKeyboardButton(text="📣 Наш канал", callback_data="channel_info")
    )
    support_button = (
        InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)
        if SUPPORT_URL
        else InlineKeyboardButton(text="🆘 Поддержка", callback_data="support_info")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить",
                    callback_data="buy_vpn",
                ),
            ],
            [
                InlineKeyboardButton(text="👤 Личный кабинет", callback_data="vpn_status"),
            ],
            [
                InlineKeyboardButton(text="🏷 Промокод", callback_data="promo_start"),
                InlineKeyboardButton(text="🧪 Попробовать", callback_data="try_start"),
            ],
            [
                InlineKeyboardButton(text="📖 Инструкции", callback_data="instructions"),
                support_button,
            ],
            [channel_button],
            *[
                [InlineKeyboardButton(text=item["text"], url=item["url"])]
                for item in CONTENT.get("main_url_buttons", [])
                if item.get("text") and item.get("url")
            ],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="main_menu",
                ),
            ],
        ]
    )


def vpn_ready_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📡 Мой VPN",
                    callback_data="vpn_status",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="main_menu",
                ),
            ],
        ]
    )


def active_vpn_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Показать ключ и QR", callback_data="vpn_key")],
        [InlineKeyboardButton(text="🔄 Перевыпустить / сменить страну", callback_data="vpn_reissue")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main_menu")],
    ])


async def show_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    *,
    parse_mode: str = "HTML",
) -> None:
    """Edit text messages and safely continue from QR/photo messages."""
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        if callback.message.photo:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        raise


def platforms_keyboard(*, purchase: bool = False) -> InlineKeyboardMarkup:
    prefix = "purchase_device" if purchase else "device"
    rows = [
        [InlineKeyboardButton(text=item["label"], callback_data=f"{prefix}:{item['id']}")]
        for item in CONTENT.get("platforms", [])
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_for_plan(plan: dict) -> str:
    return content_link(f"payment_{plan.get('code', '')}") or YOOMONEY_PAYMENT_URL


# =========================================================
# API
# =========================================================

async def get_or_create_user(message: Message) -> dict:
    telegram_user = message.from_user

    if telegram_user is None:
        raise RuntimeError("Telegram user is unavailable")

    telegram_id = telegram_user.id

    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get(
            f"/users/{telegram_id}"
        )

        if response.status_code == 200:
            return response.json()

        if response.status_code != 404:
            response.raise_for_status()

        response = await client.post(
            "/users",
            json={
                "telegram_id": telegram_id,
                "username": telegram_user.username,
                "first_name": telegram_user.first_name,
                "last_name": telegram_user.last_name,
            },
        )

        if response.status_code == 409:
            response = await client.get(
                f"/users/{telegram_id}"
            )

        response.raise_for_status()

        return response.json()


async def get_vpn_status(telegram_id: int) -> dict:
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get(
            f"/users/{telegram_id}/vpn-status"
        )

        response.raise_for_status()

        return response.json()


async def get_plans() -> list[dict]:
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get("/plans")

        response.raise_for_status()

        plans = response.json()
        selected = [plan for plan in plans if plan.get("code") in PUBLIC_PLAN_CODES]
        if not selected:
            return plans
        order = {code: index for index, code in enumerate(PUBLIC_PLAN_CODES)}
        return sorted(selected, key=lambda plan: order.get(plan.get("code"), 999))


async def get_nodes() -> list[dict]:
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get("/vpn/nodes")

        response.raise_for_status()

        return response.json()


async def get_node_configs(node_id: int) -> list[dict]:
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get(
            f"/vpn/nodes/{node_id}/configs"
        )

        response.raise_for_status()

        return response.json()


async def create_payment(
    user_id: int,
    plan_id: int,
    node_id: int,
    idempotency_key: str,
) -> dict:
    payload = subscription_payload(
        user_id=user_id,
        plan_id=plan_id,
        node_id=node_id,
    )
    payload["idempotency_key"] = idempotency_key
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.post(
            "/payments",
            json=payload,
        )

        response.raise_for_status()

        return response.json()


async def get_vpn_client_config(client_id: int) -> dict:
    async with api_client(base_url=API_URL, timeout=10.0) as client:
        response = await client.get(f"/vpn/clients/{client_id}/config")
        response.raise_for_status()
        return response.json()


async def rotate_vpn_client(subscription_id: int, node_id: int) -> dict:
    async with api_client(base_url=API_URL, timeout=15.0) as client:
        response = await client.post(
            f"/subscriptions/{subscription_id}/rotate",
            json=rotation_payload(node_id),
        )
        response.raise_for_status()
        return response.json()


async def create_access_grant(
    telegram_id: int,
    kind: str,
    node_id: int,
    code: str | None = None,
) -> dict:
    async with api_client(base_url=API_URL, timeout=15.0) as client:
        response = await client.post(
            "/subscriptions/access-grants",
            json={
                "telegram_id": telegram_id,
                "kind": kind,
                "code": code,
                "node_id": node_id,
                "client_type": "universal",
                "flow": "",
                "fingerprint": "firefox",
            },
        )
        response.raise_for_status()
        return response.json()


async def available_nodes() -> list[dict]:
    candidates = [
        node
        for node in await get_nodes()
        if node.get("status") == "active"
        and node.get("health_status") != "offline"
        and node.get("active_connections", 0) < node.get("capacity", 0)
        and country_label(node.get("region"))
    ]
    if not candidates:
        return []
    config_results = await asyncio.gather(
        *(get_node_configs(node["id"]) for node in candidates),
        return_exceptions=True,
    )
    return [
        node
        for node, configs in zip(candidates, config_results, strict=True)
        if not isinstance(configs, Exception) and supports_threexui(configs)
    ]


def qr_file(value: str) -> BufferedInputFile:
    image = qrcode.make(value)
    output = BytesIO()
    image.save(output, format="PNG")
    return BufferedInputFile(output.getvalue(), filename="vpn-key.png")


async def send_key_message(message: Message, client_id: int) -> None:
    data = await get_vpn_client_config(client_id)
    value = data["config"]
    instruction = "Импортируйте ссылку или QR-код в совместимое VLESS-приложение."
    await message.answer_photo(
        photo=qr_file(value),
        caption=f"🔑 <b>Ваш VPN-ключ</b>\n\n<code>{html.escape(value)}</code>\n\n{instruction}",
        parse_mode="HTML",
        reply_markup=active_vpn_keyboard(),
    )


async def create_vpn_client(
    user_id: int,
    subscription_id: int,
    node_id: int,
) -> dict:
    async with api_client(
        base_url=API_URL,
        timeout=15.0,
    ) as client:

        response = await client.post(
            "/vpn/clients",
            json={
                "user_id": user_id,
                "subscription_id": subscription_id,
                "node_id": node_id,
                "protocol": "vless",
            },
        )

        response.raise_for_status()

        return response.json()


# =========================================================
# /start
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message):
    try:
        user = await get_or_create_user(message)

        logging.info(
            "Telegram user registered: telegram_id=%s db_id=%s",
            message.from_user.id,
            user["id"],
        )

        await message.answer(
            content_text("welcome"),
            reply_markup=main_menu(),
            parse_mode="HTML",
        )

    except httpx.HTTPError:
        logging.exception(
            "Failed to communicate with API"
        )

        await message.answer(
            "❌ Не удалось связаться с сервером.\n"
            "Попробуй ещё раз через несколько секунд."
        )

    except Exception:
        logging.exception(
            "Unexpected error in /start"
        )

        await message.answer(
            "❌ Произошла ошибка.\n"
            "Попробуй ещё раз."
        )


@router.message(Command("menu"))
async def menu_command_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        content_text("main_menu"),
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(Command("vpn"))
async def vpn_command_handler(message: Message):
    try:
        data = await get_vpn_status(message.from_user.id)
        subscription = data.get("subscription")
        client = data.get("vpn_client")
        if not subscription:
            text = "📡 <b>Мой VPN</b>\n\nУ вас пока нет активной подписки."
            keyboard = main_menu()
        else:
            expires_at = (subscription.get("expires_at") or "—").replace("T", " ").replace("+00:00", "")
            text = (
                "📡 <b>Мой VPN</b>\n\n"
                f"Подписка: {'🟢 Активна' if subscription.get('status') == 'active' else '🔴 Истекла'}\n"
                f"VPN: {'🟢 Подключён' if client else '🔴 Не настроен'}\n"
                f"Действует до: <code>{expires_at} UTC</code>"
            )
            keyboard = active_vpn_keyboard() if client else main_menu()
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        logging.exception("Failed to show VPN status from command")
        await message.answer("❌ Не удалось получить данные VPN.", reply_markup=main_menu())


@router.message(Command("buy"))
async def buy_command_handler(message: Message):
    await message.answer(
        content_text("platforms_intro"),
        reply_markup=platforms_keyboard(purchase=True),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def help_command_handler(message: Message):
    await message.answer(
        content_text("instructions"),
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "instructions")
async def instructions_handler(callback: CallbackQuery):
    await show_screen(callback, content_text("instructions"), back_menu())
    await callback.answer()


@router.callback_query(F.data == "devices")
async def devices_handler(callback: CallbackQuery):
    await show_screen(
        callback,
        content_text("platforms_intro"),
        platforms_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("device:"))
async def device_handler(callback: CallbackQuery):
    platform_id = callback.data.split(":", 1)[1]
    item = get_platform(platform_id)
    if not item:
        await callback.answer("Устройство не найдено", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⬇️ Скачать {item['client']}", url=item["url"])],
            [InlineKeyboardButton(text="💳 Оплатить VPN", callback_data=f"purchase_device:{platform_id}")],
            [InlineKeyboardButton(text="⬅️ К устройствам", callback_data="devices")],
        ]
    )
    await show_screen(
        callback,
        f"{item['label']} <b>{item['client']}</b>\n\n{item['description']}",
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "try_start")
async def try_start_handler(callback: CallbackQuery):
    rows = []
    if TRY_PAYMENT_URL:
        rows.append([InlineKeyboardButton(text="💳 Оплатить 50 ₽", url=TRY_PAYMENT_URL)])
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton(text="✅ Я оплатил — поддержка", url=SUPPORT_URL)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main_menu")])
    await show_screen(
        callback,
        content_text("try"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    if TRY_PAYMENT_URL:
        await callback.answer()
    else:
        await callback.answer("Ссылка оплаты пока не настроена", show_alert=True)


@router.callback_query(F.data == "channel_info")
async def channel_info_handler(callback: CallbackQuery):
    await callback.answer(content_text("channel_missing"), show_alert=True)


@router.callback_query(F.data == "support_info")
async def support_info_handler(callback: CallbackQuery):
    await callback.answer(content_text("support_missing"), show_alert=True)


def access_node_keyboard(nodes: list[dict], prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=country_label(node["region"]),
                    callback_data=f"{prefix}:{node['id']}",
                )
            ]
            for node in nodes
        ]
        + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]]
    )


async def finish_access_grant(
    callback: CallbackQuery,
    *,
    kind: str,
    node_id: int,
    code: str | None = None,
) -> None:
    subscription = await create_access_grant(
        callback.from_user.id,
        kind,
        node_id,
        code,
    )
    status = await get_vpn_status(callback.from_user.id)
    client = status.get("vpn_client")
    expires_at = subscription["expires_at"].replace("T", " ").replace("+00:00", "")
    label = "Тестовый доступ" if kind == "trial" else "Промокод применён"
    await callback.message.edit_text(
        f"✅ <b>{label}</b>\n\nДоступ действует до <b>{expires_at} UTC</b>.",
        parse_mode="HTML",
        reply_markup=vpn_ready_keyboard(),
    )
    if client:
        await send_key_message(callback.message, client["id"])
    await callback.answer("VPN активирован")


@router.callback_query(F.data == "promo_start")
async def promo_start_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoFlow.waiting_code)
    await show_screen(callback, content_text("promo"), back_menu())
    await callback.answer()


@router.message(PromoFlow.waiting_code)
async def promo_code_handler(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not code or len(code) > 64:
        await message.answer("Введите корректный промокод.")
        return
    await state.update_data(code=code)
    nodes = await available_nodes()
    if not nodes:
        await message.answer("Сейчас нет доступных серверов.", reply_markup=main_menu())
        await state.clear()
        return
    await message.answer(
        "🌍 <b>Промокод принят для проверки.</b> Выберите страну:",
        parse_mode="HTML",
        reply_markup=access_node_keyboard(nodes, "promo_country"),
    )


@router.callback_query(F.data.startswith("promo_country:"))
async def promo_country_handler(callback: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        code = data.get("code")
        if not code:
            await callback.answer("Сначала введите промокод", show_alert=True)
            return
        node_id = int(callback.data.split(":")[1])
        await finish_access_grant(
            callback,
            kind="promo",
            node_id=node_id,
            code=code,
        )
        await state.clear()
    except httpx.HTTPStatusError as exc:
        message = "Промокод неверный или уже использован" if exc.response.status_code in {404, 409} else "Не удалось применить промокод"
        await callback.answer(message, show_alert=True)
    except Exception:
        logging.exception("Promo activation failed")
        await callback.answer("Не удалось применить промокод", show_alert=True)


# =========================================================
# VPN STATUS
# =========================================================

@router.callback_query(F.data == "vpn_status")
async def vpn_status_handler(callback: CallbackQuery):
    try:
        telegram_id = callback.from_user.id

        data = await get_vpn_status(telegram_id)

        subscription = data.get("subscription")
        vpn_client = data.get("vpn_client")

        if not subscription:
            text = (
                "📡 <b>Мой VPN</b>\n\n"
                "У вас пока нет активной подписки.\n\n"
                "Выберите действие:"
            )

        else:
            if subscription["status"] == "active":
                subscription_status = "🟢 Активна"
            else:
                subscription_status = "🔴 Истекла"

            expires_at = subscription.get("expires_at")

            if expires_at:
                expires_at = (
                    expires_at
                    .replace("T", " ")
                    .replace("+00:00", "")
                )
            else:
                expires_at = "—"

            if vpn_client:
                vpn_status = "🟢 Подключён"
                protocol = vpn_client.get(
                    "protocol",
                    "unknown",
                )
            else:
                vpn_status = "🔴 Не настроен"
                protocol = "—"

            text = (
                "📡 <b>Мой VPN</b>\n\n"
                f"Подписка: {subscription_status}\n"
                f"VPN: {vpn_status}\n"
                f"Протокол: <code>{protocol}</code>\n"
                f"Действует до: "
                f"<code>{expires_at} UTC</code>"
            )

        await show_screen(
            callback,
            text,
            active_vpn_keyboard() if vpn_client else main_menu(),
        )

        await callback.answer()

    except httpx.HTTPError:
        logging.exception(
            "Failed to get VPN status"
        )

        await callback.answer(
            "❌ Не удалось получить данные",
            show_alert=True,
        )

    except Exception:
        logging.exception(
            "Unexpected VPN status error"
        )

        await callback.answer(
            "❌ Произошла ошибка",
            show_alert=True,
        )


@router.callback_query(F.data == "vpn_key")
async def vpn_key_handler(callback: CallbackQuery):
    try:
        status_data = await get_vpn_status(callback.from_user.id)
        client = status_data.get("vpn_client")
        if not client:
            await callback.answer("Активный ключ не найден", show_alert=True)
            return
        await send_key_message(callback.message, client["id"])
        await callback.answer()
    except Exception:
        logging.exception("Failed to show VPN key")
        await callback.answer("Не удалось получить ключ", show_alert=True)


@router.callback_query(F.data == "vpn_reissue")
async def vpn_reissue_handler(callback: CallbackQuery):
    try:
        status_data = await get_vpn_status(callback.from_user.id)
        subscription = status_data.get("subscription")
        if not subscription or subscription.get("status") != "active":
            await callback.answer("Активная подписка не найдена", show_alert=True)
            return
        nodes = await available_nodes()
        if not nodes:
            await callback.answer(
                "Нет доступных нод 3x-ui для выпуска ключа",
                show_alert=True,
            )
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=country_label(n["region"]), callback_data=f"rotate_country:{subscription['id']}:{n['id']}")]
            for n in nodes
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn_status")]])
        await show_screen(callback, "🌍 <b>Куда перенести подключение?</b>", keyboard)
        await callback.answer()
    except Exception:
        logging.exception("Failed to prepare VPN reissue")
        await callback.answer("Не удалось открыть перевыпуск", show_alert=True)


@router.callback_query(F.data.startswith("rotate_country:"))
async def rotate_country_handler(callback: CallbackQuery):
    try:
        _, subscription_id, node_id = callback.data.split(":")
        client = await rotate_vpn_client(int(subscription_id), int(node_id))
        await show_screen(
            callback,
            "✅ Новый ключ создан на выбранной ноде 3x-ui. Старый ключ отозван.",
            vpn_ready_keyboard(),
        )
        await send_key_message(callback.message, client["id"])
        await callback.answer("Ключ обновлён")
    except httpx.HTTPStatusError as exc:
        logging.exception("3x-ui rejected VPN key rotation")
        detail = ""
        try:
            detail = str(exc.response.json().get("detail", ""))
        except ValueError:
            pass
        await callback.answer(
            f"Не удалось создать ключ в 3x-ui{': ' + detail if detail else ''}",
            show_alert=True,
        )
    except Exception:
        logging.exception("Failed to rotate VPN key")
        await callback.answer("Не удалось перевыпустить ключ", show_alert=True)


# =========================================================
# BUY VPN
# =========================================================

@router.callback_query(F.data == "buy_vpn")
async def buy_vpn_handler(callback: CallbackQuery):
    await show_screen(
        callback,
        content_text("platforms_intro"),
        platforms_keyboard(purchase=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("purchase_device:"))
async def purchase_device_handler(callback: CallbackQuery):
    platform_id = callback.data.split(":", 1)[1]
    item = get_platform(platform_id)
    if not item:
        await callback.answer("Устройство не найдено", show_alert=True)
        return
    nodes = await available_nodes()
    if not nodes:
        await callback.answer("Сейчас нет доступных серверов", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=country_label(node["region"]),
                    callback_data=f"purchase_country:{node['id']}",
                )
            ]
            for node in nodes
        ]
        + [[InlineKeyboardButton(text="⬅️ К устройствам", callback_data="buy_vpn")]]
    )
    await show_screen(
        callback,
        f"{item['label']} <b>{item['client']}</b>\n\n"
        f"{item['description']}\n\n🌍 <b>Выберите страну подключения</b>",
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("purchase_country:"))
async def purchase_country_handler(callback: CallbackQuery):
    node_id = int(callback.data.split(":", 1)[1])
    plans = await get_plans()
    if not plans:
        await callback.answer("Сейчас нет доступных тарифов", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{plan['name']} — {plan['price']} {plan['currency']}",
                    callback_data=f"purchase_plan:{plan['id']}:{node_id}",
                )
            ]
            for plan in plans
        ]
        + [[InlineKeyboardButton(text="⬅️ К странам", callback_data="buy_vpn")]]
    )
    await show_screen(
        callback,
        "🗓 <b>Выберите срок</b>\n\nТариф выбирается последним шагом перед оплатой.",
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("purchase_plan:"))
async def purchase_plan_handler(callback: CallbackQuery):
    _, raw_plan_id, raw_node_id = callback.data.split(":")
    plan_id = int(raw_plan_id)
    node_id = int(raw_node_id)
    plans = await get_plans()
    nodes = await get_nodes()
    plan = next((item for item in plans if item["id"] == plan_id), None)
    node = next((item for item in nodes if item["id"] == node_id), None)
    if not plan or not node:
        await callback.answer("Тариф или сервер недоступен", show_alert=True)
        return
    payment_url = payment_url_for_plan(plan)
    rows = []
    if payment_url:
        rows.extend(
            [
                [InlineKeyboardButton(text="💳 Перейти в ЮMoney", url=payment_url)],
                [InlineKeyboardButton(text="📷 Показать QR оплаты", callback_data=f"payment_qr:{plan_id}:{node_id}")],
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"pay_qr:{plan_id}:{node_id}")],
            [InlineKeyboardButton(text="⬅️ К тарифам", callback_data=f"purchase_country:{node_id}")],
        ]
    )
    await show_screen(
        callback,
        "💳 <b>Оплата</b>\n\n"
        f"Страна: <b>{country_label(node.get('region')) or node['name']}</b>\n"
        "Ключ: <b>VLESS Reality xHTTP</b>\n"
        f"Тариф: <b>{plan['name']}</b>\n"
        f"Стоимость: <b>{plan['price']} {plan['currency']}</b>\n\n"
        "Оплатите через ЮMoney, затем нажмите «Проверить оплату».",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    if payment_url:
        await callback.answer()
    else:
        await callback.answer("Ссылка ЮMoney пока не настроена", show_alert=True)


@router.callback_query(F.data.startswith("payment_qr:"))
async def payment_qr_handler(callback: CallbackQuery):
    _, raw_plan_id, raw_node_id = callback.data.split(":")
    plan_id = int(raw_plan_id)
    plans = await get_plans()
    plan = next((item for item in plans if item["id"] == plan_id), None)
    payment_url = payment_url_for_plan(plan or {})
    if not plan or not payment_url:
        await callback.answer("Ссылка ЮMoney пока не настроена", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Открыть ЮMoney", url=payment_url)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"pay_qr:{plan_id}:{raw_node_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"purchase_plan:{plan_id}:{raw_node_id}")],
        ]
    )
    await callback.message.answer_photo(
        photo=qr_file(payment_url),
        caption=f"📷 QR для оплаты тарифа «{html.escape(plan['name'])}»",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_qr:"))
async def pay_qr_handler(
    callback: CallbackQuery,
):
    try:
        telegram_id = callback.from_user.id

        _, raw_plan_id, raw_node_id = callback.data.split(":")
        plan_id = int(raw_plan_id)
        node_id = int(raw_node_id)

        # -------------------------------------------------
        # User
        # -------------------------------------------------

        async with api_client(
            base_url=API_URL,
            timeout=10.0,
        ) as client:

            response = await client.get(
                f"/users/{telegram_id}"
            )

            response.raise_for_status()

            user = response.json()

        user_id = user["id"]

        # -------------------------------------------------
        # Check plans
        # -------------------------------------------------

        plans = await get_plans()

        plan = next(
            (
                item
                for item in plans
                if item["id"] == plan_id
            ),
            None,
        )

        if not plan:
            await callback.answer(
                "❌ Тариф больше недоступен.",
                show_alert=True,
            )
            return

        # -------------------------------------------------
        # Check existing subscription
        # -------------------------------------------------

        current_status = await get_vpn_status(
            telegram_id
        )

        if current_status.get("subscription"):
            subscription = current_status["subscription"]

            if subscription.get("status") == "active":
                await callback.answer(
                    "У вас уже есть активный VPN.",
                    show_alert=True,
                )
                return

        # -------------------------------------------------
        # Creating subscription
        # -------------------------------------------------

        await show_screen(
            callback,
            "⏳ <b>Проверяем платёж...</b>\n\n"
            "После подтверждения VPN активируется автоматически.",
            back_menu(),
        )

        payment = await create_payment(
            user_id=user_id,
            plan_id=plan_id,
            node_id=node_id,
            idempotency_key=f"telegram:{callback.id}",
        )

        if payment["status"] != "paid" or not payment.get("subscription_id"):
            await show_screen(
                callback,
                "⏳ Платёж создан и ожидает подтверждения провайдера.",
                main_menu(),
            )
            await callback.answer("Ожидаем подтверждение платежа")
            return

        status_after_payment = await get_vpn_status(telegram_id)
        subscription = status_after_payment.get("subscription")
        if not subscription:
            raise RuntimeError("Paid payment did not create a subscription")

        logging.info(
            "Subscription created: user_id=%s subscription_id=%s",
            user_id,
            subscription["id"],
        )

        # -------------------------------------------------
        # Get the client atomically created with subscription
        # -------------------------------------------------
        status = await get_vpn_status(telegram_id)
        client_data = status.get("vpn_client")
        if not client_data:
            raise RuntimeError("VPN client was not created")
        config_data = await get_vpn_client_config(client_data["id"])
        vless_url = config_data["config"]

        # -------------------------------------------------
        # Success
        # -------------------------------------------------

        expires_at = subscription["expires_at"]

        expires_at = (
            expires_at
            .replace("T", " ")
            .replace("+00:00", "")
        )

        text = (
            "✅ <b>VPN успешно активирован!</b>\n\n"
            f"📦 Тариф: <b>{plan['name']}</b>\n"
            f"💰 Стоимость: "
            f"<b>{plan['price']} {plan['currency']}</b>\n"
            f"📅 Действует до: "
            f"<b>{expires_at} UTC</b>\n\n"
            "🔐 <b>VLESS Reality xHTTP</b>\n\n"
            "Скопируйте ссылку ниже и импортируйте в VLESS-приложение:"
        )

        text += (
            f"\n\n<code>{html.escape(vless_url)}</code>"
        )
        await show_screen(callback, text, vpn_ready_keyboard())

        await callback.answer(
            "✅ VPN активирован!"
        )
        await send_key_message(callback.message, client_data["id"])

    except httpx.HTTPStatusError as exc:
        logging.exception(
            "VPN purchase API error"
        )

        status = exc.response.status_code

        if status == 409:
            message = (
                "⚠️ У вас уже есть активная "
                "подписка или VPN-клиент."
            )

        elif status == 502:
            message = (
                "❌ Не удалось создать VPN-ключ "
                "в 3x-ui."
            )

        else:
            message = (
                "❌ Сервер не смог создать VPN."
            )

        await callback.answer(
            message,
            show_alert=True,
        )

    except httpx.HTTPError:
        logging.exception(
            "VPN purchase HTTP error"
        )

        await callback.answer(
            "❌ Не удалось связаться с API.",
            show_alert=True,
        )

    except Exception:
        logging.exception(
            "Unexpected VPN purchase error"
        )

        await callback.answer(
            "❌ Не удалось активировать VPN.",
            show_alert=True,
        )


# =========================================================
# MAIN MENU
# =========================================================

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    try:
        await show_screen(callback, content_text("main_menu"), main_menu())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise

    await callback.answer()


# =========================================================
# Main
# =========================================================

async def main():
    bot = Bot(token=BOT_TOKEN)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="vpn", description="Мой VPN"),
            BotCommand(command="buy", description="Купить VPN"),
            BotCommand(command="help", description="Инструкция"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    dp = Dispatcher()

    dp.include_router(router)

    logging.info(
        "VPN bot started, API_URL=%s",
        API_URL,
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
