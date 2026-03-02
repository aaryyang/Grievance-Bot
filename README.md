# 🤖 Grievance Bot

A production-ready **AI-powered citizen grievance management system** built on Telegram.  
Citizens file complaints via a Telegram bot; an admin web dashboard tracks, filters, and resolves them in real time.

---

## ✨ Features

- 📝 **Complaint logging** via `/log` — smart validation rejects bogus/vague input before processing
- 🧠 **Weighted keyword NLP** — unified `_RULES` dict scores phrases (3 pts) and words (1 pt) in a single pass
- 🤖 **HuggingFace Inference API fallback** — `facebook/bart-large-mnli` zero-shot called only when keyword score is zero
- 🏢 **Auto department routing** — category → department → base priority all in one lookup
- ⚡ **Emergency detection** — urgency words and emergency categories always force High priority
- 📊 **Admin dashboard** — dark/light mode, stat cards, Chart.js charts, searchable table, served at `/`
- ✅ **Resolve / Delete** — admin-only actions via Telegram commands or dashboard buttons
- 💬 **Feedback tab** — users submit feedback via `/feedback`, visible in dashboard
- ⚡ **Real-time refresh** — dashboard updates instantly via Server-Sent Events when a complaint is logged
- 🚀 **Deployable** — Dockerfile + `render.yaml` for one-click Render.com deploy

---

## 🛠️ Tech Stack

| Layer | Tech |
|---|---|
| Bot | [aiogram v3](https://docs.aiogram.dev/) |
| Web API | [FastAPI](https://fastapi.tiangolo.com/) + uvicorn |
| Database | MongoDB Atlas via pymongo + certifi TLS |
| NLP | Weighted keyword rules + HuggingFace Inference API (fallback) |
| HTTP client | httpx (async, for HF API calls) |
| Frontend | Jinja2 + Chart.js + SSE (no build step) |
| Deploy | Docker + Render.com |

---

## 🚀 Getting Started

### 1. Clone
```bash
git clone https://github.com/aaryyang/Grievance-Bot.git
cd Grievance-Bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
Create a `.env` file in the root:
```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_telegram_user_id
MONGO_URI=mongodb://localhost:27017
PORT=8000
HF_TOKEN=your_huggingface_token   # optional, for gated models
```

> Get your bot token from [@BotFather](https://t.me/BotFather)  
> Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot)

### 4. Start MongoDB
Make sure MongoDB is running locally, or set `MONGO_URI` to your Atlas connection string.

### 5. Run
```bash
python main.py
```

- Bot starts polling  
- Dashboard at → `http://localhost:8000/`  
- API docs at → `http://localhost:8000/docs`

---

## 🤖 Bot Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Welcome message |
| `/log <complaint>` | Everyone | File a new complaint |
| `/history` | Everyone | View your last 5 complaints |
| `/feedback <message>` | Everyone | Submit feedback |
| `/resolve <id>` | Admin | Mark complaint as resolved |
| `/delete <id>` | Admin | Delete a complaint |

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Admin dashboard |
| `GET` | `/dashboard` | Redirects to `/` |
| `GET` | `/events` | SSE stream — pushes `new` on each complaint logged |
| `GET` | `/api/complaints` | List complaints (filterable) |
| `PUT` | `/api/complaints/{id}/resolve` | Resolve a complaint |
| `DELETE` | `/api/complaints/{id}` | Delete a complaint |
| `GET` | `/api/feedbacks` | All feedback |
| `GET` | `/api/stats` | Summary statistics |
| `GET` | `/docs` | Swagger UI |

---

## 🐳 Deploy on Render.com

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service → Connect repo
3. Set environment variables: `BOT_TOKEN`, `ADMIN_ID`, `MONGO_URI`, `HF_TOKEN`
4. Render picks up `render.yaml` automatically — click **Deploy**

---

## 📁 Project Structure

```
├── main.py                 # Bot + FastAPI app (single entrypoint)
├── templates/
│   └── dashboard.html      # Admin dashboard UI
├── static/                 # Static assets
├── requirements.txt
├── Dockerfile
├── render.yaml
└── .env                    # Not committed — create locally
```

---

## 🧑‍💻 Author

**Aaryan Gupta** · [@aaryyang](https://github.com/aaryyang)
