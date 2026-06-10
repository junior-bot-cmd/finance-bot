#!/usr/bin/env python3
# v4.0 — Finance Bot: PostgreSQL + Charts + Income + Budget Limits + Monthly Reports

import os
import io
import csv
import logging
import asyncio
from datetime import datetime, date, timedelta
from collections import defaultdict

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

matplotlib.rcParams["font.family"] = "DejaVu Sans"

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ── Config ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "financereciepe_bot")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
REFERRAL_BONUS = 100.0

# ── Conversation states ────────────────────────────────────────────────────────

(
    EXP_AMOUNT, EXP_CATEGORY, EXP_SUBCATEGORY,
    INC_AMOUNT, INC_SOURCE,
    BUD_CATEGORY, BUD_AMOUNT,
) = range(7)

# ── Categories ─────────────────────────────────────────────────────────────────

CATEGORIES = [
    "🚌 Транспорт", "🚕 Такси", "🍔 Еда", "👗 Одежда",
    "🏠 Жильё и ЖКХ", "💊 Здоровье", "🎬 Развлечения",
    "📱 Связь и интернет", "🛒 Продукты", "📦 Другое",
]

INCOME_SOURCES = [
    "💼 Зарплата", "💰 Фриланс", "📈 Инвестиции", "🎁 Подарок", "💵 Другое",
]

# ── Database ───────────────────────────────────────────────────────────────────

_pool: pg_pool.ThreadedConnectionPool = None


def get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


def _db_exec(query: str, params=(), fetch: str = None):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            conn.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().putconn(conn)


async def db(query: str, params=(), fetch: str = None):
    return await asyncio.to_thread(_db_exec, query, params, fetch)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    BIGINT PRIMARY KEY,
            username   TEXT,
            first_name TEXT,
            referred_by BIGINT,
            bonus      REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id           SERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            amount       REAL NOT NULL,
            category     TEXT NOT NULL,
            subcategory  TEXT,
            expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
            created_at   TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS income (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            amount      REAL NOT NULL,
            source      TEXT,
            income_date DATE NOT NULL DEFAULT CURRENT_DATE,
            created_at  TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS budgets (
            user_id       BIGINT NOT NULL,
            category      TEXT NOT NULL,
            monthly_limit REAL NOT NULL,
            PRIMARY KEY (user_id, category)
        );
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id BIGINT NOT NULL,
            referred_id BIGINT NOT NULL,
            created_at  TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (referrer_id, referred_id)
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💸 Расход"), KeyboardButton("💵 Доход")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("📈 Отчёт с графиком")],
        [KeyboardButton("🎯 Лимиты"), KeyboardButton("📤 Экспорт CSV")],
        [KeyboardButton("👥 Пригласить друга")],
    ], resize_keyboard=True)


def _grid_keyboard(items, cols=2, one_time=True):
    rows = [items[i:i + cols] for i in range(0, len(items), cols)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=one_time)


def category_keyboard():
    return _grid_keyboard(CATEGORIES)


def income_keyboard():
    return _grid_keyboard(INCOME_SOURCES)


def period_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Этот месяц", callback_data="period_this_month"),
            InlineKeyboardButton("📅 Прошлый месяц", callback_data="period_last_month"),
        ],
        [
            InlineKeyboardButton("📅 7 дней", callback_data="period_7days"),
            InlineKeyboardButton("📅 Всё время", callback_data="period_all"),
        ],
    ])


# ── Chart helpers ──────────────────────────────────────────────────────────────

def _strip_emoji(text: str) -> str:
    parts = text.split(" ", 1)
    return parts[1] if len(parts) > 1 else text


