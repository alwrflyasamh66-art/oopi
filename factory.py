#!/usr/bin/env python3
# ‹ سورس 𝑹𝑶𝑺𝑺𝑬 › — المصنع (نسخة مصلحة ومحدثة)
# ======================================================
# الملف: factory.py
# الوصف: مصنع البوتات — ينصّب ويدير نسخاً من app.py
# الاستخدام: python3 factory.py
# ======================================================

import os, re, time, shutil, subprocess, json, shlex
from functools import partial
from pyrogram import Client, filters, enums, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ======================================================
#  إعدادات المصنع — عدّلها حسب حاجتك
# ======================================================
API_ID    = 39978956
API_HASH  = "e7322ed176f1527600979186b4ea8da8"
BOT_TOKEN = "8737127935:AAFuiJOMQbEeQ2mYf3P2_5D3uKcVhW1lfLU"  # توكن بوت المصنع

# المالك الأصلي لجميع البوتات (ثابت دائماً)
MASTER_OWNER = [8864493211]
# المطورون المسموح لهم بالوصول للمصنع
DEVS = [8864493211]  # مالك ومطور وأدمن المصنع

# ======================================================
#  حالة المصنع
# ======================================================
off  = None   # None = مفعّل، True = معطّل
Bots = []     # [username, admin_id, token, installer_username, deadline_ts]

# ======================================================
#  نظام إدارة الخطوات
# ======================================================
_next_handlers: dict[int, tuple] = {}  # chat_id → (func, kwargs)

FACTORY_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_ROOT = os.path.join(FACTORY_DIR, "bots")
FACTORY_STATE = os.path.join(FACTORY_DIR, "factory_bots.json")
os.makedirs(BOTS_ROOT, exist_ok=True)

def _load_bots():
    try:
        with open(FACTORY_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [list(item) for item in data if isinstance(item, list) and len(item) >= 5]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def _save_bots():
    tmp = FACTORY_STATE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(Bots, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FACTORY_STATE)
    except Exception as e:
        print(f"❌ خطأ أثناء حفظ حالة البوتات: {e}")

def _safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))
    return value.strip("._")[:80] or "unknown"

def _bot_dir(record):
    username, admin_id = record[0], record[1]
    return os.path.join(BOTS_ROOT, f"{_safe_name(admin_id)}_{_safe_name(username)}")

def _screen_name(username):
    return f"bot_{_safe_name(username)}"

def _screen_running(username):
    """التحقق الآمن من جلسة screen مع تجاهل الأسطر غير القياسية."""
    name = _screen_name(username)
    try:
        result = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    for raw_line in result.stdout.splitlines():
        entry = raw_line.strip()
        if not entry or "." not in entry:
            continue

        _pid, session_part = entry.split(".", 1)
        fields = session_part.split()
        if not fields:
            continue

        session = fields[0]
        if session == name:
            return True

    return False

