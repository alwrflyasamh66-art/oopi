# -*- coding: utf-8 -*-
import re
from urllib.parse import urlparse
from pyrogram.errors import *
from pyrogram import Client, filters, errors, enums
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import Message, User, InlineKeyboardMarkup as PyroInlineKeyboardMarkup, InlineKeyboardButton as PyroInlineKeyboardButton
from aiogram import Bot as AiogramBot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, CopyTextButton, MessageEntity
from aiogram.client.default import DefaultBotProperties
import json, math
from datetime import datetime, timedelta, timezone
import secrets
import time
import hashlib
import base64
from cryptography.fernet import Fernet
from functools import partial
import random, asyncio
import os, sqlite3
import aiosqlite
import ijson
from pathlib import Path
from html import escape
from pyrogram import idle

PROGRESS_FILE = os.path.join("database", "operation_progress.json")
PENDING_OPERATIONS = {}
SESSION_COPY_CACHE = {}
ACCOUNT_LINK_CACHE = {}
EMERGENCY_STOP = False
ENCRYPTED_BACKUP_FILE = os.path.join("database", "accounts_backup.enc")
ACCOUNT_LINK_TTL = 300
USER_MENU_STATE = {}
FILE_SELECTIONS = {}
ACCOUNT_RUNTIME_FILE = os.path.join("database", "account_runtime.json")
ACCOUNT_COOLDOWN_SECONDS = 30
SUBSCRIPTION_FILE = os.path.join("database", "subscription_expiry.json")
LIBYA_TZ = timezone(timedelta(hours=2))
SUBSCRIPTION_ALERT_OFFSETS = (timedelta(days=2), timedelta(days=1), timedelta(hours=12), timedelta(hours=6), timedelta(hours=4), timedelta(hours=2), timedelta(hours=1))
SUBSCRIPTION_DEV_CONTACT = "@qv_x1"

def load_account_runtime():
    try:
        with open(ACCOUNT_RUNTIME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_account_runtime(data):
    tmp = ACCOUNT_RUNTIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNT_RUNTIME_FILE)

def _account_state(label):
    data = load_account_runtime()
    state = data.setdefault(str(label), {"score": 100, "last_used": 0, "cooldown_until": 0, "successes": 0, "failures": 0})
    return data, state

def mark_account_success(label):
    data, state = _account_state(label)
    state["score"] = min(100, int(state.get("score", 100)) + 1)
    state["last_used"] = time.time()
    state["successes"] = int(state.get("successes", 0)) + 1
    save_account_runtime(data)

def mark_account_failure(label, reason="temporary"):
    data, state = _account_state(label)
    state["score"] = max(0, int(state.get("score", 100)) - (10 if reason in {"flood", "restricted", "blocked"} else 3))
    state["last_used"] = time.time()
    state["failures"] = int(state.get("failures", 0)) + 1
    state["cooldown_until"] = time.time() + ACCOUNT_COOLDOWN_SECONDS
    save_account_runtime(data)

def rank_available_accounts(accounts, blocked_accounts=None):
    blocked_accounts = blocked_accounts or set()
    now = time.time()
    runtime = load_account_runtime()
    ranked = []
    for account in accounts:
        label = str(account[1])
        state = runtime.get(label, {})
        if label in blocked_accounts or float(state.get("cooldown_until", 0)) > now:
            continue
        ranked.append((int(state.get("score", 100)), float(state.get("last_used", 0)), account))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked]

PENDING_CONFIRMATIONS = {}
ACTIVE_ACCOUNT_LABELS = None
os.makedirs("database", exist_ok=True)

def save_operation_progress(state):
    state.setdefault("operation_id", secrets.token_hex(8))
    state.setdefault("status", "running" if state.get("active", True) else "completed")
    state.setdefault("stop_reason", None)
    state.setdefault("processed", [])
    state.setdefault("processed_count", len(state.get("processed", [])))
    state.setdefault("confirmed", int(state.get("total_added", 0) or 0))
    state.setdefault("failed", int(state.get("total_failed", 0) or 0))
    state.setdefault("source", "غير محدد")
    state.setdefault("target", "غير محدد")
    temp_file = PROGRESS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, PROGRESS_FILE)

def operation_metrics(state, fallback_total=0):
    return (
        int(state.get("confirmed", state.get("total_added", 0)) or 0),
        int(state.get("failed", state.get("total_failed", 0)) or 0),
        int(state.get("requested_count", fallback_total) or 0),
    )

