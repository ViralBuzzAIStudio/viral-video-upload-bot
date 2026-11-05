import os
import logging
import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from supabase import create_client

# ---------------- CONFIG ----------------

BOT_TOKEN = os.environ["TELEGRAM_UPLOAD_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
PRIVATE_CHANNEL_ID = os.environ["PRIVATE_CHANNEL_ID"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------- HELPERS ----------------

def is_admin(update):
    return (
        update.effective_user
        and update.effective_user.id == ADMIN_USER_ID
    )


async def get_categories():
    """Read existing categories from the current videos table."""
    try:
        result = (
            supabase.table("videos")
            .select("category")
            .limit(1000)
            .execute()
        )

        categories = sorted({
            row["category"].strip()
            for row in (result.data or [])
            if row.get("category") and row["category"].strip()
        })

        return categories or ["Other"]

    except Exception:
        log.exception("Could not read categories")
        return ["Other"]


async def upload_thumbnail(image_bytes, mime_type):
    """Upload an image to the existing videos Storage bucket."""
    extension = mimetypes.guess_extension(mime_type) or ".jpg"
    path = f"thumbnails/{uuid4().hex}{extension}"

    try:
        supabase.storage.from_("videos").upload(
            path,
            image_bytes,
            file_options={
                "content-type": mime_type,
                "upsert": "true",
            },
        )

        return supabase.storage.from_("videos").get_public_url(path)

    except Exception:
        log.exception("Thumbnail upload failed")
        return ""


async def save_thumbnail_from_photo(photo, context):
    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    return await upload_thumbnail(image_bytes, "image/jpeg")


async def save_telegram_video_thumbnail(video, context):
    thumbnail = getattr(video, "thumbnail", None)

    if not thumbnail:
        return ""

    try:
        file = await thumbnail.get_file()
        image_bytes = bytes(await file.download_as_bytearray())
        return await upload_thumbnail(image_bytes, "image/jpeg")
    except Exception:
        log.exception("Automatic thumbnail retrieval failed")
        return ""


async def publish_video(update, context):
    data = context.user_data

    await update.effective_message.reply_text(
        "⏳ ভিডিওটি Private Channel-এ পাঠানো হচ্ছে..."
    )

    try:
        sent = await context.bot.send_video(
            chat_id=PRIVATE_CHANNEL_ID,
            video=data["video_file_id"],
            caption=data["title"],
            supports_streaming=True,
        )

        thumbnail_url = data.get("thumbnail_url", "")

        # Use Telegram's supplied video thumbnail if no custom thumbnail.
        if not thumbnail_url:
            thumbnail_url = await save_telegram_video_thumbnail(
                data["video_object"], context
            )

        duration = data.get("duration")

        record = {
            "title": data["title"],
            "category": data["category"],
            "thumbnail_url": thumbnail_url,
            "video_url": "",
            "duration": duration,
            "telegram_chat_id": str(sent.chat_id),
            "telegram_message_id": sent.message_id,
        }

        result = (
            supabase.table("videos")
            .insert(record)
            .execute()
        )

        if not result.data:
            raise Exception("Supabase did not confirm the video insert.")

        await update.effective_message.reply_text(
            "✅ ভিডিও সফলভাবে আপলোড হয়েছে!\n\n"
            f"🎬 Title: {data['title']}\n"
            f"📂 Category: {data['category']}\n"
            f"🆔 Channel Message ID: {sent.message_id}\n\n"
            "ওয়েবসাইটে ভিডিওটি দেখা যাবে, যদি বর্তমান ওয়েবসাইটের "
            "তালিকা ও Category সেটিংসের সঙ্গে তথ্য মিলে যায়।"
        )

    except Exception as error:
        log.exception("Publishing failed")
        await update.effective_message.reply_text(
            "❌ আপলোড সম্পন্ন হয়নি।\n"
            f"সমস্যা: {str(error)[:700]}"
        )

    finally:
        context.user_data.clear()


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ এই বট শুধু Admin ব্যবহার করতে পারবেন।")
        return

    context.user_data.clear()

    await update.message.reply_text(
        "👋 VIRAL VIDEO HUB AUTO UPLOADER\n\n"
        "প্রথমে আমাকে একটি ভিডিও পাঠাও।\n"
        "তারপর আমি আলাদাভাবে Title চাইব।"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❎ আপলোড বাতিল করা হয়েছে।")


async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    message = update.message

    if not message.video:
        await message.reply_text("দয়া করে Telegram Video হিসেবে পাঠাও।")
        return

    context.user_data.clear()
    context.user_data["video_file_id"] = message.video.file_id
    context.user_data["video_object"] = message.video
    context.user_data["duration"] = message.video.duration

    await message.reply_text(
        "✅ ভিডিও পেয়েছি!\n\n"
        "এখন ভিডিওর নতুন Title লিখে পাঠাও।\n"
        "ভিডিওর Caption-এর ওপর নির্ভর করতে হবে না।"
    )


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if "video_file_id" not in context.user_data:
        return

    title = (update.message.text or "").strip()

    if not title:
        await update.message.reply_text("একটি সঠিক Title লিখে পাঠাও।")
        return

    context.user_data["title"] = title

    categories = await get_categories()

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"cat:{name}")]
        for name in categories
    ]

    await update.message.reply_text(
        "📂 এবার ভিডিওর Category নির্বাচন করো:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Admin access required.")
        return

    category = query.data[4:]
    categories = await get_categories()

    if category not in categories:
        await query.edit_message_text(
            "এই Category আর পাওয়া যাচ্ছে না। /start দিয়ে আবার শুরু করো।"
        )
        return

    context.user_data["category"] = category

    await query.edit_message_text(
        f"✅ Category: {category}\n\n"
        "এখন চাইলে একটি Custom Thumbnail ছবি পাঠাও।\n"
        "না চাইলে /skip লিখে পাঠাও।"
    )


async def receive_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if "category" not in context.user_data:
        return

    if not update.message.photo:
        await update.message.reply_text(
            "Thumbnail হিসেবে একটি ছবি পাঠাও অথবা /skip লিখো।"
        )
        return

    photo = update.message.photo[-1]

    await update.message.reply_text("⏳ Thumbnail সংরক্ষণ করছি...")

    thumbnail_url = await save_thumbnail_from_photo(photo, context)
    context.user_data["thumbnail_url"] = thumbnail_url

    if thumbnail_url:
        await update.message.reply_text("✅ Thumbnail সংরক্ষণ হয়েছে।")
    else:
        await update.message.reply_text(
            "⚠️ Thumbnail Storage-এ সংরক্ষণ করা যায়নি। "
            "ভিডিওর Telegram thumbnail পাওয়া গেলে সেটি ব্যবহার করা হবে।"
        )

    await publish_video(update, context)


async def skip_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if "category" not in context.user_data:
        return

    context.user_data["thumbnail_url"] = ""
    await publish_video(update, context)


# ---------------- MAIN ----------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("skip", skip_thumbnail))

    app.add_handler(
        MessageHandler(filters.VIDEO, receive_video)
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_thumbnail,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            choose_category,
            pattern=r"^cat:",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_title,
        )
    )

    log.info("VIRAL VIDEO HUB Upload Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