def _make_pie_chart(totals: dict, title: str) -> io.BytesIO:
    labels = list(totals.keys())
    values = list(totals.values())
    clean = [_strip_emoji(l) for l in labels]

    fig, ax = plt.subplots(figsize=(8, 6))
    wedges, texts, autotexts = ax.pie(
        values, labels=clean, autopct="%1.1f%%", startangle=90, pctdistance=0.82
    )
    for t in autotexts:
        t.set_fontsize(8)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=15)
    legend_labels = [f"{clean[i]}: {values[i]:,.0f} ₽" for i in range(len(labels))]
    ax.legend(legend_labels, loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=2, fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf


def _make_bar_chart(daily: dict, title: str) -> io.BytesIO:
    if not daily:
        return None
    days = sorted(daily.keys())
    amounts = [daily[d] for d in days]
    labels = [d.strftime("%d") for d in days]

    fig, ax = plt.subplots(figsize=(10, 4))
    colors = ["#E74C3C" if a == max(amounts) else "#4A90D9" for a in amounts]
    bars = ax.bar(range(len(days)), amounts, color=colors, alpha=0.85, edgecolor="white")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Расходы (₽)", fontsize=10)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.grid(axis="y", alpha=0.3)
    for bar, amt in zip(bars, amounts):
        if amt > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{amt:,.0f}", ha="center", va="bottom", fontsize=7,
            )
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf


# ── Period helpers ─────────────────────────────────────────────────────────────

def _period_dates(period: str):
    today = date.today()
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == "7days":
        return today - timedelta(days=6), today
    return date(2000, 1, 1), today


_PERIOD_LABELS = {
    "this_month": "текущий месяц",
    "last_month": "прошлый месяц",
    "7days": "7 дней",
    "all": "всё время",
}


# ── User helpers ───────────────────────────────────────────────────────────────

