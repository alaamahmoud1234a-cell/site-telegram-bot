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

# اسم مهندس الموقع صاحب هذه النسخة (متغير بيئة ENGINEER_NAME) - يظهر في الـ PDF
ENGINEER_NAME = (os.environ.get("ENGINEER_NAME") or "").strip()

# معرّف تليجرام الرقمي لمدير المشروع: تصله نسخة تلقائية من كل تقرير (اختياري)
_admin = re.findall(r"-?\d+", os.environ.get("ADMIN_TELEGRAM_ID") or "")
ADMIN_TELEGRAM_ID = int(_admin[0]) if _admin else None

# معرّفات تليجرام المسموح لها باستخدام البوت (اختياري، مفصولة بفواصل). فارغ = متاح للجميع
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_USER_IDS") or "")}

# رقم أول تقرير لهذا البوت (يُستخدم عند بدء العدّاد أو بعد إعادة نشر الخدمة)
_start = re.findall(r"\d+", os.environ.get("REPORT_START_NUMBER") or "")
REPORT_START_NUMBER = int(_start[0]) if _start else 1
COUNTER_FILE = os.path.join("data", "report_counter.json")

ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
ANTHROPIC_FALLBACKS = ["claude-haiku-5-5"]
GROQ_BASE = "https://api.groq.com/openai/v1"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

site_notes = []      # [{"ts": "...", "text": "..."}]  نصوص وتسجيلات صوتية مفرّغة
site_photos = []     # [{"path", "caption", "voice", "timestamp", "msg_id"}]
last_photo = {"idx": None, "at": None}  # آخر صورة بانتظار تعليق صوتي
PHOTO_VOICE_WINDOW = 600  # ثوانٍ: مهلة ربط التسجيل الصوتي بالصورة السابقة


def now_local():
    """التوقيت المحلي لمصر (خوادم Render تعمل بتوقيت UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))


MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
EN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def arabic_date(d):
    return f"{d.day} {MONTHS_AR[d.month - 1]} {d.year}".translate(AR_DIGITS)


def day_and_date(d):
    return f"{DAYS_AR[d.weekday()]} {arabic_date(d)}"


def get_report_number():
    """رقم تسلسلي للتقرير: يزيد مرة واحدة لكل يوم، وإعادة إصدار تقرير اليوم تحتفظ برقمه."""
    today = now_local().date().isoformat()
    try:
        data = {}
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE, encoding="utf-8") as f:
                data = json.load(f)
        last = data.get("number")
        if data.get("date") == today and isinstance(last, int):
            return last
        number = last + 1 if isinstance(last, int) else REPORT_START_NUMBER
        os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "number": number}, f)
        return number
    except Exception as e:
        print("Report counter error:", e)
        return REPORT_START_NUMBER


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
                          "توريد، شاحنة، أسمنت، رمل، بحص، أنابيب، مقاول، مهندس، استشاري، نجارون، حدادون، "
                          "ونش، خلاطة، مضخة، الدور الثاني، المحور.",
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
                    max_tokens=3500,
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
                    "max_tokens": 3500,
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


CLASSIFY_SYSTEM = ("أنت مساعد مدير مشروع إنشاءات يعدّ التقارير اليومية. مهمتك تحويل ملاحظات مهندس الموقع "
                   "إلى بيانات منظمة وإرجاعها بصيغة JSON فقط دون أي نص إضافي.")

CLASSIFY_PROMPT = """حوّل ملاحظات مهندس الموقع التالية إلى بيانات تقرير يومي منظمة. أعد JSON صالحاً فقط (دون أي نص قبله أو بعده) بهذا الشكل بالضبط:
{
 "weather": "",
 "summary": {"achievement": "", "obstacle": "", "request": ""},
 "manpower": [{"trade": "", "count": null, "contractor": ""}],
 "equipment": [{"name": "", "count": null, "status": ""}],
 "works": [{"activity": "", "location": "", "quantity": "", "status": ""}],
 "supplies": [{"item": "", "quantity": "", "supplier": "", "receipt": "", "inspection": ""}],
 "issues": [{"description": "", "responsible": "", "needed_by": "", "priority": ""}],
 "photos": [{"n": 1, "activity": "", "location": ""}]
}

