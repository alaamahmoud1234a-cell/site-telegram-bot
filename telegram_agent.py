import os
import re
import json
import base64
import asyncio
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
                          filters, ContextTypes)
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

# الأرشفة في Google Drive عبر سكريبت Google Apps Script (اختياري)
GOOGLE_DRIVE_WEBHOOK_URL = (os.environ.get("GOOGLE_DRIVE_WEBHOOK_URL") or "").strip()

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
# 2b. الأرشفة في Google Drive (عبر Google Apps Script)
# ============================================================
def drive_post(path, filename, mimetype, report_date):
    """يرفع ملفاً واحداً إلى السكريبت. يرجع (رد_السكريبت, None) أو (None, سبب_الفشل).
    لا نطبع رابط الويب هوك ولا نضعه في أي رسالة لأنه يعمل كمفتاح سري."""
    last_err = "فشل غير معروف"
    try:
        with open(path, "rb") as f:
            payload = {
                "filename": filename,
                "mimetype": mimetype,
                "filedata": base64.b64encode(f.read()).decode("ascii"),
                "project": PROJECT_NAME,
                "engineer": ENGINEER_NAME or "غير محدد",
                "date": report_date,
            }
    except OSError as e:
        return None, f"تعذرت قراءة الملف ({type(e).__name__})"

    for _ in range(2):  # محاولتان
        try:
            res = requests.post(GOOGLE_DRIVE_WEBHOOK_URL, json=payload, timeout=90)
            if res.status_code != 200:
                last_err = f"رد غير متوقع (HTTP {res.status_code})"
                continue
            try:
                data = res.json()
            except ValueError:
                last_err = "رد غير متوقع من Google؛ تأكد أن الويب أب منشور بصلاحية Anyone"
                continue
            if isinstance(data, dict) and data.get("ok"):
                return data, None
            last_err = _s(data.get("error")) if isinstance(data, dict) else "رد غير مفهوم من السكريبت"
        except Exception as e:
            last_err = f"خطأ اتصال ({type(e).__name__})"
    return None, last_err


def upload_report_to_drive(pdf_path, pdf_filename, photos, report_date):
    """يرفع الـ PDF ثم صور اليوم. يرجع ملخصاً: uploaded / failed / folder_url / error."""
    result = {"uploaded": 0, "failed": 0, "folder_url": "", "error": ""}
    data, err = drive_post(pdf_path, pdf_filename, "application/pdf", report_date)
    if not data:
        result.update(failed=1, error=err)
        return result  # فشل الـ PDF غالباً بسبب إعداد خاطئ، فلا فائدة من محاولة الصور
    result["uploaded"] = 1
    result["folder_url"] = _s(data.get("folder_url"))

    for p in photos:
        if not os.path.exists(p["path"]):
            continue
        data, err = drive_post(p["path"], os.path.basename(p["path"]), "image/jpeg", report_date)
        if data:
            result["uploaded"] += 1
            result["folder_url"] = result["folder_url"] or _s(data.get("folder_url"))
        else:
            result["failed"] += 1
            result["error"] = result["error"] or err
    return result


def drive_status_text(drive):
    if drive["uploaded"] and not drive["failed"]:
        return f"☁️ تمت الأرشفة في Google Drive ({drive['uploaded']} ملف)."
    if drive["uploaded"]:
        return f"⚠️ تمت أرشفة {drive['uploaded']} ملف في Google Drive، وتعذر رفع {drive['failed']}: {drive['error']}"
    return f"⚠️ تعذرت الأرشفة في Google Drive: {drive['error']}\nالتقرير سليم وقد وصلك هنا."


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
    pdf_path = f"reports/Site_Report_{now_local().strftime('%Y%m%d')}_{report['number']:03d}.pdf"
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
# 5b. الحالة اليومية والتذكيرات وملخص الصباح والأزرار
# ============================================================
# إعدادات اختيارية (متغيرات بيئة في Render، ولها قيم افتراضية):
#   REMINDER_TIME          موعد تذكير نهاية الدوام للمهندس      الافتراضي 17:00
#   MORNING_SUMMARY_TIME   موعد ملخص الصباح لمدير المشروع       الافتراضي 08:00
#   OFF_DAYS               أيام الإجازة مفصولة بفواصل           الافتراضي الجمعة
def _parse_hhmm(value, default):
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", (value or "").translate(EN_DIGITS))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return datetime.time(int(m.group(1)), int(m.group(2)))
    return default


def _norm_ar(s):
    return re.sub("[أإآ]", "ا", s or "").replace("ى", "ي").strip()


