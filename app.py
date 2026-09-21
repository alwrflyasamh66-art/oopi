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
import random,asyncio
import os,sqlite3
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
# أسماء الحسابات المسموح بها للعملية الحالية؛ None تعني استخدام كل الحسابات.
ACTIVE_ACCOUNT_LABELS = None
os.makedirs("database", exist_ok=True)


def save_operation_progress(state):
    """حفظ سجل العملية الموحد بصورة ذرّية حتى يبقى بعد انقطاع الخادم."""
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
    """إرجاع أرقام Live والنتيجة من سجل العملية الموحد."""
    return (
        int(state.get("confirmed", state.get("total_added", 0)) or 0),
        int(state.get("failed", state.get("total_failed", 0)) or 0),
        int(state.get("requested_count", fallback_total) or 0),
    )


def sync_operation_record(state, processed, confirmed, failed, status=None, stop_reason=None):
    """تحديث السجل الموحد الذي تعتمد عليه Live والنتيجة والاستئناف."""
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
        # العملية المكتملة لا تُعامل كعملية قابلة للاستئناف.
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


def cleanup_temp_files(max_age_seconds=86400):
    """تنظيف الملفات المؤقتة فقط، مع عدم لمس ملفات JSON الأساسية."""
    import time
    now = time.time()
    for root in ("database", "vcf_files", "."):
        if not os.path.isdir(root):
            continue
        for path in Path(root).glob("*.tmp"):
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink()
            except OSError:
                pass
    backup_path = Path("A3DoBackUp.json")
    try:
        if backup_path.exists() and now - backup_path.stat().st_mtime > max_age_seconds:
            backup_path.unlink()
    except OSError:
        pass


def progress_summary(state):
    return (
        "<tg-emoji emoji-id='6206077285720659346'><tg-emoji emoji-id='6206077285720659346'>⚠</tg-emoji>️</tg-emoji> <b>تم العثور على عملية سابقة غير مكتملة</b>\n\n"
        f"نوع العملية: {state.get('title', 'غير محدد')}\n"
        f"من: {state.get('source', 'غير محدد')}\n"
        f"إلى: {state.get('target', 'غير محدد')}\n"
        f"العدد المطلوب: {state.get('requested_count', 0)} عضو\n"
        f"تمت معالجة: {state.get('processed_count', 0)} عضو\n"
        f"الإضافات الناجحة: {state.get('total_added', 0)} عضو\n"
        f"الإخفاقات: {state.get('total_failed', 0)}\n"
        f"عدد الحسابات: {state.get('accounts_count', 0)}\n\n"
        "هل تريد استكمال العملية من آخر نقطة محفوظة؟"
    )


def save_progress_checkpoint(state, processed, total_added, total_failed):
    state["active"] = True
    sync_operation_record(state, processed, total_added, total_failed, status="running")

def load_subscription_state():
    try:
        with open(SUBSCRIPTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_subscription_state(state):
    os.makedirs("database", exist_ok=True)
    temp = SUBSCRIPTION_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp, SUBSCRIPTION_FILE)

def parse_subscription_date(value):
    return datetime.strptime(value.strip(), "%d/%m/%Y").replace(tzinfo=LIBYA_TZ)

def subscription_message(expiry, lead=None):
    expiry_text = expiry.astimezone(LIBYA_TZ).strftime("%d/%m/%Y %H:%M")
    if lead is None:
        return (f"<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> <b>تنبيه اشتراك</b>\n\nسينتهي اشتراك البوت بتاريخ "
                f"<code>{expiry_text}</code> بتوقيت ليبيا (UTC+2).\n"
                f"للتجديد، أرسل طلب اشتراك إلى المطور {SUBSCRIPTION_DEV_CONTACT}.")
    if lead.days >= 1:
        remaining = f"{lead.days} يوم" if lead.days == 1 else f"{lead.days} يومين"
    else:
        hours = int(lead.total_seconds() // 3600)
        remaining = f"{hours} ساعة"
    return (f"<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> <b>تنبيه: الاشتراك سينتهي قريباً</b>\n\n"
            f"متبقي تقريباً: <b>{remaining}</b>.\n"
            f"موعد الإيقاف: <code>{expiry_text}</code> بتوقيت ليبيا (UTC+2).\n"
            f"إذا أردت التجديد، أرسل طلب اشتراك إلى المطور {SUBSCRIPTION_DEV_CONTACT}.")

async def subscription_watchdog():
    # كل مرحلة تُرسل مرة واحدة فقط عند دخولها، ولا تُرسل التنبيهات الفائتة دفعة واحدة.
    levels = ((timedelta(days=2), "2d"), (timedelta(days=1), "1d"), (timedelta(hours=12), "12h"), (timedelta(hours=6), "6h"), (timedelta(hours=4), "4h"), (timedelta(hours=2), "2h"), (timedelta(hours=1), "1h"))
    while True:
        try:
            state = load_subscription_state()
            raw = state.get("expires_at")
            if not raw:
                await asyncio.sleep(30)
                continue
            expiry = datetime.fromisoformat(raw).astimezone(LIBYA_TZ)
            now = datetime.now(LIBYA_TZ)
            remaining = expiry - now
            if remaining <= timedelta(0):
                if state.get("last_alert") != "expired":
                    await abot.send_message(Config.OWNER_ID, subscription_message(expiry), parse_mode=enums.ParseMode.HTML)
                    state["last_alert"] = "expired"
                    save_subscription_state(state)
                print("Subscription expired; stopping bot without deleting data.")
                await bot.stop()
                return
            current_level = None
            for threshold, key in levels:
                if remaining <= threshold:
                    current_level = (threshold, key)
            # إذا بقي أكثر من يومين فلا يُرسل تنبيه بعد.
            if current_level is not None:
                threshold, key = current_level
                if state.get("last_alert") != key:
                    await abot.send_message(Config.OWNER_ID, subscription_message(expiry, threshold), parse_mode=enums.ParseMode.HTML)
                    state["last_alert"] = key
                    save_subscription_state(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Subscription watchdog failed: {type(exc).__name__}")
        await asyncio.sleep(30)

# ===== إعدادات البوت المضمنة داخل هذا الملف =====
class Config:
    # ضع توكن البوت هنا مباشرة، أو اترك قراءة متغير البيئة عند تشغيله خارجياً.
    BOT_TOKEN = "8737127935:AAFuiJOMQbEeQ2mYf3P2_5D3uKcVhW1lfLU"
    APP_ID = 39978956
    API_HASH = "e7322ed176f1527600979186b4ea8da8"
    OWNER_ID = [8864493211]
    Devs = [8864493211]
    DEV_IDS = [8864493211]
import traceback
import itertools
from pyrogram import errors as pg_errors

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

orig_send_message = Client.send_message
orig_edit_message_text = Client.edit_message_text

async def patched_send_message(self, *args, **kwargs):
    if "disable_web_page_preview" not in kwargs:
        kwargs["disable_web_page_preview"] = True
    if "text" in kwargs:
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    elif len(args) >= 2 and isinstance(args[1], str):
        args=list(args)
        args[1]=customize_message_emojis(args[1])
    return await orig_send_message(self, *args, **kwargs)
async def patched_edit_message_text(self, *args, **kwargs):
    if "disable_web_page_preview" not in kwargs:
        kwargs["disable_web_page_preview"] = True
    if "text" in kwargs:
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    elif len(args) >= 3 and isinstance(args[2], str):
        args=list(args)
        args[2]=customize_message_emojis(args[2])
    return await orig_edit_message_text(self, *args, **kwargs)


Client.send_message = patched_send_message
Client.edit_message_text = patched_edit_message_text

# Aiogram لا يمر عبر رقعة Pyrogram السابقة؛ نطبّق التحويل نفسه على الرسائل
# التي تُرسل بها إشعارات السحب وملخصات العمليات عبر Aiogram.
orig_aiogram_send_message = AiogramBot.send_message
orig_aiogram_edit_message_text = AiogramBot.edit_message_text

async def patched_aiogram_send_message(self, *args, **kwargs):
    if "text" in kwargs and isinstance(kwargs["text"], str):
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    elif len(args) >= 2 and isinstance(args[1], str):
        args = list(args)
        args[1] = customize_message_emojis(args[1])
    return await orig_aiogram_send_message(self, *args, **kwargs)

async def patched_aiogram_edit_message_text(self, *args, **kwargs):
    if "text" in kwargs and isinstance(kwargs["text"], str):
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    elif args and isinstance(args[0], str):
        args = list(args)
        args[0] = customize_message_emojis(args[0])
    return await orig_aiogram_edit_message_text(self, *args, **kwargs)

AiogramBot.send_message = patched_aiogram_send_message
AiogramBot.edit_message_text = patched_aiogram_edit_message_text
# ===== دوال مساعدة للوحة المفاتيح (Bot API 9.4) =====
SOURCE_URL = "https://t.me/a990099aa"
SOURCE_EMOJI_ID = "6037361126268738296"
SOURCE_TEXT = "سورس 𝑹𝑶𝑺𝑺𝑬"

def source_btn():
    return InlineKeyboardButton(text=SOURCE_TEXT, url=SOURCE_URL, style="primary", icon_custom_emoji_id=SOURCE_EMOJI_ID)

async def resolve_private_chat_id(app, group_ref: str):
    """
    يُعيد المعرّف الرقمي الصحيح لمجموعة خاصة (ضروري لعمليات مثل
    leave_chat / get_chat_member / add_chat_members / get_chat_history).
    يحاول ثلاث مراحل:
      1) join_chat → يعيد كائن الدردشة مع .id الصحيح مباشرةً.
      2) إذا كان الحساب عضواً مسبقاً → get_chat مع رابط الدعوة.
      3) آخر خيار → raw API (CheckChatInvite) مع تحويل المعرف.
    يعيد int (المعرف الرقمي) أو group_ref الأصلي كبديل.
    """
    # 1) محاولة الانضمام للحصول على المعرف مباشرةً
    try:
        joined = await app.join_chat(group_ref)
        if getattr(joined, "id", None):
            return joined.id
    except errors.UserAlreadyParticipant:
        pass
    except Exception:
        pass

    # 2) get_chat يدعم روابط الدعوة في بعض إصدارات pyrofork
    try:
        chat = await app.get_chat(group_ref)
        if getattr(chat, "id", None):
            return chat.id
    except Exception:
        pass

    # 3) raw API: CheckChatInvite → استخراج المعرف الرقمي الكامل
    try:
        import re as _re
        _m = _re.search(r'(?:joinchat/|\+)([A-Za-z0-9_-]+)', group_ref)
        if _m:
            from pyrogram.raw.functions.messages import CheckChatInvite
            r = await app.invoke(CheckChatInvite(hash=_m.group(1)))
            _chat_obj = getattr(r, "channel", None) or getattr(r, "chat", None)
            if _chat_obj and getattr(_chat_obj, "id", None):
                bare_id = _chat_obj.id
                # قنوات ومجموعات عملاقة: المعرف = -100{bare_id}
                if getattr(r, "channel", None):
                    return int(f"-100{bare_id}")
                # مجموعات عادية: المعرف = -{bare_id}
                return -bare_id
    except Exception:
        pass

    return group_ref  # بديل أخير
def resolve_group_ref(raw_link: str):
    """
    يحلل رابط/معرف المجموعة المُدخل من الأدمن ويرجع:
    (المرجع الصحيح لاستخدامه مع join_chat/add_chat_members..., نوع الرابط)
    نوع الرابط: "private" لو رابط دعوة خاص (t.me/+HASH أو t.me/joinchat/HASH)
                "public"  لو يوزر عام (@username أو t.me/username)
    مهم: روابط المجموعات الخاصة يجب الحفاظ عليها كاملة (لا يتم تقطيعها) لأن
    pyrofork يحتاج الرابط كامل أو الـ hash بصيغته الصحيحة للانضمام عبر ImportChatInvite.
    """
    if raw_link is None:
        return raw_link, "invalid"
    text = raw_link.strip()
    if not text:
        return text, "invalid"

    # رابط دعوة خاص بصيغة t.me/+HASH أو t.me/joinchat/HASH أو hash يبدأ بـ +
    if "joinchat/" in text or "/+" in text or text.startswith("+"):
        # نحافظ على الرابط/الهاش كامل بدون تقطيع أي جزء منه
        if text.startswith("+") and "t.me" not in text and "joinchat" not in text:
            # هاش فقط بدون رابط كامل - نعيد كتابته كرابط كامل ليتوافق مع محلل pyrofork
            return f"https://t.me/{text}", "private"
        return text, "private"

    # رابط/يوزر عام: نستخرج اليوزر فقط من آخر جزء بعد إزالة العلامات الزائدة
    username = text.split("/")[-1].lstrip("@").strip()
    return username, "public"

BACK_EMOJI_ID = "5920497720434365688"
def back_btn(data="back_previous"):
    return InlineKeyboardButton(text="رجوع", callback_data=data, style="primary", icon_custom_emoji_id=BACK_EMOJI_ID)
def main_menu_btn():
    return InlineKeyboardButton(text="القائمة الرئيسية", callback_data="main_menu", style="primary", icon_custom_emoji_id=BACK_EMOJI_ID)

def to_add_target(entry):
    """
    يحول المعرف المخزّن (يوزر كنص أو آيدي رقمي مخزّن كنص) إلى القيمة المناسبة
    لتمريرها لدوال pyrofork (add_chat_members/get_chat_member/get_common_chats).
    الآيدي الرقمي نخزنه بصيغة نصية تبدأ بـ "id:" لتمييزه بشكل واضح عن اليوزرات.
    """
    if isinstance(entry, str) and entry.startswith("id:"):
        try:
            return int(entry[3:])
        except ValueError:
            return entry
    return entry

def display_target(entry):
    """نص العرض في رسائل التقارير للأدمن"""
    if isinstance(entry, str) and entry.startswith("id:"):
        return f"(آيدي: {entry[3:]})"
    return f"@{entry}"

def is_id_target(entry):
    return isinstance(entry, str) and entry.startswith("id:")

def stop_btn():
    return InlineKeyboardButton(text="إيقاف الإضافة", callback_data="stop_add_btn", style="danger", icon_custom_emoji_id="5895465761975047998")

def _menu_button(text, callback_data, style="success", icon_id=None):
    kwargs = {"text": text, "callback_data": callback_data, "style": style}
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
    return InlineKeyboardButton(**kwargs)


def get_main_keyboard():
    """لوحة رئيسية مختصرة، ويعرض زر التأخير القيمة المحفوظة حالياً."""
    try:
        delay_min, delay_max = get_add_delay_range()
        delay_value = f"{delay_min}ث" if delay_min == delay_max else f"{delay_min}-{delay_max}ث"
        delay_label = f"ضبط التأخير ({delay_value})"
    except Exception:
        delay_label = "ضبط التأخير"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _menu_button("👤 إدارة الحسابات", "menu_accounts", icon_id="5467904284509085470"),
            _menu_button("📥 إدارة السحب", "menu_withdraw", icon_id="5465213874045199656"),
        ],
        [
            _menu_button("👥 إدارة الجهات", "menu_contacts", icon_id="5467772583631921166"),
        ],
        [
            _menu_button("🔔 إدارة الانضمام والتصفية", "menu_groups", icon_id="6206508629286196237"),
            _menu_button("☁️ إدارة التخزين والملفات", "menu_files", icon_id="5895226403447642077"),
        ],
        [
            _menu_button("📊 إدارة التقارير", "menu_reports", icon_id="6206343625232619150"),
        ],
        [
            _menu_button(delay_label, "set_delay", icon_id="5382194935057372936"),
        ],
                [_menu_button("🛑 إيقاف طارئ شامل", "emergency_stop_all", "danger", "5904542823167824187")],
        [source_btn()],
    ])
def _section_keyboard(rows):
    """يبني لوحة القسم مع رجوع فعلي للقائمة السابقة وزر مستقل للرئيسية."""
    return InlineKeyboardMarkup(inline_keyboard=rows + [[back_btn("back_main")], [main_menu_btn()], [source_btn()]])


def get_accounts_keyboard():
    return _section_keyboard([
        [_menu_button("➕ إضافة حساب", "AddAccount", icon_id="5830238823795136938"), _menu_button("🗑 حذف حساب", "RemoveAccount", "danger", "5904542823167824187")],
        [_menu_button("📋 الحسابات المسجلة", "Accounts", icon_id="5262646461698436019"), _menu_button("🧹 حذف كل الحسابات", "del_all_accounts", "danger", "5904542823167824187")],
        [_menu_button("🔍 فحص حالة الحسابات", "check_accounts", icon_id="5285533062518566401"), _menu_button("💾 نسخ احتياطي", "BackupAccounts", icon_id="5895226403447642077")],
        [_menu_button("اختبار الصحة الشامل", "full_health_check", icon_id="5285533062518566401")],
        [_menu_button("🧹 حذف الجلسات غير الصالحة", "remove_invalid_accounts_confirm", "danger", "5904542823167824187")],
        [_menu_button("📤 رفع نسخة احتياطية", "AddBackupAccounts", icon_id="5895226403447642077")],
        [_menu_button("♻️ استعادة النسخة التلقائية", "restore_encrypted_backup", icon_id="5830238823795136938"), _menu_button("🔎 بحث برقم", "search_accounts", icon_id="5285533062518566401")],
    ])


def get_withdraw_keyboard():
    return _section_keyboard([
        [_menu_button("سحب الأعضاء (ظاهر)", "addshow", icon_id="5465213874045199656"), _menu_button("سحب الأعضاء (مخفي)", "addhide", icon_id="5465213874045199656")],
        [_menu_button("📂 سحب أعضاء من الملفات", "addmem_json", icon_id="5895226403447642077")],
        [_menu_button("👥 سحب من جهات الاتصال", "contact_hire", icon_id="5467772583631921166")],
    ])


def get_contacts_keyboard():
    return _section_keyboard([
        [_menu_button("➕ إضافة جهات اتصال", "add_contacts", icon_id="5467772583631921166"), _menu_button("🗑 حذف جهات الاتصال", "clear_contacts", "danger", "5904542823167824187")],
    ])


def get_groups_keyboard():
    return _section_keyboard([
        [_menu_button("✅ انضمام للقروب", "joinGroup", icon_id="6206508629286196237"), _menu_button("🚪 مغادرة قروب", "leaveGroup", "danger", "6206508629286196237")],
    ])


def get_files_keyboard():
    return _section_keyboard([
        [_menu_button("💾 تخزين الأعضاء في JSON", "save_json", icon_id="5895226403447642077")],
        [_menu_button("📤 رفع ملف أعضاء", "add_json", icon_id="5895226403447642077"), _menu_button("🗑 حذف ملف أعضاء", "del_json", "danger", "5904542823167824187")],
        [_menu_button("📂 استخراج ملف أعضاء", "extract_json", icon_id="5895226403447642077")],
    ])


def get_reports_keyboard():
    return _section_keyboard([
        [_menu_button("🔍 فحص الحسابات والتقارير", "check_accounts", icon_id="5285533062518566401")],
    ])
# ===== نهاية الدوال المساعدة =====

#=======مجلد الجهات=======
VCF_DIR = "vcf_files"
if not os.path.exists(VCF_DIR):
    os.makedirs(VCF_DIR)

# انشاء مجلد قاعدة البيانات
DATABASE_DIR = "database"
if not os.path.exists(DATABASE_DIR):
    os.makedirs(DATABASE_DIR)
# ملف JSON لتخزين المعرفات داخل مجلد vcf_files  
bot = Client("my_bot", api_id=Config.APP_ID, api_hash=Config.API_HASH,bot_token=Config.BOT_TOKEN)
# عميل aiogram مستقل، يُستخدم فقط لإرسال/تعديل الرسائل التي تحتوي أزرار ملونة (Bot API 9.4)
# باقي منطق البوت (استقبال الرسائل، الأزرار، قاعدة البيانات...) يبقى بالكامل على pyrofork
abot = AiogramBot(
    token=Config.BOT_TOKEN,
    default=DefaultBotProperties(link_preview_is_disabled=True)
)
_orig_aiogram_send_message = abot.send_message
_orig_aiogram_edit_message_text = abot.edit_message_text
async def _custom_aiogram_send_message(*args, **kwargs):
    if "text" in kwargs:
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    return await _orig_aiogram_send_message(*args, **kwargs)
async def _custom_aiogram_edit_message_text(*args, **kwargs):
    if "text" in kwargs:
        kwargs["text"] = customize_message_emojis(kwargs["text"])
    return await _orig_aiogram_edit_message_text(*args, **kwargs)
abot.send_message = _custom_aiogram_send_message
abot.edit_message_text = _custom_aiogram_edit_message_text
STOP_ADD = False
EMERGENCY_STOP = False
  # Initialize the set to track blocked users
EXPECTING_JSON = set()

# ملف تخزين الاعضاء داخل members.json
MEMBERS_JSON = os.path.join(DATABASE_DIR, "members.json")
if not os.path.exists(MEMBERS_JSON):
    with open(MEMBERS_JSON, "w", encoding="utf-8") as f:
        json.dump({"add_members": [], "admins": [], "bots": []}, f, ensure_ascii=False, indent=4)

# ملف إعدادات البوت (مدة الانتظار بين كل إضافة وغيرها)
SETTINGS_JSON = os.path.join(DATABASE_DIR, "settings.json")
DEFAULT_ADD_DELAY_MIN = 1
ROTATE_BATCH_SIZE = 5  # عدد الإضافات المتتالية لكل حساب قبل التبديل التلقائي للحساب التالي (تناوب حقيقي)
DEFAULT_ADD_DELAY_MAX = 3
if not os.path.exists(SETTINGS_JSON):
    with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
        json.dump({"add_delay_min": DEFAULT_ADD_DELAY_MIN, "add_delay_max": DEFAULT_ADD_DELAY_MAX}, f, ensure_ascii=False, indent=4)

def load_settings():
    try:
        with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"add_delay_min": DEFAULT_ADD_DELAY_MIN, "add_delay_max": DEFAULT_ADD_DELAY_MAX}

def save_settings(data):
    with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_add_delay_range():
    """يرجع (min, max) لمدة الانتظار بين كل إضافة بالثواني، حسب ما حدده الأدمن أو القيمة الافتراضية."""
    settings = load_settings()
    mn = settings.get("add_delay_min", DEFAULT_ADD_DELAY_MIN)
    mx = settings.get("add_delay_max", DEFAULT_ADD_DELAY_MAX)
    if mn > mx:
        mn, mx = mx, mn
    return mn, mx

async def stop_aware_sleep(seconds, quantum=0.25):
    """انتظار قابل للمقاطعة عند تفعيل الإيقاف اليدوي أو الشامل."""
    deadline = asyncio.get_running_loop().time() + max(0.0, float(seconds))
    while True:
        if STOP_ADD or EMERGENCY_STOP:
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(quantum, remaining))


async def sleep_add_delay():
    """انتظار بين كل إضافة حسب المدة التي حددها الأدمن عبر الزر المخصص."""
    mn, mx = get_add_delay_range()
    return await stop_aware_sleep(random.uniform(mn, mx))

LAST_ACCOUNT_CHECK = {}    # user_id -> {"healthy": [labels], "temp_banned": [labels], "anti_spam": [labels]}

# منطق الإيقاف الموحد المستخدم من الأمر والزر
async def activate_stop_add():
    """ينفذ نفس منطق /stop_add ويوقف جميع حلقات الإضافة التي تفحص STOP_ADD."""
    global STOP_ADD
    STOP_ADD = True
    await bot.send_message(
        Config.OWNER_ID,
        '<emoji id="5895465761975047998">🛑</emoji> تم إيقاف عملية الإضافة.',
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("stop_add") & filters.user(Config.OWNER_ID))
async def stop_add_handler(client, message):
    await activate_stop_add()


