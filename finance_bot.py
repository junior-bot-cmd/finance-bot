#!/usr/bin/env python3
"""Finance Bot v6.1 — psycopg (v3) for Python 3.13 compatibility"""

import os, io, csv, re, logging, asyncio
from datetime import date, timedelta
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
matplotlib.rcParams["font.family"] = "DejaVu Sans"

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

# ── Config ─────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN        = os.environ.get("TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "financereciepe_bot")
DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
REFERRAL_BONUS = 100.0

AMOUNT, CATEGORY, NOTE, BUD_CAT, BUD_AMT = range(5)

BACK = "⬅️ Назад"

CATS = [
    "🚌 Транспорт", "🚕 Такси", "🍔 Еда", "👗 Одежда",
    "🏠 Жильё и ЖКХ", "💊 Здоровье", "🎬 Развлечения",
    "📱 Связь", "🛒 Продукты", "📦 Другое",
]

KEYWORDS = {
    "такси": "🚕 Такси",        "uber": "🚕 Такси",
    "убер": "🚕 Такси",         "яндекс": "🚕 Такси",
    "транспорт": "🚌 Транспорт", "метро": "🚌 Транспорт",
    "автобус": "🚌 Транспорт",  "маршрутка": "🚌 Транспорт",
    "трамвай": "🚌 Транспорт",
    "еда": "🍔 Еда",            "кафе": "🍔 Еда",
    "ресторан": "🍔 Еда",       "обед": "🍔 Еда",
    "ужин": "🍔 Еда",           "завтрак": "🍔 Еда",
    "кофе": "🍔 Еда",           "перекус": "🍔 Еда",
    "одежда": "👗 Одежда",      "обувь": "👗 Одежда",
    "жкх": "🏠 Жильё и ЖКХ",  "аренда": "🏠 Жильё и ЖКХ",
    "коммуналка": "🏠 Жильё и ЖКХ", "квартира": "🏠 Жильё и ЖКХ",
    "аптека": "💊 Здоровье",    "врач": "💊 Здоровье",
    "лекарство": "💊 Здоровье", "больница": "💊 Здоровье",
    "кино": "🎬 Развлечения",   "театр": "🎬 Развлечения",
    "концерт": "🎬 Развлечения",
    "связь": "📱 Связь",        "интернет": "📱 Связь",
    "телефон": "📱 Связь",
    "продукты": "🛒 Продукты",  "магазин": "🛒 Продукты",
    "пятёрочка": "🛒 Продукты", "пятерочка": "🛒 Продукты",
    "магнит": "🛒 Продукты",    "ашан": "🛒 Продукты",
    "лента": "🛒 Продукты",     "вкусвилл": "🛒 Продукты",
}

WEEKDAYS = ["Воскресенье", "Понедельник", "Вторник", "Среда",
            "Четверг", "Пятница", "Суббота"]

# ── Database (psycopg v3) ──────────────────────────────────────────────────────

def _execute(query: str, params: tuple = (), fetch: str = None):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
    return None


async def db(query: str, params: tuple = (), fetch: str = None):
    return await asyncio.to_thread(_execute, query, params, fetch)


