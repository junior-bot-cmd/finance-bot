#!/usr/bin/env python3
"""
Telegram Finance Tracker Bot
Tracks expenses by category and provides statistics
"""

import json
import os
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token
TOKEN = "8819550333:AAEjfwcwbD8RT-kMt1SxcHW4ioeaIfoBGAM"

# Conversation states
AMOUNT, CATEGORY = range(2)

# Categories
CATEGORIES = [
    "🚌 Транспорт",
    "🚕 Такси",
    "🍔 Еда",
    "👗 Одежда",
    "🏠 Жильё и ЖКХ",
    "💊 Здоровье",
    "🎬 Развлечения",
    "📱 Связь и интернет",
    "🛒 Продукты",
    "📦 Другое",
]

# Data file
DATA_FILE = "finance_data.json"


def load_data():
    """Load expense data from file."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    """Save expense data to file."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_data(data, user_id):
    """Get or create user data."""
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"expenses": []}
    return data[uid]


def category_keyboard():
    """Build category keyboard."""
    keyboard = []
    row = []
    for i, cat in enumerate(CATEGORIES):
        row.append(cat)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation — ask for amount."""
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "💰 Я помогу тебе отслеживать расходы.\n\n"
        "Введи сумму, которую ты потратил(а):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT


async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive amount and ask for category."""
    text = update.message.text.strip().replace(",", ".")

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except ValueError:
        await update.message.reply_text(
            "❌ Пожалуйста, введи корректную сумму (например: 250 или 1500.50):"
        )
        return AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text(
        f"✅ Сумма: *{amount:,.2f} ₽*\n\nВыбери категорию расхода:",
        parse_mode="Markdown",
        reply_markup=category_keyboard(),
    )
    return CATEGORY


async def get_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive category and save expense."""
    category = update.message.text.strip()

    if category not in CATEGORIES:
        await update.message.reply_text(
            "❌ Пожалуйста, выбери категорию из списка ниже:",
            reply_markup=category_keyboard(),
        )
        return CATEGORY

    amount = context.user_data.get("amount", 0)
    user_id = update.effective_user.id

    # Save to file
    data = load_data()
    user_data = get_user_data(data, user_id)
    user_data["expenses"].append({
        "amount": amount,
        "category": category,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_data(data)

    await update.message.reply_text(
        f"✅ Записано!\n\n"
        f"💸 Сумма: *{amount:,.2f} ₽*\n"
        f"📂 Категория: {category}\n\n"
        f"Введи следующую сумму или используй команды:\n"
        f"/stats — статистика\n"
        f"/reset — сбросить данные\n"
        f"/add — добавить новый расход",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show expense statistics."""
    user_id = update.effective_user.id
    data = load_data()
    user_data = get_user_data(data, user_id)
    expenses = user_data.get("expenses", [])

    if not expenses:
        await update.message.reply_text(
            "📊 У тебя пока нет записанных расходов.\n\nИспользуй /start или /add чтобы добавить расход."
        )
        return

    # Calculate totals by category
    totals = {}
    grand_total = 0
    for exp in expenses:
        cat = exp["category"]
        amt = exp["amount"]
        totals[cat] = totals.get(cat, 0) + amt
        grand_total += amt

    # Sort by amount descending
    sorted_totals = sorted(totals.items(), key=lambda x: x[1], reverse=True)

    lines = ["📊 *Твоя статистика расходов:*\n"]
    for cat, amt in sorted_totals:
        pct = (amt / grand_total * 100) if grand_total > 0 else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        lines.append(f"{cat}\n`{bar}` {pct:.1f}%\n💰 {amt:,.2f} ₽\n")

    lines.append(f"━━━━━━━━━━━━━━━━")
    lines.append(f"💳 *Итого: {grand_total:,.2f} ₽*")
    lines.append(f"📝 Всего записей: {len(expenses)}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all user data."""
    user_id = update.effective_user.id
    data = load_data()
    uid = str(user_id)
    if uid in data:
        data[uid] = {"expenses": []}
        save_data(data)
    await update.message.reply_text(
        "🗑️ Все данные удалены.\n\nИспользуй /start или /add чтобы начать заново."
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start adding a new expense."""
    await update.message.reply_text(
        "💰 Введи сумму, которую ты потратил(а):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel current operation."""
    await update.message.reply_text(
        "❌ Отменено.\n\nИспользуй /start или /add чтобы добавить расход.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    await update.message.reply_text(
        "ℹ️ *Помощь по боту:*\n\n"
        "/start или /add — добавить расход\n"
        "/stats — посмотреть статистику\n"
        "/reset — сбросить все данные\n"
        "/cancel — отменить текущее действие\n\n"
        "*Категории расходов:*\n" + "\n".join(CATEGORIES),
        parse_mode="Markdown",
    )


def main():
    """Run the bot."""
    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("add", add),
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_category)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("help", help_command))

    print("🤖 Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