async def handle_emergency_stop(call):
    await activate_stop_add()
    await call.answer("تم إيقاف عملية الإضافة", show_alert=True)

async def handle_stop_add_btn(call):
    """معالج زر إيقاف الإضافة الأحمر"""
    global STOP_ADD
    STOP_ADD = True
    await call.answer("تم إيقاف الإضافة <tg-emoji emoji-id='5904542823167824187'>🛑</tg-emoji>", show_alert=True)
    await bot.send_message(Config.OWNER_ID, "<emoji id='5895465761975047998'>🛑</emoji> تم إيقاف الإضافة عبر زر الإيقاف.",
        parse_mode=enums.ParseMode.HTML)
async def set_delay_step(client, message):
    """يستقبل قيمة المدة الجديدة بين كل إضافة ويحفظها"""
    user_id = message.from_user.id
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    
    text = message.text.strip()
    if text.startswith("/"):
        message.continue_propagation()
        return

    try:
        if "-" in text:
            mn_s, mx_s = text.split("-", 1)
            mn, mx = int(mn_s.strip()), int(mx_s.strip())
        else:
            mn = mx = int(text)
        if mn < 0 or mx < 0:
            raise ValueError
    except Exception:
        return await bot.send_message(
            Config.OWNER_ID,
            "<emoji id='5830085055376006187'>❌</emoji> صيغة غير صحيحة. أرسل رقم مثل `3` أو مدى مثل `2-5`",
        parse_mode=enums.ParseMode.HTML)

    settings = load_settings()
    settings["add_delay_min"] = mn
    settings["add_delay_max"] = mx
    save_settings(settings)

    await abot.send_message(
        Config.OWNER_ID,
        f"<emoji id='5830243638453477409'>✅</emoji> تم تحديث مدة الانتظار بين كل إضافة إلى: {mn} - {mx} ثانية",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
def is_revoked_session_error(error):
    """يتحقق من أخطاء إلغاء/إبطال جلسة تيليجرام فقط."""
    error_name = type(error).__name__.upper()
    error_text = str(error).upper()
    revoked_markers = (
        "SESSIONREVOKED", "SESSION_REVOKED",
        "AUTHKEYUNREGISTERED", "AUTH_KEY_UNREGISTERED",
        "AUTHKEYINVALID", "AUTH_KEY_INVALID",
        "SESSIONEXPIRED", "SESSION_EXPIRED",
        "USERDEACTIVATED", "USER_DEACTIVATED",
        "USERDEACTIVATEDBAN", "USER_DEACTIVATED_BAN",
    )
    return any(marker in error_name or marker in error_text for marker in revoked_markers)


async def check_account_health(session_str, label, timeout=20):
    """
    يفحص صحة حساب ويرجع (category, label, detail):
    category: "healthy" (سليم) / "temp_banned" (محظور مؤقت) / "invalid_session" (جلسة ملغاة) / "check_unconfirmed" (الفحص غير محسوم)
    """
    try:
        app = Client(":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                     session_string=session_str, no_updates=True, in_memory=True, lang_code="ar")
        async with app:
            try:
                await app.get_me()
            except Exception as e:
                if is_revoked_session_error(e):
                    return ("invalid_session", label, f"الجلسة ملغاة أو غير صالحة ({type(e).__name__}: {e})")
                return ("check_unconfirmed", label, f"تعذر فحص الجلسة ({type(e).__name__})")

            try:
                await app.send_message("SpamBot", "/start")
                await asyncio.sleep(3)
                reply_text = ""
                async for m in app.get_chat_history("SpamBot", limit=1):
                    reply_text = m.text or m.caption or ""
                    break
            except Exception as e:
                return ("check_unconfirmed", label, f"تعذر التواصل مع @SpamBot ({type(e).__name__})")

            low = reply_text.lower()
            healthy_markers = (
                "no restrictions on your account",
                "no limits are currently applied",
                "free as a bird",
                "لاتوجد قيود على",
                "أنت حر طليق!",
            )
            temp_banned_markers = (
                "automatically released on",
                "is now limited until",
                "account is now limited",
                "limited until",
                "will be automatically released",
                "your account is now limited",
                "للأسف وجد بعض مستخدمي تيليجرام",
                "تم تقييد حسابك حتى",
                "تم تقييد حسابك",
                "حسابك مقيد",
                "ستُرفع القيود عن حسابك",
                "لن يكون بإمكانك القيام ببعض الأمور",
            )

            if any(marker in low for marker in healthy_markers):
                return ("healthy", label, "سليم، لا توجد قيود")
            elif any(marker in low for marker in temp_banned_markers):
                return ("temp_banned", label, "محظور مؤقت")
            else:
                return ("check_unconfirmed", label, "رد غير معروف/يحتاج مراجعة")
    except Exception as e:
        if is_revoked_session_error(e):
            return ("invalid_session", label, f"الجلسة ملغاة أو غير صالحة ({type(e).__name__}: {e})")
        return ("check_unconfirmed", label, f"فشل تسجيل الدخول أو تعذر الاتصال ({type(e).__name__})")


def filter_operation_accounts(accounts):
    """تطبيق اختيار الحسابات للعملية الحالية بعد فحص صحتها."""
    if ACTIVE_ACCOUNT_LABELS is None:
        return accounts
    allowed = {str(label) for label in ACTIVE_ACCOUNT_LABELS}
    return [account for account in accounts if str(account[1]) in allowed]

class database:
    def __init__(self):
        os.makedirs("database", exist_ok=True)
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=NORMAL")
            cursor = connection.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS accounts (ses TEXT,number TEXT,id TEXT)")
            connection.commit()
        os.makedirs("database", exist_ok=True)
        self.db_path = "database/data.db"
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
            # existing table
            cur.execute("CREATE TABLE IF NOT EXISTS accounts (ses TEXT, number TEXT, id TEXT)")
            # new table for vcf filenames
            cur.execute("CREATE TABLE IF NOT EXISTS vcffiles (filename TEXT)")
            conn.commit()

# New: generate vcf content helper
        
    def AddAcount(self,ses,numbers,id):
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute(f"INSERT INTO accounts VALUES ('{ses}','{numbers}','{id}')")
            connection.commit()
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed: {backup_error}")
    def RemoveAllAccounts(self):
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute("DELETE FROM accounts")
            connection.commit()
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed: {backup_error}")
    def RemoveAccount(self, numbers):
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute("DELETE FROM accounts WHERE number = ?", (numbers,))
            connection.commit()
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed: {backup_error}")
    def RemoveAccountByLabel(self, label):
        """يحذف حساباً بالمعرّف الظاهر بعد التحقق من حالته."""
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute("DELETE FROM accounts WHERE number = ?", (label,))
            connection.commit()
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed: {backup_error}")
    def accounts(self):
        list = []
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM accounts")	
            entry = cursor.fetchall()
            for i in entry:
                list.append([i[0],i[1]])
        return list
    def AddBackupAcount(self,ses,numbers,id):
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute(f"INSERT INTO accounts VALUES ('{ses}','{numbers}','{id}')")
            connection.commit()
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed: {backup_error}")
    def backupaccounts(self):
        list = []
        with sqlite3.connect("database/data.db", timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM accounts")	
            entry = cursor.fetchall()
            for i in entry:
                list.append(i)
        return list

class AsyncDatabase:
    """طبقة SQLite غير حاجبة لمسارات القراءة والكتابة داخل async handlers."""
    def __init__(self, db_path="database/data.db"):
        self.db_path = db_path

    async def _connect(self):
        conn = await aiosqlite.connect(self.db_path, timeout=30)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=30000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def accounts(self):
        async with await self._connect() as conn:
            cursor = await conn.execute("SELECT ses, number FROM accounts")
            rows = await cursor.fetchall()
            await cursor.close()
            return [[row[0], row[1]] for row in rows]

    async def backupaccounts(self):
        async with await self._connect() as conn:
            cursor = await conn.execute("SELECT ses, number, id FROM accounts")
            rows = await cursor.fetchall()
            await cursor.close()
            return [tuple(row) for row in rows]

    async def remove_all_accounts(self):
        async with await self._connect() as conn:
            await conn.execute("DELETE FROM accounts")
            await conn.commit()


async_db = AsyncDatabase()


def iter_member_chunks(path, chunk_size=1000):
    """يقرأ أعضاء JSON على دفعات، دون تحميل قائمة 50,000+ عضواً كاملة."""
    chunk = []
    with open(path, "rb") as stream:
        try:
            _, event, _ = next(ijson.parse(stream))
            stream.seek(0)
            if event == "start_array":
                items = ijson.items(stream, "item")
            elif event == "start_map":
                items = ijson.items(stream, "add_members.item")
            else:
                raise ijson.common.JSONError("unsupported JSON root")
            for item in items:
                if item:
                    chunk.append(item)
                    if len(chunk) >= chunk_size:
                        yield chunk
                        chunk = []
            if chunk:
                yield chunk
        except (ijson.common.JSONError, StopIteration, UnicodeDecodeError):
            stream.seek(0)
            data = json.load(stream)
            values = data if isinstance(data, list) else data.get("add_members", [])
            for index in range(0, len(values), chunk_size):
                yield values[index:index + chunk_size]


class ChunkedMemberQueue:
    """طابور FIFO يستهلك مصدر الأعضاء تدريجياً ولا يحتفظ بكل الملف في RAM."""
    def __init__(self, source=()):
        from collections import deque
        self._source = iter(source)
        self._queue = deque()
        self._exhausted = False

    def _fill(self):
        if not self._queue and not self._exhausted:
            try:
                self._queue.append(next(self._source))
            except StopIteration:
                self._exhausted = True

    def __bool__(self):
        self._fill()
        return bool(self._queue)

    def append(self, value):
        self._queue.append(value)

    def pop(self, index=0):
        if index != 0:
            raise IndexError("ChunkedMemberQueue supports FIFO pop(0) only")
        self._fill()
        if not self._queue:
            raise IndexError("pop from empty ChunkedMemberQueue")
        return self._queue.popleft()


def iter_member_values(path, chunk_size=1000):
    for chunk in iter_member_chunks(path, chunk_size=chunk_size):
        yield from chunk


def _backup_cipher():
    """مفتاح ثابت مشتق من إعدادات البوت، ولا يُحفظ في ملف خارجي."""
    key_material = f"{Config.BOT_TOKEN}:{Config.API_HASH}".encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key_material).digest()))

def restore_encrypted_backup():
    """استعادة النسخة المشفّرة مع تجاهل الرقم أو الجلسة المكررة."""
    if not os.path.exists(ENCRYPTED_BACKUP_FILE):
        return 0, 0
    with open(ENCRYPTED_BACKUP_FILE, "rb") as f:
        rows = json.loads(_backup_cipher().decrypt(f.read()).decode("utf-8"))
    db = database()
    existing = db.backupaccounts()
    sessions = {str(row[0]) for row in existing if row[0]}
    numbers = {str(row[1]) for row in existing if row[1]}
    added = skipped = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            skipped += 1
            continue
        session, number, account_id = map(str, row[:3])
        if session in sessions or number in numbers:
            skipped += 1
            continue
        db.AddBackupAcount(session, number, account_id)
        sessions.add(session); numbers.add(number); added += 1
    return added, skipped

