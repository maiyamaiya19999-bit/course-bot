"""
Бот для автоматической проверки оплат в Telegram-группе maysoulme

Что делает:
1. Принимает webhook от Prodamus (оплата прошла) → сохраняет в базу
2. Следит за новыми участниками в группе
3. Проверяет: оплатил ли человек?
   - Да (username совпал) → оставляет
   - Не уверен → пишет человеку в ЛС, просит email/телефон
   - Не оплатил → через 24 часа кикает
4. Позволяет админу вручную одобрить/отклонить
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web
from dotenv import load_dotenv
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import (
    init_db,
    add_payment,
    find_payment_by_username,
    find_payment_by_email,
    find_payment_by_phone,
    add_pending_user,
    remove_pending_user,
    get_pending_users,
    normalize_username,
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

# Время ожидания подтверждения (в часах) перед кик
KICK_TIMEOUT_HOURS = 24


# ─── Команды бота ───────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    await update.message.reply_text(
        "Привет! Я бот интенсива «ИИ-стратегия на миллион» от maysoulme.\n\n"
        "Если ты оплатил(а) интенсив и вступил(а) в группу — я проверю твою оплату автоматически.\n\n"
        "Если я попрошу подтвердить оплату — просто отправь мне:\n"
        "- Email, который указывал(а) при оплате, или\n"
        "- Номер телефона\n\n"
        "И я найду твою оплату!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /help"""
    await update.message.reply_text(
        "Если у тебя проблемы с доступом к группе:\n\n"
        "1. Убедись, что ты оплатил(а) интенсив\n"
        "2. Отправь мне email или номер телефона, "
        "который указывал(а) при оплате\n"
        "3. Я проверю и подтвержу доступ\n\n"
        "Если что-то не получается — напиши @maysouIme_bot"
    )


# ─── Обработка новых участников ─────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Срабатывает когда кто-то заходит в группу.
    Проверяет, есть ли оплата по username.
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

    logger.info(f"Новый участник: {user.first_name} (@{user.username}), ID: {user.id}")

    # Проверка 1: по username
    if user.username:
        payment = find_payment_by_username(user.username)
        if payment:
            logger.info(f"Оплата найдена для @{user.username}")
            remove_pending_user(user.id)
            return  # Всё ок, человек оплатил

    # Оплата не найдена — добавляем в список ожидающих
    add_pending_user(user.id, user.username, user.first_name, user.last_name or "")
    logger.info(f"Оплата НЕ найдена для @{user.username}, добавлен в pending")

    # Пишем человеку в ЛС
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"Привет, {user.first_name}! Ты вступил(а) в группу интенсива.\n\n"
                "Я не смог(ла) автоматически найти твою оплату. "
                "Отправь мне, пожалуйста:\n\n"
                "- **Email**, который указывал(а) при оплате, или\n"
                "- **Номер телефона**\n\n"
                "И я подтвержу твой доступ!"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        # Не смогли написать в ЛС (человек не начинал чат с ботом)
        logger.warning(f"Не удалось написать в ЛС пользователю {user.id}: {e}")

    # Уведомляем админа
    if ADMIN_CHAT_ID:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Одобрить", callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("Кикнуть", callback_data=f"kick_{user.id}"),
            ]
        ])
        username_text = f"@{user.username}" if user.username else "нет username"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"Новый участник в группе:\n"
                f"Имя: {user.first_name} {user.last_name or ''}\n"
                f"Username: {username_text}\n"
                f"ID: {user.id}\n\n"
                f"Оплата не найдена автоматически.\n"
                f"Одобрить или кикнуть?"
            ),
            reply_markup=keyboard,
        )

    # Планируем проверку через 24 часа
    context.job_queue.run_once(
        kick_unverified_user,
        when=timedelta(hours=KICK_TIMEOUT_HOURS),
        data={"user_id": user.id, "chat_id": update.chat_member.chat.id},
        name=f"kick_{user.id}",
    )


async def kick_unverified_user(context: ContextTypes.DEFAULT_TYPE):
    """Кикает пользователя, если за 24 часа не подтвердил оплату"""
    data = context.job.data
    user_id = data["user_id"]
    chat_id = data["chat_id"]

    # Проверяем, может уже подтвердили
    from database import get_db
    conn = get_db()
    pending = conn.execute(
        "SELECT * FROM pending_users WHERE telegram_user_id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not pending:
        return  # Уже подтверждён, не кикаем

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        # Сразу разбаниваем, чтобы человек мог вернуться после оплаты
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.info(f"Кикнут пользователь {user_id} — оплата не подтверждена")

        # Пишем человеку
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "К сожалению, я не смог(ла) подтвердить твою оплату "
                    "в течение 24 часов, и доступ к группе был закрыт.\n\n"
                    "Если ты оплатил(а) интенсив — напиши @maysouIme, "
                    "и мы разберёмся!"
                ),
            )
        except Exception:
            pass

        remove_pending_user(user_id)

    except Exception as e:
        logger.error(f"Ошибка при кике пользователя {user_id}: {e}")