def _parse_off_days(value):
    names = {_norm_ar(n): i for i, n in enumerate(DAYS_AR)}
    out = set()
    for tok in re.split(r"[,،\s]+", value or ""):
        if _norm_ar(tok) in names:
            out.add(names[_norm_ar(tok)])
    return out


REMINDER_TIME = _parse_hhmm(os.environ.get("REMINDER_TIME"), datetime.time(17, 0))
MORNING_SUMMARY_TIME = _parse_hhmm(os.environ.get("MORNING_SUMMARY_TIME"), datetime.time(8, 0))
_off_env = os.environ.get("OFF_DAYS")
OFF_DAYS = _parse_off_days(_off_env if _off_env is not None else "الجمعة")
REMINDER_GRACE_MIN = 120   # إن استيقظ البوت بعد الموعد بأكثر من ذلك لا يُرسل تذكيراً متأخراً
SUMMARY_GRACE_MIN = 240

STATE_FILE = os.path.join("data", "state.json")
# tracking_since: متى بدأ البوت يسجّل الحالة (يُصفَّر إن ضاع الملف عند إعادة التشغيل)
# chat_ids: محادثات المهندس التي يرسل لها التذكير | days: حالة كل يوم | fired: آخر يوم أُرسل فيه كل تنبيه
STATE = {"tracking_since": "", "chat_ids": [], "days": {}, "fired": {}}


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        cutoff = (now_local().date() - datetime.timedelta(days=21)).isoformat()
        STATE["days"] = {k: v for k, v in STATE["days"].items() if k >= cutoff}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
    except Exception as e:
        print("State save error:", e)


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("chat_ids", "days", "fired"):
                    if isinstance(data.get(key), type(STATE[key])):
                        STATE[key] = data[key]
                if isinstance(data.get("tracking_since"), str):
                    STATE["tracking_since"] = data["tracking_since"]
    except Exception as e:
        print("State load error:", e)
    if not STATE["tracking_since"]:
        STATE["tracking_since"] = now_local().isoformat(timespec="minutes")
        save_state()


def remember_chat(chat_id):
    """يتعلّم البوت محادثة المهندس تلقائياً ليرسل لها التذكير (ولا يضيف المدير)."""
    if chat_id and chat_id > 0 and chat_id != ADMIN_TELEGRAM_ID and chat_id not in STATE["chat_ids"]:
        STATE["chat_ids"].append(chat_id)
        save_state()


def reminder_targets():
    ids = sorted(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else list(STATE["chat_ids"])
    return [i for i in ids if i != ADMIN_TELEGRAM_ID]  # المدير لا يصله تذكير المهندس


_PRIORITY_RANK = {"عالية": 0, "متوسطة": 1, "منخفضة": 2}


def top_issue_texts(report, limit=2):
    issues = sorted(report.get("issues", []), key=lambda i: _PRIORITY_RANK.get(_s(i.get("priority")), 3))
    out = []
    for i in issues:
        d = _s(i.get("description"))
        if d:
            out.append(d if len(d) <= 70 else d[:67] + "...")
        if len(out) >= limit:
            break
    return out


def day_status(date_iso):
    return (STATE["days"].get(date_iso) or {}).get("status", "")


def record_report_day(report):
    STATE["days"][now_local().date().isoformat()] = {
        "status": "sent", "number": int(report.get("number") or 0), "issues": top_issue_texts(report)}
    save_state()


def record_no_work_day():
    STATE["days"][now_local().date().isoformat()] = {"status": "no_work"}
    save_state()


def today_counts():
    t = now_local().date().isoformat()
    return (sum(1 for n in site_notes if n["ts"][:10] == t),
            sum(1 for p in site_photos if p["timestamp"][:10] == t))


def reminder_text():
    notes, photos = today_counts()
    if notes or photos:
        body = f"سجّلت اليوم {notes} ملاحظة و{photos} صورة، ولم تُصدر التقرير بعد."
    else:
        body = "لم تسجّل أي ملاحظات أو صور اليوم، ولم يُصدر تقرير."
    return (f"⏰ تذكير نهاية الدوام - {PROJECT_NAME}\n\n{body}\n"
            "اضغط «إصدار التقرير الآن»، أو سجّل ما تبقى أولاً. "
            "وإن لم يكن هناك عمل اليوم فاضغط «لا عمل اليوم».")


def reminder_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 إصدار التقرير الآن", callback_data="do_report"),
        InlineKeyboardButton("🚫 لا عمل اليوم", callback_data="no_work"),
    ]])


def last_workday_before(today):
    d = today - datetime.timedelta(days=1)
    for _ in range(7):
        if d.weekday() not in OFF_DAYS:
            return d
        d -= datetime.timedelta(days=1)
    return today - datetime.timedelta(days=1)


