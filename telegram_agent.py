import os
import re
import json
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
from arabic_reshaper import reshape

try:
    from bidi import get_display
except ImportError:  # الإصدارات القديمة من python-bidi
    from bidi.algorithm import get_display


# ============================================================
# 1. خادم صحي بسيط لمنصة Render (يرد "OK" فقط)
# ============================================================
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

# ============================================================
# 2. الإعدادات والسجلات المؤقتة
# ============================================================
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip()

# اسم المشروع (يمكن تغييره من متغير بيئة PROJECT_NAME في Render)
PROJECT_NAME = (os.environ.get("PROJECT_NAME") or "مكادي مول").strip()

ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
ANTHROPIC_FALLBACKS = ["claude-haiku-5-5"]
GROQ_BASE = "https://api.groq.com/openai/v1"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

site_notes = []      # [{"ts": "...", "text": "..."}]  نصوص وتسجيلات صوتية مفرّغة
site_photos = []     # [{"path", "caption", "timestamp"}]
site_supplies = []   # توريدات مسجلة صراحةً بأمر /supply
site_emails = []     # بريد وارد مسجل صراحةً بأمر /mail


def now_local():
    """التوقيت المحلي لمصر (خوادم Render تعمل بتوقيت UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))


MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def arabic_date(d):
    return f"{d.day} {MONTHS_AR[d.month - 1]} {d.year}".translate(AR_DIGITS)


# ============================================================
# 3. تحويل الصوت إلى نص (Groq Whisper)
# ============================================================
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
                "prompt": "ملاحظات موقع إنشاءات: خرسانة، حديد تسليح، شدات، صب، أعمدة، كمرات، بلاطة، أساسات، "
                          "توريد، شاحنة، أسمنت، رمل، بحص، أنابيب، مقاول، مهندس، استشاري، إيميل.",
            }
            res = requests.post(f"{GROQ_BASE}/audio/transcriptions",
                                headers=headers, files=files, data=data, timeout=60)
        if res.status_code == 200:
            return res.json().get("text", ""), None
        return None, f"خطأ من المزود ({res.status_code}): {res.text[:200]}"
    except Exception as e:
        return None, str(e)


# ============================================================
# 4. الذكاء الاصطناعي (Anthropic ثم Groq)
# ============================================================
def get_groq_chat_models():
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


def ask_ai(prompt, system):
    errors = []

    if client:
        for model_name in [ANTHROPIC_MODEL] + ANTHROPIC_FALLBACKS:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=2000,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in response.content if b.type == "text")
                if text.strip():
                    return text, None
            except Exception as e:
                errors.append(f"Anthropic ({model_name}): {e}")

    if GROQ_API_KEY:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        for g_model in get_groq_chat_models()[:3]:
            try:
                payload = {
                    "model": g_model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}],
                    "max_tokens": 2000,
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

    return None, " | ".join(errors) if errors else "لم يتم الوصول لمزود ذكاء اصطناعي مفعّل."


CLASSIFY_SYSTEM = "أنت مساعد لمهندس موقع إنشاءات. مهمتك تصنيف ملاحظات اليوم وإرجاعها بصيغة JSON فقط دون أي نص إضافي."

CLASSIFY_PROMPT = """صنّف الملاحظات الميدانية التالية إلى ثلاث قوائم، وأعد JSON صالحاً فقط بهذا الشكل:
{{"works": [], "supplies": [], "emails": []}}

القواعد:
- works: الأعمال المنفذة والملاحظات العامة، كل بند جملة عربية مختصرة.
- supplies: المواد الواردة للموقع. اذكر الصنف والكمية وحالة الاستلام إن وردت، مثل: حديد تسليح (مشرط) - 15 طن (تم الاستلام والتفريغ).
- emails: رسائل البريد الإلكتروني الواردة، بصيغة: من: الجهة - ملخص الموضوع.
- صحّح الأخطاء الإملائية الواضحة الناتجة عن التفريغ الصوتي.
- لا تضف أي معلومة أو رقم أو جهة غير موجودة في الملاحظات.
- إذا لم يوجد شيء لقائمة فاتركها فارغة.