async def ensure_user(user):
    await db(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (%s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE
           SET username = EXCLUDED.username, first_name = EXCLUDED.first_name""",
        (user.id, user.username, user.first_name),
    )


# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    referred_by = None
    if context.args:
        try:
            referred_by = int(context.args[0])
        except ValueError:
            pass

    welcome_extra = ""
    if referred_by and referred_by != user.id:
        row = await db("SELECT referred_by FROM users WHERE user_id=%s", (user.id,), fetch="one")
        if row and row["referred_by"] is None:
            await db("UPDATE users SET referred_by=%s WHERE user_id=%s", (referred_by, user.id))
            dup = await db(
                "SELECT 1 FROM referrals WHERE referrer_id=%s AND referred_id=%s",
                (referred_by, user.id), fetch="one",
            )
            if not dup:
                await db("INSERT INTO referrals (referrer_id, referred_id) VALUES (%s, %s)", (referred_by, user.id))
                await db("UPDATE users SET bonus = bonus + %s WHERE user_id=%s", (REFERRAL_BONUS, referred_by))
                try:
                    cnt = await db(
                        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s",
                        (referred_by,), fetch="one",
                    )
                    await context.bot.send_message(
                        chat_id=referred_by,
                        text=(
                            f"🎉 По твоей ссылке зарегистрировался новый пользователь!\n\n"
                            f"💰 Тебе начислено *+{REFERRAL_BONUS:.0f} бонусных ₽*\n\n"
                            f"Всего приглашено: {cnt['c']} чел."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                welcome_extra = "\n\n🤝 Тебя пригласил друг!"

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!{welcome_extra}\n\n"
        "💰 Я помогу управлять личными финансами.\n\n"
        "📌 Что умею:\n"
        "• Записывать расходы и доходы по категориям\n"
        "• Показывать статистику за любой период\n"
        "• Отправлять графики (пирог + дневные бары)\n"
        "• Устанавливать лимиты бюджета с предупреждениями\n"
        "• Автоматически присылать отчёт 1-го числа каждого месяца\n"
        "• Выгружать данные в CSV\n\n"
        "Выбирай действие:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ── Expense flow ───────────────────────────────────────────────────────────────

async def ask_expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💸 *Внести расход*\n\nВведи сумму:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EXP_AMOUNT


async def get_expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 250 или 1500.50):")
        return EXP_AMOUNT

    context.user_data["exp_amount"] = amount
    await update.message.reply_text(
        f"✅ Сумма: *{amount:,.2f} ₽*\n\nВыбери категорию:",
        parse_mode="Markdown",
        reply_markup=category_keyboard(),
    )
    return EXP_CATEGORY


async def get_expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    if category not in CATEGORIES:
        await update.message.reply_text("❌ Выбери категорию из списка:", reply_markup=category_keyboard())
        return EXP_CATEGORY

    context.user_data["exp_category"] = category
    if category == "📦 Другое":
        await update.message.reply_text(
            "✏️ Напиши название (например: *книга*, *подарок*):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return EXP_SUBCATEGORY

    return await _save_expense(update, context, subcategory=None)


async def get_expense_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sub = update.message.text.strip()
    if not sub:
        await update.message.reply_text("❌ Напиши название:")
        return EXP_SUBCATEGORY
    return await _save_expense(update, context, subcategory=sub)


async def _save_expense(update: Update, context: ContextTypes.DEFAULT_TYPE, subcategory):
    user_id = update.effective_user.id
    amount = context.user_data.get("exp_amount", 0)
    category = context.user_data.get("exp_category", "")

    await db(
        "INSERT INTO expenses (user_id, amount, category, subcategory, expense_date) VALUES (%s,%s,%s,%s,%s)",
        (user_id, amount, category, subcategory, date.today()),
    )

    alert = ""
    budget_row = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    if budget_row:
        limit = budget_row["monthly_limit"]
        spent_row = await db(
            "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
            "WHERE user_id=%s AND category=%s AND expense_date >= %s",
            (user_id, category, date.today().replace(day=1)), fetch="one",
        )
        spent = spent_row["total"] if spent_row else 0
        pct = spent / limit * 100 if limit > 0 else 0
        if pct >= 100:
            alert = f"\n\n🔴 *Лимит по «{_strip_emoji(category)}» превышён!* {spent:,.0f} / {limit:,.0f} ₽"
        elif pct >= 80:
            alert = f"\n\n🟡 *Внимание!* По «{_strip_emoji(category)}» потрачено {pct:.0f}% лимита"

    sub_text = f"\n📝 Описание: *{subcategory}*" if subcategory else ""
    await update.message.reply_text(
        f"✅ Записано!\n\n💸 Сумма: *{amount:,.2f} ₽*\n📂 Категория: {category}{sub_text}{alert}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ── Income flow ────────────────────────────────────────────────────────────────

async def ask_income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💵 *Внести доход*\n\nВведи сумму:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INC_AMOUNT


async def get_income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму:")
        return INC_AMOUNT

    context.user_data["inc_amount"] = amount
    await update.message.reply_text(
        f"✅ Сумма: *{amount:,.2f} ₽*\n\nВыбери источник:",
        parse_mode="Markdown",
        reply_markup=income_keyboard(),
    )
    return INC_SOURCE


async def get_income_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    if source not in INCOME_SOURCES:
        await update.message.reply_text("❌ Выбери из списка:", reply_markup=income_keyboard())
        return INC_SOURCE

    user_id = update.effective_user.id
    amount = context.user_data.get("inc_amount", 0)
    await db(
        "INSERT INTO income (user_id, amount, source, income_date) VALUES (%s,%s,%s,%s)",
        (user_id, amount, source, date.today()),
    )
    await update.message.reply_text(
        f"✅ Доход записан!\n\n💵 Сумма: *{amount:,.2f} ₽*\n📂 Источник: {source}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ── Statistics ─────────────────────────────────────────────────────────────────

async def show_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Статистика*\n\nВыбери период:",
        parse_mode="Markdown",
        reply_markup=period_keyboard(),
    )


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    period = query.data.replace("period_", "")
    user_id = query.from_user.id
    start_dt, end_dt = _period_dates(period)

    exp_rows = await db(
        "SELECT category, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date BETWEEN %s AND %s "
        "GROUP BY category ORDER BY total DESC",
        (user_id, start_dt, end_dt), fetch="all",
    )
    inc_row = await db(
        "SELECT COALESCE(SUM(amount),0) AS total FROM income "
        "WHERE user_id=%s AND income_date BETWEEN %s AND %s",
        (user_id, start_dt, end_dt), fetch="one",
    )
    user_row = await db("SELECT bonus FROM users WHERE user_id=%s", (user_id,), fetch="one")

    grand_exp = sum(r["total"] for r in exp_rows) if exp_rows else 0
    grand_inc = inc_row["total"] if inc_row else 0
    balance = grand_inc - grand_exp
    label = _PERIOD_LABELS.get(period, period)

    lines = [f"📊 *Статистика за {label}:*\n"]
    if not exp_rows:
        lines.append("Расходов нет.")
    else:
        for row in exp_rows:
            cat, amt = row["category"], row["total"]
            pct = amt / grand_exp * 100 if grand_exp > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"{cat}\n`{bar}` {pct:.1f}%\n💰 {amt:,.2f} ₽\n")

    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"💸 *Расходы: {grand_exp:,.2f} ₽*")
    if grand_inc > 0:
        lines.append(f"💵 *Доходы: {grand_inc:,.2f} ₽*")
        sign = "+" if balance >= 0 else ""
        emoji = "✅" if balance >= 0 else "❌"
        lines.append(f"{emoji} *Баланс: {sign}{balance:,.2f} ₽*")
    if user_row and user_row["bonus"] > 0:
        lines.append(f"\n🎁 Бонусный баланс: {user_row['bonus']:,.2f} ₽")

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ── Report with charts ─────────────────────────────────────────────────────────

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = date.today()
    start_dt = today.replace(day=1)

    await update.message.reply_text("📈 Готовлю отчёт за текущий месяц…", reply_markup=main_keyboard())

    exp_by_cat = await db(
        "SELECT category, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY category",
        (user_id, start_dt), fetch="all",
    )
    daily_rows = await db(
        "SELECT expense_date, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY expense_date ORDER BY expense_date",
        (user_id, start_dt), fetch="all",
    )
    inc_row = await db(
        "SELECT COALESCE(SUM(amount),0) AS total FROM income WHERE user_id=%s AND income_date >= %s",
        (user_id, start_dt), fetch="one",
    )

    if not exp_by_cat:
        await update.message.reply_text("📊 За текущий месяц расходов нет.", reply_markup=main_keyboard())
        return

    totals = {r["category"]: r["total"] for r in exp_by_cat}
    grand_total = sum(totals.values())
    grand_income = inc_row["total"] if inc_row else 0
    month_name = today.strftime("%B %Y")

    lines = [f"📈 *Отчёт за {month_name}*\n"]
    for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        pct = amt / grand_total * 100 if grand_total > 0 else 0
        lines.append(f"{cat}: *{amt:,.0f} ₽* ({pct:.1f}%)")
    lines.append(f"\n💸 *Итого расходов: {grand_total:,.2f} ₽*")
    if grand_income > 0:
        balance = grand_income - grand_total
        sign = "+" if balance >= 0 else ""
        lines.append(f"💵 *Доходы: {grand_income:,.2f} ₽*")
        lines.append(f"{'✅' if balance >= 0 else '❌'} *Баланс: {sign}{balance:,.2f} ₽*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    pie_buf = await asyncio.to_thread(_make_pie_chart, totals, f"Расходы по категориям — {month_name}")
    if pie_buf:
        await update.message.reply_photo(photo=pie_buf, caption="🍕 Структура расходов по категориям")

    daily_dict = {r["expense_date"]: r["total"] for r in (daily_rows or [])}
    bar_buf = await asyncio.to_thread(_make_bar_chart, daily_dict, f"Расходы по дням — {month_name}")
    if bar_buf:
        await update.message.reply_photo(photo=bar_buf, caption="📊 Расходы по дням месяца")


# ── Budget limits ──────────────────────────────────────────────────────────────

async def ask_budget_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_dt = date.today().replace(day=1)

    budget_rows = await db(
        "SELECT category, monthly_limit FROM budgets WHERE user_id=%s ORDER BY category",
        (user_id,), fetch="all",
    )

    lines = ["🎯 *Мои лимиты на текущий месяц:*\n"]
    if not budget_rows:
        lines.append("Лимиты не установлены.\n")
    else:
        for b in budget_rows:
            cat, limit = b["category"], b["monthly_limit"]
            spent_row = await db(
                "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
                "WHERE user_id=%s AND category=%s AND expense_date >= %s",
                (user_id, cat, start_dt), fetch="one",
            )
            spent = spent_row["total"] if spent_row else 0
            pct = min(spent / limit * 100, 100) if limit > 0 else 0
            filled = int(pct / 10)
            if pct >= 100:
                bar = "🟥" * filled + "⬜" * (10 - filled)
                icon = "🔴"
            elif pct >= 80:
                bar = "🟨" * filled + "⬜" * (10 - filled)
                icon = "🟡"
            else:
                bar = "🟩" * filled + "⬜" * (10 - filled)
                icon = "🟢"
            lines.append(f"{icon} {cat}\n{bar} {pct:.0f}%\n{spent:,.0f} / {limit:,.0f} ₽\n")

    lines.append("Выбери категорию для установки лимита:")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=category_keyboard(),
    )
    return BUD_CATEGORY


async def get_budget_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    if category not in CATEGORIES:
        await update.message.reply_text("❌ Выбери категорию:", reply_markup=category_keyboard())
        return BUD_CATEGORY

    context.user_data["bud_category"] = category
    user_id = update.effective_user.id
    existing = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    current = f"\nТекущий лимит: *{existing['monthly_limit']:,.0f} ₽*" if existing else ""
    await update.message.reply_text(
        f"Категория: {category}{current}\n\nВведи месячный лимит (₽):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return BUD_AMOUNT


async def get_budget_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму:")
        return BUD_AMOUNT

    user_id = update.effective_user.id
    category = context.user_data.get("bud_category", "")
    await db(
        """INSERT INTO budgets (user_id, category, monthly_limit)
           VALUES (%s,%s,%s)
           ON CONFLICT (user_id, category) DO UPDATE SET monthly_limit=EXCLUDED.monthly_limit""",
        (user_id, category, amount),
    )
    await update.message.reply_text(
        f"✅ Лимит установлен!\n\n{category}: *{amount:,.0f} ₽ / месяц*\n\n"
        "Буду предупреждать при достижении 80% и 100%.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ── CSV export ─────────────────────────────────────────────────────────────────

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    expenses = await db(
        "SELECT expense_date, category, subcategory, amount FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC",
        (user_id,), fetch="all",
    )
    incomes = await db(
        "SELECT income_date, source, amount FROM income "
        "WHERE user_id=%s ORDER BY income_date DESC",
        (user_id,), fetch="all",
    )

    if not expenses and not incomes:
        await update.message.reply_text("📤 Нет данных для экспорта.", reply_markup=main_keyboard())
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Дата", "Тип", "Категория / Источник", "Описание", "Сумма (₽)"])
    for r in expenses or []:
        writer.writerow([r["expense_date"], "Расход", r["category"], r["subcategory"] or "", f"{r['amount']:.2f}"])
    for r in incomes or []:
        writer.writerow([r["income_date"], "Доход", r["source"] or "", "", f"{r['amount']:.2f}"])

    filename = f"finance_{date.today().strftime('%Y%m%d')}.csv"
    await update.message.reply_document(
        document=buf.getvalue().encode("utf-8-sig"),
        filename=filename,
        caption="📤 Твои финансовые данные в CSV",
    )


# ── Referral ───────────────────────────────────────────────────────────────────

async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cnt_row = await db("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s", (user_id,), fetch="one")
    user_row = await db("SELECT bonus FROM users WHERE user_id=%s", (user_id,), fetch="one")
    count = cnt_row["c"] if cnt_row else 0
    bonus = user_row["bonus"] if user_row else 0.0
    ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    await update.message.reply_text(
        f"👥 *Пригласи друга!*\n\n"
        f"Когда друг запустит бота по твоей ссылке — тебе начислится *+{REFERRAL_BONUS:.0f} ₽* бонусов!\n\n"
        f"🔗 Твоя ссылка:\n`{ref_link}`\n\n"
        f"👥 Приглашено: {count} чел.\n"
        f"🎁 Бонусный баланс: *{bonus:,.2f} ₽*",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ── Reset ──────────────────────────────────────────────────────────────────────

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db("DELETE FROM expenses WHERE user_id=%s", (user_id,))
    await db("DELETE FROM income WHERE user_id=%s", (user_id,))
    await update.message.reply_text(
        "🗑️ Все расходы и доходы удалены. Бонусы, лимиты и рефералы сохранены.",
        reply_markup=main_keyboard(),
    )


# ── Cancel ─────────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ── Monthly auto-report ────────────────────────────────────────────────────────

async def send_monthly_reports(app: Application):
    today = date.today()
    end_dt = today.replace(day=1) - timedelta(days=1)
    start_dt = end_dt.replace(day=1)
    month_name = start_dt.strftime("%B %Y")
    logger.info("Sending monthly reports for %s", month_name)

    users = await db("SELECT user_id, first_name FROM users", fetch="all")
    for row in users or []:
        user_id = row["user_id"]
        first_name = row["first_name"] or "друг"
        try:
            exp_rows = await db(
                "SELECT category, SUM(amount) AS total FROM expenses "
                "WHERE user_id=%s AND expense_date BETWEEN %s AND %s GROUP BY category",
                (user_id, start_dt, end_dt), fetch="all",
            )
            if not exp_rows:
                continue

            totals = {r["category"]: r["total"] for r in exp_rows}
            grand_total = sum(totals.values())
            inc_row = await db(
                "SELECT COALESCE(SUM(amount),0) AS total FROM income "
                "WHERE user_id=%s AND income_date BETWEEN %s AND %s",
                (user_id, start_dt, end_dt), fetch="one",
            )
            grand_income = inc_row["total"] if inc_row else 0

            lines = [f"📅 *Ежемесячный отчёт за {month_name}*\n", f"Привет, {first_name}!\n"]
            for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
                pct = amt / grand_total * 100 if grand_total > 0 else 0
                lines.append(f"{cat}: *{amt:,.0f} ₽* ({pct:.1f}%)")
            lines.append(f"\n💸 *Итого расходов: {grand_total:,.2f} ₽*")
            if grand_income > 0:
                balance = grand_income - grand_total
                sign = "+" if balance >= 0 else ""
                lines.append(f"💵 *Доходы: {grand_income:,.2f} ₽*")
                lines.append(f"{'✅' if balance >= 0 else '❌'} *Баланс: {sign}{balance:,.2f} ₽*")

            await app.bot.send_message(chat_id=user_id, text="\n".join(lines), parse_mode="Markdown")

            pie_buf = await asyncio.to_thread(_make_pie_chart, totals, f"Расходы — {month_name}")
            if pie_buf:
                await app.bot.send_photo(
                    chat_id=user_id, photo=pie_buf,
                    caption=f"📊 Структура расходов за {month_name}",
                )

            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("Monthly report failed for user %s: %s", user_id, e)


# ── Menu router ────────────────────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💸 Расход":
        return await ask_expense_amount(update, context)
    if text == "💵 Доход":
        return await ask_income_amount(update, context)
    if text == "📊 Статистика":
        await show_stats_menu(update, context)
    elif text == "📈 Отчёт с графиком":
        await show_report(update, context)
    elif text == "🎯 Лимиты":
        return await ask_budget_category(update, context)
    elif text == "📤 Экспорт CSV":
        await export_csv(update, context)
    elif text == "👥 Пригласить друга":
        await invite(update, context)
    return ConversationHandler.END


# ── Application lifecycle ──────────────────────────────────────────────────────

async def _post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_monthly_reports,
        CronTrigger(day=1, hour=9, minute=0),
        args=[app],
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started — monthly reports will fire on the 1st of each month at 09:00")


async def _post_shutdown(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TOKEN не найден в переменных окружения!")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в переменных окружения!")

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    menu_pattern = (
        r"^(💸 Расход|💵 Доход|📊 Статистика|📈 Отчёт с графиком|"
        r"🎯 Лимиты|📤 Экспорт CSV|👥 Пригласить друга)$"
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(menu_pattern), handle_menu),
        ],
        states={
            EXP_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_amount)],
            EXP_CATEGORY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_category)],
            EXP_SUBCATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_subcategory)],
            INC_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_income_amount)],
            INC_SOURCE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_income_source)],
            BUD_CATEGORY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_budget_category)],
            BUD_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_budget_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(stats_callback, pattern=r"^period_"))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("report", show_report))
    app.add_handler(CommandHandler("export", export_csv))

    print("🤖 Finance Bot v4.0 запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
