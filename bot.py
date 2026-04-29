
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from PIL import Image
import os 
TOKEN = os.getenv("TOKEN")
user_data = {}
waiting_name = {}

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    photo = update.message.photo[-1]
    file = await photo.get_file()

    if user_id not in user_data:
        user_data[user_id] = []

    img_path = f"{user_id}_{len(user_data[user_id])}.jpg"
    await file.download_to_drive(img_path)

    user_data[user_id].append(img_path)

    # زر
    keyboard = [[InlineKeyboardButton("📄 إنشاء PDF", callback_data="make_pdf")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📸 تم إضافة الصورة",
        reply_markup=reply_markup
    )

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id not in user_data or len(user_data[user_id]) == 0:
        await query.message.reply_text("❌ ما كاش صور")
        return

    waiting_name[user_id] = True
    await query.message.reply_text("📝 وش تحب تسمي الـ PDF؟")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in waiting_name:
        return

    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("❌ عطيني اسم صحيح")
        return

    name = name.replace(" ", "_")

    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    pdf_path = name
    images = user_data[user_id]

    image_list = []
    for img in images:
        image = Image.open(img).convert("RGB")
        image_list.append(image)

    image_list[0].save(pdf_path, save_all=True, append_images=image_list[1:])

    await update.message.reply_document(open(pdf_path, "rb"))

    # تنظيف
    for img in images:
        os.remove(img)
    os.remove(pdf_path)

    del user_data[user_id]
    del waiting_name[user_id]

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(CallbackQueryHandler(button_click))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

app.run_polling()