def verify_encrypted_backup(path=ENCRYPTED_BACKUP_FILE):
    """يتحقق من أن النسخة المشفّرة قابلة للفك وأن محتواها قائمة سجلات سليمة."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            payload = _backup_cipher().decrypt(f.read())
        rows = json.loads(payload.decode("utf-8"))
        return isinstance(rows, list) and all(isinstance(row, (list, tuple)) and len(row) >= 3 for row in rows)
    except Exception:
        return False

def save_encrypted_backup():
    """حفظ نسخة الحسابات تلقائياً مشفّرة مع تدوير آخر خمس نسخ."""
    payload = json.dumps(database().backupaccounts(), ensure_ascii=False).encode("utf-8")
    encrypted = _backup_cipher().encrypt(payload)
    if os.path.exists(ENCRYPTED_BACKUP_FILE):
        for index in range(5, 0, -1):
            old = f"{ENCRYPTED_BACKUP_FILE}.{index}"
            previous = ENCRYPTED_BACKUP_FILE if index == 1 else f"{ENCRYPTED_BACKUP_FILE}.{index - 1}"
            if os.path.exists(previous):
                os.replace(previous, old)
    temp_path = ENCRYPTED_BACKUP_FILE + ".tmp"
    with open(temp_path, "wb") as f:
        f.write(encrypted)
        os.replace(temp_path, ENCRYPTED_BACKUP_FILE)
    if not verify_encrypted_backup(ENCRYPTED_BACKUP_FILE):
        raise ValueError("encrypted backup verification failed")
async def list_vcf_pages(call, page: int = 0):
    files = [f for f in os.listdir(VCF_DIR) if os.path.isfile(os.path.join(VCF_DIR, f))]
    if not files:
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
            "لا يوجد ملفات للحذف.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            )
        , parse_mode=enums.ParseMode.HTML)

    per_page = 14  # 7 صفوف × 2 أعمدة
    total_pages = math.ceil(len(files) / per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    page_files = files[start : start + per_page]

    buttons = []
    # عرض الملفات بعمودين
    for i in range(0, len(page_files), 2):
        row = []
        fname = page_files[i]
        row.append(InlineKeyboardButton(text=fname, callback_data=f"ask_delete:{fname}"))
        if i + 1 < len(page_files):
            fname2 = page_files[i+1]
            row.append(InlineKeyboardButton(text=fname2, callback_data=f"ask_delete:{fname2}"))
        buttons.append(row)

    # أزرار التنقل
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="السابق", style="primary", callback_data=f"del_json_page:{page-1}", icon_custom_emoji_id="5431474758451476943"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="التالي", style="primary", callback_data=f"del_json_page:{page+1}", icon_custom_emoji_id="5431352862984651645"))
    if nav:
        buttons.append(nav)

    # زر الرجوع
    buttons.append([
        back_btn()
    ])

    await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
        "اختر الملف لحذفه:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    , parse_mode=enums.ParseMode.HTML)
async def list_exp_pages(call, page: int = 0):
    """
    Lists VCF files in pages and displays inline keyboard for selection.
    """
    files = [f for f in os.listdir(VCF_DIR) if os.path.isfile(os.path.join(VCF_DIR, f))]
    if not files:
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
            "لا يوجد ملفات للاستخراج.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            )
        , parse_mode=enums.ParseMode.HTML)

    per_page = 14  # 7 rows x 2 columns
    total_pages = math.ceil(len(files) / per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    page_files = files[start:start + per_page]

    buttons = []
    # عرض الملفات بعمودين
    for i in range(0, len(page_files), 2):
        row = []
        fname = page_files[i]
        row.append(InlineKeyboardButton(text=fname, callback_data=f"extract_file:{fname}"))
        if i + 1 < len(page_files):
            fname2 = page_files[i+1]
            row.append(InlineKeyboardButton(text=fname2, callback_data=f"extract_file:{fname2}"))
        buttons.append(row)

    # أزرار التنقل بين الصفحات
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="السابق", style="primary", callback_data=f"extract_json_page:{page-1}", icon_custom_emoji_id="5431474758451476943"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="التالي", style="primary", callback_data=f"extract_json_page:{page+1}", icon_custom_emoji_id="5431352862984651645"))
    if nav:
        buttons.append(nav)

    # زر الرجوع
    buttons.append([
        back_btn()
    ])

    await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
        "اختر الملف لاستخراجه:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    , parse_mode=enums.ParseMode.HTML)
async def show_clear_accounts(call, current_page: int):
    acs = database().accounts()  # قائمة [(session_string, number), …]
    buttons_per_page = 14  # 7 صفوف × عمودين
    buttons = []

    # بناء أزرار الحسابات
    for ses, num in acs:
        label = f"الرقم: {num}"
        data  = f"clear_exec:{ses}"
        buttons.append(InlineKeyboardButton(text=label, callback_data=data, icon_custom_emoji_id=("5904542823167824187" if pross == "RemoveAccount" else "5262646461698436019")))

    # تقسيم الأزرار إلى صفحات
    pages = [buttons[i : i + buttons_per_page] for i in range(0, len(buttons), buttons_per_page)]
    # تحقّق من وجود صفحة
    if not pages or current_page < 0 or current_page >= len(pages):
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="*لا توجد حسابات حالياً.*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            )
        , parse_mode=enums.ParseMode.HTML)

    # أزرار التنقل
    nav = []
    if current_page > 0:
        nav.append(InlineKeyboardButton(text="السابق", style="primary", callback_data=f"page_clear-{current_page-1}", icon_custom_emoji_id="5431474758451476943"))
    if current_page < len(pages) - 1:
        nav.append(InlineKeyboardButton(text="التالي", style="primary", callback_data=f"page_clear-{current_page+1}", icon_custom_emoji_id="5431352862984651645"))
    nav.append(back_btn())

    # ترتيب الأزرار في صفّين
    page_buttons = pages[current_page]
    keyboard = [page_buttons[i:i+2] for i in range(0, len(page_buttons), 2)]
    keyboard.append(nav)

    await abot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.id,
        text="*اختر الحساب لمسح جهاته:*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    , parse_mode=enums.ParseMode.HTML)
class Custem :
    async def add_users_contact(self, group_link: str, bot: Client, nmcount: int, resume_state=None):
        """
        إضافة جميع جهات الاتصال من كل حساب إلى مجموعة واحدة دفعة واحدة.
        يتوقف عند إيقاف يدوي أو الوصول للعدد المطلد.
        لكل حساب حد 50 إضافة و7 إخفاقات.
        تقسيم المستخدمين إلى:
          - add_users: سيتم إضافتهم
          - not_add_users: موجودين مسبقًا
          - privacy_blocked: خصوصية تمنعهم
        وتتبع الحد اليومي لكل حساب.
        """
        global STOP_ADD
        STOP_ADD = False
    
        total_to_add = nmcount
        total_added = int((resume_state or {}).get("total_added", 0))
        total_failed = int((resume_state or {}).get("total_failed", 0))
        processed_users = set((resume_state or {}).get("processed", []))
        progress_state = resume_state or {
            "active": True, "title": "إضافة جهات الاتصال", "source": "جهات اتصال الحسابات",
            "target": group_link, "requested_count": int(nmcount), "accounts_count": 0,
            "processed": [], "processed_count": 0, "total_added": 0, "total_failed": 0,
            "operation": "contacts",
        }
        save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
        live_reporter = LiveProgressReporter(bot, Config.OWNER_ID, "تقدم إضافة جهات الاتصال", lambda: operation_metrics(progress_state, total_to_add))
        await live_reporter.start()
        initial_count = None
        final_count = None
        session_success = False
    
        # استخراج chat_id من الرابط (يدعم روابط المجموعات الخاصة والعامة)
        chat_id, chat_link_type = resolve_group_ref(group_link)
    
        # جلب الحسابات
        accounts = filter_operation_accounts(await async_db.accounts())  # قراءة غير حاجبة للحسابات
        if not accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات متاحة.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        daily_limit = 50
        daily_counts = {}
        blocked_accounts = set()
        accounts = rank_available_accounts(accounts, blocked_accounts)
        total_accounts = len(accounts)
    
        add_users = set()
        not_add_users = set()
        privacy_blocked = set()
        tried_users = set(processed_users)
    
        index = 0  # مؤشر الحساب الحالي
        session_success = False  # يُعيَّن True عند أول جلسة ناجحة — لا يُعاد ضبطه داخل الحلقة
    
        # ابدأ الدورة الحسابيّة
        while not STOP_ADD and not EMERGENCY_STOP and total_added < total_to_add:
            # ضمان انتهاء الحلقة إذا أصبحت جميع الحسابات محظورة أو بلغت حدّها اليومي
            eligible = [
                acc for acc in accounts
                if daily_counts.get(acc[1], 0) < daily_limit and acc[1] not in blocked_accounts
            ]
            if not eligible:
                await bot.send_message(
                    Config.OWNER_ID,
                    "<emoji id='6206077285720659346'>⚠️</emoji> جميع الحسابات بلغت حدّها اليومي أو محظورة — تم إيقاف الإضافة تلقائياً.",
                    parse_mode=enums.ParseMode.HTML
                )
                break

            ses_str, account_label = accounts[index]
            index = (index + 1) % total_accounts  # للعودة أوتوماتيكيًا للبداية
    
            # تجاهل إذا وصل حد الحساب اليومي أو محظور
            used_today = daily_counts.get(account_label, 0)
            if used_today >= daily_limit or account_label in blocked_accounts:
                continue
    
            account_added = 0
    
            # تسجيل الدخول
            try:
                client_app = Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=ses_str, no_updates=True, in_memory=True, lang_code="ar",
                    
                )
                await client_app.start()
                session_success = True
            except Exception as login_err:
                total_failed += 1
                await bot.send_message(
                    Config.OWNER_ID,
                    f"<emoji id='5830085055376006187'>❌</emoji> فشل تسجيل الدخول للحساب {account_label}:\n{login_err}",
        parse_mode=enums.ParseMode.HTML)
                continue
    
            try:
                # انضمام للمجموعة (مرة واحدة) - نستخدم مرجعاً محلياً لهذا الحساب فقط
                # (لا نُعدّل chat_id الأصلي حتى يبقى صالحاً لانضمام الحسابات التالية بالتناوب،
                #  خصوصاً مع المجموعات الخاصة التي تحتاج رابط الدعوة الأصلي لكل حساب جديد)
                active_chat_ref = chat_id
                try:
                    await asyncio.sleep( random.randint(1, 3) )
                    _joined_chat = await client_app.join_chat(chat_id)
                    if getattr(_joined_chat, "id", None):
                        active_chat_ref = _joined_chat.id
                except errors.UserAlreadyParticipant:
                    pass
                except errors.InviteRequestSent:
                    total_failed += 1
                    blocked_accounts.add(account_label)
                    await bot.send_message(
                        Config.OWNER_ID,
                        f"تم إرسال طلب انضمام بحساب {account_label} للمجموعة"
                    )
                    continue
                except Exception as join_err:
                    total_failed += 1
                    await bot.send_message(
                        Config.OWNER_ID,
                        f"<emoji id='6206077285720659346'>⚠️</emoji> خطأ في الانضمام بالقروب ({account_label}):\n{join_err}",
        parse_mode=enums.ParseMode.HTML)
                    continue
    
                # حفظ عدد الأعضاء الابتدائي
                if initial_count is None:
                    initial_count = (await client_app.get_chat(active_chat_ref)).members_count
    
                # جلب جهات الاتصال وتصفيتها بالتحقق الدقيق من العضوية عبر get_chat_member
                contacts = await client_app.get_contacts()
                for u in contacts:
                    # إذا لا يوجد يوزر نستخدم آيدي الحساب (دعم الإضافة بالآيدي)
                    identifier = u.username if u.username else f"id:{u.id}"
                    try:
                        existing = await client_app.get_chat_member(active_chat_ref, to_add_target(identifier))
                        if existing.status in [
                            enums.ChatMemberStatus.MEMBER,
                            enums.ChatMemberStatus.ADMINISTRATOR,
                            enums.ChatMemberStatus.OWNER,
                            enums.ChatMemberStatus.RESTRICTED,
                        ]:
                            not_add_users.add(identifier)  # موجود مسبقاً، تجاهل بدون إشعار
                        else:
                            add_users.add(identifier)
                    except errors.UserNotParticipant:
                        add_users.add(identifier)
                    except Exception:
                        add_users.add(identifier)  # تعذّر التحقق، نحاول الإضافة مباشرة
    
                # حساب كم يمكن إضافته اليوم
                remaining = min(daily_limit - used_today, total_to_add - total_added, ROTATE_BATCH_SIZE)
                to_add = [u for u in add_users if u not in tried_users][:remaining]
    
                # عملية الإضافة
                for username in to_add:
                    if STOP_ADD or EMERGENCY_STOP or total_added >= total_to_add:
                        break
                    tried_users.add(username)
                    try:
                        if await common_chat_contains_target(client_app, username, active_chat_ref):
                            not_add_users.add(username)
                            processed_users.add(username)
                            continue
                        if not await sleep_add_delay():
                            break
                        await retry_transient(client_app.add_chat_members, active_chat_ref, to_add_target(username))
                        if not await verify_added_via_common_chats(client_app, username, active_chat_ref):
                            total_failed += 1
                            pass  # الفشل يُسجّل داخلياً ولا يُرسل كإشعار منفصل.
                            continue
                        total_added += 1
                        account_added += 1
                        daily_counts[account_label] = daily_counts.get(account_label, 0) + 1
                        processed_users.add(username)
                        progress_state["accounts_count"] = total_accounts
                        save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
                        # ── تحقق ما بعد الإضافة: هل العضو دخل المجموعة فعلاً؟ ──
                        try:
                            _v = await client_app.get_chat_member(active_chat_ref, to_add_target(username))
                            if _v.status in [
                                enums.ChatMemberStatus.MEMBER,
                                enums.ChatMemberStatus.ADMINISTRATOR,
                                enums.ChatMemberStatus.OWNER,
                                enums.ChatMemberStatus.RESTRICTED,
                            ]:
                                await bot.send_message(
                                    Config.OWNER_ID,
                                    f"<emoji id='5830243638453477409'>✅</emoji> إضافة مؤكدة: {display_target(username)} عبر {account_label} — تأكدنا من وجوده في المجموعة",
                                    parse_mode=enums.ParseMode.HTML)
                            else:
                                # أمر الإضافة نجح لكن العضو لا يزال خارج المجموعة
                                total_added -= 1
                                account_added -= 1
                                daily_counts[account_label] -= 1
                                total_failed += 1
                                pass  # لا يُرسل إشعار فشل أو تحقق غير مكتمل.
                        except Exception:
                            await live_reporter.note(f"تعذر التحقق: {display_target(username)} عبر {account_label}")
                    except (errors.UserPrivacyRestricted, errors.UserNotMutualContact) as priv_err:
                        total_failed += 1
                        privacy_blocked.add(username)
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='6206077285720659346'>⚠️</emoji> خصوصية تمنع إضافة {display_target(username)}",
                            parse_mode=enums.ParseMode.HTML)
                    except errors.FloodWait as e:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='5445350406215465190'>⏳</emoji> {account_label} محظور {e.value} ثانية",
                            parse_mode=enums.ParseMode.HTML)
                        break
                    except errors.PeerFlood:
                        total_failed += 1
                        blocked_accounts.add(account_label)
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيّد حالياً",
                            parse_mode=enums.ParseMode.HTML)
                        break
                    except errors.UserBannedInChannel:
                        total_failed += 1
                        blocked_accounts.add(account_label)
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد من تيليجرام قم يالتحقق من الحساب من بوت تيليجرام @SpamBot",
                            parse_mode=enums.ParseMode.HTML)
                        break
                    except errors.PeerIdInvalid:
                        # المعرف غير معروف لهذا الحساب — يُتجاهل بصمت
                        total_failed += 1
                        privacy_blocked.add(username)
                    except Exception as err:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='5830085055376006187'>❌</emoji> خطأ إضافة {display_target(username)}:\n{err}",
                            parse_mode=enums.ParseMode.HTML)
    
                # تحديث العدد النهائي للحساب
                final_count = (await client_app.get_chat(active_chat_ref)).members_count
    
            finally:
                await client_app.stop()
    
            # ملخص الحساب
            await bot.send_message(
                Config.OWNER_ID,
                f"<emoji id='5197269100878907942'>📋</emoji> الحساب: {account_label} — <emoji id='5830243638453477409'>✅</emoji> إضافات: {account_added}",
        parse_mode=enums.ParseMode.HTML)
    
        # إذا لم تنجح أي جلسة
        if not session_success:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد جلسات صالحة، رجاءً أعد إدخال الحسابات.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
    
        # إحصائيات نهائية
        if not STOP_ADD and not EMERGENCY_STOP and total_added >= total_to_add:
            clear_operation_progress()
        else:
            save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
        status = "<tg-emoji emoji-id='5895465761975047998'>🛑</tg-emoji> تم الإيقاف يدويًا" if STOP_ADD else "<emoji id='5830243638453477409'>✅</emoji> انتهاء الإضافة"
        await abot.send_message(
            Config.OWNER_ID,
            (
                f"{status}\n"
                "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل من الجهات ⛞\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                f"عدد الاضافات المؤكدة فعلياً: {operation_metrics(progress_state, total_to_add)[0]} عضو\n"
                f"عدد الاضافات الفاشله: {operation_metrics(progress_state, total_to_add)[1]} عضو\n"
                f"موجودون مسبقاً (تم تجاهلهم): {len(not_add_users)} عضو\n"
                f"اعضاء المجموعه قبل: {initial_count}\n"
                f"اعضاء المجموعه بعد: {final_count}\n"
                f"عدد الاضافات المحققه: {final_count - initial_count}\n"
                f"عدد الحسابات الموجودة: {total_accounts}\n"
                f"عدد الحسابات المحظورة: {len(blocked_accounts)}\n"
                f"عدد الحسابات النشطه: {total_accounts - len(blocked_accounts)}\n"
                f"مجموع الاضافات لكل الحسابات: {sum(daily_counts.values())}\n"
                f"عدد الاضافات المتبقية لكل الحسابات: {total_accounts * daily_limit - sum(daily_counts.values())}"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    async def ask_limit(self, client, message, user_info: dict):
        try:
            user_id = message.from_user.id
            # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
            if user_id != Config.OWNER_ID and user_id not in Config.Devs:
                await message.message.reply(
                    "عذرا الأوامر ليست لك"
                )
            pass
            limit_value = int(message.text.strip())
        except ValueError:
            return await bot.send_message(
                message.chat.id,
                "<emoji id='6206077285720659346'>⚠️</emoji> يرجى إدخال رقم صحيح للحد الأقصى",
        parse_mode=enums.ParseMode.HTML)
        user_info['limit'] = limit_value
        await bot.send_message(
            message.chat.id,
            "قم بارسال رابط القروب المراد إضافة الأعضاء له\n⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\nمثل:\n`https://t.me/a990099aa`",
        )
        # register next handler with limit included
        bot.register_next_step_handler(
            partial(statement2hide, user_info=user_info)
        )
    async def ADDuserhide(self, data, inGRob, grop2, bot: Client, nmcount, resume_state=None):
        """
        إضافة مستخدمين مخفيين إلى مجموعة بشكل متكرر عبر الحسابات بعداد أقصى لكل حساب
        يتوقف فقط عند تفعيل STOP_ADD أو الوصول للعدد المطلوب
        ولا ينتقل للحساب التالي إلا عند الانتهاء من إضافة الحد اليومي أو وقوع خطأ FloodWait/PeerFlood
        يعرض كافة الأخطاء بصيغة شيل قابلة للنسخ
        على أي خطأ في الحساب يتم تخطيه واستكمال الحسابات الأخرى
        """
        global STOP_ADD
        STOP_ADD = False
        total_added = int((resume_state or {}).get("total_added", 0))
        total_failed = int((resume_state or {}).get("total_failed", 0))
        processed_users = set((resume_state or {}).get("processed", []))
        progress_state = resume_state or {"active": True, "title": "إضافة أعضاء مخفيين", "source": inGRob, "target": grop2, "requested_count": int(nmcount), "accounts_count": 0, "processed": [], "processed_count": 0, "total_added": 0, "total_failed": 0, "operation": "add_users_hide"}
        save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
    
        # === 0. إعداد متغيرات العدّ اليومي والمتتبعات ===
        daily_limit = 50
        daily_counts = {}                # عدد الإضافات لكل حساب في هذه الجلسة
        blocked_accounts = set()         # حسابات تجاوزت الحد اليومي أو تقيدت
        add_queue = None                 # يُبنى كطابور كسول بعد تجهيز الفلاتر
        privacy_blocked = set()          # المحظورون بالخصوصية
        not_add_users = set()            # الموجودون مسبقاً
    
        # === 1. قراءة وإعداد الحسابات ===
        raw_accounts = filter_operation_accounts(await async_db.accounts())
        if not raw_accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات متاحة.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        raw_accounts = rank_available_accounts(raw_accounts, blocked_accounts)
        valid_accounts = []
        for session_str, account_label in raw_accounts:
            daily_counts[account_label] = 0
            try:
                test_client = Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_str, no_updates=True, in_memory=True, lang_code="ar",
                    
                )
                await test_client.start()
                await test_client.stop()
                valid_accounts.append((session_str, account_label))
            except Exception as login_err:
                await bot.send_message(
                    Config.OWNER_ID,
                    f"<emoji id='6206077285720659346'>⚠️</emoji> تخطي الحساب {account_label}؛ فشل تسجيل الدخول:\n```shell\n{login_err}```",
        parse_mode=enums.ParseMode.HTML)
    
        if not valid_accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد جلسات صالحة بعد اختبار الحسابات.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        total_accounts = len(valid_accounts)
    
        # === 2. إعداد ملف الأعضاء ===
        if not os.path.exists(MEMBERS_JSON):
            with open(MEMBERS_JSON, "w", encoding="utf-8") as f:
                json.dump({"add_members": []}, f, ensure_ascii=False, indent=4)
    
        # === 3. قراءة قائمة الأعضاء من الملف على دفعات ===
        try:
            raw_usernames = iter_member_values(MEMBERS_JSON, chunk_size=1000)
        except Exception as e:
            return await bot.send_message(
                Config.OWNER_ID,
                f"<emoji id='5830085055376006187'>❌</emoji> خطأ في قراءة الملف {MEMBERS_JSON}:\n```shell\n{e}```",
        parse_mode=enums.ParseMode.HTML)
    
        total_to_add = int(nmcount)
        total_added = 0
        total_failed = 0
        live_reporter = LiveProgressReporter(bot, Config.OWNER_ID, "تقدم إضافة الأعضاء", lambda: operation_metrics(progress_state, total_to_add))
        await live_reporter.start()
        initial_count = None
        final_count = None
    
        inGRob, inGRob_link_type = resolve_group_ref(inGRob)
        session_success = False
    
        # === 4. حلقة الإضافة الرئيسية ===
        # إعداد طابور كسول؛ تتم قراءة العضو التالي عند الحاجة فقط
        add_queue = ChunkedMemberQueue(
            username for username in raw_usernames
            if username and username not in not_add_users
            and username not in privacy_blocked and username not in processed_users
        )
    
        while not STOP_ADD and not EMERGENCY_STOP and total_added < total_to_add:
            for session_str, account_label in valid_accounts:
                if STOP_ADD or EMERGENCY_STOP or total_added >= total_to_add:
                    break
    
                # تخطي الحسابات التي تجاوزت الحد اليومي أو المقيدة
                if daily_counts[account_label] >= daily_limit:
                    blocked_accounts.add(account_label)
                    mark_account_failure(account_label, "blocked")
                    continue
    
                account_added = 0
                client = None
    
                try:
                    client = Client(
                        ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                        session_string=session_str, no_updates=True, in_memory=True, lang_code="ar",
                        
                    )
                    await client.start()
                    session_success = True
    
                    # انضمام للمجموعة + حل المعرف الرقمي (UserAlreadyParticipant مُعالَج داخلياً)
                    await asyncio.sleep(random.randint(1, 3))
                    try:
                        active_chat_ref = await resolve_private_chat_id(client, inGRob)
                    except errors.InviteRequestSent:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"تم ارسال طلب انضمام بالحساب {account_label} للقروبين بنجاح"
                        )
                        raise
                    except Exception as join_err:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='6206077285720659346'>⚠️</emoji> خطأ في انضمام الحساب {account_label} للقروبين:\n```shell\n{join_err}```",
        parse_mode=enums.ParseMode.HTML)
                        raise
    
                    if initial_count is None:
                        initial_count = (await client.get_chat(active_chat_ref)).members_count
    
                    # === 4.1: حل الإضافة اليومية من قائمة الانتظار ===
                    while (
                        not STOP_ADD
                        and total_added < total_to_add
                        and account_added < min((daily_limit - daily_counts[account_label]), ROTATE_BATCH_SIZE)
                        and add_queue
                    ):
                        username = add_queue.pop(0)
                        try:
                            # تحقق من حالة العضو في المجموعة الهدف بدقة عبر get_chat_member
                            try:
                                existing = await client.get_chat_member(active_chat_ref, to_add_target(username))
                                if existing.status in [
                                    enums.ChatMemberStatus.MEMBER,
                                    enums.ChatMemberStatus.ADMINISTRATOR,
                                    enums.ChatMemberStatus.OWNER,
                                    enums.ChatMemberStatus.RESTRICTED,
                                ]:
                                    not_add_users.add(username)
                                    continue  # موجود مسبقاً — يُتجاهل بصمت بدون إشعار الأدمن
                                # حالات LEFT/KICKED → يُضاف من جديد
                            except errors.UserNotParticipant:
                                pass  # غير موجود — نُضيفه
                            except Exception:
                                pass  # تعذّر التحقق — نحاول الإضافة مباشرة

                            if await common_chat_contains_target(client, username, active_chat_ref):
                                not_add_users.add(username)
                                processed_users.add(username)
                                continue
                            if not await sleep_add_delay():
                                break
                            await retry_transient(client.add_chat_members, active_chat_ref, to_add_target(username))
                            if not await verify_added_via_common_chats(client, username, active_chat_ref):
                                total_failed += 1
                                pass  # الفشل يُسجّل داخلياً ولا يُرسل كإشعار منفصل.
                                continue
                            total_added += 1
                            mark_account_success(account_label)
                            account_added += 1
                            daily_counts[account_label] += 1
                            not_add_users.add(username)
                            processed_users.add(username)
                            progress_state["accounts_count"] = total_accounts
                            save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
                            await live_reporter.note(f"إضافة ناجحة: {display_target(username)} عبر {account_label}")
                        except errors.UserPrivacyRestricted:
                            total_failed += 1
                            privacy_blocked.add(username)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='6206077285720659346'>⚠️</emoji> خصوصية المستخدم تمنع الاضافة: {display_target(username)}",
                                parse_mode=enums.ParseMode.HTML)
                        except errors.FloodWait as e:
                            total_failed += 1
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} محظور مؤقتاً: {e.value} ثانية",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.PeerFlood:
                            total_failed += 1
                            blocked_accounts.add(account_label)
                            mark_account_failure(account_label, "blocked")
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد حالياً",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.UserBannedInChannel:
                            total_failed += 1
                            blocked_accounts.add(account_label)
                            mark_account_failure(account_label, "blocked")
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد من تيليجرام قم يالتحقق من الحساب من بوت تيليجرام @SpamBot",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.PeerIdInvalid:
                            # المعرف غير معروف لهذا الحساب — يُتجاهل بصمت
                            total_failed += 1
                            privacy_blocked.add(username)
                        except Exception as add_err:
                            total_failed += 1
                            add_queue.append(username)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5830085055376006187'>❌</emoji> خطأ عند إضافة {display_target(username)} عبر {account_label}:\n```shell\n{add_err}```",
                                parse_mode=enums.ParseMode.HTML)
                        finally:
                            pass
    
                    final_count = (await client.get_chat(active_chat_ref)).members_count
    
                except Exception:
                    continue
    
                finally:
                    if client:
                        await client.stop()
    
                # تقرير عن الحساب
                await bot.send_message(
                    Config.OWNER_ID,
                    f"<emoji id='5197269100878907942'>📋</emoji> الحساب: {account_label}\n<emoji id='5830243638453477409'>✅</emoji> إضافات اليوم: {account_added}",
                    parse_mode=enums.ParseMode.HTML)
    
            # عند انتهاء الدورة، يستأنف من الحساب الأول إذا لم يتم الإيقاف
            # while سيعيد التنفيذ تلقائياً
    
        if not session_success:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد جلسات صالحة، يرجى إعادة إدخال الحسابات.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                parse_mode=enums.ParseMode.HTML)
    
        await live_reporter.close()
        # === 5. إرسال الإحصائيات النهائية ===
        status = "<tg-emoji emoji-id='5895465761975047998'>🛑</tg-emoji> تم الإيقاف يدوياً" if STOP_ADD else "<emoji id='5830243638453477409'>✅</emoji> انتهاء الإضافة"
        await abot.send_message(
            Config.OWNER_ID,
            (
                f"سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إحصــائيات النقل المــخفي {status} ⛞\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                f"عدد الاضافات المؤكدة فعلياً: {operation_metrics(progress_state, total_to_add)[0]} عضو\n"
                f"عدد الاضافات الفاشله: {operation_metrics(progress_state, total_to_add)[1]} عضو\n"
                f"موجودون مسبقاً (تم تجاهلهم): {len(not_add_users)} عضو\n"
                f"اعضاء المجموعة قبل: {initial_count}\n"
                f"اعضاء المجموعة بعد: {final_count}\n"
                f"مجموع الاضافات لكل الحسابات: {sum(daily_counts.values())}\n"
                f"عدد الحسابات: {total_accounts}\n"
                f"محظورة: {len(blocked_accounts)}\n"
                f"نشطة: {total_accounts - len(blocked_accounts)}\n"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
    
    
    
    async def ADDuser(self, inGRob, grop2, bot: Client, nmcount, resume_state=None):
        """
        إضافة مستخدمين مخفيين إلى مجموعة بشكل متكرر عبر الحسابات بعداد أقصى لكل حساب
        يتوقف فقط عند تفعيل STOP_ADD أو الوصول للعدد المطلوب
        ولا ينتقل للحساب التالي إلا عند الانتهاء من إضافة الحد اليومي أو وقوع خطأ FloodWait/PeerFlood
        يعرض كافة الأخطاء بصيغة شيل قابلة للنسخ
        على أي خطأ في الحساب يتم تخطيه واستكمال الحسابات الأخرى
        """
        global STOP_ADD
        STOP_ADD = False
        total_added = int((resume_state or {}).get("total_added", 0))
        total_failed = int((resume_state or {}).get("total_failed", 0))
        processed_users = set((resume_state or {}).get("processed", []))
        total_to_add = int(nmcount)
        progress_state = resume_state or {"active": True, "title": "إضافة أعضاء ظاهرين", "source": inGRob, "target": grop2, "requested_count": int(nmcount), "accounts_count": 0, "processed": [], "processed_count": 0, "total_added": 0, "total_failed": 0, "operation": "add_users"}
        save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
        live_reporter = LiveProgressReporter(bot, Config.OWNER_ID, "تقدم إضافة الأعضاء", lambda: operation_metrics(progress_state, total_to_add))
        await live_reporter.start()
    
        # === 0. إعداد متغيرات العدّ اليومي والمتتبعات ===
        daily_limit = 50
        daily_counts = {}                # عدد الإضافات لكل حساب في هذه الجلسة
        blocked_accounts = set()         # حسابات تجاوزت الحد اليومي أو تقيدت
        add_queue = None                 # يُبنى كطابور كسول بعد تجهيز الفلاتر
        privacy_blocked = set()          # المحظورون بالخصوصية
        not_add_users = set()            # الموجودون مسبقاً
    
        # === 1. قراءة وإعداد الحسابات ===
        raw_accounts = filter_operation_accounts(await async_db.accounts())
        if not raw_accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات متاحة.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        raw_accounts = rank_available_accounts(raw_accounts, blocked_accounts)
        valid_accounts = []
        for session_str, account_label in raw_accounts:
            daily_counts[account_label] = 0
            try:
                test_client = Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_str, no_updates=True, in_memory=True, lang_code="ar",
                    
                )
                await test_client.start()
                await test_client.stop()
                valid_accounts.append((session_str, account_label))
            except Exception as login_err:
                await bot.send_message(
                    Config.OWNER_ID,
                    f"<emoji id='6206077285720659346'>⚠️</emoji> تخطي الحساب {account_label}؛ فشل تسجيل الدخول:\n```shell\n{login_err}```",
        parse_mode=enums.ParseMode.HTML)
    
        if not valid_accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد جلسات صالحة بعد اختبار الحسابات.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        total_accounts = len(valid_accounts)
    
        # === 2. إعداد ملف الأعضاء ===
        if not os.path.exists(MEMBERS_JSON):
            with open(MEMBERS_JSON, "w", encoding="utf-8") as f:
                json.dump({"add_members": []}, f, ensure_ascii=False, indent=4)
    
        # === 3. قراءة قائمة الأعضاء من الملف على دفعات ===
        try:
            raw_usernames = iter_member_values(MEMBERS_JSON, chunk_size=1000)
        except Exception as e:
            return await bot.send_message(
                Config.OWNER_ID,
                f"<emoji id='5830085055376006187'>❌</emoji> خطأ في قراءة الملف {MEMBERS_JSON}:\n```shell\n{e}```",
        parse_mode=enums.ParseMode.HTML)
    
        total_to_add = int(nmcount)
        total_added = 0
        total_failed = 0
        initial_count = None
        final_count = None
    
        inGRob, inGRob_link_type = resolve_group_ref(inGRob)
        session_success = False
    
        # === 4. حلقة الإضافة الرئيسية ===
        # إعداد طابور كسول؛ تتم قراءة العضو التالي عند الحاجة فقط
        add_queue = ChunkedMemberQueue(
            username for username in raw_usernames
            if username and username not in not_add_users
            and username not in privacy_blocked and username not in processed_users
        )
    
        while not STOP_ADD and not EMERGENCY_STOP and total_added < total_to_add:
            for session_str, account_label in valid_accounts:
                if STOP_ADD or EMERGENCY_STOP or total_added >= total_to_add:
                    break
    
                # تخطي الحسابات التي تجاوزت الحد اليومي أو المقيدة
                if daily_counts[account_label] >= daily_limit:
                    blocked_accounts.add(account_label)
                    continue
    
                account_added = 0
                client = None
    
                try:
                    client = Client(
                        ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                        session_string=session_str, no_updates=True, in_memory=True, lang_code="ar",
                        
                    )
                    await client.start()
                    session_success = True
    
                    # انضمام للمجموعة + حل المعرف الرقمي (UserAlreadyParticipant مُعالَج داخلياً)
                    await asyncio.sleep(random.randint(1, 3))
                    try:
                        active_chat_ref = await resolve_private_chat_id(client, inGRob)
                    except errors.InviteRequestSent:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"تم ارسال طلب انضمام بالحساب {account_label} للقروبين بنجاح"
                        )
                        raise
                    except Exception as join_err:
                        total_failed += 1
                        await bot.send_message(
                            Config.OWNER_ID,
                            f"<emoji id='6206077285720659346'>⚠️</emoji> خطأ في انضمام الحساب {account_label} للقروبين:\n```shell\n{join_err}```",
        parse_mode=enums.ParseMode.HTML)
                        raise
    
                    if initial_count is None:
                        initial_count = (await client.get_chat(active_chat_ref)).members_count
    
                    # === 4.1: حل الإضافة اليومية من قائمة الانتظار ===
                    while (
                        not STOP_ADD
                        and total_added < total_to_add
                        and account_added < min((daily_limit - daily_counts[account_label]), ROTATE_BATCH_SIZE)
                        and add_queue
                    ):
                        username = add_queue.pop(0)
                        try:
                            # تحقق من حالة العضو في المجموعة الهدف بدقة عبر get_chat_member
                            try:
                                existing = await client.get_chat_member(active_chat_ref, to_add_target(username))
                                if existing.status in [
                                    enums.ChatMemberStatus.MEMBER,
                                    enums.ChatMemberStatus.ADMINISTRATOR,
                                    enums.ChatMemberStatus.OWNER,
                                    enums.ChatMemberStatus.RESTRICTED,
                                ]:
                                    not_add_users.add(username)
                                    continue  # موجود مسبقاً — يُتجاهل بصمت بدون إشعار الأدمن
                                # حالات LEFT/KICKED → يُضاف من جديد
                            except errors.UserNotParticipant:
                                pass  # غير موجود — نُضيفه
                            except Exception:
                                pass  # تعذّر التحقق — نحاول الإضافة مباشرة

                            if await common_chat_contains_target(client, username, active_chat_ref):
                                not_add_users.add(username)
                                processed_users.add(username)
                                continue
                            if not await sleep_add_delay():
                                break
                            await retry_transient(client.add_chat_members, active_chat_ref, to_add_target(username))
                            if not await verify_added_via_common_chats(client, username, active_chat_ref):
                                total_failed += 1
                                pass  # الفشل يُسجّل داخلياً ولا يُرسل كإشعار منفصل.
                                continue
                            total_added += 1
                            account_added += 1
                            daily_counts[account_label] += 1
                            not_add_users.add(username)
                            processed_users.add(username)
                            progress_state["accounts_count"] = total_accounts
                            save_progress_checkpoint(progress_state, processed_users, total_added, total_failed)
                            await live_reporter.note(f"إضافة ناجحة: {display_target(username)} عبر {account_label}")
                        except errors.UserPrivacyRestricted:
                            total_failed += 1
                            privacy_blocked.add(username)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='6206077285720659346'>⚠️</emoji> خصوصية المستخدم تمنع الاضافة: {display_target(username)}",
                                parse_mode=enums.ParseMode.HTML)
                        except errors.FloodWait as e:
                            total_failed += 1
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} محظور مؤقتاً: {e.value} ثانية",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.PeerFlood:
                            total_failed += 1
                            blocked_accounts.add(account_label)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد حالياً",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.UserBannedInChannel:
                            total_failed += 1
                            blocked_accounts.add(account_label)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد من تيليجرام قم يالتحقق من الحساب من بوت تيليجرام @SpamBot",
                                parse_mode=enums.ParseMode.HTML)
                            break
                        except errors.PeerIdInvalid:
                            # المعرف غير معروف لهذا الحساب — يُتجاهل بصمت
                            total_failed += 1
                            privacy_blocked.add(username)
                        except Exception as add_err:
                            total_failed += 1
                            add_queue.append(username)
                            await bot.send_message(
                                Config.OWNER_ID,
                                f"<emoji id='5830085055376006187'>❌</emoji> خطأ عند إضافة {display_target(username)} عبر {account_label}:\n```shell\n{add_err}```",
                                parse_mode=enums.ParseMode.HTML)
                        finally:
                            pass
    
                    final_count = (await client.get_chat(active_chat_ref)).members_count
    
                except Exception:
                    continue
    
                finally:
                    if client:
                        await client.stop()
    
                # تقرير عن الحساب
                await bot.send_message(
                    Config.OWNER_ID,
                    f"<emoji id='5197269100878907942'>📋</emoji> الحساب: {account_label}\n<emoji id='5830243638453477409'>✅</emoji> إضافات اليوم: {account_added}",
        parse_mode=enums.ParseMode.HTML)
    
            # عند انتهاء الدورة، يستأنف من الحساب الأول إذا لم يتم الإيقاف
            # while سيعيد التنفيذ تلقائياً
    
        if not session_success:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد جلسات صالحة، يرجى إعادة إدخال الحسابات.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        await live_reporter.close()
        # === 5. إرسال الإحصائيات النهائية ===
        status = "<tg-emoji emoji-id='5895465761975047998'>🛑</tg-emoji> تم الإيقاف يدوياً" if STOP_ADD else "<emoji id='5830243638453477409'>✅</emoji> انتهاء الإضافة"
        await abot.send_message(
            Config.OWNER_ID,
            (
                f"سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إحصــائيات النقل الظـــاهر {status} ⛞\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                f"عدد الاضافات المؤكدة فعلياً: {operation_metrics(progress_state, total_to_add)[0]} عضو\n"
                f"عدد الاضافات الفاشله: {operation_metrics(progress_state, total_to_add)[1]} عضو\n"
                f"موجودون مسبقاً (تم تجاهلهم): {len(not_add_users)} عضو\n"
                f"اعضاء المجموعة قبل: {initial_count}\n"
                f"اعضاء المجموعة بعد: {final_count}\n"
                f"مجموع الاضافات لكل الحسابات: {sum(daily_counts.values())}\n"
                f"عدد الحسابات: {total_accounts}\n"
                f"محظورة: {len(blocked_accounts)}\n"
                f"نشطة: {total_accounts - len(blocked_accounts)}\n"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
    
    
    
    async def add_users_hide(self, group_link: str, bot: Client, nmcount: int):
        """
        إضافة مستخدمين مخفيين إلى مجموعة معينة مع:
        - تقسيم الأعضاء إلى ثلاث مجموعات: للإضافة، ممنوعين بالخصوصية، وموجودين مسبقاً.
        - حد يومي لكل حساب بواقع 50 إضافة، ويتم التتبع عبر ملف JSON حتى عند إعادة تشغيل الأمر.
        - تخطي الحساب عند حدوث FloodWait أو PeerFlood ووضعه في قائمة المحظورين.
        - لا يتوقف إلا بإيقاف يدوي.
        """
    
        global STOP_ADD
        STOP_ADD = False
    
        # === 0. إعداد تتبع الإضافات اليومية ===
        import os, json, datetime, asyncio
        import random
    
        daily_limit = 50
        today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        counts_file = "daily_counts.json"
    
        # تحميل العدادات
        if os.path.exists(counts_file):
            with open(counts_file, "r") as f:
                all_counts = json.load(f)
        else:
            all_counts = {}
        # إعادة تعيين يوم جديد
        if today_str not in all_counts:
            all_counts = {today_str: {}}
        daily_counts = all_counts[today_str]
    
        blocked_accounts = set()       # حسابات تعرضت لـFlood/PeerFlood اليوم
        add_users = set()             # الأعضاء الذين سنتحقق منهم للإضافة
        privacy_blocked = set()       # الأعضاء المحجوبين بالخصوصية
        not_add_users = set()         # الأعضاء المنضمين مسبقاً
    
        # === 1. جلب الأعضاء والحسابات ===
        members_list = await self.get_users_saved()
        total_to_add = nmcount
        total_added = 0
        total_failed = 0
        progress_state = {
            "active": True, "title": "إضافة أعضاء من الملفات", "source": "ملفات الأعضاء",
            "target": group_link, "requested_count": int(nmcount), "accounts_count": 0,
            "processed": [], "processed_count": 0, "total_added": 0, "total_failed": 0,
            "operation": "files",
        }
        save_progress_checkpoint(progress_state, set(), total_added, total_failed)
        live_reporter = LiveProgressReporter(bot, Config.OWNER_ID, "تقدم إضافة الأعضاء", lambda: operation_metrics(progress_state, total_to_add))
        await live_reporter.start()
        username_index = 0
    
        accounts = filter_operation_accounts(database().accounts())
        accounts = rank_available_accounts(accounts, blocked_accounts)
        total_accounts = len(accounts)
        if not accounts:
            return await abot.send_message(
                Config.OWNER_ID,
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات متاحة للإضافة.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        chat_id, chat_link_type = resolve_group_ref(group_link)
        # === 2. الحصول على عدد الأعضاء الابتدائي ===
        tmp = Client(":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                     session_string=accounts[0][0], no_updates=True, in_memory=True, lang_code="ar")
        await tmp.start()
        try:
            _tmp_joined = await tmp.join_chat(chat_id)
            initial_chat_ref = getattr(_tmp_joined, "id", None) or chat_id
        except errors.UserAlreadyParticipant:
            initial_chat_ref = chat_id
        except Exception:
            initial_chat_ref = chat_id
        initial_count = (await tmp.get_chat(initial_chat_ref)).members_count
        await tmp.stop()
    
        # === 3. حلقة الإضافة الرئيسية بتتابع حسابي منتظم ===
        account_index = 0
        while not STOP_ADD and not EMERGENCY_STOP and total_added < total_to_add:
            # تدوير قائمة الأسماء
            if username_index >= len(members_list):
                username_index = 0
            username = members_list[username_index]
            username_index += 1
            if not username:
                total_failed += 1
                continue
    
            # البحث عن الحساب التالي المتاح
            chosen = None
            for _ in range(total_accounts):
                session_str, account_label = accounts[account_index]
                account_index = (account_index + 1) % total_accounts
                if daily_counts.get(account_label, 0) < daily_limit and account_label not in blocked_accounts:
                    chosen = (session_str, account_label)
                    break
            if not chosen:
                # لا حسابات متاحة
                break
            session_str, account_label = chosen
    
            # تسجيل الدخول
            try:
                client = Client(":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                                session_string=session_str, no_updates=True, in_memory=True, lang_code="ar",
                                )
                await client.start()
            except Exception as login_err:
                total_failed += 1
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='5830085055376006187'>❌</emoji> فشل تسجيل الدخول للحساب {account_label}:\n```shell\n{login_err}\n```",
        parse_mode=enums.ParseMode.HTML)
                continue
    
            # الانضمام للقروب أولاً ثم التحقق الدقيق من العضوية
            active_chat_ref = chat_id
            try:
                await asyncio.sleep(random.randint(1, 3))
                _joined_chat = await client.join_chat(chat_id)
                if getattr(_joined_chat, "id", None):
                    active_chat_ref = _joined_chat.id
            except errors.UserAlreadyParticipant:
                pass
            except Exception as join_err:
                total_failed += 1
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='6206077285720659346'>⚠️</emoji> فشل انضمام الحساب {account_label} للقروب:\n```shell\n{join_err}\n```",
        parse_mode=enums.ParseMode.HTML)
                await client.stop()
                continue

            # التحقق الدقيق من وجود العضو في المجموعة الهدف عبر get_chat_member
            try:
                existing_member = await client.get_chat_member(active_chat_ref, to_add_target(username))
                if existing_member.status in [
                    enums.ChatMemberStatus.MEMBER,
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                    enums.ChatMemberStatus.RESTRICTED,
                ]:
                    # موجود مسبقاً — يُتجاهل بصمت بدون إشعار الأدمن
                    not_add_users.add(username)
                    await client.stop()
                    continue
                add_users.add(username)
            except errors.UserNotParticipant:
                add_users.add(username)
            except Exception:
                add_users.add(username)  # تعذّر التحقق، نحاول الإضافة مباشرة

            # pyrofork لم يرمِ استثناء = نجاح 100%
            try:
                if await common_chat_contains_target(client, username, active_chat_ref):
                    not_add_users.add(username)
                    continue
                if not await sleep_add_delay():
                    break
                await retry_transient(client.add_chat_members, active_chat_ref, to_add_target(username))
                if not await verify_added_via_common_chats(client, username, active_chat_ref):
                    total_failed += 1
                    pass  # الفشل يُسجّل داخلياً ولا يُرسل كإشعار منفصل.
                    continue
                total_added += 1
                mark_account_success(account_label)
                daily_counts[account_label] = daily_counts.get(account_label, 0) + 1
                all_counts[today_str] = daily_counts
                with open(counts_file, "w") as f:
                    json.dump(all_counts, f)
                await live_reporter.note(f"إضافة ناجحة: {display_target(username)} عبر {account_label}")
                not_add_users.add(username)
            except errors.UserPrivacyRestricted:
                total_failed += 1
                privacy_blocked.add(username)
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='6206077285720659346'>⚠️</emoji> خصوصية المستخدم تمنع الاضافة: {display_target(username)}",
                                       parse_mode=enums.ParseMode.HTML)
            except errors.UserNotMutualContact:
                total_failed += 1
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='6206077285720659346'>⚠️</emoji> الحساب لا يضيف إلا جهات اتصال مشتركة: {account_label}",
                                       parse_mode=enums.ParseMode.HTML)
            except errors.FloodWait as e:
                total_failed += 1
                blocked_accounts.add(account_label)
                mark_account_failure(account_label, "blocked")
                wait_time = getattr(e, "value", 30000)
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} محظور مؤقتاً لمدة {wait_time} ثانية",
                                       parse_mode=enums.ParseMode.HTML)
            except errors.PeerFlood:
                total_failed += 1
                blocked_accounts.add(account_label)
                mark_account_failure(account_label, "blocked")
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='5445350406215465190'>⏳</emoji> الحساب {account_label} متقيد حالياً",
                                       parse_mode=enums.ParseMode.HTML)
            except errors.UserBannedInChannel:
                total_failed += 1
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='5830085055376006187'>❌</emoji> فشلت — Banned from target group: {display_target(username)} محظور من المجموعة الهدف",
                                       parse_mode=enums.ParseMode.HTML)
            except errors.PeerIdInvalid:
                # المعرف غير معروف لهذا الحساب — يُتجاهل بصمت
                total_failed += 1
                privacy_blocked.add(username)
            except Exception as add_err:
                total_failed += 1
                await bot.send_message(Config.OWNER_ID,
                                       f"<emoji id='5830085055376006187'>❌</emoji> خطأ عند إضافة {display_target(username)}:\n```shell\n{add_err}\n```",
                                       parse_mode=enums.ParseMode.HTML)
            finally:
                await client.stop()
    
        await live_reporter.close()
        # === 4. الحصول على عدد الأعضاء النهائي ===
        tmp = Client(":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                     session_string=accounts[0][0], no_updates=True, in_memory=True, lang_code="ar")
        await tmp.start()
        try:
            _tmp_joined = await tmp.join_chat(chat_id)
            final_chat_ref = getattr(_tmp_joined, "id", None) or chat_id
        except errors.UserAlreadyParticipant:
            final_chat_ref = chat_id
        except Exception:
            final_chat_ref = chat_id
        final_count = (await tmp.get_chat(final_chat_ref)).members_count
        await tmp.stop()
    
        # === 5. إنهاء السجل وإرسال الإحصائيات النهائية الجديدة ===
        sync_operation_record(progress_state, set(), total_added, total_failed, status="completed")
        await abot.send_message(
            Config.OWNER_ID,
            "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل من الملفات ⛞\n"
            "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
            f"عدد الاضافات المؤكدة فعلياً: {operation_metrics(progress_state, total_to_add)[0]} عضو\n"
            f"عدد الاضافات الفاشله: {operation_metrics(progress_state, total_to_add)[1]} عضو\n"
            f"موجودون مسبقاً (تم تجاهلهم): {len(not_add_users)} عضو\n"
            f"اعضاء المجموعه قبل: {initial_count}\n"
            f"اعضاء المجموعه بعد: {final_count}\n"
            f"عدد الاضافات المحققه: {final_count - initial_count}\n"
            f"عدد الحسابات الموجودة: {total_accounts}\n"
            f"عدد الحسابات المحظورة: {len(blocked_accounts)}\n"
            f"عدد الحسابات النشطه: {total_accounts - len(blocked_accounts)}\n"
            f"مجموع الاضافات لكل الحسابات: {sum(daily_counts.values())}\n"
            f"عدد الاضافات المتبقية لكل الحسابات: {total_accounts * daily_limit - sum(daily_counts.values())}\n",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    async def get_users_saved(self) -> list:
        """
        جلب الأعضاء من جميع ملفات JSON في مجلد VCF_DIR.
        """
        all_usernames = []
        for filename in os.listdir(VCF_DIR):
            if filename.lower().endswith('.json'):
                path = os.path.join(VCF_DIR, filename)
                try:
                    for chunk in iter_member_chunks(path, chunk_size=1000):
                        all_usernames.extend(chunk)
                except Exception as e:
                    await bot.send_message(
                        Config.OWNER_ID,
                        f"<emoji id='6206077285720659346'>⚠️</emoji> خطأ في قراءة الملف {path}: {e}",
        parse_mode=enums.ParseMode.HTML)
        # إزالة التكرارات مع الحفاظ على الترتيب
        seen = set()
        return [u for u in all_usernames if u and not (u in seen or seen.add(u))]
    async def _scan_chat_history_parallel(self, group_ref, limit, accounts, max_workers=16):
        """
        يقسم مهمة استخراج تاريخ الرسائل على عدة حسابات بالتوازي.
        - تدعم المجموعات الخاصة: تحل المعرّف الرقمي عبر resolve_private_chat_id قبل أي عملية.
        - تستخدم get_chat_history لضمان عدم تفويت رسائل محذوفة (لا فجوات).
        - كل عامل يتولى نطاقاً محدداً من offset_id بطريقة متوازية.
        يرجع: (unique_usernames set, unique_bots set, ok: bool)
        """
        unique_usernames = set()
        unique_bots = set()

        if not accounts or limit <= 0:
            return unique_usernames, unique_bots, False

        # حساب أساسي: حل المعرف الرقمي + تحديد top_id (آخر معرف رسالة)
        top_id = None
        resolved_ref = group_ref  # يُحدَّث للمعرف الرقمي حين يتوفر
        for account in accounts:
            try:
                async with Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=account[0], no_updates=True, in_memory=True, lang_code="ar"
                ) as app:
                    # حل المعرف الرقمي (ضروري للمجموعات الخاصة)
                    resolved_ref = await resolve_private_chat_id(app, group_ref)
                    # جلب آخر معرف رسالة
                    async for m in app.get_chat_history(resolved_ref, limit=1):
                        top_id = m.id
                        break
                break
            except Exception:
                continue

        if not top_id or top_id <= 0:
            return unique_usernames, unique_bots, False

        effective_limit = min(limit, top_id)
        workers_count = max(1, min(max_workers, len(accounts), effective_limit))
        chunk_size = math.ceil(effective_limit / workers_count)

        async def _worker(session_string, offset_id, count):
            """
            يجلب عدداً محدداً من الرسائل الأقدم من offset_id باستخدام get_chat_history.
            يستخدم المعرف الرقمي المُحلَّل مسبقاً لدعم المجموعات الخاصة.
            """
            local_usernames, local_bots = set(), set()
            if count <= 0:
                return local_usernames, local_bots
            try:
                async with Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_string, no_updates=True, in_memory=True, lang_code="ar"
                ) as app:
                    # الانضمام إذا لزم، وحل المعرف الرقمي للمجموعة الخاصة
                    worker_ref = await resolve_private_chat_id(app, group_ref)
                    # جلب الرسائل بـ get_chat_history مع offset_id الصحيح
                    async for msg in app.get_chat_history(worker_ref, limit=count, offset_id=offset_id):
                        u = msg.from_user
                        if not u:
                            continue
                        uname = u.username if u.username else f"id:{u.id}"
                        if u.is_bot:
                            local_bots.add(uname)
                        else:
                            local_usernames.add(uname)
            except Exception:
                pass
            return local_usernames, local_bots

        # توزيع نطاقات offset_id على الحسابات المتاحة بشكل متوازٍ
        # offset_id يعني "ابدأ من الرسائل ذات المعرف أصغر من هذه القيمة"
        workers_accounts = accounts[:workers_count] if len(accounts) >= workers_count else accounts
        tasks = []
        cursor = top_id + 1
        remaining = effective_limit
        for i in range(len(workers_accounts)):
            count = min(chunk_size, remaining)
            if count <= 0:
                break
            tasks.append(_worker(workers_accounts[i][0], cursor, count))
            cursor -= chunk_size
            remaining -= count

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    continue
                local_usernames, local_bots = res
                unique_usernames |= local_usernames
                unique_bots |= local_bots

        return unique_usernames, unique_bots, True

    async def GETuser(self, GrobUser):
        """
        جلب أعضاء المجموعة وحفظهم في ملف MEMBERS_JSON
        مع تحديد عدد الرسائل وفقاً لقيمة limiting
        """
        accounts = database().accounts()
        random.shuffle(accounts)
        GrobUser, grob_link_type = resolve_group_ref(GrobUser)

        unique_admins = set()
        unique_bots = set()
        unique_usernames = set()

        # نجرب كل حساب واحداً تلو الآخر ونتخطى الحسابات التي تحدث بها مشاكل
        for session in accounts:
            session_string = session[0]
            try:
                async with Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_string, no_updates=True, in_memory=True, lang_code="ar"
                ) as app:
                    await asyncio.sleep(random.randint(3, 5))
                    # حل المعرف الرقمي (يدعم روابط الدعوة الخاصة وUserAlreadyParticipant)
                    GrobUser = await resolve_private_chat_id(app, GrobUser)

                    # جلب كل الأعضاء مرة واحدة وفرزهم
                    try:
                        
                        async for member in app.get_chat_members(GrobUser, limit=50000):
                            user = member.user
                            if not user or getattr(user, "is_deleted", False):
                                continue

                            # إذا لا يوجد يوزر نستخدم آيدي الحساب بدلاً منه (بدعم الإضافة بالآيدي)
                            uname = user.username if user.username else f"id:{user.id}"
                            # إذا كان بوت
                            if user.is_bot:
                                unique_bots.add(uname)
                            # إذا كان مشرفاً (مالك أو أدمن)
                            elif hasattr(member, "status") and member.status in (
                                enums.ChatMemberStatus.ADMINISTRATOR,
                                enums.ChatMemberStatus.OWNER
                            ):
                                unique_admins.add(uname)
                            # وإلا فهو عضو عادي
                            else:
                                unique_usernames.add(uname)

                    except errors.ChatAdminRequired:
                        # إذا ظهر هذا الخطأ نواصل دون قطيعة
                        pass

                # إذا نجح الحساب من دون استثناء، نوقف التجربة
                break

            except Exception:
                # تخطي هذا الحساب والمحاولة مع التالي
                continue

        # نحذف أي تكرار بين الأعضاء والمشرفين
        members_only = [u for u in unique_usernames if u not in unique_admins]

        # نضمن وجود الـ "@" في بداية كل اسم
    

        data = {
            "add_members": members_only,
            "admins": list(unique_admins),
            "bots": list(unique_bots)
        }

        try:
            with open(MEMBERS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as err:
            return f"<emoji id='5830085055376006187'>❌</emoji> خطأ في حفظ الملف {MEMBERS_JSON}:\n```shell\n{err}\n```"

        return members_only, list(unique_admins), list(unique_bots)

    async def GETuserhide(self, GrobUser, limit: int):
        """
        جلب أعضاء المجموعة وحفظهم في ملف MEMBERS_JSON
        مع تحديد عدد الرسائل وفقاً لقيمة limiting
        نسخة مسرّعة: تصفّح تاريخ الرسائل يتم بالتوازي بين عدة حسابات دفعة واحدة
        """
        accounts = database().accounts()
        random.shuffle(accounts)
        GrobUser, grob_link_type = resolve_group_ref(GrobUser)

        unique_admins = set()

        # حساب أساسي للانضمام وجلب قائمة المشرفين (عملية خفيفة، لا تحتاج توازي)
        joined_ok = False
        for session in accounts:
            session_string = session[0]
            try:
                async with Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_string, no_updates=True, in_memory=True, lang_code="ar"
                ) as app:
                    # حل المعرف الرقمي (UserAlreadyParticipant مُعالَج داخلياً)
                    primary_chat_ref = await resolve_private_chat_id(app, GrobUser)
                    async for member in app.get_chat_members(
                        primary_chat_ref,
                        filter=enums.ChatMembersFilter.ADMINISTRATORS
                    ):
                        user = member.user
                        if user and user.username:
                            unique_admins.add(user.username)
                joined_ok = True
                break
            except Exception:
                continue

        if not joined_ok:
            return [], [], []

        # الجزء الأبطأ (تصفّح تاريخ الرسائل) يتم الآن بالتوازي بين عدة حسابات لتسريع الاستخراج
        unique_usernames, unique_bots, _ = await self._scan_chat_history_parallel(
            GrobUser, limit, accounts, max_workers=16
        )

        members_only = [u for u in unique_usernames if u not in unique_admins]

        data = {
            "add_members": members_only,
            "admins": list(unique_admins),
            "bots": list(unique_bots)
        }

        try:
            with open(MEMBERS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as err:
            return f"<emoji id='5830085055376006187'>❌</emoji> خطأ في حفظ الملف {MEMBERS_JSON}:\n```shell\n{err}\n```"

        return members_only, list(unique_admins), list(unique_bots)
    async def GETusersavecontact(self, group_link: str, json_name: str, limit:int):
        # مسار ملف JSON حسب اسم الملف المدخل
        path = os.path.join(VCF_DIR, f"{json_name}.json")
        # تحضير بنية JSON الافتراضية
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"add_members": [], "admins": [], "bots": []}, f, ensure_ascii=False, indent=4)
    
        # جلب بيانات المجموعة
        accounts = database().accounts()
        random.shuffle(accounts)
        group_id, group_link_type = resolve_group_ref(group_link)
    
        unique_admins = set()
    
        # حساب أساسي للانضمام وجلب قائمة المشرفين (سريعة، لا تحتاج توازي)
        joined_ok = False
        for account in accounts:
            session_string = account[0]
            try:
                async with Client(
                    ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                    session_string=session_string, no_updates=True, in_memory=True, lang_code="ar"
                ) as app:
                    # حل المعرف الرقمي (UserAlreadyParticipant مُعالَج داخلياً)
                    primary_chat_ref = await resolve_private_chat_id(app, group_id)
                    try:
                        async for member in app.get_chat_members(primary_chat_ref, filter=enums.ChatMembersFilter.ADMINISTRATORS):
                            if member.user and member.user.username:
                                unique_admins.add(member.user.username)
                    except Exception:
                        pass
                joined_ok = True
                break
            except Exception:
                continue

        # الجزء الأبطأ (تصفّح تاريخ الرسائل) بالتوازي بين عدة حسابات لتسريع الاستخراج
        if joined_ok:
            unique_usernames, unique_bots, _ = await self._scan_chat_history_parallel(
                group_id, limit, accounts, max_workers=16
            )
        else:
            unique_usernames, unique_bots = set(), set()
    
        members_only = [u for u in unique_usernames if u not in unique_admins]
    
        # تحميل ودمج البيانات القديمة مع الجديدة
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except:
            existing = {"add_members": [], "admins": [], "bots": []}
    
        data = {
            "add_members": list(set(existing.get("add_members", [])) | set(members_only)),
            "admins": list(set(existing.get("admins", [])) | unique_admins),
            "bots": list(set(existing.get("bots", [])) | unique_bots)
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    
        return data["add_members"]
    
    
    MAX_MESSAGE_LENGTH = 4096
    def chunk_text(text, limit=MAX_MESSAGE_LENGTH):
        """
        يقسم النص إلى أجزاء لا تتجاوز الحدود المحددة.
        """
        for i in range(0, len(text), limit):
            yield text[i:i+limit]
    async def joinbar(self, client, message):
        # جلب الحسابات وترتيبها عشوائياً
        accounts = database().accounts()
        random.shuffle(accounts)
    
        # استخراج معرف القروب
        group_id, group_link_type = resolve_group_ref(message.text)
        if not accounts:
            return await abot.send_message(chat_id=message.chat.id, text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا يوجد حسابات متاحة للانضمام!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    
        success = 0
        failed = 0
        join_errors = []  # لتجميع تفاصيل الأخطاء
    
        for session_string, account_name in accounts:
            try:
                async with Client(
                    "::memory::",
                    api_id=Config.APP_ID,
                    api_hash=Config.API_HASH,
                    no_updates=True,
                    in_memory=True,
                    lang_code="ar",
                    session_string=session_string
                ) as app:
                    await app.join_chat(group_id)
                    success += 1

            except errors.UserAlreadyParticipant:
                # الحساب منضم مسبقاً — يُعدّ نجاحاً ولا داعي لإشعار الأدمن
                success += 1
    
            except (FloodWait, PeerFlood):
                failed += 1
                # الحظر المؤقت بسبب Flood wait أو PeerFlood
                await message.reply(f"الحساب: {account_name} محظور مؤقتا")
    
            except InviteRequestSent:
                failed += 1
                # في حال كانت المجموعة تحتاج موافقة (طلب انضمام)
                await message.reply(f"تم ارسال طلب انضمام بالحساب: {account_name}")
    
            except (UserBannedInChannel, ChannelInvalid):
                failed += 1
                # الحساب محظور من المجموعة
                await message.reply(f"الحساب: {account_name} قد يكون محظور من القروب")
    
            except RPCError:
                failed += 1
                # أي خطأ آخر من API تلجرام نجمعه للمعاينة لاحقاً
                err = traceback.format_exc()
                join_errors.append(f"# الحساب: {account_name}\\n# الخطأ:\n```shell\n{err}\n```")
    
            # تأخير متغير بين كل محاولة
            await asyncio.sleep(random.uniform(1, 3))
        if join_errors:
            formatted = "\n".join(join_errors)
            for part in Custem.chunk_text(formatted):
                await message.reply(f"```shell\n{part}\n```")
        # إرسال ملخص النتائج
        await abot.send_message(chat_id=message.chat.id, text=
            f"<emoji id='5830243638453477409'>✅</emoji> تم انضمام {success} حساب بنجاح! <tg-emoji emoji-id='5372917041193828849'>🚀</tg-emoji>\n"
            f"<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> فشل {failed} حساب.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    async def leavebar(self, client, message):
        # جلب الحسابات وترتيبها عشوائياً
        accounts = database().accounts()
        random.shuffle(accounts)
    
        # استخراج معرف القروب
        group_id, group_link_type = resolve_group_ref(message.text)
        if not accounts:
            return await abot.send_message(chat_id=message.chat.id, text=
                "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا يوجد حسابات متاحة للمغادرة!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=
                    [[back_btn()], [source_btn()]]
                ),
        parse_mode=enums.ParseMode.HTML)
    
        success = 0
        failed = 0
        leave_errors = []  # لتجميع تفاصيل الأخطاء
    
        for session_string, account_name in accounts:
            try:
                async with Client(
                    "::memory::",
                    api_id=Config.APP_ID,
                    api_hash=Config.API_HASH,
                    no_updates=True,
                    in_memory=True,
                    lang_code="ar",
                    session_string=session_string
                ) as app:
                    # نحل المعرف الرقمي للمجموعة قبل المغادرة
                    # (ضروري للمجموعات الخاصة لأن leave_chat لا يقبل رابط الدعوة)
                    chat_ref = group_id
                    if group_link_type == "private":
                        chat_ref = await resolve_private_chat_id(app, group_id)
                    await app.leave_chat(chat_ref)
                    success += 1
    
            except (FloodWait, PeerFlood):
                failed += 1
                # الحظر المؤقت بسبب Flood wait أو PeerFlood
                await message.reply(f"الحساب: {account_name} محظور مؤقتا")
    
            except (UserNotParticipant, UserBannedInChannel):
                failed += 1
                # في حال أن الحساب غير موجود أو محظور من المجموعة
                await message.reply(f"الحساب: {account_name} غير موجود في القروب")
    
            except RPCError:
                failed += 1
                # أي خطأ آخر من API تلجرام نجمعه للمعاينة لاحقاً
                err = traceback.format_exc()
                await message.reply(f"# الحساب: {account_name}\n# الخطأ:\n```shell\n{err}\n```")
    
            # تأخير متغير بين كل محاولة
            await asyncio.sleep(random.uniform(1, 3))
    
        # إرسال ملخص النتائج
        await abot.send_message(chat_id=message.chat.id, text=
            f"<tg-emoji emoji-id='5294438969264599086'>👋</tg-emoji> تم مغادرة {success} حساب بنجاح!\n"
            f"<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> فشل {failed} حساب.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            ),
        parse_mode=enums.ParseMode.HTML)

    async def get_users_con(self) -> list:
        """
        جلب الأعضاء من جميع ملفات JSON في مجلد VCF_DIR.
        """
        all_usernames = []
        for filename in os.listdir(VCF_DIR):
            if filename.lower().endswith('.json'):
                path = os.path.join(VCF_DIR, filename)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        all_usernames.extend(data.get('add_members', []))
                except Exception:
                    continue
        seen = set()
        return [u for u in all_usernames if u and not (u in seen or seen.add(u))]
    
    async def add_contacts(self, call):
        """
        إضافة ملف جهات اتصال واحد لكل حساب بالترتيب:
        الحساب الأول يستخدم الملف الأول، والثاني الملف الثاني، وهكذا.
        وإذا انتهت الملفات قبل الحسابات، يُرسل رسالة خطأ ويتوقف.
        """
        # تعديل الرسالة لبدء العملية
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text='<emoji id="5445350406215465190">⏳</emoji> جاري إضافة جهات الاتصال إلى الحسابات...',
        parse_mode=enums.ParseMode.HTML)
    
        # جلب وترتيب ملفات الأعضاء
        files = [f for f in sorted(os.listdir(VCF_DIR)) if f.lower().endswith('.json')]
        if not files:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text='<tg-emoji emoji-id="5830085055376006187">❌</tg-emoji> لا توجد ملفات أو بيانات لإضافتها.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=
                    [[back_btn()], [source_btn()]]
                ),
        parse_mode=enums.ParseMode.HTML)
    
        # جلب الحسابات من قاعدة البيانات
        accounts = database().accounts()
        if not accounts:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text='<tg-emoji emoji-id="5830085055376006187">❌</tg-emoji> لا توجد حسابات متاحة.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=
                    [[back_btn()], [source_btn()]]
                ),
        parse_mode=enums.ParseMode.HTML)
    
        total_contacts = 0
        total_failures = 0
        file_counter = 0  # مؤشر على الملف الجاري استخدامه
    
        # لكل حساب، استخدم ملفًا مختلفًا
        for session_string, account_label in accounts:
            # إذا نفدت الملفات قبل الحسابات
            if file_counter >= len(files):
                await bot.send_message(
                    chat_id=call.message.chat.id,
                    text='<emoji id="5830085055376006187">❌</emoji> نفذت ملفات الأعضاء، قم بتخزين أو إضافة ملفات.',
        parse_mode=enums.ParseMode.HTML)
                break
    
            # مسار الملف الحالي
            file_path = os.path.join(VCF_DIR, files[file_counter])
            file_counter += 1
    
            # تحميل الأعضاء من الملف على دفعات
            try:
                users = []
                for chunk in iter_member_chunks(file_path, chunk_size=1000):
                    users.extend(chunk)
            except Exception:
                # إذا كان الملف تالفًا، ننتقل للملف التالي دون عد الحساب
                continue
    
            contacts_added = 0
            failures = 0
    
            # إضافة الأعضاء من ذلك الملف (حتى 300)
            try:
                async with Client(
                    "::memory::",
                    api_id=Config.APP_ID,
                    api_hash=Config.API_HASH,
                    no_updates=True,
                    in_memory=True,
                    lang_code="ar",
                    session_string=session_string
                ) as app:
                    for idx, username in enumerate(users[:300], start=1):
                        contact_name=f"عضو نقل {idx}"
                        try:
                            # وقت انتظار قصير لتجنب الفيض
                            await asyncio.sleep(random.randint(4, 7))
                            await app.add_contact(username, first_name=contact_name)
                            total_contacts += 1
                            contacts_added += 1
                        except Exception:
                            failures += 1
                            total_failures += 1
                            continue
            except Exception:
                # إذا فشل الاتصال بالحساب، ننتقل للحساب التالي
                continue
    
            # إرسال ملخص عن الحساب
            await asyncio.sleep(random.randint(4, 7))
            await bot.send_message(
                chat_id=call.message.chat.id,
                text=(
                    f'<emoji id="5830243638453477409">✅</emoji> حساب {account_label}: أُضيف {contacts_added} جهات اتصال، '
                    f'وفشل {failures}'
                ),
        parse_mode=enums.ParseMode.HTML)
    
        # رسالة ختامية بالإجمالي بعد الانتهاء
        await bot.send_message(
            chat_id=call.message.chat.id,
            text=(
                f'<emoji id="5830243638453477409">✅</emoji> المجموع الكلي: تم إضافة {total_contacts} جهة اتصال، '
                f'وفشل إضافة {total_failures} جهة عبر {file_counter} حسابات.'
            ),
        parse_mode=enums.ParseMode.HTML)

    async def clear_contacts(self, client, call):
        """
        حذف كل جهات الاتصال من كل الحسابات.
        تعديل الرسالة لبداية العملية، ثم رسالة نهائية.
        """
    
        # تعديل الرسالة لبداية عملية الحذف
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text='<emoji id="5445350406215465190">⏳</emoji> انتظر، سيتم حذف جميع جهات الاتصال من الحسابات...',
        parse_mode=enums.ParseMode.HTML)
    
        # جلب الحسابات من قاعدة البيانات
        accounts = database().accounts()
        if not accounts:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text='<tg-emoji emoji-id="5830085055376006187">❌</tg-emoji> لا توجد حسابات متاحة.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=
                    [[back_btn()], [source_btn()]]
                ),
        parse_mode=enums.ParseMode.HTML)
    
        deleted_accounts = 0
    
        # لكل حساب، حذف جميع جهات الاتصال
        for session_string, account_label in accounts:
            try:
                async with Client(
                    # نستخدم session_string مباشرة كاسم الجلسة حتى يحمل بيانات الجلسة
                    "::memory::",
                    api_id=Config.APP_ID,
                    api_hash=Config.API_HASH,
                    no_updates=True,
                    in_memory=True,
                    lang_code="ar",
                    session_string=session_string
                ) as app:
                    # نجلب جميع جهات الاتصال
                    users = await app.get_contacts()
                    # نحضر قائمة بكل user.id
                    user_ids = [user.id for user in users]
                    if user_ids:
                        # نحذفهم دفعة واحدة
                        await app.delete_contacts(user_ids)
                deleted_accounts += 1
    
            except Exception as e:
                # من الأفضل تسجيل الخطأ للمراجعة لاحقاً
                bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text=f'فشل حذف جهات الاتصال في الحساب {account_label}: {e}')
                continue
    
        # رسالة نهائية
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=f'<tg-emoji emoji-id="5830243638453477409">✅</tg-emoji> تم حذف جميع جهات الاتصال من {deleted_accounts} حساب.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            ),
        parse_mode=enums.ParseMode.HTML)

@bot.on_message(filters.command("sub") & filters.private & filters.user(Config.DEV_IDS))
async def subscription_command(client, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.reply_text("الاستخدام الصحيح: /sub يوم/شهر/سنة\nمثال: /sub 29/08/2026", parse_mode=enums.ParseMode.HTML)
    try:
        expiry = parse_subscription_date(parts[1])
    except ValueError:
        return await message.reply_text("التاريخ غير صالح. استخدم الصيغة: يوم/شهر/سنة، مثال: 29/08/2026", parse_mode=enums.ParseMode.HTML)
    if expiry <= datetime.now(LIBYA_TZ):
        return await message.reply_text("لا يمكن ضبط تاريخ منتهٍ أو تاريخ اليوم.", parse_mode=enums.ParseMode.HTML)
    save_subscription_state({"expires_at": expiry.isoformat(), "sent_alerts": []})
    return await message.reply_text(
        f"تم ضبط موعد انتهاء الاشتراك على <code>{expiry.strftime('%d/%m/%Y %H:%M')}</code> بتوقيت ليبيا (UTC+2).\n"
        f"سيتم إيقاف البوت فقط عند الموعد، دون حذف البيانات.\n"
        f"للتجديد: {SUBSCRIPTION_DEV_CONTACT}", parse_mode=enums.ParseMode.HTML)

@bot.on_message(filters.command('start') & filters.private)
async def admin(client, message):
    user_id = message.from_user.id
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return

    # رابط الحسابات الآمن: لا يحتوي الرقم أو الجلسة، بل رمزاً عشوائياً مؤقتاً.
    start_payload = message.command[1] if len(message.command) > 1 else ""
    if start_payload.startswith("acct_"):
        link_key = start_payload[5:]
        account_data = ACCOUNT_LINK_CACHE.get(link_key)
        if (not account_data or account_data.get("owner_id") != user_id
                or time.time() > account_data.get("expires_at", 0)):
            ACCOUNT_LINK_CACHE.pop(link_key, None)
            return await abot.send_message(
                chat_id=message.chat.id,
                text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> الرابط غير صالح أو انتهت صلاحيته.",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode=enums.ParseMode.HTML)

        return await abot.send_message(
            chat_id=message.chat.id,
            text="<tg-emoji emoji-id='6206077285720659346'><tg-emoji emoji-id='6206077285720659346'>⚠</tg-emoji>️</tg-emoji> هذا الرابط سيعرض جلسة الحساب الحساسة. هل تريد المتابعة؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="👁 عرض الجلسة", callback_data=f"reveal_account:{link_key}", style="danger"),
                InlineKeyboardButton(text="إلغاء", callback_data="cancel_reveal", style="primary")
            ]]),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode=enums.ParseMode.HTML)

    if True:
        inline_keyboard = get_main_keyboard()
        
        # إرسال رسالة الترحيب مع الأزرار للمطور/الأدمن
        fname = message.from_user.first_name or message.from_user.username or "المستخدم"
        
        await abot.send_message(
            chat_id=message.chat.id,
            text=(
                f"<tg-emoji emoji-id='5436237035369154060'>👋</tg-emoji> مرحبا بك "
                f"عزيزي {fname} في بوت نقل الأعضاء المتطور V5.7 Pro !\n\n"
                f"قناة السورس<tg-emoji emoji-id='5233546443360318055'>⚡</tg-emoji><tg-emoji emoji-id='5397857289216484878'>⚡</tg-emoji> "
                f"<a href='https://t.me/a990099aa'>سورس 𝑹𝑶𝑺𝑺𝑬</a>\n\n\n"
                f"اختر من الأزرار في الاسفل "
                f"<tg-emoji emoji-id='5328306457135821693'>👇</tg-emoji>"
            ),
            reply_markup=inline_keyboard,
            parse_mode=enums.ParseMode.HTML
        )
    else:
        # للمستخدمين غير المصرح لهم، نرسل رسالة واحدة فقط ولا نسمح بمرور أي شيء آخر
        await client.send_message(
            message.chat.id,
            "عذراً البوت ليس لك لا يمكنك لاستخدامه \n"
            "*ـــــــــــــــــــــــــــــــــــــــــــــــــــــــــ*\n\n"
            "لطلب بوت مشابه قم بالتواصل مع المطور (@qv_x1)"
        )
        message.stop_propagation()
async def send_resume_prompt():
    state = load_operation_progress()
    if not state:
        return
    await abot.send_message(
        chat_id=Config.OWNER_ID,
        text=progress_summary(state),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ استمرار العملية", callback_data="resume_previous_operation", style="success", icon_custom_emoji_id="5830238823795136938")],
            [InlineKeyboardButton(text="🗑 تجاهل العملية السابقة", callback_data="discard_previous_operation", style="danger", icon_custom_emoji_id="5904542823167824187")],
        ]),
        parse_mode=enums.ParseMode.HTML)


async def resume_previous_operation():
    state = load_operation_progress()
    if not state:
        return False
    remaining = max(0, int(state.get("requested_count", 0)) - int(state.get("total_added", 0)))
    if remaining <= 0:
        clear_operation_progress()
        return False
    operation = state.get("operation")
    if operation == "contacts":
        await Custem().add_users_contact(state.get("target"), bot, remaining, resume_state=state)
    elif operation == "add_users_hide":
        await Custem().ADDuserhide([], state.get("source"), state.get("target"), bot, remaining, resume_state=state)
    elif operation == "add_users":
        await Custem().ADDuser(state.get("source"), state.get("target"), bot, remaining, resume_state=state)
    else:
        return False
    return True


async def send_operation_preview(chat_id, kind, target, count, source="ملف الأعضاء"):
    PENDING_OPERATIONS[chat_id] = {"kind": kind, "target": target, "count": int(count), "source": source, "account_mode": None, "healthy_labels": []}
    text = (
        "<tg-emoji emoji-id='5285533062518566401'>📋</tg-emoji> <b>معاينة العملية قبل التنفيذ</b>\n\n"
        f"المصدر: {source}\n"
        f"الهدف: {target}\n"
        f"العدد المطلوب: {int(count)} عضو\n"
        f"عدد الحسابات المتاحة: {len(database().accounts())}\n\n"
        "تأكد من البيانات قبل بدء العملية."
    )
    return await abot.send_message(
        chat_id=chat_id, text=text, parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 السحب بالحسابات السليمة فقط", callback_data="select_accounts_healthy", style="success", icon_custom_emoji_id="5830243638453477409")],
            [InlineKeyboardButton(text="🔵 السحب بكل الحسابات", callback_data="select_accounts_all", style="primary", icon_custom_emoji_id="5467904284509085470")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_pending_operation", style="danger", icon_custom_emoji_id="5904542823167824187")],
        ]))


async def select_operation_accounts(call, mode):
    pending = PENDING_OPERATIONS.get(call.message.chat.id)
    if not pending:
        return await call.answer("لا توجد عملية معلقة", show_alert=True)
    if mode == "healthy":
        await call.answer("جارٍ فحص الحسابات قبل المتابعة...")
        accounts = database().accounts()
        checks = await asyncio.gather(
            *[check_account_health(session, label) for session, label in accounts],
            return_exceptions=True
        )
        healthy = []
        for result in checks:
            if not isinstance(result, Exception) and result[0] == "healthy":
                healthy.append(result[1])
        if not healthy:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> <b>لم يتم العثور على حسابات سليمة</b>\n\nتم إلغاء الاختيار لتجنب بدء العملية بحسابات غير مؤكدة.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ العودة للمعاينة", callback_data="back_to_account_choice", style="primary")]]),
                parse_mode=enums.ParseMode.HTML)
        pending["account_mode"] = "healthy"
        pending["healthy_labels"] = healthy
        mode_text = f"الحسابات السليمة فقط ({len(healthy)} حساب)"
    else:
        pending["account_mode"] = "all"
        pending["healthy_labels"] = []
        mode_text = f"كل الحسابات ({len(database().accounts())} حساب)"
    return await abot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.id,
        text=("<tg-emoji emoji-id='5397857289216484878'>⚡️</tg-emoji> <b>تأكيد إعدادات السحب</b>\n\n"
              f"المصدر: {pending['source']}\nالهدف: {pending['target']}\n"
              f"العدد المطلوب: {pending['count']} عضو\n"
              f"طريقة الحسابات: <b>{mode_text}</b>\n\nهل تريد بدء السحب الآن؟"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأكيد وبدء السحب", callback_data="confirm_pending_operation", style="success", icon_custom_emoji_id="5830243638453477409")],
            [InlineKeyboardButton(text="↩️ تغيير طريقة الحسابات", callback_data="back_to_account_choice", style="primary")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_pending_operation", style="danger", icon_custom_emoji_id="5904542823167824187")]
        ]),
        parse_mode=enums.ParseMode.HTML)


async def confirm_pending_operation(client, call):
    global ACTIVE_ACCOUNT_LABELS
    pending = PENDING_OPERATIONS.pop(call.message.chat.id, None)
    if not pending:
        await call.answer("لا توجد عملية معلقة", show_alert=True)
        return
    if pending.get("account_mode") not in {"healthy", "all"}:
        PENDING_OPERATIONS[call.message.chat.id] = pending
        return await call.answer("اختر طريقة استخدام الحسابات أولاً", show_alert=True)
    ACTIVE_ACCOUNT_LABELS = set(pending.get("healthy_labels", [])) if pending.get("account_mode") == "healthy" else None
    await call.answer("تم التأكيد، بدأت العملية")
    await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5445350406215465190'>⏳</tg-emoji> تم التأكيد، جارٍ بدء العملية...", parse_mode=enums.ParseMode.HTML)
    if pending["kind"] == "contacts":
        await Custem().add_users_contact(pending["target"], bot, pending["count"])
    elif pending["kind"] == "file_hide":
        await Custem().add_users_hide(pending["target"], bot, pending["count"])
    elif pending["kind"] == "hide":
        await Custem().ADDuserhide([], pending["source"], pending["target"], bot, pending["count"])
    else:
        await Custem().ADDuser(pending["source"], pending["target"], bot, pending["count"])
    ACTIVE_ACCOUNT_LABELS = None


async def safe_edit_message_text(**kwargs):
    """يعدل الرسالة ويتجاهل فقط حالة كون المحتوى مطابقاً للحالي."""
    try:
        return await abot.edit_message_text(**kwargs)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return None
        raise

async def request_sensitive_confirmation(client, message, action):
    token = secrets.token_urlsafe(10).replace("-", "").replace("_", "")
    PENDING_CONFIRMATIONS[token] = {"action": action, "message": message, "owner_id": message.from_user.id, "expires_at": time.time() + 180}
    labels = {"join": "الانضمام إلى القروب", "leave": "مغادرة القروب", "clear_contacts": "حذف جهات الاتصال", "restore": "استعادة النسخة الاحتياطية", "emergency": "الإيقاف الطارئ الشامل", "delete_all": "حذف جميع الحسابات"}
    await bot.send_message(message.chat.id, f"<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> <b>تأكيد الإجراء</b>\n\nهل تريد تنفيذ: <b>{labels[action]}</b>؟", reply_markup=PyroInlineKeyboardMarkup([[PyroInlineKeyboardButton("نعم، تنفيذ", callback_data=f"confirm_sensitive:{token}"), PyroInlineKeyboardButton("إلغاء", callback_data="cancel_sensitive")]]), parse_mode=enums.ParseMode.HTML)

async def call_sensitive_action(client, call, token):
    pending = PENDING_CONFIRMATIONS.pop(token, None)
    if not pending or pending["owner_id"] != call.from_user.id or time.time() > pending["expires_at"]:
        # الضغط المتكرر على زر قديم لا يجب أن يعرض تنبيهاً مزعجاً للمستخدم.
        return await call.answer("تمت معالجة التأكيد مسبقاً أو انتهت صلاحيته", show_alert=False)
    action = pending["action"]
    if action in {"delete_all", "restore", "emergency"} and not pending.get("level2"):
        token2 = secrets.token_urlsafe(10).replace("-", "").replace("_", "")
        pending["level2"] = True
        PENDING_CONFIRMATIONS[token2] = pending
        labels = {"delete_all": "حذف جميع الحسابات", "restore": "استعادة النسخة الاحتياطية", "emergency": "الإيقاف الطارئ الشامل"}
        await call.answer("يلزم تأكيد إضافي", show_alert=True)
        return await bot.edit_message_text(
            call.message.chat.id,
            call.message.id,
            f"<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> <b>تأكيد نهائي</b>\\n\\nهذا إجراء عالي الخطورة: <b>{labels[action]}</b>\\nهل تريد المتابعة فعلاً؟",
            reply_markup=PyroInlineKeyboardMarkup([[
                PyroInlineKeyboardButton("نعم، تأكيد نهائي", callback_data=f"confirm_sensitive:{token2}"),
                PyroInlineKeyboardButton("إلغاء", callback_data="cancel_sensitive")
            ]]), parse_mode=enums.ParseMode.HTML)
    await call.answer("تم التأكيد")
    if action == "join": return await Custem().joinbar(client, pending["message"])
    if action == "leave": return await Custem().leavebar(client, pending["message"])
    if action == "clear_contacts": return await Custem().clear_contacts(client, call)
    if action == "restore":
        added, skipped = restore_encrypted_backup()
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=f"<tg-emoji emoji-id='5830238823795136938'>♻️</tg-emoji> <b>تمت الاستعادة</b>\n\nتمت الإضافة: {added}\nتم تجاهله كمكرر: {skipped}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    if action == "delete_all":
        database().RemoveAllAccounts()
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<emoji id='5830243638453477409'>✅</emoji> تم حذف جميع الحسابات بنجاح.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    if action == "emergency": return await handle_emergency_stop(call)

async def run_full_health_check():
    results = []
    # Database integrity and WAL/read-write check.
    try:
        with sqlite3.connect(os.path.join(DATABASE_DIR, "data.db"), timeout=10) as conn:
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("CREATE TABLE IF NOT EXISTS __health_check (id INTEGER PRIMARY KEY, checked_at TEXT)")
            conn.execute("INSERT INTO __health_check (checked_at) VALUES (?)", (datetime.now(timezone.utc).isoformat(),))
            conn.execute("DELETE FROM __health_check WHERE id NOT IN (SELECT id FROM __health_check ORDER BY id DESC LIMIT 1)")
            conn.commit()
        results.append(("قاعدة البيانات", integrity == "ok", f"SQLite integrity={integrity}, journal={journal}"))
    except Exception as exc:
        results.append(("قاعدة البيانات", False, type(exc).__name__))
    # Required directories and writable probe.
    for label, folder in (("مجلد الملفات", VCF_DIR), ("مجلد قاعدة البيانات", DATABASE_DIR)):
        try:
            os.makedirs(folder, exist_ok=True)
            probe=os.path.join(folder, ".health_probe.tmp")
            with open(probe, "w", encoding="utf-8") as f: f.write("ok")
            os.remove(probe)
            results.append((label, True, "موجود وقابل للكتابة"))
        except Exception as exc:
            results.append((label, False, type(exc).__name__))
    # JSON state files.
    for label, path in (("حالة الاشتراك", SUBSCRIPTION_FILE), ("إعدادات البوت", SETTINGS_JSON)):
        if not os.path.exists(path):
            results.append((label, True, "غير منشأ بعد"))
            continue
        try:
            with open(path, "r", encoding="utf-8") as f: json.load(f)
            results.append((label, True, "JSON سليم"))
        except Exception as exc:
            results.append((label, False, type(exc).__name__))
    # Encrypted backup verification, without exposing session contents.
    try:
        exists=os.path.exists(ENCRYPTED_BACKUP_FILE)
        ok=verify_encrypted_backup(ENCRYPTED_BACKUP_FILE) if exists else True
        results.append(("النسخة الاحتياطية المشفرة", ok, "سليمة" if exists and ok else ("غير موجودة بعد" if not exists else "غير صالحة")))
    except Exception as exc:
        results.append(("النسخة الاحتياطية المشفرة", False, type(exc).__name__))
    # Saved operation state consistency.
    try:
        state=load_operation_progress()
        results.append(("حالة العمليات", True, "لا توجد عملية معلقة" if not state else "توجد عملية قابلة للاستئناف"))
    except Exception as exc:
        results.append(("حالة العمليات", False, type(exc).__name__))
    # Account health checks are safe read-only checks and run concurrently.
    try:
        accounts=await async_db.accounts()
        checks=await asyncio.gather(*[check_account_health(session,label) for session,label in accounts], return_exceptions=True)
        healthy=sum(1 for item in checks if not isinstance(item, Exception) and item[0] == "healthy")
        temporary=sum(1 for item in checks if not isinstance(item, Exception) and item[0] == "temp_banned")
        invalid=sum(1 for item in checks if not isinstance(item, Exception) and item[0] == "invalid_session")
        unconfirmed=len(accounts)-healthy-temporary-invalid
        status_ok=(temporary == 0 and invalid == 0)
        results.append(("الحسابات والجلسات", status_ok or not accounts, f"سليمة: {healthy} | مؤقت: {temporary} | جلسات ملغاة: {invalid} | غير محسوم: {unconfirmed}"))
    except Exception as exc:
        results.append(("الحسابات والجلسات", False, type(exc).__name__))
    return results

@bot.on_callback_query()
async def call_handler(client, call):
    user_id = call.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم استخدام الأزرار
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        # عرض رسالة في صندوق الحوار
        await call.answer(
            "عذرا البوت ليس لك لا يمكنك استخدامه\n"
            "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
            "لطلب بوت مشابه قم بالتواصل مع المطور: @qv_x1",
            show_alert=True
        )
        pass
    data = call.data
    if data.startswith("confirm_sensitive:"):
        return await call_sensitive_action(client, call, data.split(":", 1)[1])
    if data == "cancel_sensitive":
        await call.answer("تم الإلغاء")
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="تم إلغاء الإجراء.", parse_mode=enums.ParseMode.HTML)
    if data == "emergency_stop_all":
        return await request_sensitive_confirmation(client, call.message, "emergency")
    if data == "cancel_reveal":
        await call.answer("تم الإلغاء")
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="تم إلغاء عرض الجلسة.", parse_mode=enums.ParseMode.HTML)
    if data.startswith("reveal_account:"):
        link_key = data.split(":", 1)[1]
        account_data = ACCOUNT_LINK_CACHE.get(link_key)
        if (not account_data or account_data.get("owner_id") != call.from_user.id
                or time.time() > account_data.get("expires_at", 0)):
            ACCOUNT_LINK_CACHE.pop(link_key, None)
            return await call.answer("انتهت صلاحية الرابط أو أنه غير صالح", show_alert=True)
        session_text = f"Session : {account_data['session']}"
        session_offset = len("Session : ")
        await call.answer("تم عرض الجلسة")
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=session_text,
            entities=[MessageEntity(type="blockquote", offset=0, length=len(session_text)), MessageEntity(type="spoiler", offset=session_offset, length=len(account_data["session"]))],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 نسخ الرقم", copy_text=CopyTextButton(text=account_data["number"]))]]))
    if data.startswith("show_session:"):
        session_key = data.split(":", 1)[1]
        session_data = SESSION_COPY_CACHE.get(session_key)
        if not session_data:
            return await call.answer("انتهت صلاحية زر الجلسة، افتح قائمة الحسابات من جديد", show_alert=True)
        await call.answer("أرسلت الرقم والجلسة بصيغة قابلة للنسخ", show_alert=True)
        return await abot.send_message(
            chat_id=call.message.chat.id,
            text=(f"<b>Number :</b> <code>{session_data['number']}</code>\n"
                  f"<b>Session :</b> <code>{session_data['session']}</code>"),
            parse_mode=enums.ParseMode.HTML)

    if data == "select_accounts_healthy":
        return await select_operation_accounts(call, "healthy")
    if data == "select_accounts_all":
        return await select_operation_accounts(call, "all")
    if data == "back_to_account_choice":
        pending = PENDING_OPERATIONS.get(call.message.chat.id)
        if not pending:
            return await call.answer("لا توجد عملية معلقة", show_alert=True)
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<tg-emoji emoji-id='5285533062518566401'>📋</tg-emoji> <b>اختر الحسابات قبل التأكيد النهائي</b>\n\nحدد هل تريد استخدام الحسابات السليمة فقط بعد فحصها، أم جميع الحسابات.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🟢 السحب بالحسابات السليمة فقط", callback_data="select_accounts_healthy", style="success", icon_custom_emoji_id="5830243638453477409")],
                [InlineKeyboardButton(text="🔵 السحب بكل الحسابات", callback_data="select_accounts_all", style="primary", icon_custom_emoji_id="5467904284509085470")],
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_pending_operation", style="danger", icon_custom_emoji_id="5904542823167824187")]
            ]),
            parse_mode=enums.ParseMode.HTML)
    if data == "cancel_pending_operation":
        PENDING_OPERATIONS.pop(call.message.chat.id, None)
        await call.answer("تم إلغاء العملية")
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> تم إلغاء العملية قبل البدء.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    if data == "confirm_pending_operation":
        return await confirm_pending_operation(client, call)

    if data == "discard_previous_operation":
        clear_operation_progress()
        await call.answer("تم تجاهل العملية السابقة", show_alert=True)
        return await safe_edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<emoji id='5830243638453477409'>✅</emoji> تم تجاهل العملية السابقة وحذف حالة التقدم.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
            parse_mode=enums.ParseMode.HTML)

    if data == "resume_previous_operation":
        await call.answer("جارٍ استئناف العملية من آخر نقطة محفوظة")
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<tg-emoji emoji-id='5445350406215465190'>⏳</tg-emoji> جارٍ استئناف العملية السابقة...",
            parse_mode=enums.ParseMode.HTML)
        try:
            resumed = await resume_previous_operation()
            if not resumed:
                await abot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.id,
                    text="ℹ️ لا توجد عملية قابلة للاستئناف.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                    parse_mode=enums.ParseMode.HTML)
        except Exception as resume_error:
            await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text=f"<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> تعذر استئناف العملية:\n<code>{resume_error}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                parse_mode=enums.ParseMode.HTML)
        return

    # توحيد callback القديم مع زر القائمة الرئيسية.
    if data == "back":
        data = "back_main"
    # الرجوع الديناميكي إلى آخر قسم مفتوح، مع زر رئيسية مستقل.
    if data == "back_previous":
        data = USER_MENU_STATE.get(user_id, "main_menu")
    if data in ("main_menu", "back_main"):
        USER_MENU_STATE[user_id] = "main_menu"
        fname = call.from_user.first_name or call.from_user.username or "المستخدم"
        return await abot.edit_message_text(
            chat_id=call.message.chat.id, message_id=call.message.id,
            text=(f"<tg-emoji emoji-id='5436237035369154060'>👋</tg-emoji> مرحبا بك عزيزي {fname} في بوت نقل الأعضاء المتطور V5.7 Pro !\n\n"
                  f"قناة السورس<tg-emoji emoji-id='5233546443360318055'>⚡</tg-emoji><tg-emoji emoji-id='5397857289216484878'>⚡</tg-emoji> <a href='https://t.me/a990099aa'>سورس 𝑹𝑶𝑺𝑺𝑬</a>\n\n\n"
                  "اختر من الأزرار في الاسفل <tg-emoji emoji-id='5328306457135821693'>👇</tg-emoji>"),
            reply_markup=get_main_keyboard(), parse_mode=enums.ParseMode.HTML)
    if data == "back_accounts":
        USER_MENU_STATE[user_id] = "menu_accounts"
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id,
            text="<tg-emoji emoji-id='5467904284509085470'>👤</tg-emoji> <b>إدارة الحسابات</b>\nاختر العملية المطلوبة:", reply_markup=get_accounts_keyboard(), parse_mode=enums.ParseMode.HTML)
    # قوائم الأقسام الرئيسية
    if data == "menu_accounts":
        USER_MENU_STATE[user_id] = "menu_accounts"
        await safe_edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5467904284509085470'>👤</tg-emoji> <b>إدارة الحسابات</b>\nاختر العملية المطلوبة:", reply_markup=get_accounts_keyboard(), parse_mode=enums.ParseMode.HTML)
        return
    elif data == "menu_withdraw":
        USER_MENU_STATE[user_id] = "menu_withdraw"
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5465213874045199656'>📥</tg-emoji> <b>إدارة السحب</b>\nاختر طريقة سحب الأعضاء:", reply_markup=get_withdraw_keyboard(), parse_mode=enums.ParseMode.HTML)
        return
    elif data == "menu_contacts":
        USER_MENU_STATE[user_id] = "menu_contacts"
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5467772583631921166'>👥</tg-emoji> <b>إدارة الجهات</b>\nإدارة جهات الاتصال جماعياً:", reply_markup=get_contacts_keyboard(), parse_mode=enums.ParseMode.HTML)
        return
    elif data == "menu_groups":
        USER_MENU_STATE[user_id] = "menu_groups"
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='6206508629286196237'>🔔</tg-emoji> <b>إدارة الانضمام والتصفية</b>\nإدارة الانضمام والمغادرة من القروبات:", reply_markup=get_groups_keyboard(), parse_mode=enums.ParseMode.HTML)
        return
    elif data == "menu_files":
        USER_MENU_STATE[user_id] = "menu_files"
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5895226403447642077'>☁️</tg-emoji> <b>إدارة التخزين والملفات</b>\nحفظ ورفع واستخراج ملفات الأعضاء:", reply_markup=get_files_keyboard(), parse_mode=enums.ParseMode.HTML)
        return
    elif data == "menu_reports":
        USER_MENU_STATE[user_id] = "menu_reports"
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<emoji id='6206343625232619150'>📊</emoji> <b>إدارة التقارير</b>\nالفحص والتقارير المتاحة حالياً:", reply_markup=get_reports_keyboard(), parse_mode=enums.ParseMode.HTML)
        return

    # زر الرجوع
    if data == "back":
            await call.answer()
            inline_keyboard = get_main_keyboard()
            fname = call.from_user.first_name or call.from_user.username or "المستخدم"
            
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text=(
                    f"<tg-emoji emoji-id='5436237035369154060'>👋</tg-emoji> مرحبا بك "
                    f"عزيزي {fname} في بوت نقل الأعضاء المتطور V5.7 Pro !\n\n"
                    f"قناة السورس<tg-emoji emoji-id='5233546443360318055'>⚡</tg-emoji><tg-emoji emoji-id='5397857289216484878'>⚡</tg-emoji> "
                    f"<a href='https://t.me/a990099aa'>سورس 𝑹𝑶𝑺𝑺𝑬</a>\n\n\n"
                    f"اختر من الأزرار في الاسفل "
                    f"<tg-emoji emoji-id='5328306457135821693'>👇</tg-emoji>"
                ),
                reply_markup=inline_keyboard,
                parse_mode=enums.ParseMode.HTML
            )
    # إضافة حساب
    elif data == "AddAccount":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "قم بارسال الرقم الذي تريد اضافته\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "مثال:\n`+218900000000`"
            ),
        )
        bot.register_next_step_handler(AddAccount)

    elif data == "stop_add_btn":
        await handle_stop_add_btn(call)
        return

    elif data == "save_json":
        # اطلب اسم الملف
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<emoji id='5895226403447642077'>📁</emoji> أرسل اسم الملف الذي تريد حفظ الأعضاء فيه\n\n⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\nمثل:\nملف نقل 1",
        parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(partial(ask_group_link, user_info={}))

    elif data == "del_all_accounts":
        return await request_sensitive_confirmation(client, call.message, "delete_all")
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<emoji id='5830243638453477409'>✅</emoji> تم حذف جميع الحسابات بنجاح!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            ),
        parse_mode=enums.ParseMode.HTML)

    # تحديد مدة الانتظار بين كل إضافة
    elif data == "set_delay":
        mn, mx = get_add_delay_range()
        await call.answer()
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                f"<tg-emoji emoji-id='5382194935057372936'>⏱️</tg-emoji> المدة الحالية بين كل إضافة: {mn} - {mx} ثانية\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "أرسل المدة الجديدة بالثواني بصيغة:\n"
                "`min-max` مثل `2-5`\n"
                "أو رقم واحد فقط ليتم استخدامه كقيمة ثابتة مثل `3`"
            ),
        parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(set_delay_step)
        return
    # حذف الجلسات غير الصالحة أو الحسابات المعطلة بعد تأكيد الأدمن
    elif data == "remove_invalid_accounts_confirm":
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<tg-emoji emoji-id='6206077285720659346'><tg-emoji emoji-id='6206077285720659346'>⚠</tg-emoji>️</tg-emoji> <b>تأكيد حذف الحسابات غير الصالحة</b>\n\nسيتم فحص كل الحسابات، ثم حذف الجلسات التي ثبت عدم صلاحيتها أو الحسابات المعطلة فقط. الحسابات السليمة لن تُحذف.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ متابعة الفحص والحذف", callback_data="remove_invalid_accounts", style="danger", icon_custom_emoji_id="5830238823795136938")],
                [back_btn()],
                [source_btn()],
            ]),
            parse_mode=enums.ParseMode.HTML)
        return

    elif data == "remove_invalid_accounts":
        await call.answer()
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<tg-emoji emoji-id='5445350406215465190'>⏳</tg-emoji> جارٍ فحص الجلسات قبل الحذف، يرجى الانتظار...",
            parse_mode=enums.ParseMode.HTML)
        accounts = database().accounts()
        if not accounts:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات لفحصها.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                parse_mode=enums.ParseMode.HTML)

        results = await asyncio.gather(
            *[check_account_health(session_str, label) for session_str, label in accounts],
            return_exceptions=True
        )
        removable = []
        kept = []
        for result, (_session_str, label) in zip(results, accounts):
            if isinstance(result, Exception):
                kept.append(label)
                continue
            category, checked_label, _detail = result
            # لا نحذف عند فشل الشبكة أو قيود SpamBot؛ نحذف إلغاء الجلسة المؤكد فقط.
            confirmed_invalid = category == "invalid_session"
            if confirmed_invalid:
                removable.append(checked_label)
            else:
                kept.append(checked_label)

        db = database()
        for label in removable:
            db.RemoveAccountByLabel(label)

        removed_text = "\n".join(f"• {label}" for label in removable) or "لا يوجد"
        kept_text = "\n".join(f"• {label}" for label in kept) or "لا يوجد"
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                f"<emoji id='5830243638453477409'>✅</emoji> <b>اكتمل فحص الجلسات</b>\n\n"
                f"تم حذف: <b>{len(removable)}</b> حساب\n"
                f"تم الإبقاء على: <b>{len(kept)}</b> حساب\n\n"
                f"<b>الحسابات المحذوفة:</b>\n{removed_text}\n\n"
                f"<b>الحسابات التي تم الإبقاء عليها:</b>\n{kept_text}"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
            parse_mode=enums.ParseMode.HTML)

    elif data == "full_health_check":
        await call.answer()
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="<tg-emoji emoji-id='5445350406215465190'>⏳</tg-emoji> جارٍ إجراء اختبار الصحة الشامل...", parse_mode=enums.ParseMode.HTML)
        health = await run_full_health_check()
        lines = ["<tg-emoji emoji-id='5285533062518566401'>📋</tg-emoji> <b>نتيجة اختبار الصحة الشامل</b>", ""]
        for label, ok, detail in health:
            icon = "<emoji id='5830243638453477409'>✅</emoji>" if ok else "<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji>"
            lines.append(f"{icon} <b>{label}</b>: {detail}")
        lines.append("\nلا يتم تنفيذ أي إضافة أثناء هذا الاختبار.")
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    # ===== فحص صحة الحسابات (الجلسة + قيود @SpamBot) =====
    elif data == "check_accounts":
        await call.answer()
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<tg-emoji emoji-id='5445350406215465190'>⏳</tg-emoji> جاري فحص الحسابات، قد يستغرق دقيقة...",
            parse_mode=enums.ParseMode.HTML
        )
        accounts = database().accounts()
        if not accounts:
            return await abot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.id,
                text="<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد حسابات لفحصها.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                parse_mode=enums.ParseMode.HTML)

        results = await asyncio.gather(
            *[check_account_health(session_str, label) for session_str, label in accounts],
            return_exceptions=True
        )

        healthy, temp_banned, anti_spam = [], [], []
        for r, (session_str, label) in zip(results, accounts):
            if isinstance(r, Exception):
                anti_spam.append(label)
                continue
            category, lbl, _detail = r
            if category == "healthy":
                healthy.append(lbl)
            elif category == "temp_banned":
                temp_banned.append(lbl)
            else:
                anti_spam.append(lbl)

        previous_check = LAST_ACCOUNT_CHECK.get(call.from_user.id, {})
        previous_status = {label: status for status, labels in (("healthy", previous_check.get("healthy", [])), ("temp_banned", previous_check.get("temp_banned", [])), ("anti_spam", previous_check.get("anti_spam", []))) for label in labels}
        current_status = {label: status for status, labels in (("healthy", healthy), ("temp_banned", temp_banned), ("anti_spam", anti_spam)) for label in labels}
        changed = [f"{label}: {previous_status[label]} ← {current_status[label]}" for label in current_status if label in previous_status and previous_status[label] != current_status[label]]
        if changed:
            await abot.send_message(chat_id=call.message.chat.id, text="<tg-emoji emoji-id='6206508629286196237'>🔔</tg-emoji> <b>تغيّرت حالة حسابات:</b>\n" + "\n".join(changed), parse_mode=enums.ParseMode.HTML)
        # نخزن آخر نتيجة فحص لهذا الأدمن حتى يقدر يصدرها كملف txt
        LAST_ACCOUNT_CHECK[call.from_user.id] = {
            "healthy": healthy,
            "temp_banned": temp_banned,
            "anti_spam": anti_spam,
        }

        report_text = (
            f"سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - نتيجة فحص الحسابات ⛞\n"
            "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
            f"عدد الحسابات الكلي: {len(accounts)}\n"
            f"سليم: {len(healthy)}\n"
            f"محظور مؤقت: {len(temp_banned)}\n"
            f"مكافحة: {len(anti_spam)}\n"
        )

        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=report_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 تصدير النتيجة (txt)", callback_data="export_check_result", style="success")],
                [back_btn()],
                [source_btn()],
            ]),
            parse_mode=enums.ParseMode.HTML)

    elif data == "export_check_result":
        data_res = LAST_ACCOUNT_CHECK.get(call.from_user.id)
        if not data_res:
            return await call.answer("<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> لا توجد نتيجة فحص محفوظة، قم بفحص الحسابات أولاً", show_alert=True)

        await call.answer()
        lines = []
        lines.append("=== سليم ===")
        lines.extend(data_res["healthy"] or ["-"])
        lines.append("")
        lines.append("=== محظور مؤقت ===")
        lines.extend(data_res["temp_banned"] or ["-"])
        lines.append("")
        lines.append("=== مكافحة ===")
        lines.extend(data_res["anti_spam"] or ["-"])

        export_path = os.path.join(DATABASE_DIR, f"accounts_check_{call.from_user.id}.txt")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        await bot.send_document(
            chat_id=call.message.chat.id,
            document=export_path,
            caption="<emoji id='5830243638453477409'>✅</emoji> نتيجة فحص الحسابات",
        parse_mode=enums.ParseMode.HTML)
        try:
            os.remove(export_path)
        except Exception:
            pass

    # انضمام حسابات
    elif data == "joinGroup":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "قم بارسال رابط القروب للانضمام له\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "مثال:\n`https://t.me/a990099aa`"
            ),
        )
        bot.register_next_step_handler(partial(request_sensitive_confirmation, action="join"))

    # مغادرة حسابات
    elif data == "leaveGroup":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "قم بارسال رابط القروب للمغادرة منه\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "مثال:\n`https://t.me/a990099aa`"
            ),
        )
        bot.register_next_step_handler(partial(request_sensitive_confirmation, action="leave"))

    elif data == "restore_encrypted_backup":
        return await request_sensitive_confirmation(client, call.message, "restore")
        await call.answer("جاري استعادة النسخة المشفّرة...")
        try:
            added, skipped = restore_encrypted_backup()
            return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id,
                text=f"<tg-emoji emoji-id='5830238823795136938'>♻️</tg-emoji> <b>تمت الاستعادة</b>\n\nتمت الإضافة: {added}\nتم تجاهله كمكرر: {skipped}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
        except Exception as restore_error:
            return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id,
                text=f"<tg-emoji emoji-id='5830085055376006187'>❌</tg-emoji> تعذرت الاستعادة: <code>{type(restore_error).__name__}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    elif data == "search_accounts":
        await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id,
            text="<emoji id='5285533062518566401'>🔎</emoji> أرسل رقم الهاتف أو جزءاً منه للبحث:", parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(search_accounts_step)
    # حذف حساب
    elif data == "RemoveAccount":
        await show_accounts_as_buttons(call, 0, "RemoveAccount")

    # عرض الحسابات
    elif data == "Accounts":
        await show_accounts_as_buttons(call, 0, "Accounts")

    elif data.startswith("page_"):
        pross, current_page = data.split("_")[1].split("-")
        await show_accounts_as_buttons(call, int(current_page), pross)

    elif data.startswith("delaccount_"):
        del_number = data.split("_", 1)[1]
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=f"<tg-emoji emoji-id='6206077285720659346'><tg-emoji emoji-id='6206077285720659346'>⚠</tg-emoji>️</tg-emoji> هل أنت متأكد من حذف الحساب <code>{del_number}</code>؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="نعم، احذف", callback_data=f"confirm_delaccount:{del_number}", style="danger"), InlineKeyboardButton(text="إلغاء", callback_data="cancel_delete", style="primary")]
            ]), parse_mode=enums.ParseMode.HTML)
    elif data == "cancel_delete":
        await call.answer("تم إلغاء الحذف")
        return await show_accounts_as_buttons(call, 0, "RemoveAccount")
    elif data.startswith("confirm_delaccount:"):
        del_number = data.split(":", 1)[1]
        database().RemoveAccount(del_number)
        await call.answer("تم حذف الحساب")
        return await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=f"<emoji id='5830243638453477409'>✅</emoji> تم حذف الرقم: {del_number} بنجاح!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            ),
        parse_mode=enums.ParseMode.HTML)

    # باك اب حسابات
    elif data == "BackupAccounts":
        accounts = database().backupaccounts()
        with open('./A3DoBackUp.json', 'w', encoding='utf-8') as f:
            json.dump(accounts, f, ensure_ascii=False, indent=4)
        await bot.send_document(
            chat_id=call.message.chat.id,
            document='./A3DoBackUp.json',
            caption="<emoji id='5895226403447642077'>📂</emoji> النسخة الاحتياطية من الحسابات",
        parse_mode=enums.ParseMode.HTML)
        await abot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "<tg-emoji emoji-id='5472164874886846699'>✨</tg-emoji> تم حفظ البيانات في ملف A3DoBackUp.json بنجاح. "
                "استمتع بالإدارة المنظمة! <tg-emoji emoji-id='5895226403447642077'>📁</tg-emoji>"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=
                [[back_btn()], [source_btn()]]
            ),
        parse_mode=enums.ParseMode.HTML)
        os.remove('./A3DoBackUp.json')

    # رفع النسخة الاحتياطية
    elif data == "AddBackupAccounts":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="قم برفع ملف النسخة الاحتياطية (A3DoBackUp.json)"
        )
        bot.register_next_step_handler(AddBackupAccounts)

    # نقل الأعضاء ظاهر
    elif data == "addshow":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "قم بارسال العدد المراد اضافته للقروب\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "لا تنسى الانضمام للقناة:\n"
                "<a href='https://t.me/a990099aa'>سورس 𝑹𝑶𝑺𝑺𝑬.<emoji id='4958479549265347295'>⚡️</emoji></a>"
            ),
        parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(statement)

    # نقل الأعضاء مخفي
    elif data == "addhide":
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=(
                "قم بارسال العدد المراد اضافته للقروب\n"
                "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
                "لا تنسى الانضمام للقناة:\n"
                "<a href='https://t.me/a990099aa'>سورس 𝑹𝑶𝑺𝑺𝑬.<emoji id='4958479549265347295'>⚡️</emoji></a>"
            ),
        parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(statementhide)

    elif data == 'clear_contacts':
        return await request_sensitive_confirmation(client, call.message, "clear_contacts")

    elif data == "addmem_json":
        return await show_file_selection(call)
    elif data.startswith("toggle_member_file:"):
        files = _member_json_files()
        try:
            index = int(data.split(":", 1)[1])
            fname = files[index]
        except (ValueError, IndexError):
            return await call.answer("الملف لم يعد موجوداً، حدّث القائمة.", show_alert=True)
        selected = FILE_SELECTIONS.setdefault(call.from_user.id, [])
        if fname in selected:
            selected.remove(fname)
        else:
            selected.append(fname)
        return await show_file_selection(call)
    elif data == "select_all_member_files":
        FILE_SELECTIONS[call.from_user.id] = _member_json_files()
        return await show_file_selection(call)
    elif data == "start_selected_file_withdraw":
        selected = FILE_SELECTIONS.get(call.from_user.id, [])
        if not selected:
            return await call.answer("حدد ملفاً واحداً على الأقل أولاً.", show_alert=True)
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text=("✅ تم تحديد الملفات التالية:\n\n" + "\n".join(f"• `{f}`" for f in selected) +
                  "\n\nقم بإرسال رابط القروب المراد السحب إليه:"),
            parse_mode=enums.ParseMode.HTML
        )
        bot.register_next_step_handler(partial(handle_group, selected_files=list(selected)))

    elif data == 'extract_json':
        return await list_exp_pages(call, page=0)

    elif data.startswith('extract_json_page:'):
        _, page_str = data.split(':', 1)
        return await list_exp_pages(call, page=int(page_str))

    elif data.startswith('extract_file:'):
        _, fname = data.split(':', 1)
        path = os.path.join(VCF_DIR, fname)
        if os.path.exists(path):
            await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
                f'<tg-emoji emoji-id="5830243638453477409">✅</tg-emoji> تم استخراج الملف بنجاح: {fname}',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=
                    [[back_btn()], [source_btn()]]
                )
            , parse_mode=enums.ParseMode.HTML)
            return await call.message.reply_document(path, file_name=fname)
        else:
            return await call.answer('<tg-emoji emoji-id="5830085055376006187">❌</tg-emoji> الملف غير موجود.', show_alert=True)

    elif data == 'add_contacts':
        await Custem().add_contacts(call)

    elif data == "contact_hire":

        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.id,
            text="<emoji id='5895226403447642077'>📁</emoji> قم بارسال الرابط المراد اضافة الجهات له:",
        parse_mode=enums.ParseMode.HTML)
        async def _read_link(c, message):
            await han_group(c, message)
        bot.register_next_step_handler(_read_link)

    elif data == 'del_json':
        return await list_vcf_pages(call, page=0)

    elif data.startswith('del_json_page:'):
        _, page_str = data.split(':', 1)
        return await list_vcf_pages(call, page=int(page_str))

    elif data.startswith('ask_delete:'):
        _, fname = data.split(':', 1)
        buttons = [
            [InlineKeyboardButton(text="نعم", callback_data=f"confirm_delete:{fname}", style="success", icon_custom_emoji_id="5830243638453477409")],
            [InlineKeyboardButton(text="لا", callback_data="del_json", style="danger", icon_custom_emoji_id="5830085055376006187")]
        ]
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
            f"<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> هل تريد حذف الملف {fname}؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        , parse_mode=enums.ParseMode.HTML)

    elif data.startswith('confirm_delete:'):
        user_id = call.from_user.id
        # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
        if user_id != Config.OWNER_ID and user_id not in Config.Devs:
            await call.message.reply(
                "عذرا الأوامر ليست لك"
            )
        pass
        _, fname = data.split(':', 1)
        path = os.path.join(VCF_DIR, fname)
        if os.path.exists(path):
            os.remove(path)
            buttons = [[back_btn("del_json")]]
            return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
                f"<emoji id='5830243638453477409'>✅</emoji> تم حذف الملف {fname} بنجاح.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            , parse_mode=enums.ParseMode.HTML)
        else:
            return await call.answer('<tg-emoji emoji-id="5830085055376006187">❌</tg-emoji> الملف غير موجود', show_alert=True)

    elif data == 'add_json':
        user_id = call.from_user.id
        # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
        if user_id != Config.OWNER_ID and user_id not in Config.Devs:
            await call.message.reply(
                "عذرا الأوامر ليست لك"
            )
        pass
        EXPECTING_JSON.add(call.message.chat.id)
        await call.answer()
        await call.message.reply("من فضلك أرسل ملف الأعضاء بصيغة JSON:")
@bot.on_message(filters.document & filters.private)
async def handle_json_file(client, message):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return

    chat_id = message.chat.id
    if chat_id not in EXPECTING_JSON:
        return

    EXPECTING_JSON.remove(chat_id)
    file_name = message.document.file_name
    if not file_name.lower().endswith('.json'):
        await message.reply('<emoji id="5830085055376006187">❌</emoji> الملف غير مدعوم، الرجاء إرسال ملف بصيغة JSON فقط.',
        parse_mode=enums.ParseMode.HTML)
        return

    save_path = os.path.join(VCF_DIR, file_name)
    await client.download_media(message.document, file_name=save_path)
    try:
        with open(save_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        valid_shape = isinstance(payload, list) or (isinstance(payload, dict) and any(k in payload for k in ("add_members", "members", "users")))
        if not valid_shape:
            raise ValueError("يجب أن يحتوي الملف على قائمة أعضاء أو المفتاح add_members/members/users")
        records = payload if isinstance(payload, list) else (payload.get("add_members") or payload.get("members") or payload.get("users") or [])
        if not isinstance(records, list):
            raise ValueError("قائمة الأعضاء غير صالحة")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        try:
            os.remove(save_path)
        except OSError:
            pass
        await message.reply(f'<emoji id="5830085055376006187">❌</emoji> ملف JSON غير صالح: {exc}', parse_mode=enums.ParseMode.HTML)
        return
    await message.reply(f'<emoji id="5830243638453477409">✅</emoji> تم التحقق من الملف وحفظه بنجاح: `{file_name}`\nعدد السجلات: {len(records)}',
        parse_mode=enums.ParseMode.HTML)
async def handle_check_link(client, message, orig_call):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    link = message.text.strip()
    # استخراج معرف المجموعة (آخر جزء من الرابط)
    group_username, group_username_link_type = resolve_group_ref(link)

    # نعلم المستخدم ببدء الفحص
    status_msg = await bot.send_message(
        chat_id=message.chat.id,
        text=f"<emoji id='5445350406215465190'>⏳</emoji> جارٍ فحص الحسابات بالانضمام إلى @{group_username}...",
        parse_mode=enums.ParseMode.HTML)

    db = database()
    raw_accounts = db.accounts()  # [(session_str, label), ...]
    total_accounts = len(raw_accounts)

    # عدادات النتائج
    restricted = 0
    success = 0
    problems = 0
    to_delete = 0
    blocked_accounts = set()

    for session_str, label in raw_accounts:
        client_ok = None
        try:
            client_ok = Client(
                ":memory:", api_id=Config.APP_ID, api_hash=Config.API_HASH,
                session_string=session_str, no_updates=True, in_memory=True, lang_code="ar"
            )
            await client_ok.start()
            # نجرب الانضمام للمجموعة
            await client_ok.join_chat(group_username)
            # إذا وصل هنا بدون استثناء → انضمام ناجح
            success += 1

        except FloodWait:
            restricted += 1

        except PeerFlood:
            restricted += 1

        except (SessionRevoked, AuthKeyInvalid) as e:
            # حساب به مشكلة جلسة → نعدّه للحذف
            to_delete += 1
            blocked_accounts.add(label)
            db.RemoveAccount(label)

        except Exception:
            # أخطاء أخرى
            problems += 1

        finally:
            if client_ok:
                await client_ok.stop()

    # الحسابات النشطة بعد الحذف
    active_after = total_accounts - len(blocked_accounts)

    # 3) نعدل رسالة الحالة لتظهر النتائج النهائية
    report = (
        "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - فاحص الحسابات  ⛞\n"
        "⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
        f"عدد الحسابات بالكامل: {total_accounts} حساب\n"
        f"انضمام ناجح: {success} حساب\n"
        f"حسابات مقيدة (Flood/FloodWait): {restricted} حساب\n"
        f"الحسابات المحذوفة أو المنتهية الجلسة: {to_delete} حساب\n"
        f"الأخطاء الأخرى: {problems} حالة\n"
        f"الحسابات النشطة بعد الفحص: {active_after} حساب\n"
    )
    await abot.send_message(
        chat_id=message.chat.id, 
        text=report,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=
            [[back_btn()], [source_btn()]]
        )
    , parse_mode=enums.ParseMode.HTML)

async def han_group(client, message):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    group_link = message.text.strip()
    await message.reply("<emoji id='6323436631428695574'>📤</emoji> ارسل عدد الأعضاء المراد إضافتهم:",
        parse_mode=enums.ParseMode.HTML)
    bot.register_next_step_handler(partial(start_add_con, group_link=group_link))
async def start_add_con(client, message, group_link):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    try:
        count = int(message.text.strip())
    except ValueError:
        return await message.reply("<emoji id='5274099962655816924'>❗️</emoji> الرجاء إدخال رقم صحيح.",
        parse_mode=enums.ParseMode.HTML)
    await send_operation_preview(message.chat.id, "contacts", group_link, count, "جهات اتصال الحسابات")
    return
    notif_msg = await abot.send_message(chat_id=message.chat.id, text=
        "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل جهات الاتصال⛞\n"
        "⋆─┄─┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
        f"الطلب : {count} عضو\n"
        f"النقل الى : {group_link}\n\n"
        "لإلغاء العملية أرسل : /stop_add",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    try:
        await abot.pin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id, disable_notification=True)
    except Exception:
        pass
    try:
        await Custem().add_users_contact(group_link, bot, count)
    finally:
        try:
            await abot.unpin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id)
        except Exception:
            pass

def _member_json_files():
    os.makedirs(VCF_DIR, exist_ok=True)
    return [f for f in sorted(os.listdir(VCF_DIR)) if f.lower().endswith(".json")]

def _file_selection_keyboard(user_id):
    files = _member_json_files()
    selected = set(FILE_SELECTIONS.get(user_id, []))
    rows = []
    for index, fname in enumerate(files):
        active = fname in selected
        rows.append([InlineKeyboardButton(
            text=("✅ " if active else "📄 ") + fname,
            callback_data=f"toggle_member_file:{index}",
            style="success" if active else "danger"
        )])
    rows.append([InlineKeyboardButton(text="📚 السحب من كل الملفات", callback_data="select_all_member_files", style="primary")])
    rows.append([InlineKeyboardButton(text="▶️ بدء السحب", callback_data="start_selected_file_withdraw", style="success")])
    rows.append([back_btn("menu_withdraw")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _file_selection_text(user_id):
    files = _member_json_files()
    selected = FILE_SELECTIONS.get(user_id, [])
    if not files:
        return "📁 لا توجد ملفات أعضاء محفوظة حالياً. قم برفع أو حفظ ملف أولاً."
    chosen = len(selected)
    return ("📁 <b>تحديد ملفات السحب</b>\n\n"
            f"عدد الملفات: <b>{len(files)}</b>\n"
            f"المحدد حالياً: <b>{chosen}</b>\n\n"
            "الأحمر = غير محدد، والأخضر = محدد.\n"
            "اختر الملفات ثم اضغط <b>بدء السحب</b>.")

async def show_file_selection(call):
    user_id = call.from_user.id
    files = _member_json_files()
    if user_id not in FILE_SELECTIONS:
        FILE_SELECTIONS[user_id] = []
    await abot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.id,
        text=_file_selection_text(user_id),
        reply_markup=_file_selection_keyboard(user_id),
        parse_mode=enums.ParseMode.HTML
    )

async def handle_group(client, message, selected_files=None):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    group_link = message.text.strip()
    await message.reply("<emoji id='6323436631428695574'>📤</emoji> ارسل عدد الأعضاء المراد إضافتهم:",
        parse_mode=enums.ParseMode.HTML)
    # هنا نمرّر group_link للخطوة التالية عبر partial
    bot.register_next_step_handler(
        partial(start_adding, group_link=group_link, selected_files=selected_files or FILE_SELECTIONS.get(user_id, []))
    )
async def start_adding(client, message, group_link, selected_files=None):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    try:
        count = int(message.text.strip())
    except ValueError:
        return await message.reply("<emoji id='5274099962655816924'>❗️</emoji> الرجاء إدخال رقم صحيح.",
        parse_mode=enums.ParseMode.HTML)
    selected_files = list(selected_files or FILE_SELECTIONS.get(user_id, []))
    if not selected_files:
        return await message.reply("❌ لم تحدد أي ملف للسحب منه.")
    numUser = len(await Custem().get_users_saved())
    file_label = "، ".join(selected_files)
    await send_operation_preview(message.chat.id, "file_hide", group_link, count, f"ملفات الأعضاء المحددة: {file_label}")
    return
    notif_msg = await abot.send_message(chat_id=message.chat.id, text=
        "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل الاعضاء محفوظه⛞\n"
        "⋆─┄─┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
        f"المتاحين للاضافة : {numUser} عضو\n"
        f"الطلب : {count} عضو\n"
        f"النقل الى : {group_link}\n\n"
        "لإلغاء العملية أرسل : /stop_add",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    try:
        await abot.pin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id, disable_notification=True)
    except Exception:
        pass
    try:
        await Custem().add_users_hide(group_link, client, count)
    finally:
        try:
            await abot.unpin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id)
        except Exception:
            pass
####################################################################
#اضافه حساب
async def AddAccount(client, message):    
    # تجاهل الأوامر السابقة عند إعادة التشغيل
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    if message.text == '/start':
        return
    # التحقق من وجود رمز الدولة (+)
    if not message.text.startswith('+'):
        await bot.send_message(message.chat.id, "*يرجى إدخال رمز الدولة مع رقم الهاتف (مثال: +218900000000)*")
        return
    # متابعة التدفق الأصلي عند إدخال رقم صحيح
    await bot.send_message(message.chat.id, "<b>انتظر قليلاً... جاري الفحص <emoji id='5445350406215465190'>⏱</emoji></b>",
        parse_mode=enums.ParseMode.HTML)
    # API credentials (api_id & api_hash) and session storage are obtained as described at:
    # https://telegram.tools/session-string-generator#pyrogram
    _client = Client(
        "::memory::", in_memory=True,
        api_id=Config.APP_ID,
        api_hash=Config.API_HASH,
        lang_code="ar"
        
    )
    await _client.connect()
    SendCode = await _client.send_code(message.text)
    await bot.send_message(message.chat.id, "قم بإرسال الرمز المرسل من التيليجرام بالشكل التالي:\n⋆─┄─┄─┄─┄─┄┄─┄┄─┄─⋆\n\n1.2.3.4.5",)
    user_info = {
        "client": _client,
        "phone": message.text,
        "hash": SendCode.phone_code_hash,
        "name": message.text
    }
    bot.register_next_step_handler(partial(sigin_up, user_info=user_info))
async def _send_password_prompt(chat_id, user_info):
    hint = "لم يضع Telegram تلميحاً لهذه كلمة المرور."
    try:
        official_hint = await user_info["client"].get_password_hint()
        if official_hint:
            hint = f"تلميح Telegram الرسمي: <code>{escape(str(official_hint))}</code>"
    except Exception:
        pass
    await bot.send_message(
        chat_id,
        f"<b>أدخل كلمة المرور الخاصة بحسابك <emoji id='5472308992514464048'>🔐</emoji></b>\n{hint}\n\nإذا كانت كلمة المرور غير صحيحة، أرسلها مرة أخرى.",
        parse_mode=enums.ParseMode.HTML)

async def sigin_up(client, message, user_info: dict):
    user_id = message.from_user.id
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    try:
        await bot.send_message(message.chat.id, "<b>انتظر قليلا <emoji id='5445350406215465190'>⏱</emoji></b>", parse_mode=enums.ParseMode.HTML)
        code = (message.text or "").replace(".", "").replace(" ", "")
        await user_info['client'].sign_in(user_info['phone'], user_info['hash'], phone_code=code)
        await abot.send_message(message.chat.id, "<b>تم تاكيد الحساب بنجاح <emoji id='5830243638453477409'>✅</emoji> </b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
        ses = await user_info['client'].export_session_string()
        database().AddAcount(ses, user_info['name'], message.chat.id)
        try:
            await user_info['client'].stop()
        except Exception:
            pass
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await bot.send_message(message.chat.id, "<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> الكود غير صحيح أو انتهت صلاحيته. أرسل كود التحقق مرة أخرى.", parse_mode=enums.ParseMode.HTML)
        bot.register_next_step_handler(partial(sigin_up, user_info=user_info))
    except SessionPasswordNeeded:
        await _send_password_prompt(message.chat.id, user_info)
        bot.register_next_step_handler(partial(AddPassword, user_info=user_info))
    except Exception:
        # لا نرسل تفاصيل الاستثناء ولا الكود إلى المستخدم أو السجل.
        try:
            await user_info['client'].stop()
        except Exception:
            pass
        await bot.send_message(message.chat.id, "تعذر تأكيد الكود. أعد بدء إضافة الحساب وحاول مرة أخرى.", parse_mode=enums.ParseMode.HTML)

async def AddPassword(client, message, user_info: dict):
    user_id = message.from_user.id
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    try:
        await user_info['client'].check_password(message.text or "")
        ses = await user_info['client'].export_session_string()
        database().AddAcount(ses, user_info['name'], message.chat.id)
        try:
            await user_info['client'].stop()
        except Exception:
            pass
        await abot.send_message(message.chat.id, "<b>تم تاكيد الحساب بنجاح <emoji id='5830243638453477409'>✅</emoji> </b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]), parse_mode=enums.ParseMode.HTML)
    except PasswordHashInvalid:
        await bot.send_message(message.chat.id, "<tg-emoji emoji-id='6206077285720659346'>⚠️</tg-emoji> كلمة المرور غير صحيحة. أرسل كلمة المرور مرة أخرى.", parse_mode=enums.ParseMode.HTML)
        await _send_password_prompt(message.chat.id, user_info)
        bot.register_next_step_handler(partial(AddPassword, user_info=user_info))
    except Exception:
        try:
            await user_info['client'].stop()
        except Exception:
            pass
        await bot.send_message(message.chat.id, "تعذر تأكيد كلمة المرور. أعد بدء إضافة الحساب وحاول مرة أخرى.", parse_mode=enums.ParseMode.HTML)

#################################################
#نقل الاعضاء      

async def statement(client, message):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    num = message.text
    await bot.send_message(chat_id=message.chat.id,text="قم بارسال رابط القروب المراد سحب الأعضاء منه\n––––––––––––––––––––––––––––\n\nمثل:\n`https://t.me/a990099aa`",)
    Fromgrob_info = {"num":num,}
    bot.register_next_step_handler(partial(statement1,user_info=Fromgrob_info))	
async def statement1(client, message, user_info: dict):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    Fromgrob = message.text
    await bot.send_message(chat_id=message.chat.id,text="قم بارسال رابط القروب المراد اضافة الأعضاء له\n––––––––––––––––––––––––––––\n\nمثل:\n`https://t.me/a990099aa`",)
    Fromgrob_info = {"Fromgrob":Fromgrob,"num":user_info['num']}
    bot.register_next_step_handler(partial(statement2,user_info=Fromgrob_info))	
async def statement2(client, message, user_info: dict):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    Ingrob = message.text
    await bot.send_message(chat_id=message.chat.id,text="انتظر دقائق ليتم تجهيز الاعضاء <emoji id='5445350406215465190'>⏱</emoji>",
        parse_mode=enums.ParseMode.HTML)
    add_members, admins, bots = await Custem().GETuser(user_info['Fromgrob']) 
    numUser = len(add_members)
    await send_operation_preview(message.chat.id, "visible", Ingrob, int(user_info['num']), user_info['Fromgrob'])
    return
    notif_msg = await abot.send_message(message.chat.id,f"""سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل الظاهر ⛞
