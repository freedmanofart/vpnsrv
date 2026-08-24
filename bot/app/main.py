import asyncio
import logging
import os
from urllib.parse import urlencode

import httpx
from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO")
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "http://api:8000")

router = Router()


# =========================================================
# Keyboards
# =========================================================

def main_menu() -> InlineKeyboardMarkup:
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


# =========================================================
# API
# =========================================================

async def get_or_create_user(message: Message) -> dict:
    telegram_user = message.from_user

    if telegram_user is None:
        raise RuntimeError("Telegram user is unavailable")

    telegram_id = telegram_user.id

    async with httpx.AsyncClient(
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
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get(
            f"/users/{telegram_id}/vpn-status"
        )

        response.raise_for_status()

        return response.json()


async def get_plans() -> list[dict]:
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get("/plans")

        response.raise_for_status()

        return response.json()


async def get_nodes() -> list[dict]:
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get("/vpn/nodes")

        response.raise_for_status()

        return response.json()


async def get_node_configs(node_id: int) -> list[dict]:
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.get(
            f"/vpn/nodes/{node_id}/configs"
        )

        response.raise_for_status()

        return response.json()


async def create_subscription(
    user_id: int,
    plan_id: int,
) -> dict:
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=10.0,
    ) as client:

        response = await client.post(
            "/subscriptions",
            json={
                "user_id": user_id,
                "plan_id": plan_id,
            },
        )

        response.raise_for_status()

        return response.json()


async def create_vpn_client(
    user_id: int,
    subscription_id: int,
    node_id: int,
) -> dict:
    async with httpx.AsyncClient(
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
            "👋 Добро пожаловать!\n\n"
            "Это VPN-сервис.\n"
            "Здесь ты можешь управлять своей VPN-подпиской.",
            reply_markup=main_menu(),
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
                reply_markup=main_menu(),
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

        async with httpx.AsyncClient(
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

        # -------------------------------------------------
        # Confirmation
        # -------------------------------------------------

        text = (
            "💳 <b>Подтверждение покупки</b>\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Стоимость: <b>{plan['price']} "
            f"{plan['currency']}</b>\n"
            f"Срок: <b>{plan['duration_days']} дней</b>\n\n"
            "После подтверждения VPN будет "
            "активирован автоматически."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Купить",
                        callback_data=(
                            f"confirm_buy:{plan_id}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="buy_vpn",
                    ),
                ],
            ]
        )

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


# =========================================================
# CONFIRM BUY
# =========================================================

@router.callback_query(F.data.startswith("confirm_buy:"))
async def confirm_buy_handler(
    callback: CallbackQuery,
):
    try:
        telegram_id = callback.from_user.id

        plan_id = int(
            callback.data.split(":", 1)[1]
        )

        # -------------------------------------------------
        # User
        # -------------------------------------------------

        async with httpx.AsyncClient(
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
                "⏳ <b>Активируем VPN...</b>\n\n"
                "Создаём подписку.",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass

        subscription = await create_subscription(
            user_id=user_id,
            plan_id=plan_id,
        )

        logging.info(
            "Subscription created: user_id=%s subscription_id=%s",
            user_id,
            subscription["id"],
        )

        # -------------------------------------------------
        # Select active node
        # -------------------------------------------------

        nodes = await get_nodes()

        active_nodes = [
            node
            for node in nodes
            if node.get("status") == "active"
        ]

        if not active_nodes:
            raise RuntimeError(
                "No active VPN nodes available"
            )

        node = active_nodes[0]

        # -------------------------------------------------
        # Create VPN client
        # -------------------------------------------------

        client_data = await create_vpn_client(
            user_id=user_id,
            subscription_id=subscription["id"],
            node_id=node["id"],
        )

        logging.info(
            "VPN client created: user_id=%s client_id=%s",
            user_id,
            client_data["id"],
        )

        # -------------------------------------------------
        # Get VLESS config
        # -------------------------------------------------

        configs = await get_node_configs(
            node["id"]
        )

        vless_config = next(
            (
                config
                for config in configs
                if config["protocol"] == "vless"
            ),
            None,
        )

        if not vless_config:
            raise RuntimeError(
                "VLESS configuration not found"
            )

        config = vless_config["config"]

        vless_url = build_vless_url(
            client_uuid=client_data["client_uuid"],
            node=node,
            config=config,
        )

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
            f"\n\n<code>{vless_url}</code>"
        )

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
):
    try:
        await callback.message.edit_text(
            "👋 <b>VPN-сервис</b>\n\n"
            "Выберите действие:",
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