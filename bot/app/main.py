import asyncio
import base64
import math
import html
from io import BytesIO
import logging
import os
from pathlib import Path

import httpx
import qrcode
from app.content import CONTENT, link as content_link, platform as get_platform, text as content_text
from app.domain import (
    PLAN_TIERS,
    country_label,
    plan_tier,
    plans_by_tier,
    rotation_payload,
    select_public_plans,
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
    KeyboardButton,
    ReplyKeyboardMarkup,
    BufferedInputFile,
    BotCommand,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
    FSInputFile,
)


configure_logging(os.getenv("LOG_LEVEL", "INFO"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_ADMIN_CHAT_ID = int(os.getenv("BOT_ADMIN_CHAT_ID", "0") or 0)
API_URL = os.getenv("API_URL", "http://api:8000")
SERVICE_API_TOKEN = os.environ["SERVICE_API_TOKEN"]
TELEGRAM_CHANNEL_URL = content_link("channel")
SUPPORT_URL = content_link("support")
YOOMONEY_PAYMENT_URL = content_link("payment")
TRY_PAYMENT_URL = content_link("try_payment")
TRY_PAYMENT_AMOUNT_RUB = "50"
WEB_CABINET_URL = os.getenv("WEB_CABINET_URL", "").strip()
WEB_SITE_URL = os.getenv("WEB_SITE_URL", "").strip()
if not WEB_SITE_URL and WEB_CABINET_URL:
    WEB_SITE_URL = WEB_CABINET_URL.removesuffix("/cabinet").rstrip("/") + "/"
WELCOME_LOGO = Path(__file__).resolve().parent / "static" / "freedom-vpn-logo.png"
PUBLIC_PLAN_CODES = tuple(
    item.strip()
    for item in os.getenv("BOT_PLAN_CODES", "").split(",")
    if item.strip()
)


class PromoFlow(StatesGroup):
    waiting_code = State()


class PurchaseFlow(StatesGroup):
    waiting_device = State()
    waiting_country = State()
    waiting_tier = State()
    waiting_plan = State()


class ManualPaymentFlow(StatesGroup):
    waiting_receipt = State()


class EmailCabinetFlow(StatesGroup):
    waiting_email = State()


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
    site_button = (
        InlineKeyboardButton(text="🌐 Сайт", url=WEB_SITE_URL)
        if WEB_SITE_URL
        else InlineKeyboardButton(text="🌐 Сайт", callback_data="site_missing")
    )
    rows = [
        [InlineKeyboardButton(text="💳 Приобрести подписку", callback_data="buy_vpn"), InlineKeyboardButton(text="👤 Управление подпиской", callback_data="vpn_status")],
        [InlineKeyboardButton(text="🏷 Промокод", callback_data="promo_start"), InlineKeyboardButton(text="🧪 Попробовать", callback_data="try_start")],
        [InlineKeyboardButton(text="📖 Инструкции", callback_data="instructions"), support_button],
        [channel_button, site_button],
    ]
    extra_buttons = [
        InlineKeyboardButton(text=item["text"], url=item["url"])
        for item in CONTENT.get("main_url_buttons", [])
        if item.get("text") and item.get("url")
    ]
    rows.extend(extra_buttons[index:index + 2] for index in range(0, len(extra_buttons), 2))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def popup_menu() -> ReplyKeyboardMarkup:
    """Постоянное раскрывающееся меню рядом с полем ввода Telegram."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Приобрести подписку"), KeyboardButton(text="👤 Управление подпиской")],
            [KeyboardButton(text="🏷 Промокод"), KeyboardButton(text="🧪 Попробовать")],
            [KeyboardButton(text="📖 Инструкции"), KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def welcome_keyboard() -> InlineKeyboardMarkup:
    site = (
        InlineKeyboardButton(text="🌐 Сайт", url=WEB_SITE_URL)
        if WEB_SITE_URL
        else InlineKeyboardButton(text="🌐 Сайт", callback_data="site_missing")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [site, InlineKeyboardButton(text="✉️ Email", callback_data="cabinet_email")],
        ],
    )


def purchase_devices_keyboard() -> ReplyKeyboardMarkup:
    labels = [item["label"] for item in CONTENT.get("platforms", [])]
    rows = [
        [KeyboardButton(text=label) for label in labels[index:index + 2]]
        for index in range(0, len(labels), 2)
    ]
    rows.append([KeyboardButton(text="⬅️ Главное меню")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите устройство",
    )


def purchase_countries_keyboard(nodes: list[dict]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=country_label(node["region"]))]
            for node in nodes
        ] + [[KeyboardButton(text="⬅️ Главное меню")]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите страну",
    )


def plan_button_label(plan: dict) -> str:
    return f"{plan['price']} {plan['currency']} — {plan['name']}"


def purchase_plans_keyboard(plans: list[dict]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=plan_button_label(plan))]
            for plan in plans
        ] + [[KeyboardButton(text="⬅️ Главное меню")]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите тариф",
    )


def purchase_tiers_keyboard(tiers: dict[str, list[dict]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PLAN_TIERS[key]["label"])] for key in tiers]
        + [[KeyboardButton(text="⬅️ Главное меню")]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите количество подключений",
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
                    text="👤 Управление подпиской",
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
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main_menu")],
    ])


def human_traffic(value: int | None) -> str:
    if value is None:
        return "данные временно недоступны"
    gib = value / (1024 ** 3)
    return f"{gib:.1f} ГБ"


def subscription_text(data: dict) -> str:
    subscription = data.get("subscription")
    client = data.get("vpn_client")
    if not subscription:
        return "👤 <b>Управление подпиской</b>\n\nУ вас пока нет активной подписки."
    active = subscription.get("status") == "active"
    days = math.ceil(float(subscription.get("days_remaining") or 0))
    expires_at = (subscription.get("expires_at") or "—").replace("T", " ").replace("+00:00", "")
    connections = client.get("max_connections", 0) if client else 0
    connection_text = "без ограничений" if connections == 0 else str(connections)
    traffic_limit_gb = client.get("traffic_limit_gb", 0) if client else 0
    traffic_remaining = client.get("traffic_remaining_bytes") if client else None
    if client and traffic_limit_gb == 0:
        traffic_text = "без ограничений"
    elif traffic_remaining is not None:
        traffic_text = human_traffic(traffic_remaining)
    elif traffic_limit_gb:
        traffic_text = f"{traffic_limit_gb} ГБ по тарифу"
    else:
        traffic_text = "данные временно недоступны"
    return (
        "👤 <b>Управление подпиской</b>\n\n"
        f"Статус: {'🟢 Активна' if active else '🔴 Истекла'}\n"
        f"Тариф: <b>{html.escape(str(subscription.get('plan_name') or '—'))}</b>\n"
        f"Осталось дней: <b>{days}</b>\n"
        f"Осталось трафика: <b>{traffic_text}</b>\n"
        f"Одновременных подключений: <b>{connection_text}</b>\n"
        f"Действует до: <code>{expires_at} UTC</code>"
    )


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


async def send_cabinet_code_to_email(telegram_id: int, email_address: str) -> dict:
    async with api_client(base_url=API_URL, timeout=20.0) as client:
        response = await client.post(
            "/web/telegram-cabinet-link",
            json={"telegram_id": telegram_id, "email": email_address},
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

        return select_public_plans(response.json(), PUBLIC_PLAN_CODES)


async def get_nodes() -> list[dict]:
    async with api_client(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get("/vpn/nodes")

        response.raise_for_status()

        return response.json()


async def get_payment_methods() -> list[dict]:
    async with api_client(base_url=API_URL, timeout=10.0) as client:
        response = await client.get("/payment-methods")
        response.raise_for_status()
        return response.json()


async def get_payment_method_image(method_id: int) -> bytes | None:
    async with api_client(base_url=API_URL, timeout=10.0) as client:
        response = await client.get(f"/payment-methods/{method_id}/image")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content


async def sber_payment_method() -> dict | None:
    methods = await get_payment_methods()
    return next((item for item in methods if item.get("code") == "sber_qr"), None)


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


async def create_manual_payment(
    user_id: int, plan_id: int, node_id: int, method_code: str, idempotency_key: str
) -> dict:
    payload = subscription_payload(user_id=user_id, plan_id=plan_id, node_id=node_id)
    payload.update({"method_code": method_code, "idempotency_key": idempotency_key})
    async with api_client(base_url=API_URL, timeout=10.0) as client:
        response = await client.post("/payments/manual", json=payload)
        response.raise_for_status()
        return response.json()


async def attach_payment_receipt(payment_id: int, payload: dict) -> dict:
    async with api_client(base_url=API_URL, timeout=10.0) as client:
        response = await client.post(f"/payments/{payment_id}/receipt", json=payload)
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


async def show_try_payment(target: Message | CallbackQuery) -> None:
    method = await sber_payment_method()
    rows = []
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton(text="✅ Я оплатил — поддержка", url=SUPPORT_URL)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        "🧪 <b>Попробовать VPN</b>\n\n"
        f"К оплате: <b>{TRY_PAYMENT_AMOUNT_RUB} ₽</b>\n"
        "Оплатите по QR Сбербанка и нажмите «Я оплатил — поддержка» "
        "или отправьте чек в чат. Тестовый доступ будет выдан после проверки."
    )
    message = target.message if isinstance(target, CallbackQuery) else target
    if not method:
        await message.answer(
            "QR Сбербанка пока не настроен в админке.",
            reply_markup=keyboard,
        )
        return
    if method.get("has_image"):
        image = await get_payment_method_image(method["id"])
        if image:
            await message.answer_photo(
                BufferedInputFile(image, filename="sber-try-payment-qr.png"),
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
    await message.answer(
        text + f"\n\nРеквизиты: {html.escape(method.get('url') or 'не заполнены')}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


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

        await message.answer_photo(
            photo=FSInputFile(WELCOME_LOGO),
            caption=content_text("welcome"),
            reply_markup=welcome_keyboard(),
            parse_mode="HTML",
        )
        await message.answer(
            "Меню доступно по кнопке справа от поля ввода.",
            reply_markup=popup_menu(),
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
        reply_markup=popup_menu(),
        parse_mode="HTML",
    )


@router.message(Command("vpn"))
async def vpn_command_handler(message: Message):
    try:
        data = await get_vpn_status(message.from_user.id)
        subscription = data.get("subscription")
        client = data.get("vpn_client")
        if not subscription:
            text = subscription_text(data)
            keyboard = main_menu()
        else:
            expires_at = (subscription.get("expires_at") or "—").replace("T", " ").replace("+00:00", "")
            text = subscription_text(data)
            keyboard = active_vpn_keyboard() if client else main_menu()
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        logging.exception("Failed to show VPN status from command")
        await message.answer("❌ Не удалось получить данные VPN.", reply_markup=main_menu())


@router.message(Command("buy"))
async def buy_command_handler(message: Message, state: FSMContext):
    await state.set_state(PurchaseFlow.waiting_device)
    await message.answer(
        "📱 <b>Выберите ваше устройство:</b>",
        reply_markup=purchase_devices_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def help_command_handler(message: Message):
    await message.answer(
        content_text("instructions"),
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(F.text.in_({"💳 Приобрести подписку", "💳 Оплатить"}))
async def popup_buy_handler(message: Message, state: FSMContext):
    await buy_command_handler(message, state)


@router.message(F.text.in_({"👤 Управление подпиской", "👤 Личный кабинет"}))
async def popup_vpn_handler(message: Message):
    await vpn_command_handler(message)


@router.callback_query(F.data == "site_missing")
async def site_missing_handler(callback: CallbackQuery):
    await callback.answer("Ссылка на сайт пока не настроена", show_alert=True)


@router.callback_query(F.data == "cabinet_email")
async def welcome_email_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EmailCabinetFlow.waiting_email)
    await callback.message.answer(
        "✉️ Введите email. На него будет отправлен одноразовый код для входа в web-кабинет с вашей текущей подпиской.",
        reply_markup=popup_menu(),
    )
    await callback.answer()


@router.message(EmailCabinetFlow.waiting_email)
async def cabinet_email_handler(message: Message, state: FSMContext):
    email_address = (message.text or "").strip().lower()
    if not email_address or "@" not in email_address or len(email_address) > 320:
        await message.answer("Введите корректный email, например name@example.com.")
        return
    try:
        await send_cabinet_code_to_email(message.from_user.id, email_address)
        await state.clear()
        await message.answer(
            "✅ Код для входа в web-кабинет отправлен на почту. Он действует 10 минут. Проверьте также папку «Спам».",
            reply_markup=popup_menu(),
        )
    except httpx.HTTPStatusError as exc:
        detail = "Не удалось отправить код. Проверьте email или попробуйте позже."
        try:
            detail = exc.response.json().get("detail") or detail
        except ValueError:
            pass
        await message.answer(f"❌ {html.escape(detail)}", reply_markup=popup_menu())
    except httpx.HTTPError:
        logging.exception("Failed to send cabinet email")
        await message.answer("❌ Не удалось связаться с сервером.", reply_markup=popup_menu())


@router.message(F.text == "📖 Инструкции")
async def popup_help_handler(message: Message):
    await help_command_handler(message)


@router.message(F.text == "🏷 Промокод")
async def popup_promo_handler(message: Message, state: FSMContext):
    await state.set_state(PromoFlow.waiting_code)
    await message.answer(content_text("promo"), parse_mode="HTML", reply_markup=popup_menu())


@router.message(F.text == "🧪 Попробовать")
async def popup_try_handler(message: Message):
    await show_try_payment(message)


@router.message(F.text == "🆘 Поддержка")
async def popup_support_handler(message: Message):
    if SUPPORT_URL:
        await message.answer(
            f'🆘 <a href="{html.escape(SUPPORT_URL)}">Открыть поддержку</a>',
            parse_mode="HTML",
            reply_markup=popup_menu(),
        )
    else:
        await message.answer(content_text("support_missing"), reply_markup=popup_menu())


@router.message(F.text == "⬅️ Главное меню")
async def popup_home_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(content_text("main_menu"), parse_mode="HTML", reply_markup=popup_menu())


async def show_reply_tiers(message: Message, state: FSMContext, node: dict) -> None:
    plans = await get_plans()
    if not plans:
        await state.clear()
        await message.answer("Сейчас нет доступных тарифов.", reply_markup=popup_menu())
        return
    tiers = plans_by_tier(plans)
    if not tiers:
        await state.clear()
        await message.answer("Тарифные пакеты временно не настроены.", reply_markup=popup_menu())
        return
    await state.update_data(node_id=node["id"], all_plans=plans, tiers=tiers)
    await state.set_state(PurchaseFlow.waiting_tier)
    await message.answer(
        "💳 <b>Выберите пакет</b>\n\n"
        "🛴 <b>Лайт</b> — до 5 подключений, 250 ГБ\n"
        "🔥 <b>Стандарт</b> — до 15 подключений, 650 ГБ\n"
        "🚀 <b>Ультра</b> — до 30 подключений, 3 ТБ",
        parse_mode="HTML",
        reply_markup=purchase_tiers_keyboard(tiers),
    )


@router.message(PurchaseFlow.waiting_device)
async def reply_device_handler(message: Message, state: FSMContext):
    item = next(
        (platform for platform in CONTENT.get("platforms", []) if platform.get("label") == message.text),
        None,
    )
    if not item:
        await message.answer("Выберите устройство кнопкой ниже.", reply_markup=purchase_devices_keyboard())
        return
    nodes = await available_nodes()
    if not nodes:
        await state.clear()
        await message.answer("Сейчас нет доступных серверов.", reply_markup=popup_menu())
        return
    await state.update_data(platform_id=item["id"])
    if len(nodes) == 1:
        await show_reply_tiers(message, state, nodes[0])
        return
    await state.update_data(nodes={country_label(node["region"]): node for node in nodes})
    await state.set_state(PurchaseFlow.waiting_country)
    await message.answer(
        "🌍 <b>Выберите страну подключения:</b>",
        parse_mode="HTML",
        reply_markup=purchase_countries_keyboard(nodes),
    )


@router.message(PurchaseFlow.waiting_country)
async def reply_country_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    node = data.get("nodes", {}).get(message.text)
    if not node:
        await message.answer("Выберите страну кнопкой ниже.")
        return
    await show_reply_tiers(message, state, node)


@router.message(PurchaseFlow.waiting_tier)
async def reply_tier_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    tier = next(
        (key for key in data.get("tiers", {}) if PLAN_TIERS[key]["label"] == message.text),
        None,
    )
    plans = data.get("tiers", {}).get(tier, []) if tier else []
    if not plans:
        await message.answer("Выберите пакет кнопкой ниже.")
        return
    await state.update_data(plans={plan_button_label(plan): plan for plan in plans})
    await state.set_state(PurchaseFlow.waiting_plan)
    details = PLAN_TIERS[tier]
    await message.answer(
        f"{details['label']}\n\nДо {details['connections']} подключений · {details['traffic']}\n"
        f"{details['summary']}\n\nВыберите срок:",
        reply_markup=purchase_plans_keyboard(plans),
    )


@router.message(PurchaseFlow.waiting_plan)
async def reply_plan_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    plan = data.get("plans", {}).get(message.text)
    node_id = data.get("node_id")
    if not plan or not node_id:
        await message.answer("Выберите тариф кнопкой ниже.")
        return
    nodes = await get_nodes()
    node = next((item for item in nodes if item["id"] == node_id), None)
    if not node:
        await state.clear()
        await message.answer("Выбранный сервер больше недоступен.", reply_markup=popup_menu())
        return
    methods = await get_payment_methods()
    buttons = [
        InlineKeyboardButton(
            text=method["name"],
            **({"url": method["url"]} if method.get("url") and method.get("code") not in {"sber_qr", "tbank_qr", "phone_transfer"} else {
                "callback_data": f"payment_method:{method['id']}:{plan['id']}:{node_id}"
            }),
        )
        for method in methods
    ]
    rows = [buttons[:2]] if len(buttons) >= 2 else []
    rows.extend([[button] for button in (buttons[2:] if len(buttons) >= 2 else buttons)])
    await state.clear()
    await message.answer(
        "Выбери способ оплаты:\n\n"
        f"Устройство: <b>{html.escape(str(data.get('platform_id', '—')))}</b>\n"
        f"Страна: <b>{country_label(node.get('region')) or html.escape(node['name'])}</b>\n"
        f"Тариф: <b>{html.escape(plan['name'])}</b>\n"
        f"Стоимость: <b>{plan['price']} {plan['currency']}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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
            [InlineKeyboardButton(text="💳 Приобрести подписку", callback_data=f"purchase_device:{platform_id}")],
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
    await show_try_payment(callback)
    await callback.answer()


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

        text = subscription_text(data)

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
    tiers = plans_by_tier(plans)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{PLAN_TIERS[tier]['label']} · до {PLAN_TIERS[tier]['connections']} подключений",
                    callback_data=f"purchase_tier:{tier}:{node_id}",
                )
            ]
            for tier in tiers
        ]
        + [[InlineKeyboardButton(text="⬅️ К странам", callback_data="buy_vpn")]]
    )
    await show_screen(
        callback,
        "💳 <b>Выберите пакет</b>\n\nСначала количество одновременных подключений, затем срок.",
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("purchase_tier:"))
async def purchase_tier_handler(callback: CallbackQuery):
    _, tier, raw_node_id = callback.data.split(":")
    node_id = int(raw_node_id)
    plans = plans_by_tier(await get_plans()).get(tier, [])
    if not plans or tier not in PLAN_TIERS:
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    details = PLAN_TIERS[tier]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{plan['name']} | {plan['price']} {plan['currency']}",
                callback_data=f"purchase_plan:{plan['id']}:{node_id}",
            )]
            for plan in plans
        ] + [[InlineKeyboardButton(text="⬅️ К пакетам", callback_data=f"purchase_country:{node_id}")]]
    )
    await show_screen(
        callback,
        f"{details['label']}\n\nДо {details['connections']} подключений · {details['traffic']}\n"
        f"{details['summary']}\n\n🗓 <b>Выберите срок</b>",
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
    methods = await get_payment_methods()
    buttons = [
        InlineKeyboardButton(
            text=method["name"],
            **({"url": method["url"]} if method.get("url") and method.get("code") not in {"sber_qr", "tbank_qr", "phone_transfer"} else {
                "callback_data": f"payment_method:{method['id']}:{plan_id}:{node_id}"
            }),
        )
        for method in methods
    ]
    rows = []
    if len(buttons) >= 2:
        rows.append(buttons[:2])
        buttons = buttons[2:]
    rows.extend([[button] for button in buttons])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"purchase_tier:{plan_tier(plan)}:{node_id}")])
    await show_screen(
        callback,
        "Выбери способ оплаты:\n\n"
        f"Страна: <b>{country_label(node.get('region')) or node['name']}</b>\n"
        f"Тариф: <b>{plan['name']}</b>\n"
        f"Стоимость: <b>{plan['price']} {plan['currency']}</b>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("payment_method:"))
async def payment_method_handler(callback: CallbackQuery, state: FSMContext):
    _, raw_method_id, raw_plan_id, raw_node_id = callback.data.split(":")
    methods = await get_payment_methods()
    method = next((item for item in methods if item["id"] == int(raw_method_id)), None)
    if not method:
        await callback.answer("Способ оплаты недоступен", show_alert=True)
        return
    if method["code"] == "payment_safety":
        await callback.answer(
            "Платёжная страница не получает ваш VPN-ключ. Доступ выдаётся только после подтверждения платежа VPN API.",
            show_alert=True,
        )
        return
    if method["code"] == "telegram_stars":
        await callback.answer("Цена в Telegram Stars ещё не настроена для этого тарифа.", show_alert=True)
        return
    if method["code"] in {"sber_qr", "tbank_qr", "phone_transfer"}:
        telegram_id = callback.from_user.id
        async with api_client(base_url=API_URL, timeout=10.0) as client:
            response = await client.get(f"/users/{telegram_id}")
            response.raise_for_status()
            user = response.json()
        payment = await create_manual_payment(
            user["id"], int(raw_plan_id), int(raw_node_id), method["code"],
            f"manual:{telegram_id}:{callback.id}",
        )
        await state.set_state(ManualPaymentFlow.waiting_receipt)
        await state.update_data(
            payment_id=payment["id"], user_id=user["id"], method_name=method["name"],
            amount=payment["amount"], currency=payment["currency"],
        )
        requisites = method.get("url") or "Реквизиты ещё не заполнены оператором в VPN Admin."
        if method.get("has_image"):
            image = await get_payment_method_image(method["id"])
            if image:
                await callback.message.answer_photo(
                    BufferedInputFile(image, filename=f"{method['code']}-qr.png"),
                    caption=f"QR для оплаты: {html.escape(method['name'])}",
                )
        await callback.message.answer(
            f"🏦 <b>{html.escape(method['name'])}</b>\n\n"
            f"Сумма: <b>{payment['amount']} {payment['currency']}</b>\n"
            f"Реквизиты/QR: {html.escape(requisites)}\n\n"
            "После перевода пришлите сюда фотографию или файл чека. "
            "Ключ будет создан после проверки оператором.",
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await callback.answer("Добавьте ссылку этого способа оплаты в VPN Admin.", show_alert=True)


@router.message(ManualPaymentFlow.waiting_receipt, F.photo | F.document)
async def manual_payment_receipt_handler(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if message.photo:
        media = message.photo[-1]
        media_type = "photo"
        filename = "receipt.jpg"
        mime_type = "image/jpeg"
    else:
        media = message.document
        media_type = "document"
        filename = media.file_name or "receipt"
        mime_type = media.mime_type or "application/octet-stream"
    receipt_buffer = BytesIO()
    await bot.download(media, destination=receipt_buffer)
    receipt_bytes = receipt_buffer.getvalue()
    if len(receipt_bytes) > 8_000_000:
        await message.answer("Файл чека слишком большой. Максимальный размер — 8 МБ.")
        return
    await attach_payment_receipt(
        data["payment_id"],
        {
            "user_id": data["user_id"],
            "telegram_file_id": media.file_id,
            "telegram_file_unique_id": media.file_unique_id,
            "media_type": media_type,
            "filename": filename,
            "mime_type": mime_type,
            "data_base64": base64.b64encode(receipt_bytes).decode(),
        },
    )
    if BOT_ADMIN_CHAT_ID:
        await bot.copy_message(BOT_ADMIN_CHAT_ID, message.chat.id, message.message_id)
        await bot.send_message(
            BOT_ADMIN_CHAT_ID,
            f"Платёж #{data['payment_id']} ожидает проверки\n"
            f"Способ: {data['method_name']}\nСумма: {data['amount']} {data['currency']}\n"
            "Подтвердите его в разделе «Платежи» VPN Admin.",
        )
    await state.clear()
    await message.answer(
        f"✅ Чек по платежу #{data['payment_id']} получен. После проверки оператором VPN активируется автоматически.",
        reply_markup=popup_menu(),
    )


@router.message(ManualPaymentFlow.waiting_receipt)
async def manual_payment_receipt_invalid(message: Message):
    await message.answer("Пришлите чек фотографией или файлом PDF/изображением.")


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
            BotCommand(command="vpn", description="Управление подпиской"),
            BotCommand(command="buy", description="Купить VPN"),
            BotCommand(command="help", description="Инструкция"),
        ]
    )
    if WEB_CABINET_URL.startswith("https://"):
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Web кабинет",
                web_app=WebAppInfo(url=WEB_CABINET_URL),
            )
        )
    else:
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
