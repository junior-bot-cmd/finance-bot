#!/usr/bin/env python3
# v5.1 — asyncpg + Python 3.13 compatible

import os
import io
import csv
import re
import logging
import asyncio
from datetime import date, timedelta

import asyncpg

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

TOKEN        = os.environ.get("TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "financereciepe_bot")
# Railway выдаёт postgres://, asyncpg тоже принимает, но явно исправим
DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
REFERRAL_BONUS = 100.0

# ── Conversation states ────────────────────────────────────────────────────────

EXP_AMOUNT, EXP_CATEGORY, EXP_NOTE, BUD_CATEGORY, BUD_AMOUNT = range(5)

# ── Categories ─────────────────────────────────────────────────────────────────

CATEGORIES = [
    "🚌 Транспорт", "🚕 Такси", "🍔 Еда", "👗 Одежда",
    "🏠 Жильё и ЖКХ", "💊 Здоровье", "🎬 Развлечения",
    "📱 Связь и интернет", "🛒 Продукты", "📦 Другое",
]

QUICK_MAP: dict = {
    "транспорт": "🚌 Транспорт", "автобус": "🚌 Транспорт",
    "метро":     "🚌 Транспорт", "трамвай": "🚌 Транспорт",
    "маршрутка": "🚌 Транспорт",
    "такси":     "🚕 Такси",     "uber":    "🚕 Такси",
    "убер":      "🚕 Такси",     "яндекс":  "🚕 Такси",
    "еда":       "🍔 Еда",       "кафе":    "🍔 Еда",
    "ресторан":  "🍔 Еда",       "обед":    "🍔 Еда",
    "ужин":      "🍔 Еда",       "завтрак": "🍔 Еда",
    "кофе":      "🍔 Еда",       "перекус": "🍔 Еда",
    "одежда":    "👗 Одежда",    "обувь":   "👗 Одежда",
    "жкх":       "🏠 Жильё и ЖКХ", "коммуналка": "🏠 Жильё и ЖКХ",
    "аренда":    "🏠 Жильё и ЖКХ", "квартира":   "🏠 Жильё и ЖКХ",
    "здоровье":  "💊 Здоровье",  "аптека":    "💊 Здоровье",
    "лекарство": "💊 Здоровье",  "врач":      "💊 Здоровье",
    "больница":  "💊 Здоровье",  "стоматолог":"💊 Здоровье",
    "кино":      "🎬 Развлечения","театр":    "🎬 Развлечения",
    "концерт":   "🎬 Развлечения","клуб":     "🎬 Развлечения",
    "связь":     "📱 Связь и интернет", "интернет": "📱 Связь и интернет",
    "телефон":   "📱 Связь и интернет",
    "продукты":  "🛒 Продукты",  "магазин":   "🛒 Продукты",
    "супермаркет":"🛒 Продукты", "пятерочка": "🛒 Продукты",
    "пятёрочка": "🛒 Продукты",  "магнит":    "🛒 Продукты",
    "дикси":     "🛒 Продукты",  "ашан":      "🛒 Продукты",
    "лента":     "🛒 Продукты",  "вкусвилл":  "🛒 Продукты",
    "перекрёсток":"🛒 Продукты",
}

WEEKDAYS_RU = ["Воскресенье", "Понедельник", "Вторник", "Среда",
               "Четверг", "Пятница", "Суббота"]

# ── Database (asyncpg) ─────────────────────────────────────────────────────────

_pool: asyncpg.Pool = None


def _to_pg(query: str) -> str:
    """Convert %s placeholders to $1, $2, ... for asyncpg."""
    n = 0
    result = []
    for part in re.split(r"(%s)", query):
        if part == "%s":
            n += 1
            result.append(f"${n}")
        else:
            result.append(part)
    return "".join(result)


async def db(query: str, params=(), fetch: str = None):
    query = _to_pg(query)
    async with _pool.acquire() as conn:
        if fetch == "one":
            return await conn.fetchrow(query, *params)
        if fetch == "all":
            return await conn.fetch(query, *params)
        await conn.execute(query, *params)


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                referred_by BIGINT,
                bonus       REAL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT NOT NULL,
                amount       REAL NOT NULL,
                category     TEXT NOT NULL,
                note         TEXT,
                expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_user_date
            ON expenses(user_id, expense_date)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                user_id       BIGINT NOT NULL,
                category      TEXT NOT NULL,
                monthly_limit REAL NOT NULL,
                PRIMARY KEY (user_id, category)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id BIGINT NOT NULL,
                referred_id BIGINT NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (referrer_id, referred_id)
            )
        """)
    logger.info("Database initialised.")


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💸 Добавить расход")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("📈 Отчёт с графиком")],
        [KeyboardButton("💳 История"),    KeyboardButton("🎯 Лимиты")],
        [KeyboardButton("📤 Экспорт CSV"), KeyboardButton("👥 Пригласить")],
    ], resize_keyboard=True)


def _grid(items, cols=2, one_time=True):
    rows = [items[i:i + cols] for i in range(0, len(items), cols)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=one_time)


def category_keyboard():
    return _grid(CATEGORIES)


def period_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Этот месяц",    callback_data="period_this_month"),
            InlineKeyboardButton("📅 Прошлый месяц", callback_data="period_last_month"),
        ],
        [
            InlineKeyboardButton("📅 7 дней",   callback_data="period_7days"),
            InlineKeyboardButton("📅 Всё время", callback_data="period_all"),
        ],
    ])


# ── Helpers ────────────────────────────────────────────────────────────────────

_PERIOD_LABELS = {
    "this_month": "текущий месяц",
    "last_month": "прошлый месяц",
    "7days":      "7 дней",
    "all":        "всё время",
}


def period_dates(period: str):
    today = date.today()
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        first = today.replace(day=1)
        last  = first - timedelta(days=1)
        return last.replace(day=1), last
    if period == "7days":
        return today - timedelta(days=6), today
    return date(2000, 1, 1), today


def _strip_emoji(text: str) -> str:
    parts = text.split(" ", 1)
    return parts[1] if len(parts) > 1 else text


async def ensure_user(user):
    await db(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (%s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE
           SET username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
        (user.id, user.username, user.first_name),
    )


# ── Charts ─────────────────────────────────────────────────────────────────────

def _pie_chart(totals: dict, title: str) -> io.BytesIO:
    labels = list(totals.keys())
    values = [float(v) for v in totals.values()]
    clean  = [_strip_emoji(l) for l in labels]

    fig, ax = plt.subplots(figsize=(8, 6))
    _, texts, autotexts = ax.pie(
        values, labels=clean, autopct="%1.1f%%",
        startangle=90, pctdistance=0.82,
    )
    for t in autotexts:
        t.set_fontsize(8)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=15)
    legend = [f"{clean[i]}: {values[i]:,.0f} ₽" for i in range(len(labels))]
    ax.legend(legend, loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=2, fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf


def _bar_chart(daily: dict, title: str):
    if not daily:
        return None
    days    = sorted(daily.keys())
    amounts = [float(daily[d]) for d in days]
    labels  = [d.strftime("%d") for d in days]
    max_val = max(amounts)

    fig, ax = plt.subplots(figsize=(10, 4))
    colors = ["#E74C3C" if a == max_val else "#4A90D9" for a in amounts]
    bars   = ax.bar(range(len(days)), amounts, color=colors, alpha=0.85, edgecolor="white")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Расходы (₽)", fontsize=10)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.grid(axis="y", alpha=0.3)
    for bar, amt in zip(bars, amounts):
        if amt > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f"{amt:,.0f}", ha="center", va="bottom", fontsize=7)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf


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
                await db("INSERT INTO referrals (referrer_id, referred_id) VALUES (%s,%s)", (referred_by, user.id))
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
        "💸 Я помогу понять, *на что уходят деньги*.\n\n"
        "⚡️ *Быстрый ввод* — просто напиши:\n"
        "`500 такси` или `1200 продукты`\n\n"
        "📌 Или используй кнопки меню:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ── Expense flow ───────────────────────────────────────────────────────────────

async def ask_expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💸 Введи сумму расхода:",
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
            "✏️ Напиши на что именно (например: *книга*, *подарок*):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return EXP_NOTE

    return await _save_expense(update, context, note=None)


async def get_expense_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    if not note:
        await update.message.reply_text("❌ Напиши описание:")
        return EXP_NOTE
    return await _save_expense(update, context, note=note)


async def _save_expense(update, context, note, amount=None, category=None):
    user_id  = update.effective_user.id
    amount   = amount   or context.user_data.get("exp_amount", 0)
    category = category or context.user_data.get("exp_category", "")

    await db(
        "INSERT INTO expenses (user_id, amount, category, note, expense_date) VALUES (%s,%s,%s,%s,%s)",
        (user_id, amount, category, note, date.today()),
    )

    alert = ""
    budget_row = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    if budget_row:
        limit = float(budget_row["monthly_limit"])
        spent_row = await db(
            "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
            "WHERE user_id=%s AND category=%s AND expense_date >= %s",
            (user_id, category, date.today().replace(day=1)), fetch="one",
        )
        spent = float(spent_row["total"]) if spent_row else 0
        pct   = spent / limit * 100 if limit > 0 else 0
        if pct >= 100:
            alert = f"\n\n🔴 *Лимит по «{_strip_emoji(category)}» превышён!*\n{spent:,.0f} / {limit:,.0f} ₽"
        elif pct >= 80:
            alert = f"\n\n🟡 По «{_strip_emoji(category)}» потрачено *{pct:.0f}%* лимита"

    note_text = f"\n📝 {note}" if note else ""
    await update.message.reply_text(
        f"✅ Записано!\n💸 *{amount:,.2f} ₽* — {category}{note_text}{alert}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ── Quick add ──────────────────────────────────────────────────────────────────

_QUICK_RE = re.compile(r"^(\d+[.,]?\d*)\s+(.+)$", re.DOTALL)


async def quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    m = _QUICK_RE.match(text)
    if not m:
        return

    try:
        amount = float(m.group(1).replace(",", "."))
        if amount <= 0:
            return
    except ValueError:
        return

    keyword  = m.group(2).strip().lower()
    category = None
    matched_kw = None

    for kw, cat in QUICK_MAP.items():
        if keyword.startswith(kw) or kw in keyword:
            category   = cat
            matched_kw = kw
            break

    await ensure_user(update.effective_user)
    user_id = update.effective_user.id

    if category and matched_kw:
        idx  = keyword.find(matched_kw)
        tail = keyword[idx + len(matched_kw):].strip()
        note = tail if tail else None
    else:
        category = "📦 Другое"
        note     = m.group(2).strip()

    await db(
        "INSERT INTO expenses (user_id, amount, category, note, expense_date) VALUES (%s,%s,%s,%s,%s)",
        (user_id, amount, category, note, date.today()),
    )

    alert = ""
    budget_row = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    if budget_row:
        limit = float(budget_row["monthly_limit"])
        spent_row = await db(
            "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
            "WHERE user_id=%s AND category=%s AND expense_date >= %s",
            (user_id, category, date.today().replace(day=1)), fetch="one",
        )
        spent = float(spent_row["total"]) if spent_row else 0
        pct   = spent / limit * 100 if limit > 0 else 0
        if pct >= 100:
            alert = f"\n🔴 Лимит по «{_strip_emoji(category)}» превышён!"
        elif pct >= 80:
            alert = f"\n🟡 По «{_strip_emoji(category)}» потрачено {pct:.0f}% лимита"

    note_disp = f"\n📝 {note}" if note else ""
    await update.message.reply_text(
        f"✅ *{amount:,.2f} ₽* — {category}{note_disp}{alert}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ── Statistics ─────────────────────────────────────────────────────────────────

async def show_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Статистика*\n\nВыбери период:",
        parse_mode="Markdown",
        reply_markup=period_keyboard(),
    )


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    period  = query.data.replace("period_", "")
    user_id = query.from_user.id
    start_dt, end_dt = period_dates(period)

    rows = await db(
        "SELECT category, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date BETWEEN %s AND %s "
        "GROUP BY category ORDER BY total DESC",
        (user_id, start_dt, end_dt), fetch="all",
    )
    user_row = await db("SELECT bonus FROM users WHERE user_id=%s", (user_id,), fetch="one")

    grand = sum(float(r["total"]) for r in rows) if rows else 0
    label = _PERIOD_LABELS.get(period, period)

    lines = [f"📊 *Статистика за {label}:*\n"]
    if not rows:
        lines.append("Расходов нет.")
    else:
        for row in rows:
            cat = row["category"]
            amt = float(row["total"])
            pct = amt / grand * 100 if grand > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"{cat}\n`{bar}` {pct:.1f}%\n💰 {amt:,.2f} ₽\n")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"💸 *Итого: {grand:,.2f} ₽*")

    if user_row and user_row["bonus"] and float(user_row["bonus"]) > 0:
        lines.append(f"\n🎁 Бонусный баланс: {float(user_row['bonus']):,.2f} ₽")

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ── Report with charts + insights ─────────────────────────────────────────────

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    today    = date.today()
    start_dt = today.replace(day=1)

    await update.message.reply_text("📈 Готовлю отчёт…", reply_markup=main_keyboard())

    exp_by_cat = await db(
        "SELECT category, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY category ORDER BY total DESC",
        (user_id, start_dt), fetch="all",
    )
    if not exp_by_cat:
        await update.message.reply_text("За текущий месяц расходов нет.", reply_markup=main_keyboard())
        return

    daily_rows = await db(
        "SELECT expense_date, SUM(amount) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY expense_date ORDER BY expense_date",
        (user_id, start_dt), fetch="all",
    )
    avg_row = await db(
        """SELECT AVG(day_total) AS avg_day FROM (
               SELECT expense_date, SUM(amount) AS day_total
               FROM expenses WHERE user_id=%s AND expense_date >= %s
               GROUP BY expense_date
           ) t""",
        (user_id, start_dt), fetch="one",
    )
    weekday_row = await db(
        """SELECT EXTRACT(DOW FROM expense_date)::int AS dow, SUM(amount) AS total
           FROM expenses WHERE user_id=%s AND expense_date >= %s
           GROUP BY dow ORDER BY total DESC LIMIT 1""",
        (user_id, start_dt), fetch="one",
    )
    lm_end   = start_dt - timedelta(days=1)
    lm_start = lm_end.replace(day=1)
    lm_row = await db(
        "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
        "WHERE user_id=%s AND expense_date BETWEEN %s AND %s",
        (user_id, lm_start, lm_end), fetch="one",
    )

    totals     = {r["category"]: float(r["total"]) for r in exp_by_cat}
    grand      = sum(totals.values())
    month_name = today.strftime("%B %Y")
    lm_total   = float(lm_row["total"]) if lm_row else 0
    avg_daily  = float(avg_row["avg_day"]) if avg_row and avg_row["avg_day"] else 0
    top_wd     = WEEKDAYS_RU[int(weekday_row["dow"])] if weekday_row else "—"

    lines = [f"📈 *Отчёт за {month_name}*\n"]
    for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        pct = amt / grand * 100 if grand > 0 else 0
        lines.append(f"{cat}: *{amt:,.0f} ₽* ({pct:.1f}%)")
    lines.append(f"\n💸 *Итого: {grand:,.2f} ₽*")
    lines.append(f"📅 Среднедневные траты: *{avg_daily:,.0f} ₽*")
    lines.append(f"📆 Самый затратный день недели: *{top_wd}*")
    if lm_total > 0:
        diff = grand - lm_total
        sign = "+" if diff >= 0 else ""
        arrow = "📈" if diff > 0 else "📉"
        lines.append(f"{arrow} vs прошлый месяц: {sign}{diff:,.0f} ₽ ({sign}{diff / lm_total * 100:.1f}%)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    pie_buf = await asyncio.to_thread(_pie_chart, totals, f"Расходы — {month_name}")
    if pie_buf:
        await update.message.reply_photo(photo=pie_buf, caption="🍕 Структура расходов по категориям")

    daily_dict = {r["expense_date"]: float(r["total"]) for r in (daily_rows or [])}
    bar_buf = await asyncio.to_thread(_bar_chart, daily_dict, f"Расходы по дням — {month_name}")
    if bar_buf:
        await update.message.reply_photo(photo=bar_buf, caption="📊 Расходы по дням месяца")


# ── History + delete ───────────────────────────────────────────────────────────

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = await db(
        "SELECT id, expense_date, category, amount, note FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC, created_at DESC LIMIT 10",
        (user_id,), fetch="all",
    )
    if not rows:
        await update.message.reply_text("💳 История пуста.", reply_markup=main_keyboard())
        return

    text_lines = ["💳 *Последние 10 расходов:*\n"]
    buttons = []
    for r in rows:
        day  = r["expense_date"].strftime("%d.%m")
        note = f" — {r['note']}" if r["note"] else ""
        text_lines.append(f"{day} | {r['category']}{note}: *{float(r['amount']):,.0f} ₽*")
        buttons.append([InlineKeyboardButton(
            f"🗑 {day} {_strip_emoji(r['category'])} {float(r['amount']):,.0f}₽",
            callback_data=f"del_{r['id']}",
        )])

    await update.message.reply_text(
        "\n".join(text_lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def delete_expense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    exp_id  = int(query.data.split("_")[1])
    user_id = query.from_user.id

    row = await db(
        "SELECT id FROM expenses WHERE id=%s AND user_id=%s",
        (exp_id, user_id), fetch="one",
    )
    if not row:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    await db("DELETE FROM expenses WHERE id=%s AND user_id=%s", (exp_id, user_id))

    rows = await db(
        "SELECT id, expense_date, category, amount, note FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC, created_at DESC LIMIT 10",
        (user_id,), fetch="all",
    )
    if not rows:
        await query.edit_message_text("💳 История пуста.")
        return

    text_lines = ["💳 *Последние 10 расходов:*\n"]
    buttons = []
    for r in rows:
        day  = r["expense_date"].strftime("%d.%m")
        note = f" — {r['note']}" if r["note"] else ""
        text_lines.append(f"{day} | {r['category']}{note}: *{float(r['amount']):,.0f} ₽*")
        buttons.append([InlineKeyboardButton(
            f"🗑 {day} {_strip_emoji(r['category'])} {float(r['amount']):,.0f}₽",
            callback_data=f"del_{r['id']}",
        )])

    await query.edit_message_text(
        "\n".join(text_lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Budget limits ──────────────────────────────────────────────────────────────

async def ask_budget_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
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
            cat   = b["category"]
            limit = float(b["monthly_limit"])
            spent_row = await db(
                "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
                "WHERE user_id=%s AND category=%s AND expense_date >= %s",
                (user_id, cat, start_dt), fetch="one",
            )
            spent  = float(spent_row["total"]) if spent_row else 0
            pct    = min(spent / limit * 100, 100) if limit > 0 else 0
            filled = int(pct / 10)
            if pct >= 100:
                bar, icon = "🟥" * filled + "⬜" * (10 - filled), "🔴"
            elif pct >= 80:
                bar, icon = "🟨" * filled + "⬜" * (10 - filled), "🟡"
            else:
                bar, icon = "🟩" * filled + "⬜" * (10 - filled), "🟢"
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
    user_id  = update.effective_user.id
    existing = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    current = f"\nТекущий лимит: *{float(existing['monthly_limit']):,.0f} ₽*" if existing else ""
    await update.message.reply_text(
        f"{category}{current}\n\nВведи новый месячный лимит (₽):",
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

    user_id  = update.effective_user.id
    category = context.user_data.get("bud_category", "")
    await db(
        """INSERT INTO budgets (user_id, category, monthly_limit)
           VALUES (%s,%s,%s)
           ON CONFLICT (user_id, category) DO UPDATE SET monthly_limit=EXCLUDED.monthly_limit""",
        (user_id, category, amount),
    )
    await update.message.reply_text(
        f"✅ Лимит установлен!\n{category}: *{amount:,.0f} ₽ / месяц*\n\n"
        "Предупрежу при достижении 80% и 100%.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ── CSV export ─────────────────────────────────────────────────────────────────

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows    = await db(
        "SELECT expense_date, category, note, amount FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC",
        (user_id,), fetch="all",
    )
    if not rows:
        await update.message.reply_text("📤 Нет данных для экспорта.", reply_markup=main_keyboard())
        return

    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Дата", "Категория", "Описание", "Сумма (₽)"])
    for r in rows:
        writer.writerow([r["expense_date"], r["category"], r["note"] or "", f"{float(r['amount']):.2f}"])

    await update.message.reply_document(
        document=buf.getvalue().encode("utf-8-sig"),
        filename=f"expenses_{date.today().strftime('%Y%m%d')}.csv",
        caption="📤 Все твои расходы в CSV",
    )


# ── Referral ───────────────────────────────────────────────────────────────────

async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cnt_row  = await db("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s", (user_id,), fetch="one")
    user_row = await db("SELECT bonus FROM users WHERE user_id=%s", (user_id,), fetch="one")
    count = int(cnt_row["c"]) if cnt_row else 0
    bonus = float(user_row["bonus"]) if user_row and user_row["bonus"] else 0.0
    link  = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    await update.message.reply_text(
        f"👥 *Пригласи друга!*\n\n"
        f"Когда друг запустит бота по твоей ссылке — тебе начислится *+{REFERRAL_BONUS:.0f} ₽* бонусов!\n\n"
        f"🔗 Твоя ссылка:\n`{link}`\n\n"
        f"👥 Приглашено: {count} чел.\n"
        f"🎁 Бонусный баланс: *{bonus:,.2f} ₽*",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ── Reset ──────────────────────────────────────────────────────────────────────

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db("DELETE FROM expenses WHERE user_id=%s", (user_id,))
    await update.message.reply_text(
        "🗑️ Все расходы удалены. Бонусы и лимиты сохранены.",
        reply_markup=main_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ── Auto-reports ───────────────────────────────────────────────────────────────

async def _send_monthly_reports(app: Application):
    today    = date.today()
    end_dt   = today.replace(day=1) - timedelta(days=1)
    start_dt = end_dt.replace(day=1)
    month_name = start_dt.strftime("%B %Y")
    logger.info("Sending monthly reports for %s", month_name)

    users = await db("SELECT user_id, first_name FROM users", fetch="all")
    for row in users or []:
        uid  = row["user_id"]
        name = row["first_name"] or "друг"
        try:
            exp_rows = await db(
                "SELECT category, SUM(amount) AS total FROM expenses "
                "WHERE user_id=%s AND expense_date BETWEEN %s AND %s GROUP BY category",
                (uid, start_dt, end_dt), fetch="all",
            )
            if not exp_rows:
                continue

            totals = {r["category"]: float(r["total"]) for r in exp_rows}
            grand  = sum(totals.values())

            lines = [f"📅 *Ежемесячный отчёт за {month_name}*\n", f"Привет, {name}!\n"]
            for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
                pct = amt / grand * 100 if grand > 0 else 0
                lines.append(f"{cat}: *{amt:,.0f} ₽* ({pct:.1f}%)")
            lines.append(f"\n💸 *Итого: {grand:,.2f} ₽*")

            await app.bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="Markdown")

            pie_buf = await asyncio.to_thread(_pie_chart, totals, f"Расходы — {month_name}")
            if pie_buf:
                await app.bot.send_photo(chat_id=uid, photo=pie_buf,
                                         caption=f"📊 Структура расходов за {month_name}")
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("Monthly report error for %s: %s", uid, e)


async def _send_weekly_digests(app: Application):
    today    = date.today()
    start_dt = today - timedelta(days=6)
    logger.info("Sending weekly digests")

    users = await db("SELECT user_id, first_name FROM users", fetch="all")
    for row in users or []:
        uid  = row["user_id"]
        name = row["first_name"] or "друг"
        try:
            exp_rows = await db(
                "SELECT category, SUM(amount) AS total FROM expenses "
                "WHERE user_id=%s AND expense_date BETWEEN %s AND %s "
                "GROUP BY category ORDER BY total DESC",
                (uid, start_dt, today), fetch="all",
            )
            if not exp_rows:
                continue

            grand = sum(float(r["total"]) for r in exp_rows)
            lines = [
                f"📋 *Дайджест за неделю*\n"
                f"({start_dt.strftime('%d.%m')} – {today.strftime('%d.%m')})\n",
                f"Привет, {name}!\n",
            ]
            for r in exp_rows[:5]:
                pct = float(r["total"]) / grand * 100 if grand > 0 else 0
                lines.append(f"{r['category']}: *{float(r['total']):,.0f} ₽* ({pct:.0f}%)")
            if len(exp_rows) > 5:
                lines.append("…и другие категории")
            lines.append(f"\n💸 *Итого за неделю: {grand:,.2f} ₽*")
            lines.append(f"📅 Среднедневные: *{grand / 7:,.0f} ₽*")

            await app.bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("Weekly digest error for %s: %s", uid, e)


# ── Global error handler ───────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)


# ── Menu router ────────────────────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💸 Добавить расход":
        return await ask_expense_amount(update, context)
    if text == "📊 Статистика":
        await show_stats_menu(update, context)
    elif text == "📈 Отчёт с графиком":
        await show_report(update, context)
    elif text == "💳 История":
        await show_history(update, context)
    elif text == "🎯 Лимиты":
        return await ask_budget_category(update, context)
    elif text == "📤 Экспорт CSV":
        await export_csv(update, context)
    elif text == "👥 Пригласить":
        await invite(update, context)
    return ConversationHandler.END


# ── Application lifecycle ──────────────────────────────────────────────────────

async def _post_init(app: Application):
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_send_monthly_reports, CronTrigger(day=1, hour=9, minute=0), args=[app])
    scheduler.add_job(_send_weekly_digests,  CronTrigger(day_of_week="mon", hour=9, minute=0), args=[app])
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Bot ready.")


async def _post_shutdown(app: Application):
    s = app.bot_data.get("scheduler")
    if s:
        s.shutdown(wait=False)
    if _pool:
        await _pool.close()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TOKEN не найден в переменных окружения!")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в переменных окружения!")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    menu_pattern = (
        r"^(💸 Добавить расход|📊 Статистика|📈 Отчёт с графиком|"
        r"💳 История|🎯 Лимиты|📤 Экспорт CSV|👥 Пригласить)$"
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(menu_pattern), handle_menu),
        ],
        states={
            EXP_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_amount)],
            EXP_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_category)],
            EXP_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expense_note)],
            BUD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_budget_category)],
            BUD_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_budget_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(stats_callback,          pattern=r"^period_"))
    app.add_handler(CallbackQueryHandler(delete_expense_callback, pattern=r"^del_\d+$"))
    app.add_handler(MessageHandler(
        filters.Regex(r"^\d+[.,]?\d*\s+\S") & ~filters.COMMAND,
        quick_add,
    ))
    app.add_handler(CommandHandler("reset",   reset))
    app.add_handler(CommandHandler("invite",  invite))
    app.add_handler(CommandHandler("report",  show_report))
    app.add_handler(CommandHandler("export",  export_csv))
    app.add_handler(CommandHandler("history", show_history))
    app.add_error_handler(error_handler)

    print("🤖 Finance Bot v5.1 запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