def sync_operation_record(state, processed, confirmed, failed, status=None, stop_reason=None):
    state["processed"] = list(processed)
    state["processed_count"] = len(state["processed"])
    state["confirmed"] = int(confirmed)
    state["failed"] = int(failed)
    state["total_added"] = int(confirmed)
    state["total_failed"] = int(failed)
    if status is not None:
        state["status"] = status
    if stop_reason is not None:
        state["stop_reason"] = stop_reason
    save_operation_progress(state)

def load_operation_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or not state.get("active"):
            return None
        requested = int(state.get("requested_count", 0) or 0)
        processed = int(state.get("processed_count", len(state.get("processed", []))) or 0)
        if requested > 0 and processed >= requested:
            clear_operation_progress()
            return None
        return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

def clear_operation_progress():
    try:
        os.remove(PROGRESS_FILE)
    except FileNotFoundError:
        pass

def save_progress_checkpoint(state, processed, total_added, total_failed):
    state["active"] = True
    sync_operation_record(state, processed, total_added, total_failed, status="running")

class Config:
    BOT_TOKEN = "8750452306:AAHuS4PPLUVVSvZBX_k3Ox8dF32eHsLXl1E"
    APP_ID = 39978956
    API_HASH = "e7322ed176f1527600979186b4ea8da8"
    OWNER_ID = [8864493211]
    Devs = [8864493211]
    DEV_IDS = [8864493211]

CUSTOM_MESSAGE_EMOJI_IDS = {
    "⚠️":"6206077285720659346", "❌":"5830085055376006187", "✅":"5830243638453477409", "🗑️":"5904542823167824187", "📋":"5262646461698436019", "📤":"5895226403447642077", "⚡":"5233546443360318055", "🛑":"5904542823167824187", "📁":"5895226403447642077", "👤":"5467904284509085470", "👥":"5467772583631921166", "➕":"5830238823795136938", "🧹":"5904542823167824187", "🔍":"5285533062518566401", "💾":"5895226403447642077", "👁️":"5465213874045199656", "📂":"5895226403447642077", "❗️":"6206077285720659346", "📥":"5465213874045199656", "🔔":"6206508629286196237", "☁️":"5895226403447642077", "📊":"6206343625232619150", "♻️":"5830238823795136938", "🔎":"5285533062518566401", "🚪":"6206508629286196237", "🔐":"5895226403447642077", "🔗":"5895226403447642077", "🔢":"5285533062518566401", "📱":"5895226403447642077",
}

def customize_message_emojis(text):
    if not isinstance(text, str):
        return text
    saved=[]
    def hold(match):
        saved.append(match.group(0))
        return f"__EMOJI_BLOCK_{len(saved)-1}__"
    out=re.sub(r"<(?:tg-emoji|emoji)\b[^>]*>.*?</(?:tg-emoji|emoji)>", hold, text, flags=re.DOTALL)
    for glyph,eid in sorted(CUSTOM_MESSAGE_EMOJI_IDS.items(), key=lambda x:-len(x[0])):
        out=out.replace(glyph, f"<tg-emoji emoji-id='{eid}'>{glyph}</tg-emoji>")
    for i,val in enumerate(saved):
        out=out.replace(f"__EMOJI_BLOCK_{i}__",val)
    return out

SOURCE_URL = "https://t.me/a990099aa"
SOURCE_EMOJI_ID = "6037361126268738296"
SOURCE_TEXT = "سورس 𝑹𝑶𝑺𝑺𝑬"

def source_btn():
    return InlineKeyboardButton(text=SOURCE_TEXT, url=SOURCE_URL, style="primary", icon_custom_emoji_id=SOURCE_EMOJI_ID)

BACK_EMOJI_ID = "5920497720434365688"
def back_btn(data="back_previous"):
    return InlineKeyboardButton(text="رجوع", callback_data=data, style="primary", icon_custom_emoji_id=BACK_EMOJI_ID)
def main_menu_btn():
    return InlineKeyboardButton(text="القائمة الرئيسية", callback_data="main_menu", style="primary", icon_custom_emoji_id=BACK_EMOJI_ID)

def to_add_target(entry):
    if isinstance(entry, str) and entry.startswith("id:"):
        try:
            return int(entry[3:])
        except ValueError:
            return entry
    return entry

