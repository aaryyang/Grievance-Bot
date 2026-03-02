import os
import logging
import asyncio
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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Tuple, Optional
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
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

# ─── Department Mapping ───────────────────────────────────────────────────────
department_mapping = {
    # Water-Related Issues
    "Water Supply Issues": "Water Department",
    "No Water Supply": "Water Department",
    "Low Water Pressure": "Water Department",
    "Dirty Water Supply": "Water Department",
    "Contaminated Water": "Water Department",
    "Pipeline Leakage": "Water Department",
    "Burst Water Pipe": "Water Department",
    "Drinking Water Problem": "Water Department",

    # Sewage & Drainage Issues
    "Sewage & Drainage Problems": "Municipal Corporation",
    "Blocked Drainage": "Municipal Corporation",
    "Overflowing Sewage": "Municipal Corporation",
    "Open Manholes": "Municipal Corporation",
    "Drain Clogged": "Municipal Corporation",
    "Foul Smell from Drain": "Municipal Corporation",
    "Sewage Treatment Issue": "Municipal Corporation",

    # Road & Infrastructure Issues
    "Road Maintenance & Potholes": "Public Works Department",
    "Potholes on Road": "Public Works Department",
    "Damaged Road": "Public Works Department",
    "Broken Footpath": "Public Works Department",
    "Road Construction Problem": "Public Works Department",
    "Illegal Road Digging": "Public Works Department",

    # Electricity Issues
    "Electricity Issues & Power Cuts": "Electricity Board",
    "Frequent Power Cuts": "Electricity Board",
    "Voltage Fluctuation": "Electricity Board",
    "Electric Pole Damage": "Electricity Board",
    "Transformer Failure": "Electricity Board",
    "High Electricity Bill": "Electricity Board",
    "New Electricity Connection": "Electricity Board",

    # Street Light Issues
    "Street Light Problems": "Municipal Corporation",
    "Street Light Not Working": "Municipal Corporation",
    "Flickering Street Light": "Municipal Corporation",
    "Street Light Always On": "Municipal Corporation",
    "Need More Street Lights": "Municipal Corporation",

    # Garbage & Waste Management
    "Garbage Collection & Waste Management": "Sanitation Department",
    "Uncollected Garbage": "Sanitation Department",
    "Overflowing Garbage Bin": "Sanitation Department",
    "Illegal Dumping of Waste": "Sanitation Department",
    "Burning of Garbage": "Sanitation Department",
    "Need More Dustbins": "Sanitation Department",

    # Public Toilet Issues
    "Public Toilet Maintenance": "Sanitation Department",
    "Dirty Public Toilet": "Sanitation Department",
    "Public Toilet Not Working": "Sanitation Department",
    "Need More Public Toilets": "Sanitation Department",

    # Illegal Construction & Encroachments
    "Illegal Encroachments": "Urban Development Authority",
    "Illegal Construction": "Urban Development Authority",
    "Encroachment on Public Property": "Urban Development Authority",
    "Hawkers Blocking Road": "Urban Development Authority",

    # Noise Pollution
    "Noise Pollution": "Police Department",
    "Loud Music Complaint": "Police Department",
    "Vehicle Horn Noise": "Police Department",
    "Factory Noise Pollution": "Police Department",
    "Construction Noise Issue": "Police Department",

    # Corruption & Bribery
    "Corruption Complaints": "Anti-Corruption Bureau",
    "Bribe Demand": "Anti-Corruption Bureau",
    "Government Officer Taking Bribe": "Anti-Corruption Bureau",
    "Corrupt Practices in Office": "Anti-Corruption Bureau",

    # Traffic Violations
    "Traffic Violations": "Traffic Police",
    "Jumping Red Light": "Traffic Police",
    "Illegal Parking": "Traffic Police",
    "Reckless Driving": "Traffic Police",
    "Overloaded Vehicle": "Traffic Police",
    "Blocked Road Due to Traffic": "Traffic Police",

    # Public Health Hazards
    "Public Health Hazards": "Health Department",
    "Contaminated Food Complaint": "Health Department",
    "Mosquito Breeding Issue": "Health Department",
    "Garbage Causing Disease": "Health Department",
    "Hospital Negligence Complaint": "Health Department",

    # Education & School Issues
    "School Infrastructure Problems": "Education Department",
    "Broken School Building": "Education Department",
    "No Drinking Water in School": "Education Department",
    "Lack of Teachers in School": "Education Department",

    # Employment & Labor Issues
    "Employment Grievances": "Labour & Employment Department",
    "Unpaid Salary Complaint": "Labour & Employment Department",
    "Unfair Dismissal Complaint": "Labour & Employment Department",
    "Unsafe Workplace": "Labour & Employment Department",

    # Internet & Mobile Issues
    "Internet & Mobile Network Complaints": "Telecom Department",
    "Slow Internet Speed": "Telecom Department",
    "Call Drop Issue": "Telecom Department",
    "No Network Coverage": "Telecom Department",

    # Fire Safety & Emergencies
    "Fire Emergency": "Fire Department",
    "Building on Fire": "Fire Department",
    "Fire Accident Report": "Fire Department",
    "Smoke Coming from Building": "Fire Department",

    # Environmental Issues
    "Air Pollution Complaints": "Environmental Protection Agency",
    "Factory Emitting Smoke": "Environmental Protection Agency",
    "Burning of Waste": "Environmental Protection Agency",
    "Poor Air Quality": "Environmental Protection Agency",

    # Tree & Deforestation Issues
    "Tree Cutting & Deforestation Complaints": "Forest Department",
    "Illegal Tree Cutting": "Forest Department",
    "Need More Trees in Area": "Forest Department",

    # Public Transport Issues
    "Railway Station & Train Issues": "Railway Department",
    "Train Delay Complaint": "Railway Department",
    "Unhygienic Railway Station": "Railway Department",
    "Bus & Public Transport Complaints": "Transport Department",
    "Overcrowded Bus Complaint": "Transport Department",
    "Bus Not Stopping at Stops": "Transport Department",

    # Cybercrime & Online Fraud
    "Cybercrime & Online Fraud": "Cyber Crime Cell",
    "Bank Fraud": "Cyber Crime Cell",
    "Hacked Social Media Account": "Cyber Crime Cell",
    "Online Scam Complaint": "Cyber Crime Cell",

    # Consumer Rights Violations
    "Consumer Rights Violations": "Consumer Protection Department",
    "Fake Product Complaint": "Consumer Protection Department",
    "Overpriced Product Complaint": "Consumer Protection Department",
    "False Advertising Complaint": "Consumer Protection Department",

    # Women & Child Safety
    "Women & Child Safety": "Women & Child Welfare Department",
    "Harassment Complaint": "Women & Child Welfare Department",
    "Domestic Violence Complaint": "Women & Child Welfare Department",
    "Child Labor Complaint": "Women & Child Welfare Department",

    # Social Welfare & Senior Citizen Issues
    "Senior Citizen Grievances": "Social Welfare Department",
    "Elderly Neglect Complaint": "Social Welfare Department",
    "Need Help for Senior Citizen": "Social Welfare Department",
    "Homelessness & Shelter Issues": "Social Welfare Department",
    "Need Homeless Shelter": "Social Welfare Department",

    # Disaster Management & Relief
    "Flood & Disaster Relief": "Disaster Management Department",
    "Flooded Area Complaint": "Disaster Management Department",
    "Earthquake Damage Report": "Disaster Management Department",

    # Land & Property Issues
    "Land Disputes & Property Issues": "Revenue Department",
    "Illegal Land Grab": "Revenue Department",
    "Property Ownership Dispute": "Revenue Department",

    # Emergency Situations
    "Murder": "Police Department",
    "Kidnapping": "Police Department",
    "Suicide Attempt": "Emergency Services",
    "Accident Report": "Emergency Services",
    "Medical Emergency": "Emergency Services",
    "Emergency Helpline": "Emergency Services",

    # Other Issues
    "General Complaint": "General Grievance Cell",
    "Other Issues": "General Grievance Cell",
    "General": "General Grievance Cell"
}