⋆─┄─┄─┄─┄─┄┄─┄┄─┄─⋆

المتاحين للاضافة : {numUser} عضو 
النقل من  : {user_info['Fromgrob']} 
النقل الي : {Ingrob} 

لإلغاء العملية أرسل : /stop_add """,
    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    try:
        await abot.pin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id, disable_notification=True)
    except Exception:
        pass
    try:
        await Custem().ADDuser(Ingrob,user_info['Fromgrob'],bot,user_info['num'])
    finally:
        try:
            await abot.unpin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id)
        except Exception:
            pass
#################################################
async def statementhide(client, message):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    num = message.text
    await bot.send_message(
        chat_id=message.chat.id,
        text="قم بارسال رابط القروب المراد سحب الأعضاء منه\n⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\nمثل:\n`https://t.me/a990099aa`",
    )
    Fromgrob_info = {"num": num}
    bot.register_next_step_handler(
        partial(statement1hide, user_info=Fromgrob_info)
    )
async def statement1hide(client, message, user_info: dict):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    Fromgrob = message.text.strip()
    user_info['Fromgrob'] = Fromgrob
    # ask for limit before fetching
    await bot.send_message(
        chat_id=message.chat.id,
        text="قم بإدخال عدد الرسائل التي تريد جلب الاعضاء منها\n⋆─┄──┄─┄─┄─┄┄─┄┄─┄─⋆\n\nمثل:\n`30000`",
    )
    # register ask_limit
    bot.register_next_step_handler(
        partial(Custem().ask_limit, user_info=user_info)
    )

# statement2hide now receives limit in user_info and Ingrob link
async def statement2hide(client, message, user_info: dict):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    Ingrob = message.text.strip()
    limit = user_info.get('limit')
    await bot.send_message(message.chat.id, "<b>انتظر دقائق ليتم تجهيز الاعضاء <emoji id='5445350406215465190'>⏱</emoji></b>",
        parse_mode=enums.ParseMode.HTML)

    # استدعاء الدالة لجلب أعضاء المجموعة والحفظ في MEMBERS_JSON
    add_members, admins, bots = await Custem().GETuserhide(user_info['Fromgrob'], limit)
    # عدد الأعضاء المراد إضافتهم من JSON
    numUser = len(add_members)
    await send_operation_preview(message.chat.id, "hide", Ingrob, int(user_info['num']), user_info['Fromgrob'])
    return

    # إرسال إشعار بالبيانات
    notif_msg = await abot.send_message(
        message.chat.id,
        (
            "سورس 𝑹𝑶𝑺𝑺𝑬.<tg-emoji emoji-id='4958479549265347295'>⚡️</tg-emoji> - إشعـــــار النقل المخفي ⛞\n"
            "⋆─┄─┄─┄─┄─┄┄─┄┄─┄─⋆\n\n"
            f"المتاحين للاضافة : {numUser} عضو\n"
            f"النقل من  : {user_info['Fromgrob']}\n"
            f"النقل الي : {Ingrob}\n\n"
            "لإلغاء العملية أرسل : /stop_add"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_btn()], [source_btn()]]),
        parse_mode=enums.ParseMode.HTML)
    try:
        await abot.pin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id, disable_notification=True)
    except Exception:
        pass
    try:
        await Custem().ADDuserhide(add_members, Ingrob, user_info['Fromgrob'], bot, user_info['num'])
    finally:
        try:
            await abot.unpin_chat_message(chat_id=message.chat.id, message_id=notif_msg.message_id)
        except Exception:
            pass

# دمجت المهام في call_handler الأساسي
async def ask_group_link(client, message, user_info):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    # استلم اسم الملف
    user_info['filename'] = message.text.strip()
    await client.send_message(message.chat.id, "<emoji id='6037361126268738296'>🔗</emoji> أرسل رابط القروب لاستخراج الأعضاء:",
        parse_mode=enums.ParseMode.HTML)
    bot.register_next_step_handler(partial(ask_limit, user_info=user_info))
async def ask_limit(client, message, user_info):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    # استلم رابط القروب
    user_info['group_link'] = message.text.strip()
    await client.send_message(
        message.chat.id,
        "<emoji id='6323436631428695574'>🔢</emoji> كم عدد الرسائل تريد جلبها؟ (مثلاً: 30000 رساله)",
        parse_mode=enums.ParseMode.HTML)
    bot.register_next_step_handler(partial(process_save, user_info=user_info))
async def process_save(client, message, user_info):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    # استلم قيمة limit
    try:
        limiting = int(message.text.strip())
    except ValueError:
        limiting = 30000  # قيمة افتراضية في حال الإدخال خاطئ
    await client.send_message(message.chat.id, "<emoji id='5445350406215465190'>⏳</emoji> جارٍ استخراج الأعضاء وحفظها...",
        parse_mode=enums.ParseMode.HTML)
    members = await Custem().GETusersavecontact(
        user_info['group_link'],
        user_info['filename'],
        limiting  # هنا نمرر القيمة التي أدخلها المستخدم
    )
    await client.send_message(
        message.chat.id,
        f"<emoji id='5830243638453477409'>✅</emoji> تم حفظ {len(members)} عضو في الملف: `{user_info['filename']}.json` داخل المجلد `{VCF_DIR}`.",
        parse_mode=enums.ParseMode.HTML)
#################################################
#رفع النسخه الحسابات 
async def AddBackupAccounts(client, message):
    user_id = message.from_user.id
    # تحقق من الصلاحيات: فقط OWNER_ID أو أعضاء Devs يحق لهم رفع الملفات
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    # تأكد من أن هناك وثيقة مرفقة مع الرسالة
    if message.document and (message.document.file_name or "").lower().endswith(".json"):
        await message.download("./A3DoBackUp.json")
        try:
            with open("./A3DoBackUp.json", "r", encoding="utf-8") as f:
                data = json.load(f)

            db = database()
            existing_accounts = db.backupaccounts()
            existing_sessions = {str(account[0]) for account in existing_accounts if len(account) > 0 and account[0]}
            existing_numbers = {str(account[1]) for account in existing_accounts if len(account) > 1 and account[1]}
            seen_sessions = set(existing_sessions)
            seen_numbers = set(existing_numbers)
            added_count = 0
            skipped_count = 0
            invalid_count = 0

            for account in data if isinstance(data, list) else []:
                if not isinstance(account, (list, tuple)) or len(account) < 3:
                    invalid_count += 1
                    continue
                session_value, number_value, account_id = account[0], account[1], account[2]
                if not validate_session_format(session_value):
                    invalid_count += 1
                    continue
                session_key = str(session_value) if session_value else ""
                number_key = str(number_value) if number_value else ""
                # التكرار يعتمد على الجلسة أو رقم الحساب فقط، وليس على ID.
                is_duplicate = (
                    (session_key and session_key in seen_sessions)
                    or (number_key and number_key in seen_numbers)
                )
                if is_duplicate:
                    skipped_count += 1
                    continue
                try:
                    db.AddAcount(session_value, number_value, account_id)
                    added_count += 1
                    if session_key:
                        seen_sessions.add(session_key)
                    if number_key:
                        seen_numbers.add(number_key)
                except Exception as e:
                    invalid_count += 1
                    print(f"Error processing account: {e}")

            await abot.send_message(
                chat_id=message.chat.id,
                text=(
                    "<emoji id='5830243638453477409'>✅</emoji> تم رفع النسخة الاحتياطية بنجاح.\n\n"
                    f"تمت إضافة: {added_count} حساب\n"
                    f"تم تجاهل المكرر: {skipped_count} حساب\n"
                    f"السجلات غير الصالحة: {invalid_count}"
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]]),
                parse_mode=enums.ParseMode.HTML)
        finally:
            try:
                os.remove("./A3DoBackUp.json")
            except OSError:
                pass
    else:
        # إذا لم يكن هناك مستند مرفق
        await client.send_message(
            chat_id=message.chat.id,
            text="*لم يتم العثور على مستند مرفق. يُرجى إرسال النسخة الاحتياطية بصيغة JSON.*",
        )
#####################################
def validate_session_format(value):
    """يتحقق من أن القيمة تبدو Pyrogram session string دون محاولة تحويلها أو تشغيلها."""
    if not isinstance(value, str) or not (40 <= len(value.strip()) <= 4096):
        return False
    raw = value.strip()
    try:
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        return True
    except Exception:
        return False

def normalize_phone(value):
    """إرجاع الأرقام فقط، مع إزالة بادئة الاتصال الدولية 00 عند وجودها."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits

