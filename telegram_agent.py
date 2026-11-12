import os
import datetime
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from fpdf import FPDF
from fpdf.enums import XPos, YPos

# 1. خادم وهمي لمنصة Render لتفادي توقف الخدمة (Port 10000)
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    httpd = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# 2. السجلات المؤقتة للملاحظات والصور
site_notes = []
site_photos = []

# 3. إعداد مفاتيح التشغيل
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

def safe_latin_text(text):
    """تنظيف النص لتجنب مشاكل خطوط PDF"""
    return text.encode("latin-1", "ignore").decode("latin-1")

# 4. دالة تحويل الصوت إلى نص باستخدام Groq Whisper API
def transcribe_audio(file_path):
    if not GROQ_API_KEY:
        return None, "لم يتم ضبط مفتاح GROQ_API_KEY في إعدادات البيئة (Render)."
    
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "audio/ogg")}
            data = {"model": "whisper-large-v3", "language": "ar"}
            api_url = "[suspicious link removed]"
            res = requests.post(api_url, headers=headers, files=files, data=data)
            if res.status_code == 200:
                return res.json().get("text", ""), None
            else:
                return None, f"خطأ من المزود ({res.status_code})"
    except Exception as e:
        return None, str(e)

# 5. دالة توليد تقرير PDF
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Daily Construction Site Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.set_font("Helvetica", "I", 10)
        self.cell(0, 8, f"Date: {datetime.date.today().strftime('%Y-%m-%d')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
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
        clean_notes = "\n".join([f"- {safe_latin_text(n)}" for n in notes_list])
        if not clean_notes.strip():
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
            caption_text = f"Photo #{idx} [{photo['timestamp']}]: {safe_caption}"
            pdf.cell(0, 6, caption_text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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

# 6. معالجات تليجرام
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🏗️ **مرحباً بك في وكيل الملاحظات والتقارير الميدانية!**\n\n"
        "• أرسل الملاحظات النصية أو التسجيلات الصوتية مباشرة.\n"
        "• أرسل الصور الميدانية مع التعليق عليها.\n"
        "• أرسل الأمر `/report` لاستخراج التقرير اليومي وتوليد ملف PDF.\n"
        "• أرسل الأمر `/clear` لبدء يوم عمل جديد ومسح الملاحظات القديمة."
    )
    await update.message.reply_text(welcome_text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    formatted_note = f"[{timestamp}] {text}"
    site_notes.append(formatted_note)
    await update.message.reply_text(f"📝 تم تسجيل الملاحظة النصية: '{text}'")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ جاري تحويل التسجيل الصوتي إلى نص عربي...")
    
    voice_file = await update.message.voice.get_file()
    os.makedirs("voice_notes", exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    time_stamp_str = datetime.datetime.now().strftime("%H%M%S")
    file_path = f"voice_notes/voice_{time_stamp_str}.ogg"
    
    await voice_file.download_to_drive(file_path)
    
    transcribed_text, error = transcribe_audio(file_path)
    
    if transcribed_text:
        formatted_note = f"[{timestamp}] 🎙️ ملاحظة صوتية: {transcribed_text}"
        site_notes.append(formatted_note)
        await update.message.reply_text(f"✅ تم تفريغ الملاحظة الصوتية بنجاح:\n\n\"{transcribed_text}\"")
    else:
        site_notes.append(f"[{timestamp}] 🎙️ تسجيل صوتي تم إرساله")
        await update.message.reply_text(f"⚠️ تعذر تحويل الصوت إلى نص: {error}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = await update.message.photo[-1].get_file()
    os.makedirs("photos", exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    time_stamp_str = datetime.datetime.now().strftime("%H%M%S")
    file_path = f"photos/photo_{len(site_photos) + 1}_{time_stamp_str}.jpg"
    
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
    prompt = f"قم بصياغة تقرير ميداني تنفيذي منسق ومحترف باللغة العربية بناءً على الملاحظات التالية:\n\n{notes_text}"
    
    summary_text = "التقرير الميداني اليومي:\n" + notes_text
    if client:
        try:
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            summary_text = response.content[0].text
        except Exception as e:
            summary_text += f"\n\n(ملاحظة: تعذر استدعاء الذكاء الاصطناعي للصياغة: {e})"
    
    await update.message.reply_text(summary_text)
    
    try:
        pdf_path = generate_pdf_report(summary_text, site_notes, site_photos)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as pdf_file:
                await update.message.reply_document(
                    document=pdf_file,
                    filename=os.path.basename(pdf_path),
                    caption="📄 التقرير اليومي الميداني المنسق (PDF) مدمجاً به الصور الميدانية."
                )
    except Exception as pdf_err:
        await update.message.reply_text(f"⚠️ تم إرسال التقرير النصي بنجاح، وتوقف الـ PDF بسبب: {pdf_err}")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global site_notes, site_photos
    site_notes = []
    site_photos = []
    await update.message.reply_text("🧹 تم مسح جميع ملاحظات وصور اليوم وبدء سجل جديد بنجاح.")

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
