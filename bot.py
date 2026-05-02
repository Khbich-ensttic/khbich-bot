import os
import re
import tempfile
import logging
import json
import mimetypes
from functools import wraps
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand
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
ADMIN_ID = os.getenv("ADMIN_ID", "YOUR_ADMIN_ID_HERE")
ROOT_FOLDER_ID = "1pFEXGM_O5fkFtfzc-yjJp4YXN9VYQlqY"
SCOPES = ['https://www.googleapis.com/auth/drive']

FOLDERS = {
    "Khbich-ensttic": ROOT_FOLDER_ID,
    "Khbich-exams": "1Zk_-aOP2OTlvLcZRMfuzsNRlAc6632HM",
    "Khbich-contribution": "1Opeox98_mc4IHOVROkwT-c1r27Nzt52LAUKhKE1OeKeNzRBhf_KOQ0LlHyUb9P-yUogw4yx8"
}

# User Access System
MODERATORS_FILE = "moderators.json"
MODERATORS = {}

def load_users():
    global MODERATORS
    if os.path.exists(MODERATORS_FILE):
        with open(MODERATORS_FILE, "r") as f:
            try:
                data = json.load(f)
                users_data = data.get("moderators", [])
                
                # Check for old format (list of IDs) vs new format (dict)
                if isinstance(users_data, list):
                    # Migrate old list to dictionary format
                    MODERATORS = {str(uid): {"username": None, "first_name": "Unknown"} for uid in users_data}
                elif isinstance(users_data, dict):
                    MODERATORS = users_data
                else:
                    MODERATORS = {}
            except json.JSONDecodeError:
                MODERATORS = {}

def save_users():
    with open(MODERATORS_FILE, "w") as f:
        json.dump({"moderators": MODERATORS}, f)

load_users()

def is_admin_or_mod(user_id):
    uid = str(user_id)
    return uid == str(ADMIN_ID) or uid in MODERATORS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or str(user.id) != str(ADMIN_ID):
            if update.message:
                await update.message.reply_text("⛔️ Admin command only.")
            return
            
        return await func(update, context, *args, **kwargs)
    return wrapper

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
            'current_folder_id': FOLDERS["Khbich-contribution"],
            'current_folder_name': 'Khbich-contribution',
            'root_folder_key': 'Khbich-contribution',
            'folder_page': 0,
            'folder_cache': {}
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

def get_folder_info(service, folder_id):
    try:
        folder = service.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()
        return folder.get('name', 'Unknown Folder')
    except Exception as e:
        logger.error(f"Error fetching folder info for {folder_id}: {e}")
        return "Unknown Folder"

def list_folders(service, folder_id):
    print(f"Navigating to folder: {folder_id}")
    query = f"'{folder_id}' in parents and (mimeType='application/vnd.google-apps.folder' or mimeType='application/vnd.google-apps.shortcut') and trashed=false"
    folders = []
    page_token = None
    
    try:
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
    except Exception as e:
        logger.error(f"Error fetching folders for {folder_id}: {e}")
        # Return empty list on permission or API failure instead of crashing
        return []

    return folders

def upload_file_to_drive(service, file_path, file_name, folder_id):
    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type:
        mime_type = 'application/octet-stream'
        
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
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

def get_drive_keyboard(user_id, cancel_callback="main_menu"):
    keyboard = []
    if is_admin_or_mod(user_id):
        keyboard.append([InlineKeyboardButton("📁 Khbich-ensttic", callback_data="open_root_Khbich-ensttic")])
        keyboard.append([InlineKeyboardButton("📁 Khbich-exams", callback_data="open_root_Khbich-exams")])
        keyboard.append([InlineKeyboardButton("📁 Khbich-contribution", callback_data="open_root_Khbich-contribution")])
    
    if cancel_callback == "upload_drive_cancel":
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)])
    else:
        keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data=cancel_callback)])
        
    return InlineKeyboardMarkup(keyboard)

@admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_user <user_id>")
        return
        
    user_id_str = context.args[0]
    if not user_id_str.isdigit():
        await update.message.reply_text("❌ Error: user_id must be numeric.")
        return
        
    user_id = str(user_id_str)
    
    if user_id in MODERATORS:
        await update.message.reply_text("⚠️ Moderator already exists.")
        return
        
    # Set state for the admin to enter the user's name
    admin_id = update.message.from_user.id
    udata = get_user_data(admin_id)
    udata['state'] = "WAITING_FOR_NAME"
    udata['pending_user_id'] = user_id
    
    await update.message.reply_text(f"✏️ Send the moderator name for ID {user_id}:")

@admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_user <user_id>")
        return
        
    user_id = str(context.args[0])
    if user_id in MODERATORS:
        del MODERATORS[user_id]
        save_users()
        await update.message.reply_text(f"✅ Moderator {user_id} removed from allowed list.")
    else:
        await update.message.reply_text("❌ Moderator not found.")

@admin_only
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MODERATORS:
        await update.message.reply_text("📝 Moderators list is currently empty.")
    else:
        sorted_users = sorted(MODERATORS.items(), key=lambda x: x[1].get("first_name", ""))
        lines = []
        for uid, info in sorted_users:
            first_name = info.get("first_name", "Unknown")
            username = info.get("username")
            
            if username:
                lines.append(f"• {first_name} (@{username}) - {uid}")
            else:
                lines.append(f"• {first_name} - {uid}")
                
        users = "\n".join(lines)
        await update.message.reply_text(f"📝 Moderators:\n{users}")

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [KeyboardButton("📄 Create PDF"), KeyboardButton("📂 Google Drive")],
        [KeyboardButton("❌ Cancel")]
    ]
    
    if str(user_id) == str(ADMIN_ID):
        keyboard.append([
            KeyboardButton("👤 Add Moderator"),
            KeyboardButton("📋 List Moderators")
        ])
        keyboard.append([
            KeyboardButton("🗑️ Remove Moderator")
        ])
        
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    text = "🗂 **Main Menu**\nSelect an option below to continue:"
    
    if update.callback_query:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        try:
            await update.callback_query.message.delete()
        except:
            pass
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id) # initialize
    cleanup_user_files(udata)
    
    keyboard = [[InlineKeyboardButton("🚀 Start", callback_data="start_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Welcome to the Khbich Bot! 📚\nClick the button below to get started.",
        reply_markup=reply_markup
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id)

    is_image = False
    file_id = None
    file_name = None

    # Detect if the input is an uncompressed document, compressed photo, or video
    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name or "document"
        mime_type = update.message.document.mime_type or ""
        
        if mime_type.startswith('image/') or file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp')):
            is_image = True
            if file_name.lower().endswith(('.heic', '.heif')):
                await update.message.reply_text("📸 HEIC image detected — converted automatically.")
    elif update.message.photo:
        # User sent a compressed photo
        file_id = update.message.photo[-1].file_id
        file_name = f"photo_{file_id}.jpg"
        is_image = True
        await update.message.reply_text(
            "⚠️ Warning: You sent a compressed photo. For best PDF quality, send your images as a 'File' (Document)."
        )
    elif update.message.video:
        file_id = update.message.video.file_id
        file_name = update.message.video.file_name or f"video_{file_id}.mp4"
    else:
        return

    # Download the file from Telegram servers
    file = await context.bot.get_file(file_id)
    temp_dir = tempfile.gettempdir()
    
    # Safe filename for temp storage
    safe_name = sanitize_filename(file_name)
    file_path = os.path.join(temp_dir, f"{user_id}_{file_id}_{safe_name}")
    await file.download_to_drive(file_path)

    if is_image:
        # Append to the user's specific session list
        udata['images'].append(file_path)

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
    else:
        # Non-image file -> upload directly
        udata['pdf_path'] = file_path
        udata['pdf_name'] = file_name
        
        if not is_admin_or_mod(user_id):
            udata['folder_history'] = []
            udata['current_folder_id'] = FOLDERS['Khbich-contribution']
            udata['current_folder_name'] = 'Khbich-contribution'
            udata['root_folder_key'] = 'Khbich-contribution'
            udata['folder_page'] = 0
            await show_drive_folder(update, udata)
        else:
            reply_markup = get_drive_keyboard(user_id, cancel_callback="upload_drive_cancel")
            await update.message.reply_text(
                f"📎 Received '{file_name}'. Select a Drive to navigate and upload this file:", 
                reply_markup=reply_markup
            )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    user_id = query.from_user.id
    udata = get_user_data(user_id)
    data = query.data

    # Admin actions access control
    admin_callbacks = ["admin_add_user", "admin_list_users", "admin_remove_user_menu"]
    if data in admin_callbacks or data.startswith("remove_user_"):
        if str(user_id) != str(ADMIN_ID):
            await query.answer("⛔️ Access denied. Admin only.", show_alert=True)
            return

    if data == "cancel_pdf":
        images = udata.get('images', [])
        if len(images) > 0:
            await query.answer("❌ Operation cancelled.")
        else:
            await query.answer()
        cleanup_user_files(udata)
        await show_main_menu(update, context)
        return

    await query.answer()

    if data == "main_menu":
        udata['state'] = None
        udata['pending_user_id'] = None
        await show_main_menu(update, context)
        return

    elif data == "start_menu":
        udata['state'] = None
        udata['pending_user_id'] = None
        await show_main_menu(update, context)
        return

    elif data == "create_pdf_prompt":
        udata['state'] = "WAITING_FOR_IMAGES"
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await query.edit_message_text(
            "📸 Please send me the images you want to convert to PDF.\n\n"
            "💡 For BEST quality, send images as 'File' (Document) instead of Photos.\n"
            "Once you are done uploading, click the 'Create PDF' button below the images.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif data == "open_drive":
        if not is_admin_or_mod(user_id):
            udata['folder_history'] = []
            udata['current_folder_id'] = FOLDERS['Khbich-contribution']
            udata['current_folder_name'] = 'Khbich-contribution'
            udata['root_folder_key'] = 'Khbich-contribution'
            udata['folder_page'] = 0
            await show_drive_folder(query, udata)
        else:
            reply_markup = get_drive_keyboard(user_id)
            await query.edit_message_text(
                "Select a Drive to browse:", 
                reply_markup=reply_markup
            )
        return

    elif data == "admin_add_user":
        udata['state'] = "WAITING_FOR_USER_ID"
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await query.edit_message_text(
            "👤 Please enter the Telegram ID of the moderator you want to add:", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif data == "admin_remove_user_menu":
        if not MODERATORS:
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
            await query.edit_message_text("📝 No moderators to remove.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
            
        keyboard = []
        for uid, info in sorted(MODERATORS.items(), key=lambda x: x[1].get("first_name", "")):
            name = info.get("first_name", "Unknown")
            keyboard.append([InlineKeyboardButton(f"🗑️ {name} ({uid})", callback_data=f"remove_user_{uid}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
        
        await query.edit_message_text(
            "Select a moderator to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
        
    elif data.startswith("remove_user_"):
        uid_to_remove = data.replace("remove_user_", "")
        if uid_to_remove in MODERATORS:
            del MODERATORS[uid_to_remove]
            save_users()
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
            await query.edit_message_text(f"✅ Moderator {uid_to_remove} removed.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
            await query.edit_message_text("❌ Moderator not found.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "admin_list_users":
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        if not MODERATORS:
            await query.edit_message_text("📝 Moderators list is currently empty.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            sorted_users = sorted(MODERATORS.items(), key=lambda x: x[1].get("first_name", ""))
            lines = []
            for uid, info in sorted_users:
                first_name = info.get("first_name", "Unknown")
                username = info.get("username")
                
                if username:
                    lines.append(f"• {first_name} (@{username}) - {uid}")
                else:
                    lines.append(f"• {first_name} - {uid}")
                    
            users = "\n".join(lines)
            await query.edit_message_text(f"📝 Moderators:\n{users}", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "create_pdf":
        if not udata['images']:
            await query.message.reply_text("❌ No images found. Please send some images first.")
            return

        udata['state'] = "WAITING_FOR_PDF_NAME"
        await query.message.reply_text("📝 Please enter a name for the PDF file:")

    elif data.startswith("open_root_"):
        folder_key = data.replace("open_root_", "")
        if folder_key not in FOLDERS:
            await query.edit_message_text("❌ Error: Invalid folder selection.")
            return
            
        if not is_admin_or_mod(user_id) and folder_key != "Khbich-contribution":
            await query.answer("⛔️ Access denied. Public users can only access Khbich-contribution.", show_alert=True)
            return
            
        folder_id = FOLDERS[folder_key]
        
        try:
            service = get_drive_service()
            folder_name = get_folder_info(service, folder_id)
        except Exception:
            folder_name = folder_key
            
        udata['folder_history'] = []
        udata['current_folder_id'] = folder_id
        udata['current_folder_name'] = folder_name
        udata['root_folder_key'] = folder_key
        udata['folder_page'] = 0
        await show_drive_folder(query, udata)
        return

    elif data == "upload_drive_start":
        if not is_admin_or_mod(user_id):
            udata['folder_history'] = []
            udata['current_folder_id'] = FOLDERS['Khbich-contribution']
            udata['current_folder_name'] = 'Khbich-contribution'
            udata['root_folder_key'] = 'Khbich-contribution'
            udata['folder_page'] = 0
            await show_drive_folder(query, udata)
        else:
            reply_markup = get_drive_keyboard(user_id, cancel_callback="upload_drive_cancel")
            await query.edit_message_text(
                "Select a Drive to navigate and upload this file:", 
                reply_markup=reply_markup
            )
        return
        
    elif data == "upload_drive_cancel":
        cleanup_user_files(udata)
        await query.edit_message_text("❌ Upload cancelled.")

    elif data == "drive_prev":
        udata['folder_page'] = max(0, udata.get('folder_page', 0) - 1)
        await show_drive_folder(query, udata)
        
    elif data == "drive_next":
        udata['folder_page'] = udata.get('folder_page', 0) + 1
        await show_drive_folder(query, udata)

    elif data.startswith("nav_back"):
        if udata.get('folder_history'):
            prev_folder = udata['folder_history'].pop()
            udata['current_folder_id'] = prev_folder.get('id', FOLDERS["Khbich-contribution"])
            udata['current_folder_name'] = prev_folder.get('name', 'Drive')
            udata['folder_page'] = prev_folder.get('page', 0)
            await show_drive_folder(query, udata)
        else:
            if not is_admin_or_mod(user_id):
                keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
                await query.edit_message_text("You are at the root.", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                reply_markup = get_drive_keyboard(user_id)
                await query.edit_message_text(
                    "Select a Drive to browse:", 
                    reply_markup=reply_markup
                )

    elif data.startswith("folder_"):
        folder_id = data.replace("folder_", "").strip()
        
        folder_name = None
        curr_id = udata.get('current_folder_id')
        if curr_id in udata.get('folder_cache', {}):
            for f in udata['folder_cache'][curr_id]:
                if f['id'] == folder_id:
                    folder_name = f['name']
                    break
        
        if not folder_name:
            if not is_admin_or_mod(user_id):
                await query.answer("⛔️ Access denied.", show_alert=True)
                return
            try:
                service = get_drive_service()
                folder_name = get_folder_info(service, folder_id)
            except Exception:
                folder_name = "Folder"
                
        udata.setdefault('folder_history', []).append({
            'id': curr_id,
            'name': udata.get('current_folder_name', 'Drive'),
            'page': udata.get('folder_page', 0)
        })
        
        udata['current_folder_id'] = folder_id
        udata['current_folder_name'] = folder_name
        udata['folder_page'] = 0
        await show_drive_folder(query, udata)

    elif data == "upload_here":
        if not is_admin_or_mod(user_id) and udata.get('root_folder_key') != 'Khbich-contribution':
            await query.answer("⛔️ Access denied. You can only upload to Khbich-contribution.", show_alert=True)
            return
            
        folder_id = udata.get('current_folder_id', FOLDERS['Khbich-contribution'])
        pdf_path = udata.get('pdf_path')
        pdf_name = udata.get('pdf_name', 'document.pdf')
        
        if not pdf_path or not os.path.exists(pdf_path):
            await query.edit_message_text("❌ Error: File not found. It may have been deleted.")
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
            cleanup_user_files(udata)

async def show_drive_folder(update_or_query, udata):
    if isinstance(update_or_query, Update):
        user_id = update_or_query.effective_user.id
    else:
        user_id = update_or_query.from_user.id

    folder_id = udata.get('current_folder_id', FOLDERS["Khbich-contribution"])
    folder_name = udata.get('current_folder_name', 'Drive')
    page = udata.get('folder_page', 0)
    
    if 'folder_cache' not in udata:
        udata['folder_cache'] = {}
        
    msg = None
    folders = udata['folder_cache'].get(folder_id)
    
    if not folders:
        if isinstance(update_or_query, Update):
            msg = await update_or_query.message.reply_text(f"⏳ Loading folders in '{folder_name}'...")
        else:
            await update_or_query.edit_message_text(f"⏳ Loading folders in '{folder_name}'...")
            msg = update_or_query.message
            
        try:
            service = get_drive_service()
            folders = list_folders(service, folder_id)
            if folders:
                udata['folder_cache'][folder_id] = folders
        except Exception as e:
            logger.error(f"Google Drive API error: {e}")
            text = "❌ Failed to access Google Drive. Make sure token.json is valid."
            if msg:
                try:
                    await msg.edit_text(text)
                except:
                    pass
            elif isinstance(update_or_query, Update):
                await update_or_query.message.reply_text(text)
            else:
                await update_or_query.edit_message_text(text)
            return

    ITEMS_PER_PAGE = 5
    total_pages = max(1, (len(folders) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    if page >= total_pages:
        page = total_pages - 1
        udata['folder_page'] = page
        
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_folders = folders[start_idx:end_idx]

    keyboard = []
    # Add folder buttons
    for folder in page_folders:
        cb_data = f"folder_{folder['id']}"
        keyboard.append([InlineKeyboardButton(f"📁 {folder['name']}", callback_data=cb_data)])
    
    # Pagination buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="drive_prev"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data="drive_next"))
    if nav_row:
        keyboard.append(nav_row)

    # Action buttons
    keyboard.append([InlineKeyboardButton("📤 Upload here", callback_data="upload_here")])
    
    if udata.get('folder_history'):
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="nav_back")])
    else:
        # If at root, the back button goes to the Drive selection menu
        if not is_admin_or_mod(user_id):
            if udata.get('pdf_path'):
                keyboard.append([InlineKeyboardButton("❌ Cancel Upload", callback_data="upload_drive_cancel")])
            else:
                keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
        else:
            keyboard.append([InlineKeyboardButton("⬅️ Back to Drive List", callback_data="open_drive")])
            if udata.get('pdf_path'):
                keyboard.append([InlineKeyboardButton("❌ Cancel Upload", callback_data="upload_drive_cancel")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Create breadcrumbs
    history_names = [item.get('name', 'Unknown') for item in udata.get('folder_history', [])]
    if history_names:
        if len(history_names) > 2:
            path_str = "... / " + " / ".join(history_names[-2:]) + f" / {folder_name}"
        else:
            path_str = " / ".join(history_names) + f" / {folder_name}"
    else:
        path_str = folder_name
        
    if folders:
        text = f"📂 Current Location: {path_str}\n\nSelect a subfolder or click 'Upload here':"
        if total_pages > 1:
            text += f"\n(Page {page + 1}/{total_pages})"
    else:
        text = f"📂 Current Location: {path_str}\n\n📂 This folder is empty"
    
    if msg:
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
        except:
            pass
    elif isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text(text, reply_markup=reply_markup)

class AdminStateFilter(filters.MessageFilter):
    def filter(self, message):
        user_id = message.from_user.id
        if str(user_id) != str(ADMIN_ID):
            return False
        udata = get_user_data(user_id)
        return udata.get('state') in ["WAITING_FOR_USER_ID", "WAITING_FOR_NAME"]

admin_state_filter = AdminStateFilter()

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id)
    state = udata.get('state')
    
    text = update.message.text.strip()
    
    if text.lower() == "cancel":
        udata['state'] = None
        udata['pending_user_id'] = None
        await update.message.reply_text("❌ Operation cancelled.")
        return
        
    if state == "WAITING_FOR_USER_ID":
        if not text.isdigit():
            await update.message.reply_text("❌ Error: User ID must be numeric. Please try again or type 'cancel'.")
            return
            
        if text in MODERATORS:
            udata['state'] = None
            await update.message.reply_text(f"⚠️ Moderator {text} already exists.")
            return
            
        udata['pending_user_id'] = text
        udata['state'] = "WAITING_FOR_NAME"
        await update.message.reply_text(f"✏️ Send the moderator name for ID {text}:")
        return
        
    elif state == "WAITING_FOR_NAME":
        if not text:
            await update.message.reply_text("❌ Name cannot be empty. Send again or type 'cancel'.")
            return
            
        pending_user_id = udata.get('pending_user_id')
        if not pending_user_id:
            udata['state'] = None
            return
            
        MODERATORS[pending_user_id] = {
            "first_name": text,
            "username": None
        }
        save_users()
        
        udata['state'] = None
        udata['pending_user_id'] = None
        
        await update.message.reply_text(f"✅ Moderator {text} ({pending_user_id}) added successfully.")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    udata = get_user_data(user_id)
    state = udata.get('state')
    text = update.message.text.strip()

    # Main Menu Reply Keyboard Actions
    if text == "📄 Create PDF":
        udata['state'] = "WAITING_FOR_IMAGES"
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
        await update.message.reply_text(
            "📸 Please send me the images you want to convert to PDF.\n\n"
            "💡 For BEST quality, send images as 'File' (Document) instead of Photos.\n"
            "Once you are done uploading, click the 'Create PDF' button below the images.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
        
    elif text == "📂 Google Drive":
        if not is_admin_or_mod(user_id):
            udata['folder_history'] = []
            udata['current_folder_id'] = FOLDERS['Khbich-contribution']
            udata['current_folder_name'] = 'Khbich-contribution'
            udata['root_folder_key'] = 'Khbich-contribution'
            udata['folder_page'] = 0
            await show_drive_folder(update, udata)
        else:
            reply_markup = get_drive_keyboard(user_id)
            await update.message.reply_text(
                "Select a Drive to browse:", 
                reply_markup=reply_markup
            )
        return
        
    elif text == "❌ Cancel":
        cleanup_user_files(udata)
        await show_main_menu(update, context)
        return
        
    elif str(user_id) == str(ADMIN_ID):
        if text == "👤 Add Moderator":
            udata['state'] = "WAITING_FOR_USER_ID"
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
            await update.message.reply_text(
                "👤 Please enter the Telegram ID of the moderator you want to add:", 
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
            
        elif text == "📋 List Moderators":
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
            if not MODERATORS:
                await update.message.reply_text("📝 Moderators list is currently empty.", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                sorted_users = sorted(MODERATORS.items(), key=lambda x: x[1].get("first_name", ""))
                lines = []
                for uid, info in sorted_users:
                    first_name = info.get("first_name", "Unknown")
                    username = info.get("username")
                    
                    if username:
                        lines.append(f"• {first_name} (@{username}) - {uid}")
                    else:
                        lines.append(f"• {first_name} - {uid}")
                        
                users = "\n".join(lines)
                await update.message.reply_text(f"📝 Moderators:\n{users}", reply_markup=InlineKeyboardMarkup(keyboard))
            return
            
        elif text == "🗑️ Remove Moderator":
            if not MODERATORS:
                keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")]]
                await update.message.reply_text("📝 No moderators to remove.", reply_markup=InlineKeyboardMarkup(keyboard))
                return
                
            keyboard = []
            for uid, info in sorted(MODERATORS.items(), key=lambda x: x[1].get("first_name", "")):
                name = info.get("first_name", "Unknown")
                keyboard.append([InlineKeyboardButton(f"🗑️ {name} ({uid})", callback_data=f"remove_user_{uid}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="main_menu")])
            
            await update.message.reply_text(
                "Select a moderator to remove:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    if state != "WAITING_FOR_PDF_NAME":
        return

    name = text
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

        # Ask about Drive upload
        if not is_admin_or_mod(user_id):
            udata['folder_history'] = []
            udata['current_folder_id'] = FOLDERS['Khbich-contribution']
            udata['current_folder_name'] = 'Khbich-contribution'
            udata['root_folder_key'] = 'Khbich-contribution'
            udata['folder_page'] = 0
            await show_drive_folder(update, udata)
        else:
            reply_markup = get_drive_keyboard(user_id, cancel_callback="upload_drive_cancel")
            await update.message.reply_text(
                "Select a Drive to navigate and upload this file:", 
                reply_markup=reply_markup
            )

        # Cleanup images
        images_list = udata.get('images', [])
        for img_path in images_list:
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                    logger.info(f"Deleted temp image: {img_path}")
                except Exception as cleanup_error:
                    logger.warning(f"Could not delete file {img_path}: {cleanup_error}")
        udata['images'] = []
        udata['state'] = None

    except Exception as e:
        logger.error(f"Error generating PDF: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to generate PDF. Please send your images again.")
        cleanup_user_files(udata)

def cleanup_user_files(udata):
    # Cleanup temporary image files
    images = udata.get('images', [])
    for img_path in images:
        if os.path.exists(img_path):
            try:
                os.remove(img_path)
                logger.info(f"Deleted temp image: {img_path}")
            except Exception as cleanup_error:
                logger.warning(f"Could not delete file {img_path}: {cleanup_error}")
                
    # Cleanup generated PDF file
    pdf_path = udata.get('pdf_path')
    if pdf_path and os.path.exists(pdf_path):
        try:
            os.remove(pdf_path)
            logger.info(f"Deleted temp PDF: {pdf_path}")
        except Exception as cleanup_error:
            logger.warning(f"Could not delete PDF {pdf_path}: {cleanup_error}")
            
    # Completely reset the user's session data
    udata['images'] = []
    udata['pdf_path'] = None
    udata['pdf_name'] = None
    udata['state'] = None
    udata['folder_history'] = []
    udata['current_folder_id'] = FOLDERS["Khbich-contribution"]
    udata['current_folder_name'] = 'Khbich-contribution'
    udata['root_folder_key'] = 'Khbich-contribution'
    udata['folder_page'] = 0

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Show main menu"),
        BotCommand("add_user", "Add a moderator (Admin)"),
        BotCommand("remove_user", "Remove a moderator (Admin)"),
        BotCommand("list_users", "List all moderators (Admin)")
    ])

def main():
    if not TOKEN:
        logger.error("TOKEN environment variable is not set.")
        return
        
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_user", add_user))
    app.add_handler(CommandHandler("remove_user", remove_user))
    app.add_handler(CommandHandler("list_users", list_users))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, handle_file))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & admin_state_filter, handle_admin_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()