async def search_accounts_step(client, message):
    user_id = message.from_user.id
    if user_id != Config.OWNER_ID and user_id not in Config.Devs:
        return
    query = (message.text or "").strip()
    query_digits = normalize_phone(query)
    accounts = database().backupaccounts()
    matches = [row for row in accounts if query_digits and query_digits in normalize_phone(row[1])]
    if not matches:
        text = "<emoji id='5285533062518566401'>🔎</emoji> لم يتم العثور على حساب يطابق البحث."
    else:
        text = "<emoji id='5285533062518566401'>🔎</emoji> <b>نتائج البحث:</b>\n\n" + "\n".join(f"• <code>{row[1]}</code>" for row in matches)
    await abot.send_message(chat_id=message.chat.id, text=text, parse_mode=enums.ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))

#عرض الحسابات وحذف الحسابات
async def show_accounts_as_buttons(call, current_page, pross):
    # تنظيف رموز الروابط المنتهية قبل بناء القائمة.
    now = time.time()
    for key, value in list(ACCOUNT_LINK_CACHE.items()):
        if now > value.get("expires_at", 0):
            ACCOUNT_LINK_CACHE.pop(key, None)
    accounts = database().backupaccounts()  # جلب الحسابات من قاعدة البيانات
    buttons_per_page = 14  # 7 صفوف × عمودين = 14 زرّ في كل صفحة
    buttons = []

    # تحويل كل رقم إلى رابط داخلي آمن في البوت.
    bot_info = await bot.get_me()
    bot_username = getattr(bot_info, "username", None)
    for account in accounts:
        label = f"الرقم: {account[1]}"
        if pross == "RemoveAccount":
            data = f"delaccount_{account[1]}"
            buttons.append(InlineKeyboardButton(text=f"🗑 {label}", callback_data=data, style="danger", icon_custom_emoji_id="5904542823167824187"))
        else:
            session_value = str(account[0]) if len(account) > 0 and account[0] else ""
            link_key = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
            ACCOUNT_LINK_CACHE[link_key] = {
                "number": str(account[1]),
                "session": session_value,
                "owner_id": call.from_user.id,
                "expires_at": time.time() + ACCOUNT_LINK_TTL,
            }
            account_url = f"https://t.me/{bot_username}?start=acct_{link_key}" if bot_username else None
            if account_url:
                buttons.append(InlineKeyboardButton(text=f"📱 {label}", url=account_url, style="primary", icon_custom_emoji_id="5895226403447642077"))

    # تقسيم الأزرار إلى صفحات
    pages = [buttons[i : i + buttons_per_page] for i in range(0, len(buttons), buttons_per_page)]

    # إعداد أزرار التنقل
    page_buttons = []
    if current_page > 0:
        page_buttons.append(InlineKeyboardButton(text="السابق", style="primary", callback_data=f"page_{pross}-{current_page - 1}", icon_custom_emoji_id="5262646461698436019"))
    if current_page < len(pages) - 1:
        page_buttons.append(InlineKeyboardButton(text="التالي", style="primary", callback_data=f"page_{pross}-{current_page + 1}", icon_custom_emoji_id="5262646461698436019"))
    page_buttons.append(back_btn("back_accounts"))

    # التحقق من صحة رقم الصفحة
    if current_page < 0 or current_page >= len(pages):
        return await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
            "*لا توجد حسابات حالياً.*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_btn()], [source_btn()]])
        , parse_mode=enums.ParseMode.HTML)

    # بناء لوحة الأزرار: عمودين
    current_buttons = pages[current_page]
    keyboard = [
        current_buttons[i : i + 2]
        for i in range(0, len(current_buttons), 2)
    ]
    if page_buttons:
        keyboard.append(page_buttons)

    # عرض الرسالة مع لوحة الأزرار
    await abot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=
        "*حساباتك المسجلة بالكامل:*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    , parse_mode=enums.ParseMode.HTML)