def setup_db():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     BIGINT PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    referred_by BIGINT,
                    bonus       REAL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
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
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_exp ON expenses(user_id, expense_date)"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS budgets (
                    user_id       BIGINT NOT NULL,
                    category      TEXT NOT NULL,
                    monthly_limit REAL NOT NULL,
                    PRIMARY KEY (user_id, category)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    referrer_id BIGINT NOT NULL,
                    referred_id BIGINT NOT NULL,
                    PRIMARY KEY (referrer_id, referred_id)
                )
            """)
    logger.info("Database ready.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _label(cat: str) -> str:
    parts = cat.split(" ", 1)
    return parts[1] if len(parts) > 1 else cat


def _period(key: str):
    today = date.today()
    if key == "month":
        return today.replace(day=1), today
    if key == "prev":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if key == "week":
        return today - timedelta(days=6), today
    return date(2000, 1, 1), today


async def _upsert_user(user):
    await db(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (%s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE
           SET username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
        (user.id, user.username, user.first_name),
    )


async def _budget_alert(user_id: int, category: str) -> str:
    row = await db(
        "SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
        (user_id, category), fetch="one",
    )
    if not row:
        return ""
    limit = row["monthly_limit"]
    sp = await db(
        "SELECT COALESCE(SUM(amount),0) AS s FROM expenses "
        "WHERE user_id=%s AND category=%s AND expense_date >= %s",
        (user_id, category, date.today().replace(day=1)), fetch="one",
    )
    spent = sp["s"] if sp else 0
    pct = spent / limit * 100 if limit else 0
    if pct >= 100:
        return f"\n\n🔴 Лимит «{_label(category)}» *превышён!* ({spent:,.0f}/{limit:,.0f} ₽)"
    if pct >= 80:
        return f"\n\n🟡 По «{_label(category)}» потрачено *{pct:.0f}%* лимита"
    return ""


# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_main():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💸 Добавить расход")],
        [KeyboardButton("📊 Статистика"),  KeyboardButton("📈 Отчёт")],
        [KeyboardButton("💳 История"),     KeyboardButton("🎯 Лимиты")],
        [KeyboardButton("📤 Экспорт CSV"), KeyboardButton("👥 Пригласить")],
        [KeyboardButton("💰 Баланс")],
    ], resize_keyboard=True)


def kb_cats():
    rows = [CATS[i:i+2] for i in range(0, len(CATS), 2)]
    rows.append([BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def kb_cancel():
    return ReplyKeyboardMarkup([[BACK]], resize_keyboard=True)


def kb_period():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Этот месяц",    callback_data="p_month"),
        InlineKeyboardButton("Прошлый месяц", callback_data="p_prev"),
    ], [
        InlineKeyboardButton("7 дней",        callback_data="p_week"),
        InlineKeyboardButton("Всё время",     callback_data="p_all"),
    ]])


# ── Charts ─────────────────────────────────────────────────────────────────────

def _make_pie(totals: dict, title: str) -> io.BytesIO:
    labels = [_label(k) for k in totals]
    values = list(totals.values())
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.pie(values, labels=labels, autopct="%1.0f%%", startangle=90, pctdistance=0.8)
    ax.set_title(title, fontsize=12, fontweight="bold")
    legend = [f"{labels[i]}: {values[i]:,.0f} ₽" for i in range(len(labels))]
    ax.legend(legend, loc="lower center", bbox_to_anchor=(0.5, -0.2), ncol=2, fontsize=7)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=130, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


def _make_bar(daily: dict, title: str) -> io.BytesIO:
    days = sorted(daily.keys())
    vals = [daily[d] for d in days]
    mx   = max(vals)
    fig, ax = plt.subplots(figsize=(9, 3))
    colors = ["#E74C3C" if v == mx else "#4A90D9" for v in vals]
    ax.bar(range(len(days)), vals, color=colors, alpha=0.85)
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d.strftime("%d") for d in days], fontsize=7)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel("₽")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=130, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _upsert_user(user)
    extra = ""
    if context.args:
        try:
            ref_id = int(context.args[0])
        except ValueError:
            ref_id = None
        if ref_id and ref_id != user.id:
            me = await db("SELECT referred_by FROM users WHERE user_id=%s", (user.id,), fetch="one")
            if me and me["referred_by"] is None:
                await db("UPDATE users SET referred_by=%s WHERE user_id=%s", (ref_id, user.id))
                dup = await db(
                    "SELECT 1 FROM referrals WHERE referrer_id=%s AND referred_id=%s",
                    (ref_id, user.id), fetch="one",
                )
                if not dup:
                    await db("INSERT INTO referrals(referrer_id,referred_id) VALUES(%s,%s)", (ref_id, user.id))
                    await db("UPDATE users SET bonus=bonus+%s WHERE user_id=%s", (REFERRAL_BONUS, ref_id))
                    try:
                        cnt = await db(
                            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s",
                            (ref_id,), fetch="one",
                        )
                        await context.bot.send_message(
                            chat_id=ref_id,
                            text=f"🎉 Новый пользователь по твоей ссылке!\n"
                                 f"*+{REFERRAL_BONUS:.0f} ₽* бонусов. Всего: {cnt['c']} чел.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    extra = "\n\n🤝 Тебя пригласил друг!"
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!{extra}\n\n"
        "💸 Помогу понять, *на что уходят деньги*.\n\n"
        "⚡ *Быстрый ввод:* `500 такси` или `1200 продукты`\n\n"
        "Или используй кнопки:",
        parse_mode="Markdown", reply_markup=kb_main(),
    )


# ── Expense dialog ─────────────────────────────────────────────────────────────

async def dlg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💸 Введи сумму:", reply_markup=kb_cancel())
    return AMOUNT


async def dlg_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == BACK:
        return await cmd_cancel(update, context)
    txt = update.message.text.strip().replace(",", ".")
    try:
        amt = float(txt)
        assert amt > 0
    except Exception:
        await update.message.reply_text("❌ Введи число больше нуля:")
        return AMOUNT
    context.user_data["amt"] = amt
    await update.message.reply_text(
        f"Сумма: *{amt:,.2f} ₽*\n\nВыбери категорию:",
        parse_mode="Markdown", reply_markup=kb_cats(),
    )
    return CATEGORY


async def dlg_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if cat == BACK:
        return await cmd_cancel(update, context)
    if cat not in CATS:
        await update.message.reply_text("❌ Выбери из списка:", reply_markup=kb_cats())
        return CATEGORY
    context.user_data["cat"] = cat
    if cat == "📦 Другое":
        await update.message.reply_text(
            "✏️ Напиши описание (например: *подарок*):",
            parse_mode="Markdown", reply_markup=kb_cancel(),
        )
        return NOTE
    return await _write_expense(update, context, note=None)


async def dlg_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    if note == BACK:
        return await cmd_cancel(update, context)
    if not note:
        await update.message.reply_text("❌ Напиши описание:")
        return NOTE
    return await _write_expense(update, context, note=note)


async def _write_expense(update, context, note, amount=None, category=None):
    uid = update.effective_user.id
    amt = amount   if amount   is not None else context.user_data.get("amt", 0)
    cat = category if category is not None else context.user_data.get("cat", "")
    await db(
        "INSERT INTO expenses(user_id,amount,category,note,expense_date) VALUES(%s,%s,%s,%s,%s)",
        (uid, amt, cat, note, date.today()),
    )
    alert = await _budget_alert(uid, cat)
    note_txt = f"\n📝 {note}" if note else ""
    await update.message.reply_text(
        f"✅ *{amt:,.2f} ₽* — {cat}{note_txt}{alert}",
        parse_mode="Markdown", reply_markup=kb_main(),
    )
    return ConversationHandler.END


# ── Quick add ──────────────────────────────────────────────────────────────────

_RE = re.compile(r"^(\d+[.,]?\d*)\s+(.+)$")


async def quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = _RE.match(update.message.text.strip())
    if not m:
        return
    try:
        amt = float(m.group(1).replace(",", "."))
        assert amt > 0
    except Exception:
        return

    kw  = m.group(2).strip().lower()
    cat = None
    tail = ""
    for key, val in KEYWORDS.items():
        if kw.startswith(key):
            cat  = val
            tail = kw[len(key):].strip()
            break
        if key in kw:
            cat  = val
            tail = kw.replace(key, "", 1).strip()
            break
    if cat is None:
        cat  = "📦 Другое"
        tail = m.group(2).strip()

    await _upsert_user(update.effective_user)
    await _write_expense(update, context, note=tail or None, amount=amt, category=cat)


# ── Statistics ─────────────────────────────────────────────────────────────────

_PERIOD_NAMES = {
    "month": "текущий месяц", "prev": "прошлый месяц",
    "week":  "7 дней",        "all":  "всё время",
}


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Статистика* — выбери период:",
        parse_mode="Markdown", reply_markup=kb_period(),
    )


async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key  = q.data[2:]
    uid  = q.from_user.id
    s, e = _period(key)
    rows = await db(
        "SELECT category, SUM(amount) AS t FROM expenses "
        "WHERE user_id=%s AND expense_date BETWEEN %s AND %s "
        "GROUP BY category ORDER BY t DESC",
        (uid, s, e), fetch="all",
    )
    total = sum(r["t"] for r in rows) if rows else 0
    label = _PERIOD_NAMES.get(key, key)
    lines = [f"📊 *Статистика за {label}:*\n"]
    if not rows:
        lines.append("Расходов нет.")
    else:
        for r in rows:
            pct = r["t"] / total * 100 if total else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"{r['category']}\n`{bar}` {pct:.1f}%\n{r['t']:,.0f} ₽\n")
        lines += ["━━━━━━━━━━━━", f"💸 *Итого: {total:,.0f} ₽*"]
    await q.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ── Report ─────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    s    = date.today().replace(day=1)
    name = date.today().strftime("%B %Y")
    await update.message.reply_text("📈 Готовлю отчёт…", reply_markup=kb_main())

    by_cat = await db(
        "SELECT category, SUM(amount) AS t FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY category ORDER BY t DESC",
        (uid, s), fetch="all",
    )
    if not by_cat:
        await update.message.reply_text("За текущий месяц расходов нет.")
        return

    daily = await db(
        "SELECT expense_date, SUM(amount) AS t FROM expenses "
        "WHERE user_id=%s AND expense_date >= %s GROUP BY expense_date ORDER BY expense_date",
        (uid, s), fetch="all",
    )
    avg_row = await db(
        "SELECT AVG(day_t) AS a FROM "
        "(SELECT expense_date, SUM(amount) AS day_t FROM expenses "
        " WHERE user_id=%s AND expense_date >= %s GROUP BY expense_date) x",
        (uid, s), fetch="one",
    )
    wd_row = await db(
        "SELECT EXTRACT(DOW FROM expense_date)::int AS wd, SUM(amount) AS t "
        "FROM expenses WHERE user_id=%s AND expense_date >= %s "
        "GROUP BY wd ORDER BY t DESC LIMIT 1",
        (uid, s), fetch="one",
    )
    lm_end = s - timedelta(days=1)
    lm_row = await db(
        "SELECT COALESCE(SUM(amount),0) AS t FROM expenses "
        "WHERE user_id=%s AND expense_date BETWEEN %s AND %s",
        (uid, lm_end.replace(day=1), lm_end), fetch="one",
    )

    totals   = {r["category"]: r["t"] for r in by_cat}
    grand    = sum(totals.values())
    avg_day  = avg_row["a"] if avg_row and avg_row["a"] else 0
    top_wd   = WEEKDAYS[int(wd_row["wd"])] if wd_row else "—"
    lm_total = lm_row["t"] if lm_row else 0

    lines = [f"📈 *Отчёт за {name}*\n"]
    for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{cat}: *{amt:,.0f} ₽* ({amt/grand*100:.1f}%)")
    lines.append(f"\n💸 *Итого: {grand:,.0f} ₽*")
    lines.append(f"📅 В среднем в день: *{avg_day:,.0f} ₽*")
    lines.append(f"📆 Самый затратный день: *{top_wd}*")
    if lm_total:
        d = grand - lm_total
        lines.append(f"{'📈' if d>0 else '📉'} vs прошлый месяц: {'+' if d>=0 else ''}{d:,.0f} ₽")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    pie = await asyncio.to_thread(_make_pie, totals, f"Расходы — {name}")
    await update.message.reply_photo(pie, caption="🍕 По категориям")
    daily_dict = {r["expense_date"]: r["t"] for r in daily}
    if daily_dict:
        bar = await asyncio.to_thread(_make_bar, daily_dict, f"По дням — {name}")
        await update.message.reply_photo(bar, caption="📊 По дням")


# ── History ────────────────────────────────────────────────────────────────────

def _history_msg(rows):
    lines   = ["💳 *Последние 10 расходов:*\n"]
    buttons = []
    for r in rows:
        day  = r["expense_date"].strftime("%d.%m")
        note = f" {r['note']}" if r["note"] else ""
        lines.append(f"{day} | {r['category']}{note}: *{r['amount']:,.0f} ₽*")
        buttons.append([InlineKeyboardButton(
            f"🗑 {day} {_label(r['category'])} {r['amount']:,.0f}₽",
            callback_data=f"del_{r['id']}",
        )])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    rows = await db(
        "SELECT id,expense_date,category,amount,note FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC, id DESC LIMIT 10",
        (uid,), fetch="all",
    )
    if not rows:
        await update.message.reply_text("💳 История пуста.", reply_markup=kb_main())
        return
    text, markup = _history_msg(rows)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def cb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    eid = int(q.data.split("_")[1])
    row = await db("SELECT id FROM expenses WHERE id=%s AND user_id=%s", (eid, uid), fetch="one")
    if not row:
        await q.answer("Запись не найдена.", show_alert=True)
        return
    await db("DELETE FROM expenses WHERE id=%s", (eid,))
    rows = await db(
        "SELECT id,expense_date,category,amount,note FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC, id DESC LIMIT 10",
        (uid,), fetch="all",
    )
    if not rows:
        await q.edit_message_text("💳 История пуста.")
        return
    text, markup = _history_msg(rows)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)


# ── Budget limits ──────────────────────────────────────────────────────────────

async def dlg_bud_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    s    = date.today().replace(day=1)
    rows = await db("SELECT category,monthly_limit FROM budgets WHERE user_id=%s", (uid,), fetch="all")
    lines = ["🎯 *Мои лимиты:*\n"]
    if not rows:
        lines.append("Лимиты не установлены.\n")
    else:
        for b in rows:
            cat, limit = b["category"], b["monthly_limit"]
            sp = await db(
                "SELECT COALESCE(SUM(amount),0) AS s FROM expenses "
                "WHERE user_id=%s AND category=%s AND expense_date >= %s",
                (uid, cat, s), fetch="one",
            )
            spent  = sp["s"] if sp else 0
            pct    = min(spent / limit * 100, 100) if limit else 0
            filled = int(pct / 10)
            if pct >= 100:
                bar, icon = "🟥"*filled + "⬜"*(10-filled), "🔴"
            elif pct >= 80:
                bar, icon = "🟨"*filled + "⬜"*(10-filled), "🟡"
            else:
                bar, icon = "🟩"*filled + "⬜"*(10-filled), "🟢"
            lines.append(f"{icon} {cat}\n{bar} {pct:.0f}%\n{spent:,.0f}/{limit:,.0f} ₽\n")
    lines.append("Выбери категорию для лимита:")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_cats())
    return BUD_CAT


async def dlg_bud_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if cat == BACK:
        return await cmd_cancel(update, context)
    if cat not in CATS:
        await update.message.reply_text("❌ Выбери из списка:", reply_markup=kb_cats())
        return BUD_CAT
    context.user_data["bud_cat"] = cat
    uid = update.effective_user.id
    ex  = await db("SELECT monthly_limit FROM budgets WHERE user_id=%s AND category=%s",
                   (uid, cat), fetch="one")
    cur = f"\nТекущий лимит: *{ex['monthly_limit']:,.0f} ₽*" if ex else ""
    await update.message.reply_text(
        f"{cat}{cur}\n\nВведи лимит (₽/месяц):",
        parse_mode="Markdown", reply_markup=kb_cancel(),
    )
    return BUD_AMT


async def dlg_bud_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == BACK:
        return await cmd_cancel(update, context)
    txt = update.message.text.strip().replace(",", ".")
    try:
        amt = float(txt)
        assert amt > 0
    except Exception:
        await update.message.reply_text("❌ Введи число больше нуля:")
        return BUD_AMT
    uid = update.effective_user.id
    cat = context.user_data.get("bud_cat", "")
    await db(
        "INSERT INTO budgets(user_id,category,monthly_limit) VALUES(%s,%s,%s) "
        "ON CONFLICT(user_id,category) DO UPDATE SET monthly_limit=EXCLUDED.monthly_limit",
        (uid, cat, amt),
    )
    await update.message.reply_text(
        f"✅ {cat}: *{amt:,.0f} ₽/месяц*",
        parse_mode="Markdown", reply_markup=kb_main(),
    )
    return ConversationHandler.END


# ── Export ─────────────────────────────────────────────────────────────────────

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    rows = await db(
        "SELECT expense_date,category,note,amount FROM expenses "
        "WHERE user_id=%s ORDER BY expense_date DESC",
        (uid,), fetch="all",
    )
    if not rows:
        await update.message.reply_text("Нет данных.", reply_markup=kb_main())
        return
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["Дата", "Категория", "Описание", "Сумма"])
    for r in rows:
        w.writerow([r["expense_date"], r["category"], r["note"] or "", f"{r['amount']:.2f}"])
    await update.message.reply_document(
        document=buf.getvalue().encode("utf-8-sig"),
        filename=f"expenses_{date.today()}.csv",
        caption="📤 Твои расходы",
    )


# ── Invite ─────────────────────────────────────────────────────────────────────

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await _upsert_user(update.effective_user)
    cnt = await db("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s", (uid,), fetch="one")
    usr = await db("SELECT bonus FROM users WHERE user_id=%s", (uid,), fetch="one")
    invited = cnt["c"] if cnt else 0
    bonus   = usr["bonus"] if usr and usr["bonus"] else 0

    link       = f"https://t.me/{BOT_USERNAME}?start={uid}"
    share_text = "Веду учёт расходов в этом боте — присоединяйся!"
    share_url  = f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Скопировать ссылку", copy_text=CopyTextButton(text=link))],
        [InlineKeyboardButton("📤 Поделиться с другом", url=share_url)],
    ])
    await update.message.reply_text(
        f"👥 *Пригласи друга!*\n\n"
        f"За каждого друга — *+{REFERRAL_BONUS:.0f} ₽* бонусов!\n\n"
        f"Твоя ссылка:\n`{link}`\n\n"
        f"Приглашено: *{invited}* чел. · Баланс: *{bonus:,.0f} ₽*",
        parse_mode="Markdown", reply_markup=kb,
    )


# ── Balance ────────────────────────────────────────────────────────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await _upsert_user(update.effective_user)
    usr = await db("SELECT bonus FROM users WHERE user_id=%s", (uid,), fetch="one")
    cnt = await db("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s", (uid,), fetch="one")
    bonus   = usr["bonus"] if usr and usr["bonus"] else 0
    invited = cnt["c"] if cnt else 0
    await update.message.reply_text(
        f"💰 *Твой баланс: {bonus:,.0f} ₽*\n\n"
        f"Пополняется за приглашённых друзей — *+{REFERRAL_BONUS:.0f} ₽* за каждого.\n"
        f"Приглашено: *{invited}* чел.\n\n"
        f"Нажми «👥 Пригласить», чтобы получить свою ссылку.",
        parse_mode="Markdown", reply_markup=kb_main(),
    )


# ── Reset ──────────────────────────────────────────────────────────────────────

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db("DELETE FROM expenses WHERE user_id=%s", (update.effective_user.id,))
    await update.message.reply_text("🗑 Все расходы удалены.", reply_markup=kb_main())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔙 Главное меню", reply_markup=kb_main())
    return ConversationHandler.END


# ── Scheduled reports ──────────────────────────────────────────────────────────

async def _monthly(app: Application):
    today = date.today()
    e = today.replace(day=1) - timedelta(days=1)
    s = e.replace(day=1)
    name = s.strftime("%B %Y")
    users = await db("SELECT user_id, first_name FROM users", fetch="all")
    for u in users or []:
        uid  = u["user_id"]
        rows = await db(
            "SELECT category, SUM(amount) AS t FROM expenses "
            "WHERE user_id=%s AND expense_date BETWEEN %s AND %s GROUP BY category",
            (uid, s, e), fetch="all",
        )
        if not rows:
            continue
        totals = {r["category"]: r["t"] for r in rows}
        grand  = sum(totals.values())
        lines  = [f"📅 *Отчёт за {name}*\nПривет, {u['first_name'] or 'друг'}!\n"]
        for cat, amt in sorted(totals.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"{cat}: *{amt:,.0f} ₽* ({amt/grand*100:.1f}%)")
        lines.append(f"\n💸 *Итого: {grand:,.0f} ₽*")
        try:
            await app.bot.send_message(uid, "\n".join(lines), parse_mode="Markdown")
            pie = await asyncio.to_thread(_make_pie, totals, f"Расходы — {name}")
            await app.bot.send_photo(uid, pie)
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("monthly report %s: %s", uid, e)


async def _weekly(app: Application):
    today = date.today()
    s = today - timedelta(days=6)
    users = await db("SELECT user_id, first_name FROM users", fetch="all")
    for u in users or []:
        uid  = u["user_id"]
        rows = await db(
            "SELECT category, SUM(amount) AS t FROM expenses "
            "WHERE user_id=%s AND expense_date BETWEEN %s AND %s "
            "GROUP BY category ORDER BY t DESC",
            (uid, s, today), fetch="all",
        )
        if not rows:
            continue
        grand = sum(r["t"] for r in rows)
        lines = [f"📋 *Дайджест* ({s.strftime('%d.%m')}–{today.strftime('%d.%m')})\n"
                 f"Привет, {u['first_name'] or 'друг'}!\n"]
        for r in rows[:5]:
            lines.append(f"{r['category']}: *{r['t']:,.0f} ₽*")
        lines.append(f"\n💸 *{grand:,.0f} ₽* · avg *{grand/7:,.0f} ₽*/день")
        try:
            await app.bot.send_message(uid, "\n".join(lines), parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("weekly digest %s: %s", uid, e)


# ── Error handler ──────────────────────────────────────────────────────────────

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)


# ── Menu dispatcher ────────────────────────────────────────────────────────────

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "💸 Добавить расход": return await dlg_start(update, context)
    if txt == "📊 Статистика":      await cmd_stats(update, context)
    elif txt == "📈 Отчёт":         await cmd_report(update, context)
    elif txt == "💳 История":        await cmd_history(update, context)
    elif txt == "🎯 Лимиты":         return await dlg_bud_start(update, context)
    elif txt == "📤 Экспорт CSV":    await cmd_export(update, context)
    elif txt == "👥 Пригласить":     await cmd_invite(update, context)
    elif txt == "💰 Баланс":         await cmd_balance(update, context)
    return ConversationHandler.END


# ── Lifecycle ──────────────────────────────────────────────────────────────────

async def on_start(app: Application):
    sched = AsyncIOScheduler()
    sched.add_job(_monthly, CronTrigger(day=1,             hour=9), args=[app])
    sched.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=9), args=[app])
    sched.start()
    app.bot_data["sched"] = sched


async def on_stop(app: Application):
    s = app.bot_data.get("sched")
    if s:
        s.shutdown(wait=False)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise SystemExit("TOKEN not set")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")

    setup_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_start)
        .post_shutdown(on_stop)
        .build()
    )

    MENU_RE = (
        r"^(💸 Добавить расход|📊 Статистика|📈 Отчёт|"
        r"💳 История|🎯 Лимиты|📤 Экспорт CSV|👥 Пригласить|💰 Баланс)$"
    )
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Regex(MENU_RE), menu),
        ],
        states={
            AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, dlg_amount)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, dlg_category)],
            NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, dlg_note)],
            BUD_CAT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, dlg_bud_cat)],
            BUD_AMT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, dlg_bud_amt)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_stats,  pattern=r"^p_"))
    app.add_handler(CallbackQueryHandler(cb_delete, pattern=r"^del_\d+$"))
    app.add_handler(MessageHandler(
        filters.Regex(r"^\d+[.,]?\d*\s+\S") & ~filters.COMMAND, quick_add,
    ))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_error_handler(on_error)

    logger.info("Finance Bot v6.1 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
