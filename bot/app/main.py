import asyncio
import html
from io import BytesIO
import logging
import os
from urllib.parse import urlencode

import httpx
import qrcode
from app.domain import country_label, profile_flow, rotation_payload, subscription_payload
from app.logging_config import configure_logging
from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
)


configure_logging(os.getenv("LOG_LEVEL", "INFO"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "http://api:8000")
SERVICE_API_TOKEN = os.environ["SERVICE_API_TOKEN"]
TELEGRAM_CHANNEL_URL = os.getenv("TELEGRAM_CHANNEL_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()


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
                    text="📡 Мой VPN",
                    callback_data="vpn_status",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Купить VPN",
                    callback_data="buy_vpn",
                ),
            ],
            [
                InlineKeyboardButton(text="🎁 Тест на 3 дня", callback_data="trial_start"),
                InlineKeyboardButton(text="🏷 Промокод", callback_data="promo_start"),
            ],
            [
                InlineKeyboardButton(text="📖 Инструкция", callback_data="instructions"),
                support_button,
            ],
            [channel_button],
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


def plans_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    buttons = []

    for plan in plans:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"💳 {plan['name']} — "
                        f"{plan['price']} {plan['currency']}"
                    ),
                    callback_data=f"buy_plan:{plan['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="main_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
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

        return response.json()


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
    client_type: str,
    flow: str,
    idempotency_key: str,
) -> dict:
    profile = "vision" if flow == "xtls-rprx-vision" else "standard"
    payload = subscription_payload(
        user_id=user_id,
        plan_id=plan_id,
        node_id=node_id,
        client_type=client_type,
        profile=profile,
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


async def rotate_vpn_client(subscription_id: int, node_id: int, client_type: str, flow: str) -> dict:
    profile = "vision" if flow == "xtls-rprx-vision" else "standard"
    async with api_client(base_url=API_URL, timeout=15.0) as client:
        response = await client.post(
            f"/subscriptions/{subscription_id}/rotate",
            json=rotation_payload(node_id, client_type, profile),
        )
        response.raise_for_status()
        return response.json()


async def create_access_grant(
    telegram_id: int,
    kind: str,
    node_id: int,
    client_type: str,
    flow: str,
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
                "client_type": client_type,
                "flow": flow,
                "fingerprint": "chrome",
            },
        )
        response.raise_for_status()
        return response.json()


async def available_nodes() -> list[dict]:
    return [
        node
        for node in await get_nodes()
        if node.get("status") == "active"
        and node.get("health_status") != "offline"
        and node.get("active_connections", 0) < node.get("capacity", 0)
        and country_label(node.get("region"))
    ]


def qr_file(value: str) -> BufferedInputFile:
    image = qrcode.make(value)
    output = BytesIO()
    image.save(output, format="PNG")
    return BufferedInputFile(output.getvalue(), filename="vpn-key.png")


async def send_key_message(message: Message, client_id: int, client_type: str = "universal") -> None:
    data = await get_vpn_client_config(client_id)
    value = data["config"]
    instruction = (
        "AmneziaVPN → Добавить → Вставить ключ из буфера."
        if client_type == "amnezia"
        else "Импортируйте ссылку или QR-код в совместимое VLESS-приложение."
    )
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
# VLESS
# =========================================================

def build_vless_url(
    client_uuid: str,
    node: dict,
    config: dict,
) -> str:
    host = config["host"]
    port = config["port"]

    params = {
        "type": config.get("type", "tcp"),
        "security": config.get("security", "reality"),
        "sni": config["sni"],
        "fp": config["fp"],
        "pbk": config["pbk"],
        "sid": config["sid"],
    }

    query = urlencode(params)

    name = node.get("name", "VPN")

    return (
        f"vless://{client_uuid}"
        f"@{host}:{port}"
        f"?{query}"
        f"#{name}"
    )


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
            "👋 <b>Добро пожаловать в VPN-сервис!</b>\n\n"
            "• VLESS Reality и XTLS Vision\n"
            "• серверы в США, Нидерландах и Германии\n"
            "• ключ для AmneziaVPN или универсального VLESS\n\n"
            "Можно начать с бесплатного теста на 3 дня.",
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