القواعد الصارمة:
1. استخدم فقط ما ورد في الملاحظات. لا تخمّن ولا تضف رقماً أو كمية أو اسم مورد أو جهة أو تاريخاً لم يُذكر. الحقل غير المذكور يُترك "" (أو null للأعداد)، والقائمة التي لا بيانات لها تُترك [].
2. كل الأقسام اختيارية، ولا تُنشئ بنداً لمجرد ملء قسم فارغ.
3. صياغة مهنية مختصرة: جمل بالماضي المبني للمجهول مثل "تم صب أعمدة الدور الثاني"، دون صفات أو مبالغة أو عبارات عامية.
4. quantity: الكمية مع وحدتها كما ذُكرت (مثل "12 م³" أو "15 طن" أو "400 كيس").
5. works.status إحدى القيم: منجز / جارٍ / متأخر / متوقف، أو "" إن لم يتضح.
6. equipment.status: تعمل / معطلة أو "". issues.priority: عالية / متوسطة / منخفضة أو "".
7. manpower: كل تخصص بعدده (نجارون، حدادون، عمال...). contractor اسم مقاول الباطن إن ذُكر، أو "" إن كانوا عمالة المقاول الرئيسي.
8. weather: اذكر الطقس فقط إن ورد وكان مؤثراً على العمل.
9. summary: جملة واحدة لكل حقل: أهم إنجاز، أهم معوّق، أهم ما يُطلب من الإدارة. يُترك "" إن لم يتضح من الملاحظات.
10. issues: المعوقات والمشكلات وما يُطلب من الإدارة أو الاستشاري أو الجهات الأخرى. needed_by التاريخ أو الموعد المطلوب كما ذُكر.
11. supplies: المواد الواردة للموقع. receipt حالة الاستلام (مثل: تم الاستلام والتفريغ)، inspection حالة فحص الجودة والاعتماد إن ذُكرت.
12. الملاحظات الموسومة [توريد] أو [عمالة] أو [معوق] أو [طقس] تخص القسم المقابل (supplies / manpower / issues / weather).
13. photos: لكل صورة ورد لها وصف استخرج النشاط والموقع (الدور أو المحور) فقط مما ورد، وn هو رقم الصورة.
14. صحّح أخطاء التفريغ الصوتي الواضحة للمصطلحات الإنشائية (خرسانة، شدات، حديد تسليح، كمرات، بلاطة، أساسات...).

الملاحظات:
<<NOTES>>

