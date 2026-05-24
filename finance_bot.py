#!/usr/bin/env python3
# v3.0
import json
import os
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "financereciepe_bot")

AMOUNT, CATEGORY, SUBCATEGORY = range(3)

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

REFERRAL_BONUS = 100.0
DATA_FILE = "finance_data.json"


def main_keyboard():
    keyboard = [
        [KeyboardButton("💰 Внести сумму")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("🗑️ Сбросить данные")],
        [KeyboardButton("👥 Пригласить друга")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def category_keyboard():
    keyboard = []
    row = []
    for cat in CATEGORIES:
        row.append(cat)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_data(data, user_id):
    uid = str(user_id)
    if uid not in data:
        data[uid] = {
            "expenses": [],
            "bonus": 0.0,
            "referrals": [],
            "referred_by": None,
        }
    if "bonus" not in data[uid]:
        data[uid]["bonus"] = 0.0
    if "referrals" not in data[uid]:
        data[uid]["referrals"] = []
    if "referred_by" not in data[uid]:
        data[uid]["referred_by"] = None
    return data[uid]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_id = user.id

    data = load_data()
    user_data = get_user_data(data, user_id)

    referred_by = None
    if context.args:
        try:
            referred_by = int(context.args[0])
        except ValueError:
            referred_by = None

    welcome_text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "💰 Я помогу отслеживать твои расходы.\n\n"
        "Используй кнопки внизу экрана:"
    )

    if referred_by and referred_by != user_id and user_data["referred_by"] is None:
        user_data["referred_by"] = referred_by
        save_data(data)

        data = load_data()
        referrer_data = get_user_data(data, referred_by)
        if str(user_id) not in referrer_data["referrals"]:
            referrer_data["referrals"].append(str(user_id))
            referrer_data["bonus"] = referrer_data.get("bonus", 0.0) + REFERRAL_BONUS
            save_data(data)

            try:
                await context.bot.send_message(
                    chat_id=referred_by,
                    text=f"🎉 По твоей ссылке зарегистрировался новый пользователь!\n\n"
                         f"💰 Тебе начислено *+{REFERRAL_BONUS:.0f} бонусных ₽*\n\n"
                         f"Всего приглашено: {len(referrer_data['referrals'])} чел.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        welcome_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            f"🤝 Тебя пригласил друг!\n\n"
            f"💰 Я помогу отслеживать твои расходы.\n\n"
            f"Используй кнопки внизу экрана:"
        )

    await update.message.reply_text(
        welcome_text,
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "💰 Введи сумму, которую ты потратил(а):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT


async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 250 или 1500.50):")
        return AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text(
        f"✅ Сумма: *{amount:,.2f} ₽*\n\nВыбери категорию:",
        parse_mode="Markdown",
        reply_markup=category_keyboard(),
    )
    return CATEGORY


async def get_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    category = update.message.text.strip()
    if category not in CATEGORIES:
        await update.message.reply_text("❌ Выбери категорию из списка:", reply_markup=category_keyboard())
        return CATEGORY

    context.user_data["category"] = category

    if category == "📦 Другое":
        await update.message.reply_text(
            "✏️ Напиши на что именно потрачено?\n\nНапример: *книга*, *подарок*, *ремонт*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SUBCATEGORY

    return await save_expense(update, context, subcategory=None)


async def get_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subcategory = update.message.text.strip().lower()
    if not subcategory:
        await update.message.reply_text("❌ Напиши название подкатегории:")
        return SUBCATEGORY
    return await save_expense(update, context, subcategory=subcategory)


async def save_expense(update: Update, context: ContextTypes.DEFAULT_TYPE, subcategory) -> int:
    amount = context.user_data.get("amount", 0)
    category = context.user_data.get("category", "")
    user_id = update.effective_user.id

    data = load_data()
    user_data = get_user_data(data, user_id)
    user_data["expenses"].append({
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_data(data)

    sub_text = f"\n📝 Подкатегория: *{subcategory}*" if subcategory else ""
    await update.message.reply_text(
        f"✅ Записано!\n\n💸 Сумма: *{amount:,.2f} ₽*\n📂 Категория: {category}{sub_text}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_data()
    user_data = get_user_data(data, user_id)
    expenses = user_data.get("expenses", [])
    bonus = user_data.get("bonus", 0.0)
    referrals = user_data.get("referrals", [])

    if not expenses and bonus == 0:
        await update.message.reply_text(
            "📊 Нет расходов. Нажми *💰 Внести сумму* чтобы добавить.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return

    totals = {}
    subcategory_totals = {}
    grand_total = 0

    for exp in expenses:
        cat = exp["category"]
        amt = exp["amount"]
        sub = exp.get("subcategory")
        totals[cat] = totals.get(cat, 0) + amt
        grand_total += amt

        if sub and cat == "📦 Другое":
            if cat not in subcategory_totals:
                subcategory_totals[cat] = {}
            subcategory_totals[cat][sub] = subcategory_totals[cat].get(sub, 0) + amt

    sorted_totals = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    lines = ["📊 *Статистика расходов:*\n"]

    for cat, amt in sorted_totals:
        pct = (amt / grand_total * 100) if grand_total > 0 else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        lines.append(f"{cat}\n`{bar}` {pct:.1f}%\n💰 {amt:,.2f} ₽")

        if cat in subcategory_totals:
            subs = sorted(subcategory_totals[cat].items(), key=lambda x: x[1], reverse=True)
            for sub_name, sub_amt in subs:
                lines.append(f"  └ {sub_name}: {sub_amt:,.2f} ₽")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"💳 *Итого расходов: {grand_total:,.2f} ₽*")
    lines.append(f"📝 Записей: {len(expenses)}")

    if bonus > 0:
        lines.append(f"\n🎁 *Бонусный баланс: {bonus:,.2f} ₽*")
        lines.append(f"👥 Приглашено друзей: {len(referrals)}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_data()
    user_data = get_user_data(data, user_id)
    referrals = user_data.get("referrals", [])
    bonus = user_data.get("bonus", 0.0)

    ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    text = (
        f"👥 *Пригласи друга!*\n\n"
        f"Поделись ссылкой — когда друг запустит бота, "
        f"тебе начислится *+{REFERRAL_BONUS:.0f} ₽* на бонусный счёт!\n\n"
        f"🔗 Твоя ссылка:\n`{ref_link}`\n\n"
        f"📊 *Твоя статистика:*\n"
        f"👥 Приглашено друзей: {len(referrals)}\n"
        f"🎁 Бонусный баланс: {bonus:,.2f} ₽"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_data()
    uid = str(user_id)
    if uid in data:
        bonus = data[uid].get("bonus", 0.0)
        referrals = data[uid].get("referrals", [])
        referred_by = data[uid].get("referred_by", None)
        data[uid] = {
            "expenses": [],
            "bonus": bonus,
            "referrals": referrals,
            "referred_by": referred_by,
        }
        save_data(data)
    await update.message.reply_text(
        "🗑️ Расходы удалены. Бонусы и рефералы сохранены.",
        reply_markup=main_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "💰 Внести сумму":
        return await ask_amount(update, context)
    elif text == "📊 Статистика":
        await stats(update, context)
        return ConversationHandler.END
    elif text == "🗑️ Сбросить данные":
        await reset(update, context)
        return ConversationHandler.END
    elif text == "👥 Пригласить друга":
        await invite(update, context)
        return ConversationHandler.END
    return ConversationHandler.END


def main():
    if not TOKEN:
        raise ValueError("TOKEN не найден!")

    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(
                filters.Regex("^(💰 Внести сумму|📊 Статистика|🗑️ Сбросить данные|👥 Пригласить друга)$"),
                handle_menu
            ),
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_category)],
            SUBCATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_subcategory)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("invite", invite))

    print("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