@router.callback_query(F.data == "instructions")
async def instructions_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "📖 <b>Как подключиться</b>\n\n"
        "1. Купите тариф или включите тест на 3 дня.\n"
        "2. Выберите страну, AmneziaVPN и профиль Reality.\n"
        "3. Нажмите «Показать ключ и QR».\n"
        "4. В AmneziaVPN: <b>Добавить → Вставить ключ из буфера</b>.\n\n"
        "Если подключение не заработало, пришлите поддержке страну, модель устройства и время ошибки — сам ключ отправляйте только в активной debug-сессии.",
        parse_mode="HTML",
        reply_markup=back_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "channel_info")
async def channel_info_handler(callback: CallbackQuery):
    await callback.answer("Ссылка на канал пока настраивается", show_alert=True)


@router.callback_query(F.data == "support_info")
async def support_info_handler(callback: CallbackQuery):
    await callback.answer("Контакт поддержки пока настраивается", show_alert=True)


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
    client_type: str,
    profile: str,
    code: str | None = None,
) -> None:
    subscription = await create_access_grant(
        callback.from_user.id,
        kind,
        node_id,
        client_type,
        profile_flow(profile),
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
        await send_key_message(callback.message, client["id"], client.get("client_type", client_type))
    await callback.answer("VPN активирован")


@router.callback_query(F.data == "trial_start")
async def trial_start_handler(callback: CallbackQuery):
    nodes = await available_nodes()
    if not nodes:
        await callback.answer("Сейчас нет доступных серверов", show_alert=True)
        return
    await callback.message.edit_text(
        "🎁 <b>Бесплатный тест на 3 дня</b>\n\nДоступ выдаётся один раз. Выберите страну:",
        parse_mode="HTML",
        reply_markup=access_node_keyboard(nodes, "trial_country"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("trial_country:"))
async def trial_country_handler(callback: CallbackQuery):
    node_id = int(callback.data.split(":")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 AmneziaVPN", callback_data=f"trial_client:{node_id}:amnezia")],
        [InlineKeyboardButton(text="🔗 Универсальный VLESS", callback_data=f"trial_client:{node_id}:universal")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="trial_start")],
    ])
    await callback.message.edit_text("📱 <b>Выберите VPN-клиент</b>", parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("trial_client:"))
async def trial_client_handler(callback: CallbackQuery):
    _, node_id, client_type = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Reality", callback_data=f"trial_confirm:{node_id}:{client_type}:standard")],
        [InlineKeyboardButton(text="⚡ Reality + XTLS Vision", callback_data=f"trial_confirm:{node_id}:{client_type}:vision")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"trial_country:{node_id}")],
    ])
    await callback.message.edit_text("🔐 <b>Выберите профиль VLESS</b>", parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("trial_confirm:"))
async def trial_confirm_handler(callback: CallbackQuery):
    try:
        _, node_id, client_type, profile = callback.data.split(":")
        await finish_access_grant(
            callback,
            kind="trial",
            node_id=int(node_id),
            client_type=client_type,
            profile=profile,
        )
    except httpx.HTTPStatusError as exc:
        message = "Тест уже использован или у вас была подписка" if exc.response.status_code == 409 else "Не удалось выдать тестовый доступ"
        await callback.answer(message, show_alert=True)
    except Exception:
        logging.exception("Trial activation failed")
        await callback.answer("Не удалось выдать тестовый доступ", show_alert=True)


@router.callback_query(F.data == "promo_start")
async def promo_start_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoFlow.waiting_code)
    await callback.message.edit_text(
        "🏷 <b>Введите промокод одним сообщением</b>",
        parse_mode="HTML",
        reply_markup=back_menu(),
    )
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
async def promo_country_handler(callback: CallbackQuery):
    node_id = int(callback.data.split(":")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 AmneziaVPN", callback_data=f"promo_client:{node_id}:amnezia")],
        [InlineKeyboardButton(text="🔗 Универсальный VLESS", callback_data=f"promo_client:{node_id}:universal")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="promo_start")],
    ])
    await callback.message.edit_text("📱 <b>Выберите VPN-клиент</b>", parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("promo_client:"))
