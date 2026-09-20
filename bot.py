import telebot
from telebot import types

# ----------------------------------------------------
# 1. التهيئات وإعداد البوت
# ----------------------------------------------------
TOKEN = "ضع_توكن_البوت_هنا"  # استبدل هذا بتوكن البوت الخاص بك من BotFather
bot = telebot.TeleBot(TOKEN)

# معرف المطور (استبدله بـ ID الخاص بك)
ADMIN_ID = 123456789  

# تخزين البيانات في الذاكرة
user_data = {}         # لتتبع حالة المستخدم أثناء الإدخال
auto_replies = {}      # تخزين الردود النصية: { "الكلمة": "الرد" }
photo_replies = {}     # تخزين الردود بالصور: { "الكلمة": {"photo_id": ..., "caption": ...} }
is_auto_reply_active = True  # حالة الرد التلقائي (مُفعل تلقائياً)
mandatory_channel = "@a990099aa" # قناة الاشتراك الإجباري

# ----------------------------------------------------
# 2. لوحات الأزرار (Keyboards)
# ----------------------------------------------------
def get_main_keyboard():
    """إنشاء الأزرار الـ 6 الرئيسية"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn1 = types.InlineKeyboardButton("➕ أضف رد", callback_data="add_text_reply")
    btn2 = types.InlineKeyboardButton("⚙️ رد تلقائي", callback_data="toggle_auto_reply")
    btn3 = types.InlineKeyboardButton("🗑️ مسح رد", callback_data="delete_reply")
    btn4 = types.InlineKeyboardButton("🖼️ أضف رد بصورة", callback_data="add_photo_reply")
    btn5 = types.InlineKeyboardButton("👨‍💻 المطور", callback_data="developer_info")
    btn6 = types.InlineKeyboardButton("👑 لوحة المطور", callback_data="admin_panel")
    
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn5, btn6)
    return markup

def check_subscription(user_id):
    """التحقق من اشتراك المستخدم في القناة الإجبارية"""
    if not mandatory_channel:
        return True
    try:
        member = bot.get_chat_member(mandatory_channel, user_id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
        return False
    except Exception:
        return True

# ----------------------------------------------------
# 3. معالج الأمر /start
# ----------------------------------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    
    # التحقق من الاشتراك الإجباري
    if not check_subscription(user_id):
        markup = types.InlineKeyboardMarkup()
        btn = types.InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{mandatory_channel.replace('@', '')}")
        markup.add(btn)
        bot.send_message(
            message.chat.id, 
            f"⚠️ **عذراً! يجب عليك الاشتراك في القناة أولاً لاستخدام البوت:**\n{mandatory_channel}", 
            reply_markup=markup, 
            parse_mode="Markdown"
        )
        return

    welcome_text = (
        "أهلاً بك في بوت الردود 🤖✨\n\n"
        "استمتع وضيف ردودك الخاصة.\n"
        "أضف البوت في حسابك وأرسل ستارت مرة ثانية ليتم إعطاؤك القائمة.\n\n"
        "📢 **قناة البوت:** https://t.me/a990099aa"
    )
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(), disable_web_page_preview=True)

# ----------------------------------------------------
# 4. معالج ضغطات الأزرار (Callback Queries)
# ----------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    global is_auto_reply_active
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    data = call.data

    # 1. زر "أضف رد" (نصي)
    if data == "add_text_reply":
        user_data[user_id] = {'step': 'WAITING_FOR_TRIGGER_WORD'}
        bot.send_message(chat_id, "✏️ اكتب الكلمة التي تريد الرد عليها:")

    # 2. زر "رد تلقائي" (تفعيل / تعطيل)
    elif data == "toggle_auto_reply":
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_on = types.InlineKeyboardButton("🟢 تفعيل", callback_data="set_auto_on")
        btn_off = types.InlineKeyboardButton("🔴 تعطيل", callback_data="set_auto_off")
        markup.add(btn_on, btn_off)
        
        status = "مُفعل 🟢" if is_auto_reply_active else "معطل 🔴"
        bot.send_message(chat_id, f"⚙️ **إعدادات الرد التلقائي:**\nالحالة الحالية: {status}\n\nاختر من الأسفل:", reply_markup=markup, parse_mode="Markdown")

    elif data == "set_auto_on":
        is_auto_reply_active = True
        bot.answer_callback_query(call.id, "تم تفعيل الرد التلقائي بنجاح!")
        bot.send_message(chat_id, "🟢 تم **تفعيل** الرد التلقائي عندما تكون غير متصل.")

    elif data == "set_auto_off":
        is_auto_reply_active = False
        bot.answer_callback_query(call.id, "تم تعطيل الرد التلقائي!")
        bot.send_message(chat_id, "🔴 تم **تعطيل** الرد التلقائي.")

    # 3. زر "مسح رد"
    elif data == "delete_reply":
        user_data[user_id] = {'step': 'WAITING_FOR_DELETE_WORD'}
        bot.send_message(chat_id, "🗑️ اكتب الكلمة التي تريد حذف الرد المخصص لها:")

    # 4. زر "أضف رد بصورة"
    elif data == "add_photo_reply":
        user_data[user_id] = {'step': 'WAITING_FOR_PHOTO_TRIGGER'}
        bot.send_message(chat_id, "✏️ اكتب الكلمة التي تريد أن يرد البوت عليها بالصورة:")

    # 5. زر "المطور"
    elif data == "developer_info":
        dev_text = (
            "👨‍💻 **معلومات المطور:**\n\n"
            "• الحساب الشخصي: @qv_x1\n"
            "• قناة البوت: https://t.me/a990099aa\n\n"
            "أهلاً بك في أي وقت للتواصل والدعم الفني!"
        )
        bot.send_message(chat_id, dev_text, parse_mode="Markdown")

    # 6. زر "لوحة المطور"
    elif data == "admin_panel":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⚠️ هذه اللوحة مخصصة لمالك البوت فقط!", show_alert=True)
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_sub = types.InlineKeyboardButton("📢 إعدادات الاشتراك الإجباري", callback_data="admin_sub")
        btn_deposit = types.InlineKeyboardButton("💰 إدارة الإيداع والاشتراكات", callback_data="admin_deposit")
        markup.add(btn_sub, btn_deposit)
        bot.send_message(chat_id, "👑 **مرحباً بك في لوحة تحكم المطور:**", reply_markup=markup, parse_mode="Markdown")

    elif data == "admin_sub":
        bot.send_message(chat_id, f"📢 القناة الحالية للاشتراك الإجباري هي: {mandatory_channel}")

    elif data == "admin_deposit":
        bot.send_message(chat_id, "💳 **قسم الإيداع والاشتراكات المدفوعة:**\nيمكنك ربط طرق الدفع أو إدارة شحن الرصيد هنا.")

# ----------------------------------------------------
# 5. معالجة النصوص والصور المدخلة خطوة بخطوة
# ----------------------------------------------------
@bot.message_handler(content_types=['text', 'photo'])
def handle_user_input(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text

    # أ) مراحل إضافة رد نصي
    if user_id in user_data and user_data[user_id].get('step') == 'WAITING_FOR_TRIGGER_WORD':
        user_data[user_id]['trigger'] = text
        user_data[user_id]['step'] = 'WAITING_FOR_RESPONSE_TEXT'
        bot.send_message(chat_id, f"👍 تم حفظ الكلمة: ({text})\n\nالآن اكتب **الرد** الذي تريده:")
        return

    elif user_id in user_data and user_data[user_id].get('step') == 'WAITING_FOR_RESPONSE_TEXT':
        trigger = user_data[user_id]['trigger']
        auto_replies[trigger] = text
        del user_data[user_id]
        bot.send_message(chat_id, "✅ تم إضافة الرد بنجاح!")
        return

    # ب) مراحل حذف رد
    elif user_id in user_data and user_data[user_id].get('step') == 'WAITING_FOR_DELETE_WORD':
        word_to_delete = text
        del user_data[user_id]
        deleted = False
        if word_to_delete in auto_replies:
            del auto_replies[word_to_delete]
            deleted = True
        if word_to_delete in photo_replies:
            del photo_replies[word_to_delete]
            deleted = True

        if deleted:
            bot.send_message(chat_id, f"✅ تم حذف الرد الخاص بالكلمة: ({word_to_delete}) بنجاح!")
        else:
            bot.send_message(chat_id, f"❌ الكلمة ({word_to_delete}) غير موجودة في قائمة الردود!")
        return

    # ج) مراحل إضافة رد بصورة
    elif user_id in user_data and user_data[user_id].get('step') == 'WAITING_FOR_PHOTO_TRIGGER':
        user_data[user_id]['trigger'] = text
        user_data[user_id]['step'] = 'WAITING_FOR_PHOTO_AND_CAPTION'
        bot.send_message(chat_id, f"👍 الكلمة المفتاحية: ({text})\n\nالآن أرسل **الصورة** مع **النص التوضيحي** (Caption) المرفق معها:")
        return

    elif user_id in user_data and user_data[user_id].get('step') == 'WAITING_FOR_PHOTO_AND_CAPTION':
        if message.content_type == 'photo':
            photo_id = message.photo[-1].file_id
            caption = message.caption or ""
            trigger = user_data[user_id]['trigger']
            photo_replies[trigger] = {"photo_id": photo_id, "caption": caption}
            del user_data[user_id]
            bot.send_message(chat_id, "✅ تم إضافة الرد بالصورة والنص بنجاح!")
        else:
            bot.send_message(chat_id, "⚠️ يرجى إرسال صورة!")
        return

    # ----------------------------------------------------
    # 6. منطق الرد التلقائي على المحادثات والخاص
    # ----------------------------------------------------
    if is_auto_reply_active and text:
        # البحث عن رد نصي
        if text in auto_replies:
            bot.reply_to(message, auto_replies[text])
            return
        
        # البحث عن رد بصورة
        if text in photo_replies:
            item = photo_replies[text]
            bot.send_photo(chat_id, item['photo_id'], caption=item['caption'], reply_to_message_id=message.message_id)
            return

# تشغيل البوت
if __name__ == '__main__':
    print("البوت يعمل الآن على الاستضافة...")
    bot.infinity_polling()
