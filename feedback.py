"""Utility script — print all feedback from MongoDB (run standalone)."""
from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

def main():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    db     = client["grievance_bot"]
    docs   = list(db["feedback"].find())
    if not docs:
        print("ℹ️  No feedback found.")
    else:
        for d in docs:
            print(f"User: {d.get('user_id')} | @{d.get('username','—')} | {d.get('message')} | {d.get('timestamp','')}")
    client.close()

main()