async def promo_client_handler(callback: CallbackQuery):
    _, node_id, client_type = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Reality", callback_data=f"promo_confirm:{node_id}:{client_type}:standard")],
        [InlineKeyboardButton(text="⚡ Reality + XTLS Vision", callback_data=f"promo_confirm:{node_id}:{client_type}:vision")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"promo_country:{node_id}")],
    ])
    await callback.message.edit_text("🔐 <b>Выберите профиль VLESS</b>", parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("promo_confirm:"))
async def promo_confirm_handler(callback: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        code = data.get("code")
        if not code:
            await callback.answer("Сначала введите промокод", show_alert=True)
            return
        _, node_id, client_type, profile = callback.data.split(":")
        await finish_access_grant(
            callback,
            kind="promo",
            node_id=int(node_id),
            client_type=client_type,
            profile=profile,
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

        try:
            await callback.message.edit_text(
                text,
                reply_markup=(active_vpn_keyboard() if vpn_client else main_menu()),
                parse_mode="HTML",
            )

        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

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
        await send_key_message(callback.message, client["id"], client.get("client_type", "universal"))
        await callback.answer()
    except Exception:
        logging.exception("Failed to show VPN key")
        await callback.answer("Не удалось получить ключ", show_alert=True)


@router.callback_query(F.data == "vpn_reissue")
async def vpn_reissue_handler(callback: CallbackQuery):
    status_data = await get_vpn_status(callback.from_user.id)
    subscription = status_data.get("subscription")
    if not subscription or subscription.get("status") != "active":
        await callback.answer("Активная подписка не найдена", show_alert=True)
        return
    nodes = [
        n
        for n in await get_nodes()
        if n.get("status") == "active"
        and n.get("health_status") != "offline"
        and n.get("active_connections", 0) < n.get("capacity", 0)
        and country_label(n.get("region"))
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=country_label(n["region"]), callback_data=f"rotate_country:{subscription['id']}:{n['id']}")]
        for n in nodes
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn_status")]])
    await callback.message.edit_text("🌍 <b>Куда перенести подключение?</b>", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rotate_country:"))
async def rotate_country_handler(callback: CallbackQuery):
    _, subscription_id, node_id = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 AmneziaVPN", callback_data=f"rotate_client:{subscription_id}:{node_id}:amnezia")],
        [InlineKeyboardButton(text="🔗 Универсальный VLESS", callback_data=f"rotate_client:{subscription_id}:{node_id}:universal")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vpn_reissue")],
    ])
    await callback.message.edit_text("📱 <b>Выберите приложение</b>", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rotate_client:"))
async def rotate_client_handler(callback: CallbackQuery):
    _, subscription_id, node_id, client_type = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Reality", callback_data=f"rotate_confirm:{subscription_id}:{node_id}:{client_type}:standard")],
        [InlineKeyboardButton(text="⚡ Reality + XTLS Vision", callback_data=f"rotate_confirm:{subscription_id}:{node_id}:{client_type}:vision")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rotate_country:{subscription_id}:{node_id}")],
    ])
    await callback.message.edit_text("🔐 <b>Выберите профиль</b>\n\nСтарый ключ будет отозван.", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rotate_confirm:"))
async def rotate_confirm_handler(callback: CallbackQuery):
    try:
        _, subscription_id, node_id, client_type, profile = callback.data.split(":")
        client = await rotate_vpn_client(int(subscription_id), int(node_id), client_type, profile_flow(profile))
        await callback.message.edit_text("✅ Ключ перевыпущен. Старый ключ отозван.")
        await send_key_message(callback.message, client["id"], client_type)
        await callback.answer("Ключ обновлён")
    except Exception:
        logging.exception("Failed to rotate VPN key")
        await callback.answer("Не удалось перевыпустить ключ", show_alert=True)


# =========================================================
# BUY VPN
# =========================================================

@router.callback_query(F.data == "buy_vpn")
async def buy_vpn_handler(callback: CallbackQuery):
    try:
        plans = await get_plans()

        if not plans:
            await callback.answer(
                "❌ Сейчас нет доступных тарифов.",
                show_alert=True,
            )
            return

        text = (
            "💳 <b>Купить VPN</b>\n\n"
            "Выберите тариф:"
        )

        try:
            await callback.message.edit_text(
                text,
                reply_markup=plans_keyboard(plans),
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

        await callback.answer()

    except httpx.HTTPError:
        logging.exception(
            "Failed to get plans"
        )

        await callback.answer(
            "❌ Не удалось получить тарифы.",
            show_alert=True,
        )

    except Exception:
        logging.exception(
            "Unexpected buy VPN error"
        )

        await callback.answer(
            "❌ Произошла ошибка.",
            show_alert=True,
        )


# =========================================================
# BUY PLAN
# =========================================================

@router.callback_query(F.data.startswith("buy_plan:"))
async def buy_plan_handler(callback: CallbackQuery):
    try:
        telegram_id = callback.from_user.id

        plan_id = int(
            callback.data.split(":", 1)[1]
        )

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

        # -------------------------------------------------
        # Get selected plan
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
        # Check current VPN
        # -------------------------------------------------

        current_status = await get_vpn_status(
            telegram_id
        )

        if current_status.get("subscription"):
            subscription = current_status["subscription"]

            if subscription.get("status") == "active":
                await callback.answer(
                    "У вас уже есть активная подписка.",
                    show_alert=True,
                )
                return

        nodes = [
            node
            for node in await get_nodes()
            if node.get("status") == "active"
            and node.get("health_status") != "offline"
            and node.get("active_connections", 0) < node.get("capacity", 0)
        ]
        buttons = []
        for node in nodes:
            label = country_label(node.get("region"))
            if label:
                buttons.append([InlineKeyboardButton(text=label, callback_data=f"country:{plan_id}:{node['id']}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_vpn")])
        text = "🌍 <b>Выберите страну подключения</b>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

        await callback.answer()

    except ValueError:
        await callback.answer(
            "❌ Некорректный тариф.",
            show_alert=True,
        )

    except httpx.HTTPError:
        logging.exception(
            "Failed to prepare VPN purchase"
        )

        await callback.answer(
            "❌ Не удалось подготовить покупку.",
            show_alert=True,
        )

    except Exception:
        logging.exception(
            "Unexpected plan selection error"
        )

        await callback.answer(
            "❌ Произошла ошибка.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("country:"))
async def country_handler(callback: CallbackQuery):
    _, plan_id, node_id = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 AmneziaVPN", callback_data=f"client:{plan_id}:{node_id}:amnezia")],
        [InlineKeyboardButton(text="🔗 Универсальный VLESS", callback_data=f"client:{plan_id}:{node_id}:universal")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"buy_plan:{plan_id}")],
    ])
    await callback.message.edit_text("📱 <b>Выберите VPN-клиент</b>", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("client:"))
async def client_handler(callback: CallbackQuery):
    _, plan_id, node_id, client_type = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Reality (совместимый)", callback_data=f"profile:{plan_id}:{node_id}:{client_type}:standard")],
        [InlineKeyboardButton(text="⚡ Reality + XTLS Vision", callback_data=f"profile:{plan_id}:{node_id}:{client_type}:vision")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"country:{plan_id}:{node_id}")],
    ])
    await callback.message.edit_text(
        "🔐 <b>Выберите профиль VLESS</b>\n\n"
        "Совместимый профиль подходит большинству клиентов. Vision обычно быстрее, но требует поддержки XTLS.",
        reply_markup=keyboard, parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:"))
async def profile_handler(callback: CallbackQuery):
    _, plan_id, node_id, client_type, profile = callback.data.split(":")
    plans = await get_plans()
    nodes = await get_nodes()
    plan = next((p for p in plans if p["id"] == int(plan_id)), None)
    node = next((n for n in nodes if n["id"] == int(node_id)), None)
    if not plan or not node:
        await callback.answer("Тариф или сервер недоступен", show_alert=True)
        return
    profile_name = "Reality + XTLS Vision" if profile == "vision" else "Reality"
    client_name = "AmneziaVPN" if client_type == "amnezia" else "универсальный VLESS"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать", callback_data=f"confirm_buy:{plan_id}:{node_id}:{client_type}:{profile}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"client:{plan_id}:{node_id}:{client_type}")],
    ])
    await callback.message.edit_text(
        "💳 <b>Подтверждение</b>\n\n"
        f"Тариф: <b>{plan['name']}</b>\nСтрана: <b>{node.get('region') or node['name']}</b>\n"
        f"Клиент: <b>{client_name}</b>\nПрофиль: <b>{profile_name}</b>\n"
        f"Стоимость: <b>{plan['price']} {plan['currency']}</b>",
        reply_markup=keyboard, parse_mode="HTML",
    )
    await callback.answer()


