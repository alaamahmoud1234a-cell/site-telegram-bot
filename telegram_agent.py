import os
import asyncio
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from fpdf import FPDF
from fpdf.enums import XPos, YPos


# 1. خادم صحي بسيط لمنصة Render (يرد "OK" فقط، ولا يعرض ملفات السيرفر)
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()

# 2. السجلات المؤقتة
site_notes = []
site_photos = []

# 3. المفاتيح
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip()

# يمكنك تغيير النموذج من إعدادات Render دون تعديل الكود
ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
ANTHROPIC_FALLBACKS = ["claude-haiku-5-5"]

GROQ_BASE = "https://api.groq.com/openai/v1"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def safe_latin_text(text):
    return text.encode("latin-1", "ignore").decode("latin-1")


# 4. تحويل الصوت إلى نص (Groq Whisper)
def transcribe_audio(file_path):
    if not GROQ_API_KEY:
        return None, "لم يتم ضبط مفتاح GROQ_API_KEY في إعدادات البيئة (Render)."

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "audio/ogg")}
            data = {
                "model": "whisper-large-v3",
                "language": "ar",
                "temperature": "0",
                # كلمات مساعدة ترفع دقة التعرف على مصطلحات الموقع
                "prompt": "ملاحظات موقع إنشاءات: خرسانة، حديد تسليح، شدات، صب، أعمدة، كمرات، بلاطة، أساسات، مقاول، مهندس، مورد.",
            }
            res = requests.post(f"{GROQ_BASE}/audio/transcriptions",
                                headers=headers, files=files, data=data, timeout=60)
        if res.status_code == 200:
            return res.json().get("text", ""), None
        return None, f"خطأ من المزود ({res.status_code}): {res.text[:200]}"
    except Exception as e:
        return None, str(e)


# 5. توليد صياغة التقرير (Anthropic ثم Groq)
def get_groq_chat_models():
    """يجلب قائمة النماذج المتاحة فعلاً في حسابك بدل كتابتها يدوياً."""
    try:
        res = requests.get(f"{GROQ_BASE}/models",
                           headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=15)
        if res.status_code != 200:
            return []
        skip = ("whisper", "guard", "tts", "playai", "orpheus", "safeguard", "embed")
        ids = [m["id"] for m in res.json().get("data", [])
               if m.get("active", True) and not any(s in m["id"].lower() for s in skip)]

        def rank(model_id):
            m = model_id.lower()
            return (0 if "llama" in m else 1, 0 if ("70b" in m or "120b" in m) else 1, m)

        return sorted(ids, key=rank)
    except Exception:
        return []


