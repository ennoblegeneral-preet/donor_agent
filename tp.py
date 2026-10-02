import os
import sys
import asyncio
import smtplib
from pathlib import Path
from dotenv import load_dotenv

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')


dotenv_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path)

print("=" * 70)

print(" interview ai api keys and services diagnodstic ")

print("=" * 70)

results = {}

def test_groq():
    key = os.envrion.get("groo_apio", "")
    