# =========================================================
# CONFIRM BUY
# =========================================================

@router.callback_query(F.data.startswith("confirm_buy:"))
async def confirm_buy_handler(callback: CallbackQuery):
    _, plan_id, node_id, client_type, profile = callback.data.split(":")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📱 Оплатить по QR",
                callback_data=f"pay_qr:{plan_id}:{node_id}:{client_type}:{profile}",
            )
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"profile:{plan_id}:{node_id}:{client_type}:{profile}")],
    ])
    await callback.message.edit_text(
        "💳 <b>Выберите способ оплаты</b>\n\n"
        "Сейчас доступна оплата по QR. После подключения реального провайдера здесь появятся дополнительные варианты.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_qr:"))
async def pay_qr_handler(
    callback: CallbackQuery,
):
    try:
        telegram_id = callback.from_user.id

        _, raw_plan_id, raw_node_id, client_type, profile = callback.data.split(":")
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

        try:
            await callback.message.edit_text(
                "⏳ <b>Создаём QR-платёж...</b>\n\n"
                "После подтверждения VPN активируется автоматически.",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass

        payment = await create_payment(
            user_id=user_id,
            plan_id=plan_id,
            node_id=node_id,
            client_type=client_type,
            flow=profile_flow(profile),
            idempotency_key=f"telegram:{callback.id}",
        )

        if payment["status"] != "paid" or not payment.get("subscription_id"):
            await callback.message.edit_text(
                "⏳ Платёж создан и ожидает подтверждения провайдера.",
                reply_markup=main_menu(),
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
            "🔐 <b>VLESS Reality</b>\n\n"
            "Скопируй ссылку ниже и добавь её "
            "в V2Ray-клиент:"
        )

        text += (
            f"\n\n<code>{html.escape(vless_url)}</code>"
        )
        if client_type == "amnezia":
            text += "\n\nОткройте AmneziaVPN → Добавить → Вставить ключ из буфера."

        try:
            await callback.message.edit_text(
                text,
                reply_markup=vpn_ready_keyboard(),
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

        await callback.answer(
            "✅ VPN активирован!"
        )
        await send_key_message(callback.message, client_data["id"], client_type)

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
                "❌ Не удалось добавить VPN "
                "клиента на Xray."
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
        await callback.message.edit_text(
            "👋 <b>VPN-сервис</b>\n\n"
            "VLESS Reality для AmneziaVPN. Выберите действие:",
            reply_markup=main_menu(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise

    await callback.answer()


# =========================================================
# Main
# =========================================================

async def main():
    bot = Bot(token=BOT_TOKEN)

    dp = Dispatcher()

    dp.include_router(router)

    logging.info(
        "VPN bot started, API_URL=%s",
        API_URL,
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