# ─── Keyword-based NLP (no heavy deps — works within 512 MB RAM) ──────────────

# Keywords mapped to categories (lowercase)
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("No Water Supply",               ["no water", "water supply", "water not coming", "no drinking water"]),
    ("Pipeline Leakage",              ["pipe leak", "pipeline leak", "burst pipe", "water leaking"]),
    ("Dirty Water Supply",            ["dirty water", "contaminated water", "muddy water", "bad water"]),
    ("Blocked Drainage",              ["blocked drain", "clogged drain", "drain overflow", "sewage overflow", "open manhole", "manhole"]),
    ("Road Maintenance & Potholes",   ["pothole", "road damage", "broken road", "road repair", "road condition", "bad road"]),
    ("Electricity Issues & Power Cuts",["power cut", "no electricity", "electricity gone", "power outage", "voltage", "electric", "transformer"]),
    ("Street Light Problems",          ["street light", "streetlight", "light not working", "dark road", "pole light"]),
    ("Garbage Collection & Waste Management",["garbage", "waste", "trash", "dustbin", "litter", "dumping", "overflowing bin"]),
    ("Public Toilet Maintenance",     ["public toilet", "toilet dirty", "toilet not working", "washroom"]),
    ("Illegal Encroachments",         ["encroachment", "illegal construction", "footpath blocked", "hawker", "squatter"]),
    ("Noise Pollution",               ["noise", "loud music", "horn", "construction noise", "sound pollution"]),
    ("Corruption Complaints",         ["bribe", "corruption", "corrupt", "money demanded", "illegal money"]),
    ("Traffic Violations",            ["traffic", "reckless driving", "illegal parking", "red light", "overloaded vehicle"]),
    ("Public Health Hazards",         ["mosquito", "disease", "contaminated food", "health hazard", "hospital negligence"]),
    ("School Infrastructure Problems",["school", "teacher", "classroom", "education", "college"]),
    ("Animal Nuisance",               ["stray dog", "stray animal", "cow on road", "animal attack", "snake"]),
    ("Police Misconduct",             ["police", "cop", "officer misconduct", "police brutality", "false arrest"]),
    ("Ration & PDS Issues",           ["ration", "ration card", "pds", "food grain", "ration shop"]),
    ("Land & Property Disputes",      ["land dispute", "property dispute", "encroach land", "boundary dispute"]),
    ("Fire Hazards",                  ["fire", "fire hazard", "burning", "smoke", "flame"]),
]

