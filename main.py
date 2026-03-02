import os
import logging
import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from datetime import datetime
import pytz
from pymongo import MongoClient, ReturnDocument
from concurrent.futures import ThreadPoolExecutor
from bson import ObjectId
import re
import certifi
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Tuple, Optional
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
HF_TOKEN  = os.getenv("HF_TOKEN", "")
PORT      = int(os.getenv("PORT", 8000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ─── MongoDB globals (populated in main() before anything else starts) ────────
mongo_client   = None
db             = None
complaints_col = None
feedback_col   = None
counters_col   = None

# Thread pool for running sync pymongo calls without event-loop binding issues
_executor = ThreadPoolExecutor(max_workers=8)


async def _db(fn):
    """Run a zero-arg sync pymongo callable in the thread pool.
    Always uses asyncio.get_running_loop() so it is bound to the
    current (correct) event loop — immune to Motor’s loop-binding bug."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn)


def next_seq(name: str) -> int:
    """Atomic auto-increment counter using sync pymongo findOneAndUpdate."""
    result = counters_col.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["seq"]


def serialize(doc: dict) -> dict:
    """Convert MongoDB doc to JSON-safe dict."""
    doc["_id"] = str(doc["_id"])
    return doc


# ─── FastAPI lifespan (indexes only — pymongo client set up in main()) ───────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _db(lambda: complaints_col.create_index("complaint_id", unique=True))
    await _db(lambda: complaints_col.create_index("user_id"))
    await _db(lambda: complaints_col.create_index("status"))
    logging.info("MongoDB indexes ensured.")
    yield
    # pymongo client is closed in main()


app = FastAPI(
    title="Grievance Bot API",
    description="API for managing citizens' grievances",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("templates"):
    os.makedirs("templates")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ─── Unified Rules ───────────────────────────────────────────────────────────
# Single source of truth: category → dept, base_priority, phrases (3 pts), words (1 pt)
# Phrases are matched before words so multi-word specificity wins.
_RULES: dict[str, dict] = {
    # Water
    "No Water Supply":       {"dept": "Water Department",                  "priority": "Medium",
                              "phrases": ["no water", "water not coming", "no drinking water", "water supply"],
                              "words":   ["water"]},
    "Pipeline Leakage":      {"dept": "Water Department",                  "priority": "Medium",
                              "phrases": ["pipe leak", "pipeline leak", "burst pipe", "water leaking"],
                              "words":   []},
    "Dirty Water Supply":    {"dept": "Water Department",                  "priority": "High",
                              "phrases": ["dirty water", "contaminated water", "muddy water", "bad water"],
                              "words":   []},
    # Sewage
    "Blocked Drainage":      {"dept": "Municipal Corporation",             "priority": "Medium",
                              "phrases": ["blocked drain", "clogged drain", "drain overflow", "sewage overflow", "open manhole"],
                              "words":   ["manhole", "sewage", "drainage"]},
    # Road
    "Road Maintenance & Potholes": {"dept": "Public Works Department",    "priority": "Medium",
                              "phrases": ["road damage", "broken road", "road repair", "bad road"],
                              "words":   ["pothole", "road"]},
    # Electricity
    "Electricity Issues & Power Cuts": {"dept": "Electricity Board",      "priority": "Medium",
                              "phrases": ["power cut", "no electricity", "power outage", "voltage fluctuation"],
                              "words":   ["electricity", "voltage", "transformer", "electric"]},
    # Street Lights
    "Street Light Problems": {"dept": "Municipal Corporation",             "priority": "Low",
                              "phrases": ["street light", "light not working", "dark road", "pole light"],
                              "words":   ["streetlight"]},
    # Garbage
    "Garbage Collection & Waste Management": {"dept": "Sanitation Department", "priority": "Low",
                              "phrases": ["overflowing bin", "garbage collection", "illegal dumping", "burning garbage"],
                              "words":   ["garbage", "waste", "trash", "dustbin", "litter"]},
    # Public Toilet
    "Public Toilet Maintenance": {"dept": "Sanitation Department",         "priority": "Low",
                              "phrases": ["public toilet", "toilet dirty", "toilet not working"],
                              "words":   ["washroom", "toilet"]},
    # Encroachment
    "Illegal Encroachments": {"dept": "Urban Development Authority",       "priority": "Medium",
                              "phrases": ["illegal construction", "footpath blocked", "encroachment on"],
                              "words":   ["encroachment", "hawker", "squatter"]},
    # Noise
    "Noise Pollution":       {"dept": "Police Department",                 "priority": "Low",
                              "phrases": ["loud music", "construction noise", "sound pollution", "vehicle horn"],
                              "words":   ["noise"]},
    # Corruption
    "Corruption Complaints": {"dept": "Anti-Corruption Bureau",            "priority": "High",
                              "phrases": ["money demanded", "illegal money", "asked for bribe"],
                              "words":   ["bribe", "corruption", "corrupt"]},
    # Traffic
    "Traffic Violations":    {"dept": "Traffic Police",                    "priority": "Medium",
                              "phrases": ["reckless driving", "illegal parking", "red light", "overloaded vehicle"],
                              "words":   ["traffic"]},
    # Health
    "Public Health Hazards": {"dept": "Health Department",                 "priority": "High",
                              "phrases": ["contaminated food", "health hazard", "hospital negligence", "mosquito breeding"],
                              "words":   ["disease", "mosquito"]},
    # Education
    "School Infrastructure Problems": {"dept": "Education Department",    "priority": "Medium",
                              "phrases": ["no drinking water in school", "broken school", "lack of teachers"],
                              "words":   ["school", "teacher", "classroom", "education"]},
    # Animal
    "Animal Nuisance":       {"dept": "Municipal Corporation",             "priority": "Medium",
                              "phrases": ["stray dog", "stray animal", "cow on road", "animal attack"],
                              "words":   ["snake", "animal"]},
    # Police Misconduct
    "Police Misconduct":     {"dept": "Police Department",                 "priority": "High",
                              "phrases": ["officer misconduct", "police brutality", "false arrest"],
                              "words":   ["misconduct"]},
    # Ration
    "Ration & PDS Issues":   {"dept": "Food Department",                   "priority": "Medium",
                              "phrases": ["ration card", "ration shop", "food grain"],
                              "words":   ["ration", "pds"]},
    # Land
    "Land & Property Disputes": {"dept": "Revenue Department",            "priority": "Medium",
                              "phrases": ["land dispute", "property dispute", "land grab", "boundary dispute"],
                              "words":   []},
    # Fire
    "Fire Hazards":          {"dept": "Fire Department",                   "priority": "High",
                              "phrases": ["fire hazard", "building on fire", "fire accident", "smoke coming"],
                              "words":   ["fire", "burning", "flame", "smoke"]},
    # Cybercrime
    "Cybercrime & Online Fraud": {"dept": "Cyber Crime Cell",             "priority": "High",
                              "phrases": ["bank fraud", "online scam", "hacked account", "cyber crime"],
                              "words":   ["fraud", "scam", "hacked"]},
    # Women & Child
    "Women & Child Safety":  {"dept": "Women & Child Welfare Department", "priority": "High",
                              "phrases": ["domestic violence", "child labor", "sexual harassment", "harassment complaint"],
                              "words":   ["harassment", "violence"]},
    # Environment
    "Air Pollution":         {"dept": "Environmental Protection Agency",   "priority": "Medium",
                              "phrases": ["air pollution", "factory smoke", "poor air quality", "burning waste"],
                              "words":   []},
    # Transport
    "Public Transport":      {"dept": "Transport Department",              "priority": "Low",
                              "phrases": ["bus not stopping", "overcrowded bus", "train delay", "public transport"],
                              "words":   ["bus", "train", "transport"]},
    # ── Emergencies (base priority always High) ───────────────────────────────
    "Kidnapping":            {"dept": "Police Department",                 "priority": "High",
                              "phrases": ["missing person", "taken away", "been abducted", "child missing"],
                              "words":   ["kidnap", "kidnapping", "abduct", "abduction"]},
    "Murder":                {"dept": "Police Department",                 "priority": "High",
                              "phrases": ["dead body", "found dead", "been killed", "shot dead"],
                              "words":   ["murder", "homicide", "killed", "shooting", "stabbing"]},
    "Suicide Attempt":       {"dept": "Emergency Services",                "priority": "High",
                              "phrases": ["self harm", "want to die", "trying to kill", "about to jump"],
                              "words":   ["suicide", "hanging", "overdose"]},
    "Medical Emergency":     {"dept": "Emergency Services",                "priority": "High",
                              "phrases": ["heart attack", "not breathing", "medical emergency", "lost consciousness"],
                              "words":   ["unconscious", "collapsed", "stroke"]},
    "Accident Report":       {"dept": "Emergency Services",                "priority": "High",
                              "phrases": ["road accident", "vehicle accident", "major accident", "hit and run"],
                              "words":   ["accident", "crash"]},
    "Flood & Disaster Relief": {"dept": "Disaster Management Department", "priority": "High",
                              "phrases": ["flood relief", "flooded area", "earthquake damage"],
                              "words":   ["flood", "earthquake"]},
    # Fallback
    "General":               {"dept": "General Grievance Cell",            "priority": "Low",
                              "phrases": [], "words": []},
}

# Words that escalate any category's priority to High
_URGENCY_WORDS = {
    "urgent", "emergency", "immediately", "critical", "danger", "dying",
    "bleeding", "sos", "help", "life", "death", "explosion", "bomb", "rape",
}

_HF_API_URL = "https://api-inference.huggingface.co/models/facebook/bart-large-mnli"
_HF_LABELS  = [c for c in _RULES if c != "General"]


def is_valid_complaint(text: str) -> bool:
    return bool(re.search(r"[a-zA-Z]", text))


def _keyword_score(lower: str) -> tuple[str, int]:
    """Weighted keyword scan. Phrases = 3 pts, words = 1 pt. Single pass."""
    best_cat, best_score = "General", 0
    for cat, rule in _RULES.items():
        if cat == "General":
            continue
        score = (sum(3 for p in rule["phrases"] if p in lower) +
                 sum(1 for w in rule["words"]   if w in lower))
        if score > best_score:
            best_score, best_cat = score, cat
    return best_cat, best_score


async def classify_complaint(text: str) -> tuple[str, str, str]:
    """Returns (category, department, priority).
    Keyword scorer runs first — HF API only called when score == 0 (truly ambiguous)."""
    if not is_valid_complaint(text):
        return "Invalid Complaint", "General Grievance Cell", "Low"

    lower = text.lower()
    cat, score = _keyword_score(lower)

    # Only escalate to HF when keywords give zero signal
    if score == 0 and HF_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.post(
                    _HF_API_URL,
                    headers={"Authorization": f"Bearer {HF_TOKEN}"},
                    json={"inputs": text, "parameters": {"candidate_labels": _HF_LABELS}},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    cat = data["labels"][0]
                    logging.info(f"HF classified (ambiguous): '{cat}' ({data['scores'][0]:.2f})")
                else:
                    logging.warning(f"HF API {resp.status_code}, using General")
        except Exception as e:
            logging.warning(f"HF API error: {e}")

    rule     = _RULES.get(cat, _RULES["General"])
    dept     = rule["dept"]
    priority = rule["priority"]

    # Urgency escalation — any category can be bumped to High
    if set(re.findall(r"\w+", lower)) & _URGENCY_WORDS:
        priority = "High"

    logging.info(f"Classified: '{cat}' | {dept} | {priority} (kw_score={score})")
    return cat, dept, priority


def analyze_sentiment(text: str, priority: str) -> str:
    """Sentiment derived from priority + negative word cues."""
    if priority == "High":
        return "Negative"
    neg = {"bad", "worst", "terrible", "horrible", "useless", "broken", "damaged",
           "dirty", "blocked", "overflow", "no", "not", "never", "fail", "failed"}
    if set(re.findall(r"\w+", text.lower())) & neg:
        return "Negative"
    return "Neutral"


# ─── Telegram Bot ─────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


@dp.message(Command("log"))
async def log_complaint(message: types.Message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.reply(
                "Please provide a complaint after /log.\n"
                "Example: /log The road near my house is damaged."
            )
            return
        text = parts[1].strip()
        await message.reply("🕐 Processing your complaint…")

        category, department, priority = await classify_complaint(text)
        if category == "Invalid Complaint":
            await message.reply("\u274c Your complaint is invalid. Please provide a real issue.")
            return

        sentiment    = analyze_sentiment(text, priority)
        ist          = pytz.timezone("Asia/Kolkata")
        timestamp    = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
        complaint_id = await _db(lambda: next_seq("complaints"))

        doc = {
            "complaint_id": complaint_id,
            "user_id":      str(message.from_user.id),
            "complaint":    text,
            "category":     category,
            "sentiment":    sentiment,
            "priority":     priority,
            "department":   department,
            "status":       "Pending",
            "timestamp":    timestamp,
        }
        await _db(lambda: complaints_col.insert_one(doc))

        await message.reply(
            f"✅ Complaint *#{complaint_id}* registered!\n\n"
            f"📂 *Category:* {category}\n"
            f"⚡ *Priority:* {priority}\n"
            f"🏢 *Department:* {department}\n\n"
            "We will process your complaint soon. Thank you for your patience.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await message.reply(f"❌ An error occurred: {e}")
        logging.exception("Error in log_complaint")


@dp.message(Command("resolve"))
async def resolve_complaint(message: types.Message):
    try:
        if message.from_user.id != ADMIN_ID:
            await message.reply("🚫 You are not authorized to perform this action.")
            return
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply("❌ Usage: /resolve <complaint_id>")
            return
        complaint_id = int(args[1])
        result = await _db(lambda: complaints_col.find_one_and_update(
            {"complaint_id": complaint_id},
            {"$set": {"status": "Resolved"}},
            return_document=ReturnDocument.AFTER,
        ))
        if result:
            await message.reply(f"✅ Complaint *#{complaint_id}* marked as Resolved!", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply(f"❌ Complaint #{complaint_id} does not exist.")
    except (ValueError, TypeError):
        await message.reply("❌ Invalid complaint ID.")
    except Exception as e:
        await message.reply(f"❌ An error occurred: {e}")


@dp.message(Command("history"))
async def complaint_history(message: types.Message):
    user_id = str(message.from_user.id)
    docs    = await _db(lambda: list(
        complaints_col.find({"user_id": user_id},
                            {"complaint_id": 1, "complaint": 1, "status": 1}).limit(50)
    ))
    if not docs:
        await message.reply("You have no complaints logged.")
        return
    lines = ["📜 *Your Complaint History:*"]
    for d in docs:
        lines.append(f"\n🆔 #{d['complaint_id']} — {d['complaint'][:60]}… *(Status: {d['status']})*")
    await message.reply("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("feedback"))
async def feedback_command(message: types.Message):
    feedback_text = message.text.replace("/feedback", "").strip()
    if feedback_text:
        fb = {
            "user_id":   message.from_user.id,
            "username":  message.from_user.username or "",
            "message":   feedback_text,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await _db(lambda: feedback_col.insert_one(fb))
        await message.reply("✅ Thank you for your feedback!")
    else:
        await message.reply("Please provide feedback. Example: /feedback I love this bot!")


@dp.message(Command("delete"))
async def delete_complaint(message: types.Message):
    try:
        if message.from_user.id != ADMIN_ID:
            await message.reply("🚫 You are not authorized to perform this action.")
            return
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply("❌ Usage: /delete <complaint_id>")
            return
        complaint_id = int(args[1])
        result = await _db(lambda: complaints_col.delete_one({"complaint_id": complaint_id}))
        if result.deleted_count:
            await message.reply(f"🗑 Complaint #{complaint_id} deleted successfully!")
        else:
            await message.reply(f"⚠️ No complaint found with ID #{complaint_id}.")
    except ValueError:
        await message.reply("❌ Invalid complaint ID.")
    except Exception as e:
        await message.reply(f"❌ An error occurred: {e}")
        logging.exception("Error in delete_complaint")


@dp.message(lambda m: m.from_user.id == ADMIN_ID)
async def admin_message_handler(message: types.Message):
    await message.reply(
        "👋 Hello Admin! Available commands:\n\n"
        "/resolve <id> — Mark complaint as resolved\n"
        "/delete <id>  — Delete a complaint\n"
        "/feedback <text> — Submit feedback"
    )


@dp.message()
async def general_message_handler(message: types.Message):
    await message.reply(
        "👋 Hello! I'm the Grievance Bot. Here's what you can do:\n\n"
        "/log <complaint>  — Submit a new complaint\n"
        "/history          — View your complaint history\n"
        "/feedback <text>  — Send feedback"
    )


# ─── REST API ─────────────────────────────────────────────────────────────────
@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/complaints")
async def get_complaints(
    category:   Optional[str] = Query(None),
    status:     Optional[str] = Query(None),
    priority:   Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    page:       int = Query(1, ge=1),
    per_page:   int = Query(20, ge=1, le=100),
):
    query: dict = {}
    if category:   query["category"]   = category
    if status:     query["status"]     = status
    if priority:   query["priority"]   = priority
    if department: query["department"] = department

    total = await _db(lambda: complaints_col.count_documents(query))
    docs  = await _db(lambda: list(
        complaints_col.find(query).sort("complaint_id", -1)
        .skip((page - 1) * per_page).limit(per_page)
    ))
    return {"total": total, "page": page, "per_page": per_page, "data": [serialize(d) for d in docs]}


@app.get("/api/complaints/{complaint_id}")
async def get_complaint(complaint_id: int):
    doc = await _db(lambda: complaints_col.find_one({"complaint_id": complaint_id}))
    if not doc:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return serialize(doc)


@app.put("/api/complaints/{complaint_id}/resolve")
async def resolve_api(complaint_id: int):
    result = await _db(lambda: complaints_col.find_one_and_update(
        {"complaint_id": complaint_id},
        {"$set": {"status": "Resolved"}},
        return_document=ReturnDocument.AFTER,
    ))
    if not result:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return {"message": f"Complaint #{complaint_id} resolved"}


@app.delete("/api/complaints/{complaint_id}")
async def delete_api(complaint_id: int):
    result = await _db(lambda: complaints_col.delete_one({"complaint_id": complaint_id}))
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return {"message": f"Complaint #{complaint_id} deleted"}


@app.get("/api/feedbacks")
async def get_feedbacks():
    docs = await _db(lambda: list(feedback_col.find().sort("_id", -1).limit(200)))
    return {"data": [serialize(d) for d in docs]}


@app.get("/api/stats")
async def get_stats():
    total     = await _db(lambda: complaints_col.count_documents({}))
    pending   = await _db(lambda: complaints_col.count_documents({"status": "Pending"}))
    resolved  = await _db(lambda: complaints_col.count_documents({"status": "Resolved"}))
    high_prio = await _db(lambda: complaints_col.count_documents({"priority": "High"}))

    cat_data  = await _db(lambda: list(complaints_col.aggregate(
        [{"$group": {"_id": "$category",   "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 10}]
    )))
    dept_data = await _db(lambda: list(complaints_col.aggregate(
        [{"$group": {"_id": "$department", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 10}]
    )))
    sent_data = await _db(lambda: list(complaints_col.aggregate(
        [{"$group": {"_id": "$sentiment",  "count": {"$sum": 1}}}]
    )))
    prio_data = await _db(lambda: list(complaints_col.aggregate(
        [{"$group": {"_id": "$priority",   "count": {"$sum": 1}}}]
    )))

    return {
        "summary":       {"total": total, "pending": pending, "resolved": resolved, "high_priority": high_prio},
        "by_category":   [{"label": d["_id"], "count": d["count"]} for d in cat_data],
        "by_department": [{"label": d["_id"], "count": d["count"]} for d in dept_data],
        "by_sentiment":  [{"label": d["_id"], "count": d["count"]} for d in sent_data],
        "by_priority":   [{"label": d["_id"], "count": d["count"]} for d in prio_data],
    }


@app.get("/dashboard")
async def dashboard():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/")


# ─── Entry point ─────────────────────────────────────────────────────────────
async def main():
    """Single event loop entry point.
    Uses sync pymongo (no loop binding) via run_in_executor — immune to
    the Motor/Python 3.13 'Future attached to a different loop' bug.
    """
    global mongo_client, db, complaints_col, feedback_col, counters_col

    # Sync MongoClient — no asyncio loop binding whatsoever
    mongo_client   = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,
        tlsCAFile=certifi.where(),
    )
    db             = mongo_client["grievance_bot"]
    complaints_col = db["complaints"]
    feedback_col   = db["feedback"]
    counters_col   = db["counters"]

    # Drop any existing Telegram connection before polling — prevents
    # TelegramConflictError when Render starts a new instance before the old one stops
    await bot.delete_webhook(drop_pending_updates=True)
    bot_task = asyncio.create_task(dp.start_polling(bot))

    logging.info(f"✅ Dashboard → http://localhost:{PORT}/")
    logging.info(f"✅ API Docs  → http://localhost:{PORT}/docs")

    try:
        config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        _executor.shutdown(wait=False)
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())