وصف الصور (رقم الصورة: وصفها):
<<PHOTOS>>"""


def _s(v):
    return str(v).strip() if v is not None else ""


def _count(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d+", _s(v).translate(EN_DIGITS))
    return int(m.group()) if m else None


def empty_report():
    return {
        "weather": "",
        "summary": {"achievement": "", "obstacle": "", "request": ""},
        "manpower": [], "equipment": [], "works": [], "supplies": [], "issues": [],
        "photos": {},
        "number": 0,
    }


def _rows(data, key, fields):
    out = []
    items = data.get(key)
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            row = {f: _s(it.get(f)) for f in fields}
            if any(row.values()):
                out.append(row)
    return out


def normalize_report(data):
    r = empty_report()
    if not isinstance(data, dict):
        return r
    r["weather"] = _s(data.get("weather"))
    s = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    for k in r["summary"]:
        r["summary"][k] = _s(s.get(k))

    items = data.get("manpower")
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            row = {"trade": _s(it.get("trade")), "count": _count(it.get("count")),
                   "contractor": _s(it.get("contractor"))}
            if row["trade"] or row["count"] is not None:
                r["manpower"].append(row)

    items = data.get("equipment")
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            row = {"name": _s(it.get("name")), "count": _count(it.get("count")),
                   "status": _s(it.get("status"))}
            if row["name"]:
                r["equipment"].append(row)

    r["works"] = _rows(data, "works", ("activity", "location", "quantity", "status"))
    r["supplies"] = _rows(data, "supplies", ("item", "quantity", "supplier", "receipt", "inspection"))
    r["issues"] = _rows(data, "issues", ("description", "responsible", "needed_by", "priority"))

    items = data.get("photos")
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            n, a, loc = _count(it.get("n")), _s(it.get("activity")), _s(it.get("location"))
            if n and (a or loc):
                r["photos"][n] = {"activity": a, "location": loc}
    return r


def parse_report_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return normalize_report(data) if isinstance(data, dict) else None


def split_tag(text):
    m = re.match(r"\s*\[(توريد|عمالة|معوق|طقس)\]\s*(.*)", text, re.S)
    return (m.group(1), m.group(2).strip()) if m else (None, text.strip())


def classify_fallback(notes):
    """تصنيف احتياطي بسيط عند تعذر الذكاء الاصطناعي: بنود غير مفصّلة تُضاف كما كُتبت."""
    supply_kw = ("توريد", "استلام", "شاحنة", "شاحنات", "تفريغ")
    r = empty_report()
    for n in notes:
        tag, t = split_tag(n)
        if not t:
            continue
        if tag == "طقس":
            r["weather"] = t
        elif tag == "عمالة":
            r["manpower"].append({"trade": t, "count": None, "contractor": ""})
        elif tag == "معوق":
            r["issues"].append({"description": t, "responsible": "", "needed_by": "", "priority": ""})
        elif tag == "توريد" or any(k in t for k in supply_kw):
            r["supplies"].append({"item": t, "quantity": "", "supplier": "", "receipt": "", "inspection": ""})
        else:
            r["works"].append({"activity": t, "location": "", "quantity": "", "status": ""})
    return r


def photo_raw_text(photo):
    """ما كتبه المهندس مع الصورة + تعليقه الصوتي المفرَّغ."""
    parts = [p.strip() for p in (photo.get("caption"), photo.get("voice")) if p and p.strip()]
    return " - ".join(parts)


def build_report():
    """يبني بيانات التقرير. يرجع (report, used_ai, error)."""
    report, used_ai, error = empty_report(), False, None
    notes = [n["text"] for n in site_notes]
    photo_lines = [(i, photo_raw_text(p)) for i, p in enumerate(site_photos, 1)]
    photo_lines = [(i, t) for i, t in photo_lines if t]

    if notes or photo_lines:
        prompt = (CLASSIFY_PROMPT
                  .replace("<<NOTES>>", "\n".join(f"- {n}" for n in notes) or "لا توجد")
                  .replace("<<PHOTOS>>", "\n".join(f"{i}: {t}" for i, t in photo_lines) or "لا توجد"))
        text, error = ask_ai(prompt, CLASSIFY_SYSTEM)
        parsed = parse_report_json(text) if text else None
        if parsed:
            report, used_ai, error = parsed, True, None
        else:
            report = classify_fallback(notes)
            error = error or "تعذر قراءة رد الذكاء الاصطناعي."

    report["number"] = get_report_number()
    return report, used_ai, error


def photo_label(i, photo, report):
    info = report.get("photos", {}).get(i)
    if info:
        text = " - ".join(x for x in (info["activity"], info["location"]) if x)
    else:
        text = photo_raw_text(photo)
    return f"صورة {i}: {text}" if text else f"صورة {i}"


def missing_sections(report, photos_count):
    missing = []
    if not (report["manpower"] or report["equipment"]):
        missing.append("العمالة والمعدات")
    if not report["works"]:
        missing.append("الأعمال المنفذة")
    if not report["supplies"]:
        missing.append("التوريدات")
    if not report["issues"]:
        missing.append("المعوقات")
    if not photos_count:
        missing.append("الصور")
    return missing


# ============================================================
# 5. تقرير PDF بخط عربي (على نمط القالب)
# ============================================================
FONT_FAMILY = "Amiri"
FONT_REGULAR = os.path.join("fonts", "Amiri-Regular.ttf")
FONT_BOLD = os.path.join("fonts", "Amiri-Bold.ttf")

_EMOJI_RE = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")

GREEN, AMBER, RED, GRAY, NAVY = (46, 160, 67), (230, 160, 0), (200, 40, 40), (120, 120, 120), (30, 50, 90)
STATUS_COLORS = {
    "منجز": GREEN, "جار": AMBER, "متأخر": RED, "متوقف": GRAY,
    "عالية": RED, "متوسطة": AMBER, "منخفضة": GREEN,
    "تعمل": GREEN, "معطل": RED,
}


def status_color(text):
    t = _s(text)
    for key, color in STATUS_COLORS.items():
        if key in t:
            return color
    return None


def clean_text(text):
    text = _EMOJI_RE.sub("", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    return text


def ar(text):
    return get_display(reshape(text.translate(AR_DIGITS)), base_dir="R")


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


def dot_icon(color, r=2.2):
    def _icon(pdf, cx, cy):
        pdf.set_fill_color(*color)
        pdf.ellipse(cx - r, cy - r, 2 * r, 2 * r, style="F")
    return _icon


# ----- مكوّنات الصفحة -----
def draw_header(pdf, report):
    pdf.set_font(FONT_FAMILY, "B", 22)
    pdf.cell(0, 12, ar("تقرير نشاط الموقع اليومي"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(2)

    # صفوف الرأس: (النص الأيمن, النص الأيسر) ، والأيسر None = صف بعرض الصفحة
    rows = [
        (f"اسم المشروع: {PROJECT_NAME}", f"رقم التقرير: {report['number']:03d}"),
        (f"مهندس الموقع: {ENGINEER_NAME}" if ENGINEER_NAME else "", day_and_date(now_local().date())),
    ]
    if report["weather"]:
        rows.append((f"حالة الطقس: {report['weather']}", None))

    x0, w = pdf.l_margin, pdf.epw
    half = w / 2
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    for idx, (right, left) in enumerate(rows):
        pdf.set_font(FONT_FAMILY, "B" if idx == 0 else "", 12)
        y = pdf.get_y()
        if left is None:
            lines = wrap_rtl(pdf, clean_text(right), w - 2 * pdf.c_margin - 2)
            h = len(lines) * 6.5 + 3
            pdf.rect(x0, y, w, h)
            for j, line in enumerate(lines):
                pdf.set_xy(x0, y + 1.5 + j * 6.5)
                pdf.cell(w - 2, 6.5, line, align="R")
        else:
            h = 9
            pdf.rect(x0, y, w, h)
            pdf.line(x0 + half, y, x0 + half, y + h)
            pdf.set_xy(x0 + half, y + 1)
            pdf.cell(half - 2, 7, ar(right) if right else "", align="R")
            pdf.set_xy(x0, y + 1)
            pdf.cell(half - 2, 7, ar(left) if left else "", align="R")
        pdf.set_xy(x0, y + h)
    reset_draw(pdf)
    pdf.ln(6)


def section_bar(pdf, title, need=34):
    ensure_space(pdf, need)
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
    x0, w = pdf.l_margin, pdf.epw
    icon_w = 14 if icon else 0
    text_w = w - icon_w - 4
    pdf.set_font(FONT_FAMILY, "", 12)
    lines = wrap_rtl(pdf, clean_text(text), text_w - 2 * pdf.c_margin)
    h = max(11, len(lines) * ROW_LH + 4)
    ensure_space(pdf, h)
    y = pdf.get_y()

    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    pdf.rect(x0, y, w, h)
    if icon:
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


def draw_table(pdf, headers, rows, fracs, dot_cols=()):
    """جدول RTL: العمود الأول المنطقي هو الأيمن. fracs نسب عرض الأعمدة (مجموعها 1)."""
    LH, PAD = 6, 2
    widths = [pdf.epw * f for f in fracs]
    x_right = pdf.l_margin + pdf.epw

    def col_x(i):
        return x_right - sum(widths[:i + 1])

    def header_row():
        ensure_space(pdf, 8 + LH + 2 * PAD)
        y = pdf.get_y()
        pdf.set_fill_color(236, 236, 236)
        pdf.set_draw_color(60, 60, 60)
        pdf.set_line_width(0.3)
        pdf.set_font(FONT_FAMILY, "B", 11)
        for i, title in enumerate(headers):
            pdf.rect(col_x(i), y, widths[i], 8, style="DF")
            pdf.set_xy(col_x(i), y + 0.8)
            pdf.cell(widths[i], 6.4, ar(title), align="C")
        reset_draw(pdf)
        pdf.set_xy(pdf.l_margin, y + 8)

    header_row()
    for row in rows:
        pdf.set_font(FONT_FAMILY, "", 11)
        cells, dots = [], []
        for i, val in enumerate(row):
            text = _s(val) or "—"
            color = status_color(val) if i in dot_cols else None
            dots.append(color)
            avail = widths[i] - 2 * pdf.c_margin - (5 if color else 0)
            cells.append(wrap_rtl(pdf, clean_text(text), avail))
        h = max(len(c) for c in cells) * LH + 2 * PAD

        if pdf.get_y() + h > pdf.h - pdf.b_margin:
            pdf.add_page()
            header_row()
        y = pdf.get_y()

        pdf.set_draw_color(60, 60, 60)
        pdf.set_line_width(0.3)
        pdf.set_font(FONT_FAMILY, "", 11)
        for i, lines in enumerate(cells):
            x = col_x(i)
            pdf.rect(x, y, widths[i], h)
            tw = widths[i] - (5 if dots[i] else 0)
            for j, line in enumerate(lines):
                pdf.set_xy(x, y + PAD + j * LH)
                pdf.cell(tw, LH, line, align="R")
            if dots[i]:
                dot_icon(dots[i], 1.6)(pdf, x + widths[i] - 3.5, y + PAD + LH / 2)
        reset_draw(pdf)
        pdf.set_xy(pdf.l_margin, y + h)
    pdf.ln(4)


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def draw_photo_row(pdf, pair, start_idx, report):
    """صف صور: الأولى في العمود الأيمن والثانية في الأيسر، وتحت كل صورة رقمها وتعليقها."""
    gap, img_h, cap_lh = 4, 52, 5.5
    x0 = pdf.l_margin
    col_w = (pdf.epw - 3 * gap) / 2

    pdf.set_font(FONT_FAMILY, "", 11)
    caps = [wrap_rtl(pdf, clean_text(photo_label(start_idx + k, p, report)), col_w - 2 * pdf.c_margin)[:3]
            for k, p in enumerate(pair)]
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
        pdf.cell(bw - 2, 7, ar(label), align="R")
        if i == 0 and ENGINEER_NAME:
            pdf.set_font(FONT_FAMILY, "", 11)
            pdf.set_xy(bx, y + h - 9)
            pdf.cell(bw - 2, 7, ar(ENGINEER_NAME), align="R")
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

    # 1) رأس التقرير
    draw_header(pdf, report)

    # 2) الملخص التنفيذي
    s = report["summary"]
    summary_rows = [(label, text, icon) for label, text, icon in (
        ("أهم إنجاز", s["achievement"], dot_icon(GREEN)),
        ("أهم معوّق", s["obstacle"], dot_icon(RED)),
        ("المطلوب من الإدارة", s["request"], dot_icon(NAVY)),
    ) if text]
    if summary_rows:
        section_bar(pdf, "الملخص التنفيذي", need=40)
        for label, text, icon in summary_rows:
            draw_row(pdf, f"{label}: {text}", icon)
        pdf.ln(5)

    # 3) العمالة والمعدات
    if report["manpower"] or report["equipment"]:
        section_bar(pdf, "العمالة والمعدات")
        if report["manpower"]:
            rows = [[m["trade"], m["count"] if m["count"] is not None else "", m["contractor"]]
                    for m in report["manpower"]]
            counted = [m["count"] for m in report["manpower"] if m["count"] is not None]
            if len(counted) > 1:
                rows.append(["الإجمالي", sum(counted), ""])
            draw_table(pdf, ["التخصص", "العدد", "مقاول الباطن"], rows, [0.45, 0.15, 0.40])
        if report["equipment"]:
            rows = [[e["name"], e["count"] if e["count"] is not None else "", e["status"]]
                    for e in report["equipment"]]
            draw_table(pdf, ["المعدة", "العدد", "الحالة"], rows, [0.50, 0.15, 0.35], dot_cols=(2,))
        pdf.ln(1)

    # 4) الأعمال المنفذة
    if report["works"]:
        section_bar(pdf, "الأعمال المنفذة")
        rows = [[w["activity"], w["location"], w["quantity"], w["status"]] for w in report["works"]]
        draw_table(pdf, ["النشاط", "الموقع", "الكمية", "الحالة"], rows,
                   [0.40, 0.22, 0.16, 0.22], dot_cols=(3,))
        pdf.ln(1)

    # 5) التوريدات
    if report["supplies"]:
        section_bar(pdf, "التوريدات التي تمت خلال اليوم")
        rows = [[x["item"], x["quantity"], x["supplier"], x["receipt"], x["inspection"]]
                for x in report["supplies"]]
        draw_table(pdf, ["الصنف", "الكمية", "المورد", "حالة الاستلام", "الفحص والاعتماد"], rows,
                   [0.26, 0.14, 0.20, 0.18, 0.22])
        pdf.ln(1)

    # 6) المعوقات والمطلوب من الإدارة
    if report["issues"]:
        section_bar(pdf, "المعوقات والمطلوب من الإدارة")
        rows = [[i["description"], i["responsible"], i["needed_by"], i["priority"]] for i in report["issues"]]
        draw_table(pdf, ["الوصف", "الجهة المسؤولة", "تاريخ الحاجة", "الأولوية"], rows,
                   [0.44, 0.20, 0.18, 0.18], dot_cols=(3,))
        pdf.ln(1)

    # 7) الصور
    if photos_list:
        section_bar(pdf, "الصور الميدانية", need=95)
        for i in range(0, len(photos_list), 2):
            draw_photo_row(pdf, photos_list[i:i + 2], i + 1, report)
        pdf.ln(4)

    # 8) التوقيعات
    draw_signatures(pdf)

    os.makedirs("reports", exist_ok=True)
    pdf_path = f"reports/Site_Report_{now_local().strftime('%Y%m%d')}.pdf"
    pdf.output(pdf_path)
    return pdf_path


def _join(parts, sep=" - "):
    return sep.join(p for p in parts if p)


def format_report_text(report):
    """نسخة نصية من التقرير للإرسال داخل تليجرام."""
    L = [f"📋 تقرير نشاط الموقع اليومي رقم {report['number']:03d}",
         f"المشروع: {PROJECT_NAME}"]
    if ENGINEER_NAME:
        L.append(f"مهندس الموقع: {ENGINEER_NAME}")
    L.append(f"التاريخ: {day_and_date(now_local().date())}")
    if report["weather"]:
        L.append(f"الطقس: {report['weather']}")

    s = report["summary"]
    summary = [(k, v) for k, v in (("أهم إنجاز", s["achievement"]), ("أهم معوّق", s["obstacle"]),
                                   ("المطلوب من الإدارة", s["request"])) if v]
    if summary:
        L += ["", "📌 الملخص التنفيذي:"] + [f"• {k}: {v}" for k, v in summary]

    if report["manpower"] or report["equipment"]:
        L += ["", "👷 العمالة والمعدات:"]
        counted = []
        for m in report["manpower"]:
            line = _join([m["trade"], str(m["count"]) if m["count"] is not None else ""], ": ")
            if m["contractor"]:
                line += f" ({m['contractor']})"
            L.append(f"• {line}")
            if m["count"] is not None:
                counted.append(m["count"])
        if len(counted) > 1:
            L.append(f"• الإجمالي: {sum(counted)}")
        for e in report["equipment"]:
            name = e["name"] + (f" ({e['count']})" if e["count"] is not None else "")
            L.append(f"• {_join([name, e['status']])}")

    if report["works"]:
        L += ["", "🔧 الأعمال المنفذة:"]
        for w in report["works"]:
            status = f" ({w['status']})" if w["status"] else ""
            L.append(f"• {_join([w['activity'], w['location'], w['quantity']])}{status}")

    if report["supplies"]:
        L += ["", "✅ التوريدات:"]
        for x in report["supplies"]:
            L.append(f"• {_join([x['item'], x['quantity'], x['supplier'], x['receipt'], x['inspection']])}")

    if report["issues"]:
        L += ["", "⚠️ المعوقات والمطلوب من الإدارة:"]
        for i in report["issues"]:
            L.append("• " + _join([i["description"],
                                   f"المسؤول: {i['responsible']}" if i["responsible"] else "",
                                   f"قبل: {i['needed_by']}" if i["needed_by"] else "",
                                   f"الأولوية: {i['priority']}" if i["priority"] else ""]))
    return "\n".join(L)


# ============================================================
# 6. معالجات تليجرام
# ============================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🏗️ وكيل التقارير الميدانية - مشروع {PROJECT_NAME}\n\n"
        "كل الأقسام اختيارية: سجّل ما يخص يومك فقط، وما لا تسجله لا يظهر في التقرير.\n\n"
        "• أرسل ملاحظاتك نصاً أو صوتاً بحرية (أعمال، كميات، عمالة، معدات، توريدات، معوقات، طقس) "
        "وسيُصنَّف تلقائياً.\n"
        "• أرسل الصورة ثم تسجيلاً صوتياً بعدها مباشرة ليصبح تعليقها أسفلها "
        "(أو اعمل Reply على الصورة بالتسجيل).\n"
        "• أوامر اختيارية لتحديد القسم صراحةً:\n"
        "   /supply توريد    /labor عمالة    /issue معوق أو طلب    /weather طقس\n"
        "• /report لاستخراج التقرير اليومي وملف PDF.\n"
        "• /clear لبدء يوم جديد ومسح السجلات."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    site_notes.append({"ts": now_local().strftime("%Y-%m-%d %H:%M"), "text": text})
    last_photo["idx"] = None  # أي رسالة أخرى تقطع ربط الصوت بالصورة السابقة
    await update.message.reply_text(f"📝 تم تسجيل الملاحظة: '{text}'")


def find_photo_for_voice(update, now):
    """الصورة التي يعود إليها التسجيل: الصورة المردود عليها، أو آخر صورة أُرسلت قبله مباشرة."""
    reply = update.message.reply_to_message
    if reply:
        for p in site_photos:
            if p.get("msg_id") == reply.message_id:
                return p
    idx, at = last_photo["idx"], last_photo["at"]
    if idx is not None and idx < len(site_photos) and at and (now - at).total_seconds() <= PHOTO_VOICE_WINDOW:
        if not site_photos[idx].get("voice"):
            return site_photos[idx]
    return None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ جاري تحويل التسجيل الصوتي إلى نص عربي...")

    voice_file = await update.message.voice.get_file()
    os.makedirs("voice_notes", exist_ok=True)

    now = now_local()
    file_path = f"voice_notes/voice_{now.strftime('%H%M%S')}.ogg"
    await voice_file.download_to_drive(file_path)

    transcribed_text, error = await asyncio.to_thread(transcribe_audio, file_path)

    if transcribed_text and transcribed_text.strip():
        text = transcribed_text.strip()
        photo = find_photo_for_voice(update, now)
        if photo is not None:
            photo["voice"] = text
            n = site_photos.index(photo) + 1
            await update.message.reply_text(f"✅ تم ربط التعليق الصوتي بالصورة رقم {n}:\n\n\"{text}\"")
        else:
            site_notes.append({"ts": now.strftime("%Y-%m-%d %H:%M"), "text": text})
            await update.message.reply_text(f"✅ تم تفريغ الملاحظة الصوتية:\n\n\"{text}\"")
        last_photo["idx"] = None
    else:
        await update.message.reply_text(f"⚠️ تعذر تحويل الصوت إلى نص ولم يُسجَّل: {error}\nأعد الإرسال أو اكتبه نصاً.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = await update.message.photo[-1].get_file()
    os.makedirs("photos", exist_ok=True)

    now = now_local()
    file_path = f"photos/photo_{len(site_photos) + 1}_{now.strftime('%H%M%S')}.jpg"
    await photo_file.download_to_drive(file_path)

    caption = (update.message.caption or "").strip()
    site_photos.append({
        "path": file_path,
        "caption": caption,
        "voice": "",
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "msg_id": update.message.message_id,
    })
    last_photo["idx"], last_photo["at"] = len(site_photos) - 1, now
    await update.message.reply_text(
        f"📸 تم حفظ الصورة رقم {len(site_photos)}. أرسل الآن تسجيلاً صوتياً ليصبح تعليقها أسفلها.")


def make_tag_command(cmd, tag, example):
    """أمر اختياري يسجّل ملاحظة موسومة بقسمها ليصنّفها الذكاء الاصطناعي بدقة."""
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.partition(" ")[2].strip()
        if not text:
            await update.message.reply_text(f"اكتب النص بعد الأمر، مثال:\n/{cmd} {example}")
            return
        site_notes.append({"ts": now_local().strftime("%Y-%m-%d %H:%M"), "text": f"[{tag}] {text}"})
        last_photo["idx"] = None
        await update.message.reply_text(f"✅ تم تسجيل ({tag}): '{text}'")
    return _handler


TAG_COMMANDS = [
    ("supply", "توريد", "حديد تسليح - 15 طن - المورد: الشركة المصرية - تم الاستلام والتفريغ"),
    ("labor", "عمالة", "نجارون 6 وحدادون 4 وعمال 10، ومقاول الباطن 8 عمال"),
    ("issue", "معوق", "تأخر وصول الخرسانة ساعتين - المطلوب من الإدارة تغيير المورد قبل الخميس - أولوية عالية"),
    ("weather", "طقس", "رياح شديدة أوقفت الصب لمدة ساعة"),
]


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (site_notes or site_photos):
        await update.message.reply_text("⚠️ لا توجد أي ملاحظات أو صور مسجلة حتى الآن اليوم.")
        return

    await update.message.reply_text("⏳ جاري تصنيف الملاحظات وتوليد التقرير وملف الـ PDF...")

    report, used_ai, err = await asyncio.to_thread(build_report)
    if err:
        print("AI classification failed:", err)  # التفاصيل في سجلات Render فقط

    text = format_report_text(report)
    if not used_ai and site_notes:
        text += "\n\n(تم التصنيف تلقائياً بدون الذكاء الاصطناعي، راجع البنود.)"
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])

    try:
        pdf_path = await asyncio.to_thread(generate_pdf_report, report, list(site_photos))
        with open(pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=os.path.basename(pdf_path),
                caption=f"📄 تقرير نشاط الموقع اليومي رقم {report['number']:03d} - {PROJECT_NAME}",
                read_timeout=60,
                write_timeout=60,
            )
        await forward_to_admin(update, context, text, pdf_path, missing_sections(report, len(site_photos)))
    except Exception as pdf_err:
        await update.message.reply_text(f"⚠️ تم إرسال التقرير النصي، وتوقف الـ PDF بسبب: {pdf_err}")


async def forward_to_admin(update, context, text, pdf_path, missing):
    """إرسال نسخة من التقرير تلقائياً لمدير المشروع (ADMIN_TELEGRAM_ID)."""
    if not ADMIN_TELEGRAM_ID or ADMIN_TELEGRAM_ID == update.effective_chat.id:
        return
    try:
        eng = ENGINEER_NAME or f"@{context.bot.username}"
        notice = f"🔔 تقرير جديد من: {eng}\n🏗️ المشروع: {PROJECT_NAME}\n\n" + text
        if missing:
            notice += "\n\nأقسام غير مسجلة اليوم: " + "، ".join(missing)
        for i in range(0, len(notice), 4000):
            await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=notice[i:i + 4000])
        slug = re.sub(r"[^\w\-]+", "_", eng).strip("_") or "report"
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_TELEGRAM_ID,
                document=f,
                filename=f"Report_{slug}_{now_local().strftime('%Y%m%d')}.pdf",
                caption=f"📄 نسخة مدير المشروع - تقرير {eng}",
                read_timeout=60,
                write_timeout=60,
            )
    except Exception as e:
        print("Failed to forward report to admin:", e)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    site_notes.clear()
    site_photos.clear()
    last_photo.update(idx=None, at=None)
    await update.message.reply_text("🧹 تم مسح سجلات اليوم وبدء سجل جديد.")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        # تقييد الاستخدام بمعرّفات محددة إن ضُبط ALLOWED_USER_IDS، وإلا يعمل للجميع
        access = filters.User(user_id=list(ALLOWED_USER_IDS)) if ALLOWED_USER_IDS else filters.ALL
        app.add_handler(CommandHandler("start", start_command, filters=access))
        app.add_handler(CommandHandler("report", handle_report, filters=access))
        app.add_handler(CommandHandler("clear", clear_command, filters=access))
        for cmd, tag, example in TAG_COMMANDS:
            app.add_handler(CommandHandler(cmd, make_tag_command(cmd, tag, example), filters=access))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & access, handle_text))
        app.add_handler(MessageHandler(filters.VOICE & access, handle_voice))
        app.add_handler(MessageHandler(filters.PHOTO & access, handle_photo))

        print("=== بوت تليجرام يعمل الآن ومستعد لاستقبال النص والصوت والصور ===")
        app.run_polling()
