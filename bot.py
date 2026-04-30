import os
import re
import tempfile
import logging
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from PIL import Image
from pillow_heif import register_heif_opener

# Initialize HEIC support
register_heif_opener()

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
TOKEN = os.getenv("TOKEN")
ROOT_FOLDER_ID = "1pFEXGM_O5fkFtfzc-yjJp4YXN9VYQlqY"
SCOPES = ['https://www.googleapis.com/auth/drive']

# In-memory storage for user data
user_data_store: Dict[int, dict] = {}

def get_user_data(user_id: int) -> dict:
    if user_id not in user_data_store:
        user_data_store[user_id] = {
            'images': [],
            'state': None,
            'pdf_path': None,
            'pdf_name': None,
            'folder_history': [],
            'current_folder_id': ROOT_FOLDER_ID
        }
    return user_data_store[user_id]

# Google Drive API helpers
def get_drive_service():
    """
    Authenticates using User OAuth instead of Service Account.
    Expects token.json to be present in the working directory.
    """
    # For deployment platforms like Railway, you might inject the file contents 
    # via environment variables and write it to disk at runtime if it doesn't exist.
    token_json_env = os.getenv("TOKEN_JSON_CONTENT")
    if token_json_env and not os.path.exists('token.json'):
        with open('token.json', 'w') as f:
            f.write(token_json_env)

    if not os.path.exists('token.json'):
        raise ValueError("token.json not found. Run auth.py locally first, or set TOKEN_JSON_CONTENT environment variable.")

    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    return service

def list_folders(service, folder_id):
    query = f"'{folder_id}' in parents and (mimeType='application/vnd.google-apps.folder' or mimeType='application/vnd.google-apps.shortcut') and trashed=false"
    folders = []
    page_token = None
    
    while True:
        results = service.files().list(
            q=query, 
            fields="nextPageToken, files(id, name, mimeType, shortcutDetails)", 
            pageSize=1000,
            orderBy="folder, name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
            pageToken=page_token
        ).execute()
        
        items = results.get('files', [])
        for item in items:
            if item.get('mimeType') == 'application/vnd.google-apps.shortcut':
                target_mime = item.get('shortcutDetails', {}).get('targetMimeType')
                if target_mime == 'application/vnd.google-apps.folder':
                    # Use the real folder ID instead of the shortcut ID
                    item['id'] = item['shortcutDetails']['targetId']
                    folders.append(item)
            else:
                folders.append(item)
            
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    logger.info(f"Retrieved {len(folders)} folders for parent ID: {folder_id}")
    return folders

def upload_file_to_drive(service, file_path, file_name, folder_id):
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype='application/pdf', resumable=True)
    file = service.files().create(
        body=file_metadata, 
        media_body=media, 
        fields='id',
        supportsAllDrives=True
    ).execute()
    return file.get('id')