def generate_ai_summary(prompt):
    errors = []

    # المحاولة 1: Anthropic
    if client:
        for model_name in [ANTHROPIC_MODEL] + ANTHROPIC_FALLBACKS:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=1500,
                    system="أنت مهندس ميداني خبير. اكتب تقريراً تنفيذياً منسقاً ومحترفاً باللغة العربية.",
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in response.content if b.type == "text")
                if text.strip():
                    return text, None
            except Exception as e:
                errors.append(f"Anthropic ({model_name}): {e}")

    # المحاولة 2: Groq
    if GROQ_API_KEY:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        for g_model in get_groq_chat_models()[:3]:
            try:
                payload = {
                    "model": g_model,
                    "messages": [
                        {"role": "system", "content": "أنت مهندس ميداني خبير. اكتب تقريراً تنفيذياً منسقاً ومحترفاً باللغة العربية."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 1500,
                }
                res = requests.post(f"{GROQ_BASE}/chat/completions",
                                    headers=headers, json=payload, timeout=60)
                if res.status_code == 200:
                    choices = res.json().get("choices", [])
                    if choices:
                        return choices[0]["message"]["content"], None
                errors.append(f"Groq ({g_model}): {res.status_code} {res.text[:150]}")
            except Exception as e:
                errors.append(f"Groq ({g_model}): {e}")

    error_details = " | ".join(errors) if errors else "لم يتم الوصول لمزود ذكاء اصطناعي مفعّل."
    return None, error_details


# 6. تقرير PDF
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Daily Construction Site Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.set_font("Helvetica", "I", 10)
        self.cell(0, 8, f"Date: {datetime.date.today().strftime('%Y-%m-%d')}",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.line(10, 30, 200, 30)
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")


def generate_pdf_report(summary_text, notes_list, photos_list):
    pdf = PDFReport()
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "1. Executive Summary & Site Log:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 10)
    if notes_list:
        clean_notes = "\n".join(f"- {safe_latin_text(n)}" for n in notes_list)
        if not clean_notes.replace("-", "").strip():
            clean_notes = "Site notes recorded (Arabic text saved for message report)."
        pdf.multi_cell(0, 6, clean_notes)
    else:
        pdf.cell(0, 6, "No text notes recorded today.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "2. Field Photos Gallery:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    if photos_list:
        for idx, photo in enumerate(photos_list, 1):
            pdf.set_font("Helvetica", "B", 10)
            safe_caption = safe_latin_text(photo["caption"]) or "Photo Note"
            pdf.cell(0, 6, f"Photo #{idx} [{photo['timestamp']}]: {safe_caption}",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
            if os.path.exists(photo["path"]):
                try:
                    pdf.image(photo["path"], w=120)
                except Exception as e:
                    pdf.set_font("Helvetica", "I", 9)
                    pdf.cell(0, 6, f"(Error rendering image: {e})", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(8)
    else:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, "No field photos uploaded today.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    os.makedirs("reports", exist_ok=True)
    pdf_path = f"reports/Site_Report_{datetime.date.today().strftime('%Y%m%d')}.pdf"
    pdf.output(pdf_path)
    return pdf_path


# 7. معالجات تليجرام
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏗️ مرحباً بك في وكيل الملاحظات والتقارير الميدانية!\n\n"
        "• أرسل الملاحظات النصية أو التسجيلات الصوتية مباشرة.\n"
        "• أرسل الصور الميدانية مع التعليق عليها.\n"
        "• أرسل /report لاستخراج التقرير اليومي وملف PDF.\n"
        "• أرسل /clear لبدء يوم عمل جديد ومسح الملاحظات القديمة."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    site_notes.append(f"[{timestamp}] {text}")
    await update.message.reply_text(f"📝 تم تسجيل الملاحظة النصية: '{text}'")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ جاري تحويل التسجيل الصوتي إلى نص عربي...")

    voice_file = await update.message.voice.get_file()
    os.makedirs("voice_notes", exist_ok=True)

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    file_path = f"voice_notes/voice_{now.strftime('%H%M%S')}.ogg"
    await voice_file.download_to_drive(file_path)

    # استدعاء شبكي متزامن: ننقله لخيط منفصل حتى لا يتجمد البوت
    transcribed_text, error = await asyncio.to_thread(transcribe_audio, file_path)

    if transcribed_text:
        site_notes.append(f"[{timestamp}] 🎙️ ملاحظة صوتية: {transcribed_text}")
        await update.message.reply_text(f"✅ تم تفريغ الملاحظة الصوتية:\n\n\"{transcribed_text}\"")
    else:
        site_notes.append(f"[{timestamp}] 🎙️ تسجيل صوتي تم إرساله")
        await update.message.reply_text(f"⚠️ تعذر تحويل الصوت إلى نص: {error}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = await update.message.photo[-1].get_file()
    os.makedirs("photos", exist_ok=True)

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    file_path = f"photos/photo_{len(site_photos) + 1}_{now.strftime('%H%M%S')}.jpg"
    await photo_file.download_to_drive(file_path)

    caption = update.message.caption or "صورة ميدانية بدون تعليق"
    site_photos.append({"path": file_path, "caption": caption, "timestamp": timestamp})
    site_notes.append(f"[{timestamp}] 📷 صورة: {caption}")
    await update.message.reply_text(f"📸 تم حفظ الصورة الميدانية بنجاح: '{caption}'")


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not site_notes and not site_photos:
        await update.message.reply_text("⚠️ لا توجد أي ملاحظات أو صور مسجلة حتى الآن اليوم.")
        return

    await update.message.reply_text("⏳ جاري تحليل الملاحظات وتوليد التقرير وملف الـ PDF...")

    notes_text = "\n".join(site_notes)
    prompt = ("قم بصياغة تقرير ميداني تنفيذي منسق ومحترف باللغة العربية "
              f"بناءً على الملاحظات التالية:\n\n{notes_text}")

    ai_summary, err = await asyncio.to_thread(generate_ai_summary, prompt)
    if ai_summary:
        summary_text = f"📋 التقرير الميداني اليومي:\n\n{ai_summary}"
    else:
        print("AI summary failed:", err)  # التفاصيل في سجلات Render فقط
        summary_text = ("📋 التقرير الميداني اليومي (بدون صياغة آلية):\n\n" + notes_text +
                        "\n\n(تعذرت الصياغة بالذكاء الاصطناعي، راجع سجلات الخادم)")

    # تليجرام يحدّ الرسالة بـ 4096 حرفاً
    for i in range(0, len(summary_text), 4000):
        await update.message.reply_text(summary_text[i:i + 4000])

    try:
        pdf_path = await asyncio.to_thread(generate_pdf_report, summary_text, site_notes, site_photos)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as pdf_file:
                await update.message.reply_document(
                    document=pdf_file,
                    filename=os.path.basename(pdf_path),
                    caption="📄 التقرير اليومي الميداني (PDF) مدمجاً به الصور الميدانية.",
                    read_timeout=60,
                    write_timeout=60,
                )
    except Exception as pdf_err:
        await update.message.reply_text(f"⚠️ تم إرسال التقرير النصي، وتوقف الـ PDF بسبب: {pdf_err}")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global site_notes, site_photos
    site_notes = []
    site_photos = []
    await update.message.reply_text("🧹 تم مسح جميع ملاحظات وصور اليوم وبدء سجل جديد.")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("report", handle_report))
        app.add_handler(CommandHandler("clear", clear_command))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
        app.add_handler(MessageHandler(filters.VOICE, handle_voice))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

        print("=== بوت تليجرام يعمل الآن ومستعد لاستقبال النص والصوت والصور ===")
        app.run_polling()