######################################################################
async def common_chat_contains_target(client, user_ref, target_ref):
    """يفحص وجود المجموعة الهدف ضمن المجموعات المشتركة للحساب والعضو."""
    try:
        common = await client.get_common_chats(to_add_target(user_ref))
        target_id = target_ref if isinstance(target_ref, int) else None
        target_name = str(target_ref).lstrip("@").lower() if target_ref is not None else ""
        for chat in common or []:
            if target_id is not None and getattr(chat, "id", None) == target_id:
                return True
            username = getattr(chat, "username", None)
            if target_name and username and str(username).lstrip("@").lower() == target_name:
                return True
        return False
    except Exception as exc:
        # فشل الاستعلام لا يُعامل كإضافة ناجحة ولا كموجود مسبقاً.
        print(f"Common-chat verification failed: {type(exc).__name__}")
        return False

async def verify_added_via_common_chats(client, user_ref, target_ref, wait_seconds=None):
    """تحقق فعلي متعدد المسارات من دخول العضو للمجموعة الهدف."""
    delay = random.uniform(5.0, 10.0) if wait_seconds is None else float(wait_seconds)
    if not await stop_aware_sleep(delay):
        return False
    user_target = to_add_target(user_ref)

    # التحقق المباشر هو الأدق، خصوصاً في المجموعات التي تخفي قائمة الأعضاء.
    try:
        member = await client.get_chat_member(target_ref, user_target)
        status = getattr(member, "status", None)
        status_name = getattr(status, "name", str(status)).upper()
        if status_name not in {"LEFT", "KICKED", "BANNED"}:
            return True
        return False
    except errors.UserNotParticipant:
        return False
    except Exception as direct_exc:
        print(f"Direct membership verification failed: {type(direct_exc).__name__}: {direct_exc}")

    # fallback للمجموعات المشتركة عند تعذر الاستعلام المباشر.
    try:
        common = await client.get_common_chats(user_target)
        target_id = target_ref if isinstance(target_ref, int) else None
        target_name = str(target_ref).lstrip("@").lower() if target_ref is not None else ""
        for chat in common or []:
            if target_id is not None and getattr(chat, "id", None) == target_id:
                return True
            username = getattr(chat, "username", None)
            if target_name and username and str(username).lstrip("@").lower() == target_name:
                return True
        return False
    except Exception as fallback_exc:
        print(f"Common-chat verification failed: {type(fallback_exc).__name__}: {fallback_exc}")
        return False

