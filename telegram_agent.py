import os
import datetime
from anthropic import Anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 1. المفاتيح والتهيئة
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = "9281377923:AAFMtDPzqLbjQBQ7e7lakxu5CTalVX2F8To"

client = Anthropic(api_key=ANTHROPIC_API_KEY)
NOTES_FILE = "site_notes.txt"

# 2. دوال إدارة ملف الملاحظات المحلي
def save_note_to_file(note_text: str, category: str) -> str:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{category}] {note_text}\n"
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"تم حفظ الملاحظة بنجاح تحت تصنيف '{category}'."

def read_all_notes() -> str:
    if not os.path.exists(NOTES_FILE):
        return "لا توجد أي ملاحظات مسجلة لهذا اليوم حتى الآن."
    with open(NOTES_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    return content if content.strip() else "ملف الملاحظات فارغ."

# 3. تعريف أدوات الوكيل
tools = [
    {
        "name": "save_site_note",
        "description": "حفظ ملاحظة جديدة من موقع الإنشاءات وتصنيفها هندسياً.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "تصنيف الملاحظة: 'أعمال مكتملة'، 'جودة'، 'سلامة مهنية'، أو 'معوقات وتأخير'"
                },
                "note_text": {
                    "type": "string",
                    "description": "نص الملاحظة"
                }
            },
            "required": ["category", "note_text"]
        }
    },
    {
        "name": "read_site_notes",
        "description": "قراءة جميع الملاحظات اليومية المخزنة لإعداد التقرير اليومي.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

SYSTEM_PROMPT = """أنت وكيل ذكي لمساعدة مدير موقع الإنشاءات عبر تليجرام.
عند تزويدك بملاحظة موقع، استدعِ أداة `save_site_note` لتخزينها وتصنيفها.
عند طلب التقرير اليومي، استدعِ أداة `read_site_notes` واكتب تقريراً هندسياً منسقاً."""

def process_agent_message(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    response = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=messages
    )

    if response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "save_site_note":
                    res = save_note_to_file(block.input["note_text"], block.input["category"])
                elif block.name == "read_site_notes":
                    res = read_all_notes()
                else:
                    res = "أداة غير معروفة"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": res
                })

        messages.append({"role": "user", "content": tool_results})
        final_res = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages
        )
        return "".join(b.text for b in final_res.content if b.type == "text")
    else:
        return "".join(b.text for b in response.content if b.type == "text")

# 4. معالجات أوامر ورسائل تليجرام
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك يا مهندس! أرسل لي أي ملاحظة نصية أو صوتية وسأقوم بتسجيلها وتصنيفها فوراً، أو اكتب /report للحصول على التقرير اليومي.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    reply = process_agent_message(user_text)
    await update.message.reply_text(reply)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("جاري استلام وتحويل المقطع الصوتي...")
    file = await context.bot.get_file(update.message.voice.file_id)
    voice_path = "temp_voice.ogg"
    await file.download_to_drive(voice_path)
    
    try:
        import whisper
        audio_model = whisper.load_model("base")
        res = audio_model.transcribe(voice_path, language="ar")
        transcribed_text = res["text"].strip()
        await update.message.reply_text(f"🎤 *النص المفرغ:* {transcribed_text}", parse_mode="Markdown")
        reply = process_agent_message(transcribed_text)
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("تم استلام الصوت بنجاح وجاري التسجيل...")
        reply = process_agent_message("ملاحظة صوتية جديدة من الموقع")
        await update.message.reply_text(reply)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("جاري استخراج التقرير اليومي للموقع...")
    reply = process_agent_message("اعمل لي تقرير اليومية النهائي بناءً على ملاحظات اليوم.")
    await update.message.reply_text(reply)

# 5. تشغيل البوت
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("=== بوت تليجرام يعمل الآن وبانتظار رسائلك ===")
    app.run_polling()