def sanitize_filename(filename):
    # Remove invalid characters
    clean_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    clean_name = clean_name.strip()
    return clean_name if clean_name else "document"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    get_user_data(user_id) # initialize
    await update.message.reply_text(
        "Welcome! Please send me one or multiple images.\n\n"
        "💡 For BEST quality, send images as 'File' (Document) instead of Photos.\n"
        "Once you're done, click the 'Create PDF' button."
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id)

    # Detect if the input is an uncompressed document or a compressed photo
    if update.message.document:
        # User sent a file/document (Best quality)
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name or ""
        if file_name.lower().endswith(('.heic', '.heif')):
            await update.message.reply_text("📸 HEIC image detected — converted automatically.")
    elif update.message.photo:
        # User sent a compressed photo
        # update.message.photo is an array of different sizes; [-1] gets the highest resolution available
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(
            "⚠️ Warning: You sent a compressed photo. For best PDF quality, send your images as a 'File' (Document)."
        )
    else:
        return

    # Download the file from Telegram servers
    file = await context.bot.get_file(file_id)

    # Save to a temporary directory with a unique filename
    temp_dir = tempfile.gettempdir()
    img_path = os.path.join(temp_dir, f"{user_id}_{file_id}.jpg")
    await file.download_to_drive(img_path)

    # Append to the user's specific session list
    udata['images'].append(img_path)

    # Generate the Create PDF and Cancel buttons
    keyboard = [
        [
            InlineKeyboardButton("📄 Create PDF", callback_data="create_pdf"),
            InlineKeyboardButton("❌ Annuler", callback_data="cancel_pdf")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send confirmation message
    await update.message.reply_text(
        f"📸 Received image. Total images: {len(udata['images'])}",
        reply_markup=reply_markup
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    udata = get_user_data(user_id)
    data = query.data

    if data == "create_pdf":
        if not udata['images']:
            await query.message.reply_text("❌ No images found. Please send some images first.")
            return

        udata['state'] = "WAITING_FOR_PDF_NAME"
        await query.message.reply_text("📝 Please enter a name for the PDF file:")

    elif data == "cancel_pdf":
        # Delete all temporarily stored images
        images = udata.get('images', [])
        for img_path in images:
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as cleanup_e:
                    logger.warning(f"Could not delete file {img_path}: {cleanup_e}")
                    
        # Reset user session entirely
        udata['images'] = []
        udata['pdf_path'] = None
        udata['pdf_name'] = None
        udata['state'] = None
        
        await query.edit_message_text("❌ Operation cancelled. You can start again by sending new images.")

    elif data == "upload_drive_start":
        udata['folder_history'] = []
        udata['current_folder_id'] = ROOT_FOLDER_ID
        await show_drive_folder(query, udata)
        
    elif data == "upload_drive_cancel":
        await cleanup_pdf(udata)
        await query.edit_message_text("❌ Upload cancelled. You can send new images to create another PDF.")

    elif data.startswith("nav_back"):
        if udata['folder_history']:
            prev_folder = udata['folder_history'].pop()
            udata['current_folder_id'] = prev_folder
            await show_drive_folder(query, udata)
        else:
            udata['current_folder_id'] = ROOT_FOLDER_ID
            await show_drive_folder(query, udata)

    elif data.startswith("folder_"):
        folder_id = data.split("_")[1]
        udata['folder_history'].append(udata['current_folder_id'])
        udata['current_folder_id'] = folder_id
        await show_drive_folder(query, udata)

    elif data == "upload_here":
        folder_id = udata.get('current_folder_id', ROOT_FOLDER_ID)
        pdf_path = udata.get('pdf_path')
        pdf_name = udata.get('pdf_name', 'document.pdf')
        
        if not pdf_path or not os.path.exists(pdf_path):
            await query.edit_message_text("❌ Error: PDF file not found. It may have been deleted.")
            return
            
        await query.edit_message_text(f"⏳ Uploading '{pdf_name}' to Google Drive...")
        
        try:
            service = get_drive_service()
            upload_file_to_drive(service, pdf_path, pdf_name, folder_id)
            await query.edit_message_text("✅ Successfully uploaded to Google Drive!")
        except Exception as e:
            logger.error(f"Upload error: {e}")
            await query.edit_message_text(f"❌ Failed to upload: {str(e)}")
        finally:
            await cleanup_pdf(udata)

async def show_drive_folder(query, udata):
    folder_id = udata['current_folder_id']
    try:
        service = get_drive_service()
        folders = list_folders(service, folder_id)
    except Exception as e:
        logger.error(f"Google Drive API error: {e}")
        await query.edit_message_text("❌ Failed to access Google Drive. Make sure token.json is valid.")
        return

    keyboard = []
    # Add folder buttons
    for folder in folders:
        cb_data = f"folder_{folder['id']}"
        keyboard.append([InlineKeyboardButton(f"📁 {folder['name']}", callback_data=cb_data)])
    
    # Add "Upload here" button
    keyboard.append([InlineKeyboardButton("📤 Upload here", callback_data="upload_here")])
    
    # Add "Back" button if not in root
    if folder_id != ROOT_FOLDER_ID:
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="nav_back")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Select a subfolder to navigate, or click 'Upload here':", 
        reply_markup=reply_markup
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id)

    if udata.get('state') != "WAITING_FOR_PDF_NAME":
        return

    name = update.message.text
    pdf_name = sanitize_filename(name)
    if not pdf_name.lower().endswith(".pdf"):
        pdf_name += ".pdf"

    udata['pdf_name'] = pdf_name
    images = udata.get('images', [])

    if not images:
        await update.message.reply_text("❌ No images found.")
        udata['state'] = None
        return

    await update.message.reply_text("⏳ Generating PDF, please wait...")

    try:
        def process_image(img_path):
            try:
                img = Image.open(img_path)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                # Upscale if small to improve clarity
                if img.width < 1000:
                    new_width = img.width * 2
                    new_height = img.height * 2
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                return img
            except Exception as e:
                logger.error(f"Failed to open image {img_path}: {e}")
                return None

        first_image = None
        valid_images = []
        
        # Try finding a valid first image
        for idx, img_path in enumerate(images):
            img = process_image(img_path)
            if img is not None:
                first_image = img
                valid_images = images[idx+1:]
                break
                
        if first_image is None:
            raise ValueError("All submitted images were invalid or failed to process.")

        def generate_images():
            for img_path in valid_images:
                img = process_image(img_path)
                if img is not None:
                    yield img

        temp_dir = tempfile.gettempdir()
        pdf_path = os.path.join(temp_dir, f"{user_id}_{pdf_name}")
        
        first_image.save(
            pdf_path, 
            save_all=True, 
            append_images=generate_images(),
            resolution=300.0,
            quality=95
        )
        udata['pdf_path'] = pdf_path

        # Send the document back to user
        with open(pdf_path, "rb") as doc:
            await update.message.reply_document(document=doc, filename=pdf_name)

        # Cleanup images
        for img_path in images:
            if os.path.exists(img_path):
                os.remove(img_path)
        udata['images'] = []
        udata['state'] = None

        # Ask about Drive upload
        keyboard = [
            [InlineKeyboardButton("✅ Yes, upload to Drive", callback_data="upload_drive_start")],
            [InlineKeyboardButton("❌ No, thanks", callback_data="upload_drive_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Do you want to upload this file to Google Drive?", 
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Error generating PDF: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to generate PDF. Please send your images again.")
        
        # Cleanup temporary image files
        for img_path in images:
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as cleanup_error:
                    logger.warning(f"Could not delete file {img_path}: {cleanup_error}")
                    
        # Completely reset the user's session data
        udata['images'] = []
        udata['pdf_path'] = None
        udata['pdf_name'] = None
        udata['state'] = None

async def cleanup_pdf(udata):
    pdf_path = udata.get('pdf_path')
    if pdf_path and os.path.exists(pdf_path):
        os.remove(pdf_path)
    udata['pdf_path'] = None
    udata['pdf_name'] = None
    udata['folder_history'] = []
    udata['current_folder_id'] = ROOT_FOLDER_ID

def main():
    if not TOKEN:
        logger.error("TOKEN environment variable is not set.")
        return
        
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()