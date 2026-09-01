"""One-off recovery of legacy Telegram receipts into VPN API storage."""

import asyncio
import base64
from io import BytesIO
import os
import sys

import httpx
from aiogram import Bot


async def main() -> None:
    payment_id, user_id, file_id, unique_id, media_type = sys.argv[1:6]
    buffer = BytesIO()
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        await bot.download(file_id, destination=buffer)
    is_photo = media_type == "photo"
    payload = {
        "user_id": int(user_id),
        "telegram_file_id": file_id,
        "telegram_file_unique_id": unique_id or None,
        "media_type": media_type,
        "filename": "receipt.jpg" if is_photo else "receipt.pdf",
        "mime_type": "image/jpeg" if is_photo else "application/pdf",
        "data_base64": base64.b64encode(buffer.getvalue()).decode(),
    }
    async with httpx.AsyncClient(
        base_url=os.getenv("API_URL", "http://api:8000"),
        headers={"Authorization": f"Bearer {os.environ['SERVICE_API_TOKEN']}"},
        timeout=30,
    ) as client:
        response = await client.post(f"/payments/{payment_id}/receipt", json=payload)
        response.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())