def tracking_started_after(date_obj):
    """هل بدأ التتبع بعد بداية ذلك اليوم؟ (إذن قد تكون سجلاته ضاعت)."""
    try:
        since = datetime.datetime.fromisoformat(STATE["tracking_since"])
        start = datetime.datetime.combine(date_obj, datetime.time(0, 0), tzinfo=since.tzinfo)
        return since > start
    except Exception:
        return False


def morning_summary_text(report_date, eng):
    rec = STATE["days"].get(report_date.isoformat()) or {}
    status = rec.get("status", "")
    head = f"📊 {eng} - {day_and_date(report_date)}: "
    if status == "sent":
        head += f"✅ أُرسل التقرير رقم {int(rec.get('number') or 0):03d}"
        issues = rec.get("issues") or []
        line2 = ("⚠️ أهم المعوقات: " + "؛ ".join(issues)) if issues else "✅ لا معوقات مسجلة."
    elif status == "no_work":
        head += "🚫 لا عمل"
        line2 = "—"
    elif tracking_started_after(report_date):
        head += "❔ غير معروف (أُعيد تشغيل البوت وضاعت سجلات ذلك اليوم)"
        line2 = "لا بيانات متاحة."
    else:
        head += "❌ لم يُرسل التقرير"
        line2 = "لا بيانات لهذا اليوم."
    return head + "\n" + line2


def _due(target_time, fired_key, now, grace_min):
    if STATE["fired"].get(fired_key) == now.date().isoformat():
        return False
    target = datetime.datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)
    return 0 <= (now - target).total_seconds() / 60 <= grace_min


async def scheduler_tick(bot):
    now = now_local()
    today = now.date()
    if today.weekday() in OFF_DAYS:
        return

    # 1) تذكير نهاية الدوام للمهندس إن لم يُصدر تقريره
    if _due(REMINDER_TIME, "reminder", now, REMINDER_GRACE_MIN):
        targets = reminder_targets()
        if targets:
            STATE["fired"]["reminder"] = today.isoformat()
            save_state()
            if not day_status(today.isoformat()):
                for chat_id in targets:
                    try:
                        await bot.send_message(chat_id=chat_id, text=reminder_text(),
                                               reply_markup=reminder_markup())
                    except Exception as e:
                        print("Reminder failed:", e)

    # 2) ملخص الصباح لمدير المشروع عن آخر يوم عمل
    if ADMIN_TELEGRAM_ID and _due(MORNING_SUMMARY_TIME, "summary", now, SUMMARY_GRACE_MIN):
        STATE["fired"]["summary"] = today.isoformat()
        save_state()
        eng = ENGINEER_NAME or f"@{bot.username}"
        try:
            await bot.send_message(chat_id=ADMIN_TELEGRAM_ID,
                                   text=morning_summary_text(last_workday_before(today), eng))
        except Exception as e:
            print("Morning summary failed:", e)


async def scheduler_loop(bot):
    while True:
        try:
            await scheduler_tick(bot)
        except Exception as e:
            print("Scheduler error:", e)
        await asyncio.sleep(30)


async def post_init(app):
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(app.bot))


# ----- أزرار سريعة أسفل الشاشة -----
BTN_LABOR = "👷 عمالة"
BTN_SUPPLY = "📦 توريد"
BTN_ISSUE = "⚠️ معوق أو طلب"
BTN_REPORT = "📄 إصدار التقرير"

MAIN_KEYBOARD = ReplyKeyboardMarkup([[BTN_LABOR, BTN_SUPPLY], [BTN_ISSUE, BTN_REPORT]], resize_keyboard=True)

BUTTON_TAGS = {
    BTN_LABOR: ("عمالة", "👷 اكتب أو سجّل صوتياً بيانات العمالة الآن (رسالة واحدة).\n"
                         "مثال: نجارون 6 وحدادون 4 وعمال 10، ومقاول الباطن 8 عمال"),
    BTN_SUPPLY: ("توريد", "📦 اكتب أو سجّل صوتياً بيانات التوريد الآن (رسالة واحدة).\n"
                          "مثال: حديد تسليح 15 طن - المورد: الشركة المصرية - تم الاستلام والتفريغ"),
    BTN_ISSUE: ("معوق", "⚠️ اكتب أو سجّل صوتياً المعوق أو الطلب الآن (رسالة واحدة).\n"
                        "مثال: تأخر وصول الخرسانة ساعتين - المطلوب تغيير المورد قبل الخميس - أولوية عالية"),
}
AWAITING_WINDOW = 600  # ثوانٍ: مهلة وصول رسالة المهندس بعد ضغط الزر