def _stop_screen(username):
    subprocess.run(["screen", "-S", _screen_name(username), "-X", "quit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _run_screen_record(record):
    username = record[0]
    bot_dir = _bot_dir(record)
    app_file = os.path.join(bot_dir, "app.py")
    if not os.path.isfile(app_file):
        raise FileNotFoundError(f"لم يتم العثور على app.py في {bot_dir}")
    _stop_screen(username)
    log_file = os.path.join(bot_dir, "bot.log")
    command = f"cd -- {shlex.quote(bot_dir)} && exec python3 app.py >> {shlex.quote(log_file)} 2>&1"
    result = subprocess.run(["screen", "-dmS", _screen_name(username), "bash", "-lc", command], capture_output=True, text=True)
    if result.returncode != 0 or not _screen_running(username):
        raise RuntimeError(result.stderr.strip() or "فشل إنشاء جلسة screen")

def _bot_status(record):
    return "يعمل 🟢" if _screen_running(record[0]) else "متوقف 🔴"

def _find_bot(username):
    return next((b for b in Bots if b[0] == username), None)

async def _safe_edit(message, text, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except errors.MessageNotModified:
        return None

Bots = _load_bots()

def register_next(chat_id: int, func, **kwargs):
    """سجّل معالج الرسالة التالية لهذه المحادثة."""
    _next_handlers[chat_id] = (func, kwargs)

# ======================================================
#  بوت المصنع
# ======================================================
bot = Client("awab_factory", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ── معالج الخطوات بأولوية عالية ──────────────────────
@bot.on_message(filters.private, group=-1)
async def _next_step_dispatcher(client, message):
    chat_id = message.chat.id
    if chat_id in _next_handlers:
        func, kwargs = _next_handlers.pop(chat_id)
        await func(client, message, **kwargs)
        message.stop_propagation()


# ── بوابة التعطيل العامة ──────────────────────────────
@bot.on_message(filters.private, group=0)
async def _global_gate(client, message):
    global off
    if off and message.from_user.id not in DEVS:
        await message.reply_text("❌ الصانع معطل حالياً.")
        message.stop_propagation()


# ── /start ────────────────────────────────────────────
def _dashboard_text():
    total = len(Bots)
    running = sum(1 for b in Bots if _screen_running(b[0]))
    stopped = total - running
    now = int(time.time())
    expiring = sum(1 for b in Bots if b[4] and 0 < b[4] - now <= 7 * 86400)
    return ("<b>لوحة التحكم الاحترافية — سورس ROSSE</b>

"
            f"البوتات المسجلة: <b>{total}</b>
"
            f"البوتات العاملة: <b>{running}</b>
"
            f"البوتات المتوقفة: <b>{stopped}</b>
"
            f"اشتراكات تنتهي خلال 7 أيام: <b>{expiring}</b>

"
            "اختر الإجراء المطلوب من القائمة:")

@bot.on_message(filters.command(["start", "رجوع"]) & filters.private, group=1)
async def cmd_start(client, message):
    if message.from_user.id not in DEVS:
        return await message.reply_text(
            "عذراً، هذا المصنع خاص ولا يمكنك استخدامه.
"
            "للتواصل مع المطور: @qv_x1"
        )
    await message.reply_text(_dashboard_text(), reply_markup=_main_keyboard(), parse_mode=enums.ParseMode.HTML)


# ── معالج الأزرار ────────────────────────────────────
@bot.on_callback_query(group=1)
async def callback_handler(client, query):
    global off
    data  = query.data
    uid   = query.from_user.id
    cid   = query.message.chat.id

    if uid not in DEVS:
        return await query.answer("❌ ليس لك صلاحية.", show_alert=True)

    # ── تفعيل / تعطيل المجاني ──
    if data == "enable_free":
        off = None
        await query.message.edit_text("✅ تم تفعيل المصنع للمستخدمين", reply_markup=_main_keyboard())

    elif data == "disable_free":
        off = True
        await query.message.edit_text("❌ تم تعطيل المصنع للمستخدمين", reply_markup=_main_keyboard())

    # ── صنع بوت ──
    elif data == "make_bot":
        await query.message.edit_text("✏️ أرسل توكن البوت الجديد (من @BotFather):")
        register_next(cid, _step_ask_token)

    elif data == "dashboard":
        await _safe_edit(query.message, _dashboard_text(), reply_markup=_main_keyboard(), parse_mode=enums.ParseMode.HTML)

    elif data in ("all_start", "all_stop"):
        action_label = "تشغيل" if data == "all_start" else "إيقاف"
        rows = [[InlineKeyboardButton(f"تأكيد {action_label} الكل", callback_data=f"confirm_{data}")],
                [InlineKeyboardButton("إلغاء", callback_data="dashboard")]]
        await query.message.edit_text(f"هل تريد {action_label} جميع البوتات؟
هذا الإجراء سيؤثر على {len(Bots)} بوتاً.", reply_markup=InlineKeyboardMarkup(rows))

    elif data in ("confirm_all_start", "confirm_all_stop"):
        err_count = 0
        for record in list(Bots):
            try:
                if data == "confirm_all_start":
                    _run_screen_record(record)
                else:
                    _stop_screen(record[0])
            except Exception:
                err_count += 1
        _save_bots()
        await query.message.edit_text(f"تم تنفيذ العملية الجماعية.
عدد البوتات: {len(Bots)}
الأخطاء: {err_count}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للوحة", callback_data="dashboard")]]))

    # ── قائمة البوتات ──
    elif data == "list_bots":
        if not Bots:
            await query.message.edit_text("لا توجد بوتات مصنوعة حالياً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="dashboard")]]))
        else:
            rows = []
            for b in Bots:
                rows.append([InlineKeyboardButton(f"@{b[0]} — {_bot_status(b)}", callback_data=f"bot_manage:{b[0]}")])
            rows.append([InlineKeyboardButton("رجوع", callback_data="dashboard")])
            await query.message.edit_text("اختر البوت الذي تريد التحكم به:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("bot_manage:"):
        username = data.split(":", 1)[1]
        record = _find_bot(username)
        if not record:
            return await query.answer("البوت غير مسجل.", show_alert=True)
        bot_dir = _bot_dir(record)
        deadline = time.strftime("%Y-%m-%d %H:%M", time.localtime(record[4])) if record[4] else "—"
        text = (f"<b>إدارة البوت @{record[0]}</b>

"
                f"الحالة: <b>{_bot_status(record)}</b>
"
                f"المالك: <code>{record[1]}</code>
"
                f"مسار الملف: <code>{bot_dir}/app.py</code>
"
                f"اسم الجلسة: <code>{_screen_name(record[0])}</code>
"
                f"الانتهاء: <code>{deadline}</code>")
        rows = [
            [InlineKeyboardButton("تشغيل ▶️", callback_data=f"bot_start:{username}"), InlineKeyboardButton("إيقاف ⏸️", callback_data=f"bot_stop:{username}")],
            [InlineKeyboardButton("إعادة تشغيل 🔄", callback_data=f"bot_restart:{username}"), InlineKeyboardButton("السجل 📜", callback_data=f"bot_log:{username}")],
            [InlineKeyboardButton("رجوع للقائمة", callback_data="list_bots")],
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=enums.ParseMode.HTML)

    elif data.startswith("bot_start:") or data.startswith("bot_stop:") or data.startswith("bot_restart:"):
        action, username = data.split(":", 1)
        record = _find_bot(username)
        if not record:
            return await query.answer("البوت غير مسجل.", show_alert=True)
        try:
            if action == "bot_stop":
                _stop_screen(username)
            else:
                _run_screen_record(record)
            _save_bots()
            await query.answer("تم التنفيذ بنجاح")
        except Exception as exc:
            return await query.answer(f"فشل التنفيذ: {type(exc).__name__}", show_alert=True)
        return await query.message.edit_text(f"تم تنفيذ الأمر على @{username}.
الحالة الحالية: {_bot_status(record)}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data=f"bot_manage:{username}")]]))

    elif data.startswith("bot_log:"):
        username = data.split(":", 1)[1]
        record = _find_bot(username)
        if not record:
            return await query.answer("البوت غير مسجل.", show_alert=True)
        log_path = os.path.join(_bot_dir(record), "bot.log")
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()[-3500:]
        except OSError:
            content = "لا يوجد سجل بعد."
        await query.message.edit_text(f"<b>آخر سجل للبوت @{username}</b>
<pre>{content.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')}</pre>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data=f"bot_manage:{username}")]]), parse_mode=enums.ParseMode.HTML)

    # ── حذف بوت ──
    elif data == "delete_bot":
        await query.message.edit_text(
            "🗑️ أرسل التوكن أو اليوزر (@username) أو آيدي الأدمن لحذف البوت:"
        )
        register_next(cid, _step_delete_bot)

    # ── تجديد / زيادة مدة ──
    elif data == "renew_bot":
        if not Bots:
            return await query.answer("لا توجد بوتات مصنوعة حالياً.", show_alert=True)
        rows = []
        for b in Bots:
            username, _, _, _, deadline_ts = b
            deadline_str = time.strftime("%d/%m %H:%M", time.localtime(deadline_ts)) if deadline_ts else "—"
            rows.append([InlineKeyboardButton(
                f"@{username}  |  ⏰ {deadline_str}",
                callback_data=f"renew_pick:{username}"
            )])
        rows.append([InlineKeyboardButton("رجوع", callback_data="dashboard")])
        await query.message.edit_text(
            "🔄 اختر البوت الذي تريد تجديده:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif data.startswith("renew_pick:"):
        username = data.split(":", 1)[1]
        found = next((b for b in Bots if b[0] == username), None)
        if not found:
            return await query.answer("❌ البوت غير موجود.", show_alert=True)
        deadline_ts  = found[4]
        deadline_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(deadline_ts)) if deadline_ts else "—"
        remaining    = max(0, deadline_ts - int(time.time())) if deadline_ts else 0
        rem_days     = remaining // 86400
        rem_hours    = (remaining % 86400) // 3600
        await query.message.edit_text(
            f"🔄 تجديد: @{username}

"
            f"⏰ ينتهي حالياً: {deadline_str}
"
            f"📅 المتبقي: {rem_days} يوم و {rem_hours} ساعة

"
            "كم يوماً تريد إضافتها؟ (أرسل رقماً):",
        )
        register_next(cid, partial(_step_renew_days, username=username))

    # ── رجوع ──
    elif data == "back":
        await query.message.edit_text(
            _dashboard_text(),
            reply_markup=_main_keyboard(),
            parse_mode=enums.ParseMode.HTML
        )


# ======================================================
#  خطوات صنع البوت
# ======================================================

async def _step_ask_token(client, message):
    """الخطوة 1: استقبال التوكن."""
    token = message.text.strip()
    if message.from_user.id in DEVS:
        await message.reply_text("🔢 أرسل آيدي الأدمن المسؤول عن هذا البوت:")
        register_next(message.chat.id, partial(_step_ask_admin, info={"token": token}))
    else:
        await message.reply_text("📅 كم يوماً تريد تشغيل البوت؟ (أرسل رقماً):")
        register_next(
            message.chat.id,
            partial(_step_ask_days, info={"token": token, "admin_id": message.from_user.id})
        )


async def _step_ask_admin(client, message, info: dict):
    """الخطوة 2: استقبال آيدي الأدمن."""
    try:
        admin_id = int(message.text.strip())
    except ValueError:
        await message.reply_text("⚠️ أرسل آيدي صحيح (أرقام فقط).")
        return register_next(message.chat.id, partial(_step_ask_admin, info=info))

    info["admin_id"] = admin_id
    await message.reply_text("📅 كم يوماً تريد تشغيل البوت؟ (أرسل رقماً):")
    register_next(message.chat.id, partial(_step_ask_days, info=info))


async def _step_ask_days(client, message, info: dict):
    """الخطوة 3: استقبال عدد الأيام."""
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.reply_text("⚠️ أرسل عدد أيام صحيح (رقم موجب).")
        return register_next(message.chat.id, partial(_step_ask_days, info=info))

    info["days"] = days
    await _do_install(client, message, info)


async def _do_install(client, message, info: dict):
    """تنفيذ التنصيب الفعلي."""
    token    = (info.get("token") or "").strip()
    admin_id = info["admin_id"]
    days     = info["days"]

    if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{20,}", token):
        return await message.reply_text("❌ صيغة توكن غير صحيحة. أرسل توكن BotFather كاملاً دون مسافات.")

    await message.reply_text("⏳ جارٍ التحقق من التوكن...")
    try:
        test = Client(
            f"_test_{token[:8]}",
            api_id=API_ID, api_hash=API_HASH,
            bot_token=token, in_memory=True
        )
        await test.start()
        me = await test.get_me()
        username = me.username or str(me.id)
        await test.stop()
        await message.reply_text(f"✅ تم التحقق من التوكن بنجاح: @{username}
جارٍ تجهيز ملفات البوت...")
    except Exception as e:
        return await message.reply_text(f"❌ فشل التحقق من التوكن: {e}
تأكد من أنه صادر من BotFather وغير ملغى.")

    if any(b[0] == username for b in Bots):
        return await message.reply_text(f"⚠️ البوت @{username} مصنوع مسبقاً.")

    deadline_ts = int(time.time()) + days * 86400
    deadline_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(deadline_ts))

    src_template = os.path.join(FACTORY_DIR, "app.py")
    dest_dir     = os.path.join(BOTS_ROOT, f"{_safe_name(admin_id)}_{_safe_name(username)}")
    dest_file    = os.path.join(dest_dir, "app.py")

    if not os.path.isfile(src_template):
        return await message.reply_text(
            f"❌ لم يتم العثور على ملف app.py المصدري في:
`{src_template}`

"
            "تأكد من وضع ملف app.py الرئيسي في نفس مجلد المصنع."
        )

    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy2(src_template, dest_file)

    with open(dest_file, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r'BOT_TOKEN\s*=\s*"[^"]*"', f'BOT_TOKEN = "{token}"', content)
    content = re.sub(r'ADMIN_ID\s*=\s*\d+', f'ADMIN_ID = {admin_id}', content)
    content = re.sub(r'INSTALL_DEADLINE\s*=\s*\d+', f'INSTALL_DEADLINE = {deadline_ts}', content)

    with open(dest_file, "w", encoding="utf-8") as f:
        f.write(content)

    installer = message.from_user.username or str(message.from_user.id)
    record = [username, admin_id, token, installer, deadline_ts]
    
    try:
        _run_screen_record(record)
        Bots.append(record)
        _save_bots()

        await message.reply_text(
            f"✅ <b>تم إنشاء البوت وتشغيله بنجاح!</b>

"
            f"🤖 البوت: @{username}
"
            f"👤 الأدمن: <code>{admin_id}</code>
"
            f"📅 مدة التشغيل: <b>{days} يوم</b>
"
            f"⏰ ينتهي في: <code>{deadline_str}</code>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للوحة", callback_data="dashboard")]])
        )
    except Exception as exc:
        await message.reply_text(f"⚠️ تم تجهيز الملفات ولكن فشل تشغيل الجلسة: {exc}")


# ======================================================
#  تجديد / زيادة مدة اشتراك
# ======================================================

async def _step_renew_days(client, message, username: str):
    """استقبال عدد الأيام المراد إضافتها."""
    try:
        extra_days = int(message.text.strip())
        if extra_days <= 0:
            raise ValueError
    except ValueError:
        await message.reply_text("⚠️ أرسل عدد أيام صحيح (رقم موجب).")
        return register_next(message.chat.id, partial(_step_renew_days, username=username))

    await _do_renew(client, message, username, extra_days)


async def _do_renew(client, message, username: str, extra_days: int):
    """تنفيذ التجديد."""
    found_idx = next((i for i, b in enumerate(Bots) if b[0] == username), None)
    if found_idx is None:
        return await message.reply_text(f"❌ البوت @{username} غير موجود في القائمة.")

    bot_entry    = Bots[found_idx]
    old_deadline = bot_entry[4]
    admin_id     = bot_entry[1]
    token        = bot_entry[2]

    base         = max(old_deadline, int(time.time()))
    new_deadline = base + extra_days * 86400
    new_str      = time.strftime("%Y-%m-%d %H:%M", time.localtime(new_deadline))
    old_str      = time.strftime("%Y-%m-%d %H:%M", time.localtime(old_deadline)) if old_deadline else "—"

    dest_file   = os.path.join(_bot_dir(bot_entry), "app.py")

    if not os.path.isfile(dest_file):
        return await message.reply_text(f"❌ لم يُعثَر على ملف البوت:
`{dest_file}`")

    with open(dest_file, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(
        r"INSTALL_DEADLINE\s*=\s*\d+",
        f"INSTALL_DEADLINE = {new_deadline}",
        content
    )

    with open(dest_file, "w", encoding="utf-8") as f:
        f.write(new_content)

    Bots[found_idx][4] = new_deadline
    _save_bots()

    _run_screen_record(bot_entry)

    try:
        notify_bot = Client(
            f"_notify_{username[:8]}",
            api_id=API_ID, api_hash=API_HASH,
            bot_token=token, in_memory=True
        )
        await notify_bot.start()
        await notify_bot.send_message(
            admin_id,
            f"✅ <b>تم تجديد اشتراك بوتك بنجاح!</b>

"
            f"📅 المدة المضافة: <b>{extra_days} يوم</b>
"
            f"⏰ كان ينتهي في: {old_str}
"
            f"🆕 ينتهي الآن في: <b>{new_str}</b>",
            parse_mode=enums.ParseMode.HTML
        )
        await notify_bot.stop()
    except Exception as e:
        print(f"⚠️ فشل إشعار مالك البوت: {e}")

    await message.reply_text(
        f"✅ <b>تم تجديد البوت @{username} بنجاح!</b>

"
        f"📅 أيام مضافة: <b>{extra_days} يوم</b>
"
        f"⏰ كان: {old_str}
"
        f"🆕 أصبح: <b>{new_str}</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="dashboard")]])
    )


# ======================================================
#  حذف بوت
# ======================================================

async def _step_delete_bot(client, message):
    query_text = message.text.strip().replace("@", "")
    found = None
    for b in Bots:
        username, admin_id, token, installer, _ = b
        if query_text in [username, token, str(admin_id)]:
            found = b
            break

    if not found:
        return await message.reply_text(
            "❌ لم يتم العثور على البوت.
"
            "تأكد من إرسال التوكن أو اليوزر أو آيدي الأدمن بشكل صحيح."
        )

    username = found[0]
    Bots.remove(found)
    _save_bots()

    _stop_screen(username)
    bot_dir = _bot_dir(found)
    if os.path.isdir(bot_dir):
        shutil.rmtree(bot_dir, ignore_errors=True)

    await message.reply_text(
        f"🗑️ تم حذف البوت @{username} مع كافة ملفاته وجلساته بنجاح.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للوحة", callback_data="dashboard")]])
    )


# ======================================================
#  لوحة المفاتيح الرئيسية
# ======================================================

def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("صنع بوت 🔧", callback_data="make_bot"), InlineKeyboardButton("إدارة البوتات 🖥️", callback_data="list_bots")],
        [InlineKeyboardButton("حالة المصنع 📊", callback_data="dashboard"), InlineKeyboardButton("تحديث الحالات 🔄", callback_data="dashboard")],
        [InlineKeyboardButton("تجديد اشتراك 📅", callback_data="renew_bot"), InlineKeyboardButton("حذف بوت 🗑️", callback_data="delete_bot")],
        [InlineKeyboardButton("تشغيل الكل ▶️", callback_data="all_start"), InlineKeyboardButton("إيقاف الكل ⏸️", callback_data="all_stop")],
        [InlineKeyboardButton("تعطيل المجاني ❌", callback_data="disable_free"), InlineKeyboardButton("تفعيل المجاني ✅", callback_data="enable_free")],
        [InlineKeyboardButton("سورس 𝑹𝑶𝑺𝑺𝑬", url="https://t.me/a990099aa")],
    ])


# ======================================================
#  نقطة الدخول
# ======================================================

if __name__ == "__main__":
    print("""
###########################################
#                                         #
#     سورس 𝑹𝑶𝑺𝑺𝑬              #
#            المصنع — factory.py          #
#                                         #
###########################################
تم تشغيل مصنع سورس 𝑹𝑶𝑺𝑺𝑬 بنجاح
المطور: @qv_x1 | القناة: https://t.me/a990099aa
""")
    bot.run()