الملاحظات:
{notes}"""


def parse_report_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key in ("works", "supplies", "emails"):
        v = data.get(key, [])
        out[key] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    return out


def classify_by_keywords(notes):
    """تصنيف احتياطي بسيط عند تعذر الذكاء الاصطناعي."""
    mail_kw = ("إيميل", "ايميل", "بريد", "email", "mail")
    supply_kw = ("توريد", "استلام", "شاحنة", "شاحنات", "تفريغ")
    out = {"works": [], "supplies": [], "emails": []}
    for n in notes:
        low = n.lower()
        if any(k in low for k in mail_kw):
            out["emails"].append(n)
        elif any(k in low for k in supply_kw):
            out["supplies"].append(n)
        else:
            out["works"].append(n)
    return out


def build_report():
    """يبني بنود التقرير. يرجع (report, used_ai, error)."""
    report = {"works": [], "supplies": [], "emails": []}
    used_ai, error = False, None
    notes = [n["text"] for n in site_notes]

    if notes:
        text, error = ask_ai(CLASSIFY_PROMPT.format(notes="\n".join(f"- {n}" for n in notes)),
                             CLASSIFY_SYSTEM)
        parsed = parse_report_json(text) if text else None
        if parsed:
            report, used_ai, error = parsed, True, None
        else:
            report = classify_by_keywords(notes)
            error = error or "تعذر قراءة رد الذكاء الاصطناعي."

    report["supplies"] = list(site_supplies) + report["supplies"]
    report["emails"] = list(site_emails) + report["emails"]
    return report, used_ai, error


# ============================================================
# 5. تقرير PDF بخط عربي (على نمط القالب)
# ============================================================
FONT_FAMILY = "Amiri"
FONT_REGULAR = os.path.join("fonts", "Amiri-Regular.ttf")
FONT_BOLD = os.path.join("fonts", "Amiri-Bold.ttf")

_EMOJI_RE = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]")


def clean_text(text):
    text = _EMOJI_RE.sub("", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    return text


def ar(text):
    return get_display(reshape(text), base_dir="R")


def wrap_rtl(pdf, text, max_w):
    """تقسيم النص إلى أسطر بترتيبه المنطقي ثم تشكيل كل سطر على حدة."""
    lines = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        current = ""
        for word in para.split():
            candidate = f"{current} {word}".strip()
            if current and pdf.get_string_width(ar(candidate)) > max_w:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return [ar(line) for line in lines] or [""]


class PDFReport(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 8, f"{self.page_no()}/{{nb}}", align="C")


def ensure_space(pdf, h):
    if pdf.get_y() + h > pdf.h - pdf.b_margin:
        pdf.add_page()


def reset_draw(pdf):
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.2)


# ----- الأيقونات -----
def icon_check(pdf, cx, cy):
    r = 4
    pdf.set_fill_color(46, 160, 67)
    pdf.ellipse(cx - r, cy - r, 2 * r, 2 * r, style="F")
    pdf.set_draw_color(255, 255, 255)
    pdf.set_line_width(0.7)
    pdf.line(cx - 2.2, cy + 0.1, cx - 0.6, cy + 1.9)
    pdf.line(cx - 0.6, cy + 1.9, cx + 2.4, cy - 1.8)
    reset_draw(pdf)


def icon_mail(pdf, cx, cy):
    w, h = 8, 5.5
    x, y = cx - w / 2, cy - h / 2
    pdf.set_draw_color(70, 70, 70)
    pdf.set_line_width(0.4)
    pdf.rect(x, y, w, h)
    pdf.line(x, y, cx, cy + 0.5)
    pdf.line(x + w, y, cx, cy + 0.5)
    reset_draw(pdf)


def icon_dot(pdf, cx, cy):
    r = 1.4
    pdf.set_fill_color(30, 50, 90)
    pdf.ellipse(cx - r, cy - r, 2 * r, 2 * r, style="F")


# ----- مكوّنات الصفحة -----
def draw_header(pdf):
    pdf.set_font(FONT_FAMILY, "B", 22)
    pdf.cell(0, 12, ar("تقرير نشاط الموقع اليومي"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(2)
    pdf.set_font(FONT_FAMILY, "", 12)
    half = pdf.epw / 2
    pdf.cell(half, 7, ar(f"التاريخ: {arabic_date(now_local().date())}"),
             new_x=XPos.RIGHT, new_y=YPos.TOP, align="L")
    pdf.cell(half, 7, ar("تقرير يومية موقع إنشاءات"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
    pdf.set_font(FONT_FAMILY, "B", 13)
    pdf.cell(0, 8, ar(f"اسم المشروع: {PROJECT_NAME}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
    pdf.ln(1)
    y = pdf.get_y()
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
    reset_draw(pdf)
    pdf.ln(5)


def section_bar(pdf, title):
    ensure_space(pdf, 22)
    pdf.set_font(FONT_FAMILY, "B", 13)
    pdf.set_fill_color(214, 214, 214)
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    pdf.set_x(pdf.l_margin)
    pdf.cell(pdf.epw, 9, ar(title), border=1, fill=True,
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
    reset_draw(pdf)


def draw_row(pdf, text, icon=None):
    """صف نصي بمحاذاة يمين، وأيقونة اختيارية في العمود الأيمن."""
    ROW_LH = 6.5
    x0, w, icon_w = pdf.l_margin, pdf.epw, 14
    text_w = w - icon_w - 4
    pdf.set_font(FONT_FAMILY, "", 12)
    lines = wrap_rtl(pdf, clean_text(text), text_w - 2 * pdf.c_margin)
    h = max(11, len(lines) * ROW_LH + 4)
    ensure_space(pdf, h)
    y = pdf.get_y()

    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    pdf.rect(x0, y, w, h)
    pdf.line(x0 + w - icon_w, y, x0 + w - icon_w, y + h)
    reset_draw(pdf)

    ty = y + (h - len(lines) * ROW_LH) / 2
    pdf.set_font(FONT_FAMILY, "", 12)
    for i, line in enumerate(lines):
        pdf.set_xy(x0 + 2, ty + i * ROW_LH)
        pdf.cell(text_w, ROW_LH, line, align="R")

    if icon:
        icon(pdf, x0 + w - icon_w / 2, y + h / 2)

    pdf.set_xy(x0, y + h)


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def draw_photo_row(pdf, pair):
    """صف صور: الأولى في العمود الأيمن والثانية في الأيسر، وتحت كل صورة تعليقها."""
    gap, img_h, cap_lh = 4, 62, 5.5
    x0 = pdf.l_margin
    col_w = (pdf.epw - 3 * gap) / 2

    pdf.set_font(FONT_FAMILY, "", 11)
    caps = [wrap_rtl(pdf, clean_text(p["caption"]), col_w - 2 * pdf.c_margin)[:3] for p in pair]
    cap_lines = max(len(c) for c in caps)
    h = gap + img_h + 2 + cap_lines * cap_lh + gap

    ensure_space(pdf, h)
    y = pdf.get_y()
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    pdf.rect(x0, y, pdf.epw, h)
    reset_draw(pdf)

    for i, (photo, cap) in enumerate(zip(pair, caps)):
        cx = x0 + pdf.epw - gap - col_w - i * (col_w + gap)
        size = image_size(photo["path"]) if os.path.exists(photo["path"]) else None
        try:
            if size:
                iw, ih = size
                scale = min(col_w / iw, img_h / ih)
                w_, h_ = iw * scale, ih * scale
                pdf.image(photo["path"], x=cx + (col_w - w_) / 2, y=y + gap + (img_h - h_) / 2, w=w_, h=h_)
            elif os.path.exists(photo["path"]):
                pdf.image(photo["path"], x=cx, y=y + gap, w=col_w)
        except Exception as e:
            print("Image error:", e)
        pdf.set_font(FONT_FAMILY, "", 11)
        for j, line in enumerate(cap):
            pdf.set_xy(cx, y + gap + img_h + 2 + j * cap_lh)
            pdf.cell(col_w, cap_lh, line, align="R")

    pdf.set_xy(x0, y + h)


def draw_signatures(pdf):
    h, gap = 30, 4
    ensure_space(pdf, h + 8)
    pdf.ln(8)
    y, x0 = pdf.get_y(), pdf.l_margin
    bw = (pdf.epw - gap) / 2
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    # اليمين: توقيع مهندس الموقع | اليسار: اعتماد مدير المشروع
    for i, label in enumerate(["توقيع مهندس الموقع", "اعتماد مدير المشروع"]):
        bx = x0 + pdf.epw - bw - i * (bw + gap)
        pdf.rect(bx, y, bw, h)
        pdf.set_font(FONT_FAMILY, "B", 12)
        pdf.set_xy(bx, y + 2)
        pdf.cell(bw, 7, ar(label), align="R")
    reset_draw(pdf)
    pdf.set_xy(x0, y + h)


def generate_pdf_report(report, photos_list):
    if not os.path.exists(FONT_REGULAR):
        raise FileNotFoundError(f"ملف الخط العربي غير موجود: {FONT_REGULAR}")

    pdf = PDFReport(format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font(FONT_FAMILY, "", FONT_REGULAR)
    pdf.add_font(FONT_FAMILY, "B", FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_REGULAR)
    pdf.alias_nb_pages()
    pdf.add_page()

    draw_header(pdf)

    # 1) صور الموقع
    section_bar(pdf, "حالة الموقع اليوم (الصور)")
    if photos_list:
        for i in range(0, len(photos_list), 2):
            draw_photo_row(pdf, photos_list[i:i + 2])
    else:
        draw_row(pdf, "لا توجد صور ميدانية اليوم.")
    pdf.ln(4)

    # 2) الأعمال المنفذة والملاحظات
    if report["works"]:
        section_bar(pdf, "الأعمال المنفذة والملاحظات")
        for item in report["works"]:
            draw_row(pdf, item, icon_dot)
        pdf.ln(4)

    # 3) التوريدات
    section_bar(pdf, "التوريدات التي تمت خلال اليوم")
    if report["supplies"]:
        for item in report["supplies"]:
            draw_row(pdf, item, icon_check)
    else:
        draw_row(pdf, "لا توجد توريدات مسجلة اليوم.")
    pdf.ln(4)

    # 4) البريد الإلكتروني الوارد
    section_bar(pdf, "البريد الإلكتروني الوارد (الإيميلات المستلمة)")
    if report["emails"]:
        for item in report["emails"]:
            draw_row(pdf, item, icon_mail)
    else:
        draw_row(pdf, "لا يوجد بريد وارد مسجل اليوم.")

    # 5) التوقيعات
    draw_signatures(pdf)

    os.makedirs("reports", exist_ok=True)
    pdf_path = f"reports/Site_Report_{now_local().strftime('%Y%m%d')}.pdf"
    pdf.output(pdf_path)
    return pdf_path


def format_report_text(report):
    """نسخة نصية مختصرة من التقرير للإرسال داخل تليجرام."""
    def block(title, items):
        if not items:
            return ""
        return f"\n{title}\n" + "\n".join(f"• {i}" for i in items) + "\n"

    return (f"📋 تقرير نشاط الموقع اليومي\nالمشروع: {PROJECT_NAME}\n"
            f"التاريخ: {arabic_date(now_local().date())}\n"
            + block("🔧 الأعمال والملاحظات:", report["works"])
            + block("✅ التوريدات:", report["supplies"])
            + block("✉️ البريد الوارد:", report["emails"]))


# ============================================================
# 6. معالجات تليجرام
# ============================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🏗️ وكيل التقارير الميدانية - مشروع {PROJECT_NAME}\n\n"
        "• أرسل ملاحظاتك نصاً أو تسجيلاً صوتياً (يُصنَّف تلقائياً إلى أعمال وتوريدات وبريد).\n"
        "• أرسل الصور الميدانية مع تعليق عليها.\n"
        "• /supply نص التوريد  لتسجيل توريد صراحةً.\n"
        "• /mail نص الرسالة  لتسجيل بريد وارد صراحةً.\n"
        "• /report لاستخراج التقرير اليومي وملف PDF.\n"
        "• /clear لبدء يوم جديد ومسح السجلات."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    site_notes.append({"ts": now_local().strftime("%Y-%m-%d %H:%M"), "text": text})
    await update.message.reply_text(f"📝 تم تسجيل الملاحظة: '{text}'")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ جاري تحويل التسجيل الصوتي إلى نص عربي...")

    voice_file = await update.message.voice.get_file()
    os.makedirs("voice_notes", exist_ok=True)

    now = now_local()
    file_path = f"voice_notes/voice_{now.strftime('%H%M%S')}.ogg"
    await voice_file.download_to_drive(file_path)

    transcribed_text, error = await asyncio.to_thread(transcribe_audio, file_path)

    if transcribed_text and transcribed_text.strip():
        site_notes.append({"ts": now.strftime("%Y-%m-%d %H:%M"), "text": transcribed_text.strip()})
        await update.message.reply_text(f"✅ تم تفريغ الملاحظة الصوتية:\n\n\"{transcribed_text}\"")
    else:
        await update.message.reply_text(f"⚠️ تعذر تحويل الصوت إلى نص ولم تُسجَّل الملاحظة: {error}\nأعد الإرسال أو اكتبها نصاً.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = await update.message.photo[-1].get_file()
    os.makedirs("photos", exist_ok=True)

    now = now_local()
    file_path = f"photos/photo_{len(site_photos) + 1}_{now.strftime('%H%M%S')}.jpg"
    await photo_file.download_to_drive(file_path)

    caption = update.message.caption or "صورة ميدانية"
    site_photos.append({"path": file_path, "caption": caption, "timestamp": now.strftime("%Y-%m-%d %H:%M")})
    await update.message.reply_text(f"📸 تم حفظ الصورة: '{caption}'")


async def supply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("اكتب التوريد بعد الأمر، مثال:\n/supply حديد تسليح (مشرط) - 15 طن (تم الاستلام والتفريغ)")
        return
    site_supplies.append(text)
    await update.message.reply_text(f"✅ تم تسجيل التوريد: '{text}'")


async def mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("اكتب الرسالة بعد الأمر، مثال:\n/mail من: الاستشاري - اعتماد تعديلات المخططات الإنشائية")
        return
    site_emails.append(text)
    await update.message.reply_text(f"✉️ تم تسجيل البريد الوارد: '{text}'")


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (site_notes or site_photos or site_supplies or site_emails):
        await update.message.reply_text("⚠️ لا توجد أي ملاحظات أو صور مسجلة حتى الآن اليوم.")
        return

    await update.message.reply_text("⏳ جاري تصنيف الملاحظات وتوليد التقرير وملف الـ PDF...")

    report, used_ai, err = await asyncio.to_thread(build_report)
    if err:
        print("AI classification failed:", err)  # التفاصيل في سجلات Render فقط

    text = format_report_text(report)
    if not used_ai and site_notes:
        text += "\n(تم التصنيف تلقائياً بدون الذكاء الاصطناعي، راجع البنود.)"
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])

    try:
        pdf_path = await asyncio.to_thread(generate_pdf_report, report, list(site_photos))
        with open(pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=os.path.basename(pdf_path),
                caption=f"📄 تقرير نشاط الموقع اليومي - {PROJECT_NAME}",
                read_timeout=60,
                write_timeout=60,
            )
    except Exception as pdf_err:
        await update.message.reply_text(f"⚠️ تم إرسال التقرير النصي، وتوقف الـ PDF بسبب: {pdf_err}")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    site_notes.clear()
    site_photos.clear()
    site_supplies.clear()
    site_emails.clear()
    await update.message.reply_text("🧹 تم مسح سجلات اليوم وبدء سجل جديد.")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("report", handle_report))
        app.add_handler(CommandHandler("clear", clear_command))
        app.add_handler(CommandHandler("supply", supply_command))
        app.add_handler(CommandHandler("mail", mail_command))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
        app.add_handler(MessageHandler(filters.VOICE, handle_voice))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

        print("=== بوت تليجرام يعمل الآن ومستعد لاستقبال النص والصوت والصور ===")
        app.run_polling()
