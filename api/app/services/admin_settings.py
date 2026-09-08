from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.admin_setting import AdminSetting


ADMIN_NOTIFICATION_EMAIL = "admin_notification_email"
BOT_ADMIN_CHAT_ID = "bot_admin_chat_id"


@dataclass(frozen=True)
class AdminContacts:
    admin_notification_email: str
    bot_admin_chat_id: int


async def get_admin_contacts(db: AsyncSession) -> AdminContacts:
    result = await db.execute(
        select(AdminSetting).where(
            AdminSetting.key.in_((ADMIN_NOTIFICATION_EMAIL, BOT_ADMIN_CHAT_ID))
        )
    )
    stored = {item.key: item.value for item in result.scalars()}
    raw_chat_id = stored.get(BOT_ADMIN_CHAT_ID) or str(settings.bot_admin_chat_id or "")
    try:
        chat_id = int(raw_chat_id) if raw_chat_id else 0
    except ValueError:
        chat_id = 0
    return AdminContacts(
        admin_notification_email=stored.get(ADMIN_NOTIFICATION_EMAIL)
        or settings.admin_notification_email
        or "",
        bot_admin_chat_id=chat_id,
    )


async def set_admin_contacts(
    db: AsyncSession,
    *,
    admin_notification_email: str,
    bot_admin_chat_id: int,
) -> AdminContacts:
    values = {
        ADMIN_NOTIFICATION_EMAIL: admin_notification_email.strip(),
        BOT_ADMIN_CHAT_ID: str(bot_admin_chat_id or ""),
    }
    for key, value in values.items():
        item = await db.get(AdminSetting, key)
        if item is None:
            db.add(AdminSetting(key=key, value=value))
        else:
            item.value = value
    await db.commit()
    return await get_admin_contacts(db)
