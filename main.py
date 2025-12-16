# main.py
import os
from fastapi import FastAPI, Request
from aiogram import types
from dotenv import load_dotenv

from bot import bot, dp, on_startup, on_shutdown

load_dotenv()

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") + WEBHOOK_PATH

app = FastAPI()


@app.on_event("startup")
async def startup():
    await on_startup()
    await bot.set_webhook(WEBHOOK_URL)


@app.on_event("shutdown")
async def shutdown():
    await bot.delete_webhook()
    await on_shutdown()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot, update)
    return {"ok": True}