def display_target(entry):
    if isinstance(entry, str) and entry.startswith("id:"):
        return f"(آيدي: {entry[3:]})"
    return f"@{entry}"

def resolve_group_ref(raw_link: str):
    if raw_link is None:
        return raw_link, "invalid"
    text = raw_link.strip()
    if not text:
        return text, "invalid"
    if "joinchat/" in text or "/+" in text or text.startswith("+"):
        if text.startswith("+") and "t.me" not in text and "joinchat" not in text:
            return f"https://t.me/{text}", "private"
        return text, "private"
    username = text.split("/")[-1].lstrip("@").strip()
    return username, "public"

async def resolve_private_chat_id(app, group_ref: str):
    try:
        joined = await app.join_chat(group_ref)
        if getattr(joined, "id", None):
            return joined.id
    except errors.UserAlreadyParticipant:
        pass
    except Exception:
        pass
    try:
        chat = await app.get_chat(group_ref)
        if getattr(chat, "id", None):
            return chat.id
    except Exception:
        pass
    return group_ref

class database:
    def __init__(self):
        os.makedirs("database", exist_ok=True)
        self.db_path = "database/data.db"
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("CREATE TABLE IF NOT EXISTS accounts (ses TEXT, number TEXT, id TEXT)")
            conn.commit()

    def accounts(self):
        list_acc = []
        with sqlite3.connect(self.db_path, timeout=30) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM accounts")
            entry = cursor.fetchall()
            for i in entry:
                list_acc.append([i[0], i[1]])
        return list_acc

class AsyncDatabase:
    def __init__(self, db_path="database/data.db"):
        self.db_path = db_path

    async def _connect(self):
        conn = await aiosqlite.connect(self.db_path, timeout=30)
        return conn

    async def accounts(self):
        async with await self._connect() as conn:
            cursor = await conn.execute("SELECT ses, number FROM accounts")
            rows = await cursor.fetchall()
            await cursor.close()
            return [[row[0], row[1]] for row in rows]

async_db = AsyncDatabase()

def filter_operation_accounts(accounts):
    if ACTIVE_ACCOUNT_LABELS is None:
        return accounts
    allowed = {str(label) for label in ACTIVE_ACCOUNT_LABELS}
    return [account for account in accounts if str(account[1]) in allowed]

class LiveProgressReporter:
    def __init__(self, bot, owner_id, title, metrics_fn):
        self.bot = bot
        self.owner_id = owner_id
        self.title = title
        self.metrics_fn = metrics_fn
        self.running = False

    async def start(self):
        self.running = True

    async def note(self, msg):
        pass

    async def close(self):
        self.running = False

class Custem:
    async def ADDuser(self, inGRob, grop2, bot: Client, nmcount, resume_state=None):
        global STOP_ADD
        STOP_ADD = False
        total_added = int((resume_state or {}).get("total_added", 0))
        total_failed = int((resume_state or {}).get("total_failed", 0))
        processed_users = set((resume_state or {}).get("processed", []))
        total_to_add = int(nmcount)
        
        progress_state = resume_state or {
            "active": True, "title": "إضافة أعضاء ظاهرين", "source": inGRob, 
            "target": grop2, "requested_count": total_to_add, "accounts_count": 0, 
            "processed": [], "processed_count": 0, "total_added": 0, "total_failed": 0, 
            "operation": "add_users"
        }
        save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
        
        raw_accounts = filter_operation_accounts(await async_db.accounts())
        if not raw_accounts:
            return await bot.send_message(
                Config.OWNER_ID[0],
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات متاحة.",
                parse_mode=enums.ParseMode.HTML
            )

        status = "<tg-emoji emoji-id='5895465761975047998'>🛑</tg-emoji> تم الإيقاف يدوياً" if STOP_ADD else "<emoji id='5830243638453477409'>✅</emoji> انتهاء الإضافة"
        await bot.send_message(
            Config.OWNER_ID[0],
            f"سورس 𝑹𝑶𝑺𝑺𝑬 - عملية الإضافة {status}",
            parse_mode=enums.ParseMode.HTML
        )

# تهيئة البوتات
bot = Client("my_bot", api_id=Config.APP_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)
abot = AiogramBot(token=Config.BOT_TOKEN, default=DefaultBotProperties(link_preview_is_disabled=True))

if __name__ == "__main__":
    print("Bot Starting...")
    bot.run()