def pop_awaiting(context):
    """القسم الذي ضغط المهندس زره قبل رسالته (يُستهلك مرة واحدة وخلال 10 دقائق)."""
    a = context.user_data.pop("awaiting", None)
    if a and (now_local() - a["at"]).total_seconds() <= AWAITING_WINDOW:
        return a["tag"]
    return None


load_state()


# ============================================================
# 6. معالجات تليجرام
# ============================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    drive_note = " (وتُؤرشف نسخة منه مع الصور في Google Drive تلقائياً)" if GOOGLE_DRIVE_WEBHOOK_URL else ""
    await update.message.reply_text(
        f"🏗️ وكيل التقارير الميدانية - مشروع {PROJECT_NAME}\n\n"
        "كل الأقسام اختيارية: سجّل ما يخص يومك فقط، وما لا تسجله لا يظهر في التقرير.\n\n"
        "• أزرار سريعة أسفل الشاشة: اضغط «عمالة» أو «توريد» أو «معوق»، ثم اكتب أو سجّل صوتياً "
        "رسالة واحدة وستُسجَّل في قسمها.\n"
        "• أو أرسل ملاحظاتك نصاً أو صوتاً بحرية وسيُصنَّف تلقائياً.\n"
        "• أرسل الصورة ثم تسجيلاً صوتياً بعدها مباشرة ليصبح تعليقها أسفلها "
        "(أو اعمل Reply على الصورة بالتسجيل).\n"
        "• أوامر اختيارية: /supply /labor /issue /weather\n"
        f"• /report أو زر «إصدار التقرير» لاستخراج التقرير اليومي وملف PDF{drive_note}.\n"
        "• /clear لبدء يوم جديد ومسح السجلات.\n\n"
        f"⏰ إن لم تُصدر تقريرك حتى {REMINDER_TIME.strftime('%H:%M')} يصلك تذكير.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_REPORT:
        context.user_data.pop("awaiting", None)
        await handle_report(update, context)
        return
    if text in BUTTON_TAGS:
        tag, prompt = BUTTON_TAGS[text]
        context.user_data["awaiting"] = {"tag": tag, "at": now_local()}
        last_photo["idx"] = None
        await update.message.reply_text(prompt, reply_markup=MAIN_KEYBOARD)
        return

    tag = pop_awaiting(context)
    site_notes.append({"ts": now_local().strftime("%Y-%m-%d %H:%M"),
                       "text": f"[{tag}] {text}" if tag else text})
    last_photo["idx"] = None  # أي رسالة أخرى تقطع ربط الصوت بالصورة السابقة
    if tag:
        await update.message.reply_text(f"✅ تم تسجيل ({tag}): '{text}'")
    else:
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
        tag = pop_awaiting(context)
        photo = None if tag else find_photo_for_voice(update, now)
        if photo is not None:
            photo["voice"] = text
            n = site_photos.index(photo) + 1
            await update.message.reply_text(f"✅ تم ربط التعليق الصوتي بالصورة رقم {n}:\n\n\"{text}\"")
        else:
            site_notes.append({"ts": now.strftime("%Y-%m-%d %H:%M"),
                               "text": f"[{tag}] {text}" if tag else text})
            label = f"✅ تم تسجيل ({tag}):" if tag else "✅ تم تفريغ الملاحظة الصوتية:"
            await update.message.reply_text(f"{label}\n\n\"{text}\"")
        last_photo["idx"] = None
    else:
        await update.message.reply_text(f"⚠️ تعذر تحويل الصوت إلى نص ولم يُسجَّل: {error}\nأعد الإرسال أو اكتبه نصاً.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)  # الصورة تقطع انتظار زر القسم
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
    msg = update.effective_message  # يعمل مع الأمر ومع زر «إصدار التقرير» في التذكير
    if not (site_notes or site_photos):
        await msg.reply_text("⚠️ لا توجد أي ملاحظات أو صور مسجلة حتى الآن اليوم.")
        return

    await msg.reply_text("⏳ جاري تصنيف الملاحظات وتوليد التقرير وملف الـ PDF...")

    report, used_ai, err = await asyncio.to_thread(build_report)
    if err:
        print("AI classification failed:", err)  # التفاصيل في سجلات Render فقط

    text = format_report_text(report)
    if not used_ai and site_notes:
        text += "\n\n(تم التصنيف تلقائياً بدون الذكاء الاصطناعي، راجع البنود.)"
    for i in range(0, len(text), 4000):
        await msg.reply_text(text[i:i + 4000])

    record_report_day(report)  # يوقف تذكير اليوم ويغذّي ملخص الصباح

    try:
        pdf_path = await asyncio.to_thread(generate_pdf_report, report, list(site_photos))
        with open(pdf_path, "rb") as pdf_file:
            await msg.reply_document(
                document=pdf_file,
                filename=os.path.basename(pdf_path),
                caption=f"📄 تقرير نشاط الموقع اليومي رقم {report['number']:03d} - {PROJECT_NAME}",
                read_timeout=60,
                write_timeout=60,
            )
        drive = None
        if GOOGLE_DRIVE_WEBHOOK_URL:
            drive = await asyncio.to_thread(upload_report_to_drive, pdf_path, os.path.basename(pdf_path),
                                            list(site_photos), now_local().date().isoformat())
            await msg.reply_text(drive_status_text(drive))
        await forward_to_admin(update, context, text, pdf_path, missing_sections(report, len(site_photos)), drive)
    except Exception as pdf_err:
        await msg.reply_text(f"⚠️ تم إرسال التقرير النصي، وتوقف الـ PDF بسبب: {pdf_err}")


async def forward_to_admin(update, context, text, pdf_path, missing, drive=None):
    """إرسال نسخة من التقرير تلقائياً لمدير المشروع (ADMIN_TELEGRAM_ID)."""
    if not ADMIN_TELEGRAM_ID or ADMIN_TELEGRAM_ID == update.effective_chat.id:
        return
    try:
        eng = ENGINEER_NAME or f"@{context.bot.username}"
        notice = f"🔔 تقرير جديد من: {eng}\n🏗️ المشروع: {PROJECT_NAME}\n\n" + text
        if missing:
            notice += "\n\nأقسام غير مسجلة اليوم: " + "، ".join(missing)
        if drive:
            if drive["uploaded"] and drive["folder_url"]:
                notice += f"\n\n☁️ مجلد Google Drive: {drive['folder_url']}"
            elif not drive["uploaded"]:
                notice += "\n\n⚠️ تعذرت أرشفة هذا التقرير في Google Drive."
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


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يتعلّم محادثة المهندس تلقائياً (لا يمنع بقية المعالجات)."""
    chat = update.effective_chat
    if chat and chat.type == "private":
        remember_chat(chat.id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أزرار رسالة التذكير: إصدار التقرير الآن / لا عمل اليوم."""
    q = update.callback_query
    uid = q.from_user.id if q.from_user else None
    if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
        await q.answer("غير مصرّح لك باستخدام هذا البوت.")
        return

    if q.data == "do_report":
        await q.answer()
        await handle_report(update, context)
    elif q.data == "no_work":
        today = now_local().date()
        sent_date = q.message.date.astimezone(now_local().tzinfo).date() if q.message else today
        if sent_date != today:
            await q.answer("انتهت صلاحية هذا الزر (تذكير ليوم سابق).", show_alert=True)
        elif day_status(today.isoformat()) == "sent":
            await q.answer("أُصدر تقرير اليوم بالفعل.", show_alert=True)
        else:
            record_no_work_day()
            await q.answer("تم التسجيل")
            try:
                await q.edit_message_text("🚫 تم تسجيل: لا عمل اليوم. لن تصلك تذكيرات أخرى اليوم.")
            except Exception:
                pass
    else:
        await q.answer()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
        # تقييد الاستخدام بمعرّفات محددة إن ضُبط ALLOWED_USER_IDS، وإلا يعمل للجميع
        access = filters.User(user_id=list(ALLOWED_USER_IDS)) if ALLOWED_USER_IDS else filters.ALL
        app.add_handler(MessageHandler(access, track_chat), group=-1)
        app.add_handler(CommandHandler("start", start_command, filters=access))
        app.add_handler(CommandHandler("report", handle_report, filters=access))
        app.add_handler(CommandHandler("clear", clear_command, filters=access))
        for cmd, tag, example in TAG_COMMANDS:
            app.add_handler(CommandHandler(cmd, make_tag_command(cmd, tag, example), filters=access))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & access, handle_text))
        app.add_handler(MessageHandler(filters.VOICE & access, handle_voice))
        app.add_handler(MessageHandler(filters.PHOTO & access, handle_photo))
        app.add_handler(CallbackQueryHandler(on_callback))

        off = "، ".join(DAYS_AR[i] for i in sorted(OFF_DAYS)) or "لا يوجد"
        print(f"التذكير {REMINDER_TIME:%H:%M} | ملخص الصباح {MORNING_SUMMARY_TIME:%H:%M} | الإجازة: {off}")
        print("=== بوت تليجرام يعمل الآن ومستعد لاستقبال النص والصوت والصور ===")
        app.run_polling()
