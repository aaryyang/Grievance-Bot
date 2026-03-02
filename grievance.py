"""Utility script — print all complaints from MongoDB (run standalone)."""
from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

def main():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    db     = client["grievance_bot"]
    docs   = list(db["complaints"].find().sort("complaint_id", -1))
    if not docs:
        print("ℹ️  No complaints found.")
    else:
        for d in docs:
            print(
                f"#{d.get('complaint_id')} | {d.get('status')} | {d.get('priority')} | "
                f"{d.get('department')} | {d.get('complaint','')[:60]}"
            )
    client.close()

main()