_HIGH_KEYWORDS   = {"urgent", "emergency", "critical", "immediately", "danger", "life",
                    "death", "murder", "fire", "accident", "assault", "kidnap", "flood"}
_LOW_KEYWORDS    = {"suggestion", "feedback", "minor", "small", "request", "whenever"}


def is_valid_complaint(text: str) -> bool:
    return bool(re.search(r"[a-zA-Z]", text))


def classify_complaint(text: str) -> str:
    if not is_valid_complaint(text):
        return "Invalid Complaint"
    lower = text.lower()
    best_cat, best_score = "General", 0
    for category, keywords in _CATEGORY_KEYWORDS:
        score = sum(1 for kw in keywords if kw in lower)
        if score > best_score:
            best_score, best_cat = score, category
    return best_cat


def analyze_sentiment(text: str) -> Tuple[str, str]:
    lower = text.lower()
    words = set(re.findall(r"\w+", lower))
    if words & _HIGH_KEYWORDS:
        return "Negative", "High"
    if words & _LOW_KEYWORDS:
        return "Neutral", "Low"
    # Negative sentiment keywords
    neg = {"bad", "worst", "terrible", "horrible", "useless", "broken", "damaged",
           "leak", "dirty", "blocked", "overflow", "dead", "no", "not", "never", "fail", "failed"}
    neg_count = len(words & neg)
    if neg_count >= 2:
        return "Negative", "High"
    if neg_count == 1:
        return "Negative", "Medium"
    return "Neutral", "Low"


def auto_assign_department(category: str) -> str:
    return department_mapping.get(category, "General Grievance Cell")


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

        category = classify_complaint(text)
        if category == "Invalid Complaint":
            await message.reply("❌ Your complaint is invalid. Please provide a real issue.")
            return

        sentiment, priority = analyze_sentiment(text)
        department   = auto_assign_department(category)
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
async def home():
    return {"message": "Grievance Bot API v2 is running!", "dashboard": "/dashboard", "docs": "/docs"}


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
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ─── Entry point ─────────────────────────────────────────────────────────────
async def main():
    """Single event loop entry point.
    Uses sync pymongo (no loop binding) via run_in_executor — immune to
    the Motor/Python 3.13 'Future attached to a different loop' bug.
    """
    global mongo_client, db, complaints_col, feedback_col, counters_col

    # Sync MongoClient — no asyncio loop binding whatsoever
    mongo_client   = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db             = mongo_client["grievance_bot"]
    complaints_col = db["complaints"]
    feedback_col   = db["feedback"]
    counters_col   = db["counters"]

    bot_task = asyncio.create_task(dp.start_polling(bot))

    logging.info(f"✅ Dashboard → http://localhost:{PORT}/dashboard")
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