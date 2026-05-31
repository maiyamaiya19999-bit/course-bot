"""
Бот-вышибала для Telegram-группы интенсива maysoulme

Что делает:
1. Принимает webhook от Prodamus (оплата прошла) → сохраняет в базу
2. Следит за новыми участниками в группе
3. Проверяет по username: оплатил ли человек?
   - Да → оставляет
   - Нет → кикает через 24 часа
4. Админ получает уведомление и может вручную одобрить/кикнуть
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from aiohttp import web
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

from database import (
    init_db,
    add_payment,
    find_payment_by_username,
)
from prodamus import extract_payment_data

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PRODAMUS_SECRET = os.getenv("PRODAMUS_SECRET")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Время запуска бота — все кто был в группе ДО этого момента НЕ проверяются
BOT_START_TIME = None

# Ссылки-исключения: кто вступает по ним — не проверяются
WHITELIST_INVITE_LINKS = {
    "https://t.me/+skIW4Cj80zkzMjIy",
}


# ─── Обработка новых участников ─────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Срабатывает когда кто-то заходит в группу.
    Проверяет, есть ли оплата по username.
    Если нет — кикает через 24 часа.
    """
    if not update.chat_member:
        return

    new_member = update.chat_member.new_chat_member
    old_member = update.chat_member.old_chat_member

    # Проверяем только новых участников (было "не участник" → стало "участник")
    if old_member.status in ("member", "administrator", "creator"):
        return
    if new_member.status not in ("member", "restricted"):
        return

    user = new_member.user

    # Не проверяем ботов
    if user.is_bot:
        return

    # Не проверяем тех, кто был в группе до запуска бота
    if BOT_START_TIME and update.chat_member.date < BOT_START_TIME:
        logger.info(f"Пропускаем {user.first_name} — был в группе до запуска бота")
        return

    # Не проверяем тех, кого добавил админ вручную
    if update.chat_member.from_user and update.chat_member.from_user.id == ADMIN_CHAT_ID:
        logger.info(f"Пропускаем {user.first_name} — добавлен админом вручную")
        return

    # Не проверяем тех, кто вступил по спец-ссылке (оплата не через Продамус)
    invite_link = update.chat_member.invite_link
    if invite_link and invite_link.invite_link in WHITELIST_INVITE_LINKS:
        logger.info(f"Пропускаем {user.first_name} — вступил по спец-ссылке")
        return

    logger.info(f"Новый участник: {user.first_name} (@{user.username}), ID: {user.id}")

    # Проверка по username
    if user.username:
        payment = find_payment_by_username(user.username)
        if payment:
            logger.info(f"Оплата найдена для @{user.username}")
            return  # Всё ок, человек оплатил

    # Оплата не найдена — кикаем сразу
    logger.info(f"Оплата НЕ найдена для @{user.username}, кикаем")

    chat_id = update.chat_member.chat.id
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
        logger.info(f"Кикнут {user.first_name} (@{user.username}) — оплата не найдена")
    except Exception as e:
        logger.error(f"Ошибка при кике {user.id}: {e}")

    # Уведомляем админа
    if ADMIN_CHAT_ID:
        username_text = f"@{user.username}" if user.username else "нет username"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🚫 Кикнут: {user.first_name} {user.last_name or ''}\n"
                f"Username: {username_text}\n"
                f"ID: {user.id}\n\n"
                f"Оплата не найдена в базе."
            ),
        )




# ─── Webhook от Prodamus ────────────────────────────────────

async def handle_prodamus_webhook(request):
    """
    Принимает POST-запрос от Prodamus при успешной оплате.
    Сохраняет данные в базу.
    """
    try:
        data = await request.post()
        data = dict(data)
        logger.info(f"Webhook от Prodamus: {data}")

        # Извлекаем данные об оплате
        payment_data = extract_payment_data(data)
        logger.info(f"Извлечённые данные: {payment_data}")

        # Проверяем статус
        status = payment_data.get("status", "").lower()
        if status not in ("success", "paid", "completed", ""):
            logger.info(f"Пропускаем — статус: {status}")
            return web.Response(text="OK", status=200)

        # Сохраняем в базу
        add_payment(
            telegram_username=payment_data.get("telegram_username", ""),
            email=payment_data.get("email", ""),
            phone=payment_data.get("phone", ""),
            name=payment_data.get("name", ""),
            product=str(payment_data.get("product", "")),
            amount=str(payment_data.get("amount", "")),
            order_id=str(payment_data.get("order_id", "")),
        )

        logger.info(
            f"Оплата сохранена: {payment_data.get('name')} "
            f"(@{payment_data.get('telegram_username')})"
        )

        # Уведомляем админа
        if ADMIN_CHAT_ID and BOT_TOKEN:
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"💰 Новая оплата!\n\n"
                    f"Имя: {payment_data.get('name', '—')}\n"
                    f"Email: {payment_data.get('email', '—')}\n"
                    f"Телефон: {payment_data.get('phone', '—')}\n"
                    f"Telegram: @{payment_data.get('telegram_username', '—')}\n"
                    f"Сумма: {payment_data.get('amount', '—')} руб.\n"
                    f"Продукт: {payment_data.get('product', '—')}"
                ),
            )

        return web.Response(text="OK", status=200)

    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}", exc_info=True)
        return web.Response(text="Error", status=500)


# ─── Админ-команды ──────────────────────────────────────────

async def list_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать последние оплаты (только для админа)"""
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return

    from database import get_db
    conn = get_db()
    payments = conn.execute(
        "SELECT * FROM payments ORDER BY paid_at DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if not payments:
        await update.message.reply_text("Оплат пока нет.")
        return

    text = "Последние 10 оплат:\n\n"
    for p in payments:
        text += (
            f"• {p['name'] or '—'} | @{p['telegram_username'] or '—'} | "
            f"{p['email'] or '—'} | {p['amount']} руб.\n"
        )

    await update.message.reply_text(text)


async def list_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать ожидающих проверки (только для админа)"""
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return

    pending = get_pending_users()
    if not pending:
        await update.message.reply_text("Нет ожидающих проверки.")
        return

    text = "Ожидают проверки:\n\n"
    for p in pending:
        text += (
            f"• {p['first_name']} {p['last_name'] or ''} | "
            f"@{p['telegram_username'] or 'нет ника'} | "
            f"ID: {p['telegram_user_id']}\n"
        )

    await update.message.reply_text(text)


# ─── Запуск ─────────────────────────────────────────────────

async def run_webhook_server(app_telegram):
    """Запускает HTTP-сервер для вебхуков Prodamus"""
    app = web.Application()
    app["telegram_app"] = app_telegram
    app.router.add_post("/webhook/prodamus", handle_prodamus_webhook)

    # Health check
    app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("Webhook сервер запущен на порту %s", os.getenv("PORT", 8080))


def main():
    """Запуск бота"""
    global BOT_START_TIME
    BOT_START_TIME = datetime.now(timezone.utc)
    logger.info(f"Бот запущен в {BOT_START_TIME}. Все кто был в группе ДО этого момента — не проверяются.")

    init_db()

    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Админ-команды
    application.add_handler(CommandHandler("payments", list_payments))
    application.add_handler(CommandHandler("pending", list_pending))

    # Отслеживание новых участников
    application.add_handler(
        ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER)
    )

    # Запускаем webhook-сервер для Prodamus + polling для Telegram
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(run_webhook_server(application))
    logger.info("Бот запущен!")

    # Polling для Telegram (получение обновлений)
    application.run_polling(
        allowed_updates=[Update.CHAT_MEMBER, Update.MESSAGE],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