# ─── Обработка сообщений в ЛС (подтверждение оплаты) ────────

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Когда человек пишет боту email или телефон —
    проверяем по базе оплат
    """
    if update.message.chat.type != "private":
        return

    text = update.message.text.strip()
    user = update.message.from_user

    # Проверяем, есть ли этот человек в списке ожидающих
    from database import get_db
    conn = get_db()
    pending = conn.execute(
        "SELECT * FROM pending_users WHERE telegram_user_id = ?", (user.id,)
    ).fetchone()
    conn.close()

    if not pending:
        await update.message.reply_text(
            "Привет! Если тебе нужна помощь — напиши /help"
        )
        return

    # Пробуем найти оплату по email
    payment = find_payment_by_email(text)
    if not payment:
        # Пробуем по телефону
        payment = find_payment_by_phone(text)

    if payment:
        # Нашли оплату!
        remove_pending_user(user.id)

        # Отменяем запланированный кик
        jobs = context.job_queue.get_jobs_by_name(f"kick_{user.id}")
        for job in jobs:
            job.schedule_removal()

        await update.message.reply_text(
            "Оплата найдена! Доступ подтверждён. "
            "Добро пожаловать на интенсив!"
        )
        logger.info(f"Оплата подтверждена для пользователя {user.id} по данным: {text}")

        # Уведомляем админа
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"Оплата подтверждена для {user.first_name} (@{user.username}) по: {text}",
            )
    else:
        await update.message.reply_text(
            "Не нашла оплату по этим данным.\n\n"
            "Попробуй отправить:\n"
            "- Другой email\n"
            "- Номер телефона в формате +79991234567\n\n"
            "Или напиши @maysouIme для помощи."
        )


# ─── Кнопки админа (одобрить / кикнуть) ─────────────────────

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок 'Одобрить' / 'Кикнуть'"""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_CHAT_ID:
        return

    action, user_id_str = query.data.split("_", 1)
    user_id = int(user_id_str)

    if action == "approve":
        remove_pending_user(user_id)
        # Отменяем кик
        jobs = context.job_queue.get_jobs_by_name(f"kick_{user_id}")
        for job in jobs:
            job.schedule_removal()

        await query.edit_message_text(
            query.message.text + "\n\n Одобрено вручную"
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Твой доступ к группе подтверждён! Добро пожаловать на интенсив!",
            )
        except Exception:
            pass

    elif action == "kick":
        remove_pending_user(user_id)
        # Отменяем запланированный кик (кикнем сейчас)
        jobs = context.job_queue.get_jobs_by_name(f"kick_{user_id}")
        for job in jobs:
            job.schedule_removal()

        if GROUP_ID:
            try:
                await context.bot.ban_chat_member(chat_id=GROUP_ID, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=GROUP_ID, user_id=user_id)
            except Exception as e:
                logger.error(f"Ошибка при кике: {e}")

        await query.edit_message_text(
            query.message.text + "\n\n Кикнут"
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
                    f"Новая оплата!\n"
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


# ─── Узнать ID группы ────────────────────────────────────────

async def groupid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Напиши /groupid в группе — бот отправит ID в личку"""
    chat = update.message.chat
    user = update.message.from_user

    # Удаляем команду из группы, чтобы никто не видел
    try:
        await update.message.delete()
    except Exception:
        pass

    # Отправляем в личку
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"ID группы: `{chat.id}`\n"
                f"Название: {chat.title}\n"
                f"Тип: {chat.type}"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass


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
    BOT_START_TIME = datetime.now()
    logger.info(f"Бот запущен в {BOT_START_TIME}. Все кто был в группе ДО этого момента — не проверяются.")

    init_db()

    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("payments", list_payments))
    application.add_handler(CommandHandler("pending", list_pending))
    application.add_handler(CommandHandler("groupid", groupid_command))

    # Отслеживание новых участников
    application.add_handler(
        ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER)
    )

    # Кнопки админа
    application.add_handler(CallbackQueryHandler(handle_admin_callback))

    # Сообщения в ЛС (для подтверждения оплаты)
    application.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message)
    )

    # Запускаем webhook-сервер для Prodamus + polling для Telegram
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(run_webhook_server(application))
    logger.info("Бот запущен!")

    # Polling для Telegram (получение обновлений)
    application.run_polling(
        allowed_updates=[Update.CHAT_MEMBER, Update.MESSAGE, Update.CALLBACK_QUERY],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