async def retry_transient(operation, *args, retries=2, base_delay=2, **kwargs):
    """إعادة محاولة الأخطاء المؤقتة فقط؛ لا يعيد أخطاء التقييد أو الجلسات."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            name = type(exc).__name__.lower()
            if any(part in name for part in ("flood", "peerflood", "sessionrevoked", "authkeyinvalid", "userprivacy")):
                raise
            if attempt >= retries:
                raise
            if not await stop_aware_sleep(base_delay * (attempt + 1)):
                raise asyncio.CancelledError("تم إيقاف العملية أثناء إعادة المحاولة")
    raise last_error

class LiveProgressReporter:
    def __init__(self, client, chat_id, title, getter):
        self.client, self.chat_id, self.title, self.getter = client, chat_id, title, getter
        self.message = None
        self.task = None
        self.closed = False
        self.events = []
        self.update_lock = asyncio.Lock()

    def _render(self, status="جارية"):
        confirmed, failed, total = self.getter()
        recent = "\n".join(self.events[-2:])
        text = ("<emoji id='5397857289216484878'>⚡️</emoji> "
                "<b>إحصائيات النقل - SOURCE A3Do</b>\n\n"
                f"<emoji id='5830243638453477409'>✅</emoji> "
                f"الإضافات المؤكدة فعلياً: <b>{confirmed}</b>\n"
                f"<emoji id='6206343625232619150'>📊</emoji> "
                f"العدد المطلوب: <b>{total}</b>\n"
                "<emoji id='5285533062518566401'>🔎</emoji> "
                f"الحالة: <b>{status}</b>")
        if recent:
            text += ("\n\n<emoji id='5830243638453477409'>✅</emoji> "
                     "<b>آخر الإضافات المؤكدة:</b>\n" + recent)
        return text

    async def update_now(self, status="جارية"):
        if not self.message or self.closed:
            return
        async with self.update_lock:
            if not self.message or self.closed:
                return
            await update_live_status(self.chat_id, self.message.id, self._render(status))

    async def note(self, text):
        text = str(text)
        if "إضافة ناجحة" not in text and "تمت الإضافة" not in text:
            return
        self.events.append(text)
        self.events = self.events[-3:]
        await self.update_now()

    async def start(self):
        confirmed, failed, total = self.getter()
        self.message = await self.client.send_message(
            self.chat_id,
            (f"<emoji id='5397857289216484878'>⚡️</emoji> "
             f"<b>سورس 𝑹𝑶𝑺𝑺𝑬</b>\n\n"
             f"<emoji id='6206343625232619150'>📊</emoji> <b>{self.title}</b>\n"
             f"<emoji id='5830243638453477409'>✅</emoji> الإضافات المؤكدة فعلياً: <b>{confirmed}</b>\n"
             f"<emoji id='5285533062518566401'>🔎</emoji> الحالة: <b>جارٍ البدء</b>"),
            parse_mode=enums.ParseMode.HTML)
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closed:
            try:
                await self.update_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(8)

    async def close(self, final_text=None):
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if final_text and self.message:
            await update_live_status(self.chat_id, self.message.id, final_text)

def bot_api_compatible_html(text):
    """تحويل وسوم الإيموجي الخاصة بصيغة Pyrogram إلى صيغة Bot API HTML."""
    text = str(text)
    # Bot API يدعم tg-emoji مع emoji-id، وليس وسم emoji المستخدم في Pyrogram.
    text = re.sub(
        r"<emoji\s+id=['\"]([^'\"]+)['\"]>(.*?)</emoji>",
        r"<tg-emoji emoji-id='\1'>\2</tg-emoji>",
        text,
        flags=re.DOTALL,
    )
    return text


async def update_live_status(chat_id, message_id, text):
    """تحديث رسالة تقدم واحدة بصيغة HTML المتوافقة مع Telegram Bot API."""
    text = bot_api_compatible_html(text)
    try:
        return await safe_edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=enums.ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as exc:
        error_type = type(exc).__name__
        error_text = str(exc) or repr(exc)
        log_line = (
            f"Live status update failed | type={error_type} | "
            f"chat_id={chat_id} | message_id={message_id} | "
            f"error={error_text!r} | text_preview={str(text)[:300]!r}"
        )
        print(log_line)
        try:
            with open("bot_runtime.log", "a", encoding="utf-8") as log_file:
                log_file.write(log_line + "\\n")
        except Exception:
            pass
        return None

async def scheduled_account_health_check(interval_seconds=21600):
    """فحص دوري للحسابات وإرسال تنبيه فقط عند تغير الحالة."""
    previous = {}
    while True:
        try:
            for session_string, label in database().accounts():
                status, _, detail = await check_account_health(session_string, label)
                old = previous.get(label)
                previous[label] = status
                if old is not None and old != status:
                    safe_detail = str(detail).replace(str(session_string), "[SESSION_HIDDEN]")
                    print(f"Account health changed: {label} -> {status}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Scheduled health check failed: {type(exc).__name__}")
        await asyncio.sleep(interval_seconds)

async def run_bot_with_resume():
    await bot.start()
    health_task = asyncio.create_task(scheduled_account_health_check())
    subscription_task = asyncio.create_task(subscription_watchdog())
    try:
        try:
            save_encrypted_backup()
        except Exception as backup_error:
            print(f"Encrypted backup failed at startup: {backup_error}")
        cleanup_temp_files()
        await send_resume_prompt()
        await idle()
    finally:
        health_task.cancel()
        subscription_task.cancel()
        for task in (health_task, subscription_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.stop()


if __name__ == "__main__":
    bot.run(run_bot_with_resume())
