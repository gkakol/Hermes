import json
import os
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Automatyczne wykrycie właściwego położenia pliku (katalog główny lub docs/)
if os.path.isfile(os.path.join(BASE_DIR, "watchlist.json")):
    WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
elif os.path.isfile(os.path.join(BASE_DIR, "docs", "watchlist.json")):
    WATCHLIST_PATH = os.path.join(BASE_DIR, "docs", "watchlist.json")
else:
    WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
WARSAW_TZ = ZoneInfo("Europe/Warsaw")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

session = requests.Session()
session.headers.update(HEADERS)


def send_discord_alert(title, description, color, fields=None):
    if not DISCORD_WEBHOOK_URL:
        print("[!] Brak zdefiniowanego DISCORD_WEBHOOK_URL, pomijam powiadomienie.")
        return

    payload = {
        "username": "Sentinel Sniper",
        "avatar_url": "https://raw.githubusercontent.com/gkakol/Hermes/main/docs/bus-icon.png",
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "footer": {"text": f"Sentinel Radar • {datetime.now(WARSAW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"}
            }
        ]
    }
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        print(f"[+] Wysłano alert Discord: {title}")
    except Exception as e:
        print(f"[!] Błąd wysyłania na Discord: {e}")


def fetch_neobus_courses(from_id, to_id, date_dmy, from_name="", to_name=""):
    """
    Wysyła żądanie AJAX POST na https://neobus.pl/ (wymaga formatu daty DD.MM.YYYY).
    """
    url = "https://neobus.pl/"
    try:
        session.get(url, timeout=10)
    except Exception:
        pass

    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": "1",
        "date_there": str(date_dmy).strip(),  # Format DD.MM.YYYY
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name
    }

    try:
        resp = session.post(url, data=payload, timeout=15)
        if resp.status_code != 200:
            print(f"[!] Neobus zwrócił status HTTP {resp.status_code}")
            return {}

        data = resp.json()
        courses = {}
        
        schedules = data.get("schedules", {})
        if isinstance(schedules, list):
            items = schedules
        elif isinstance(schedules, dict):
            items = schedules.values()
        else:
            items = []

        for sch in items:
            t1 = sch.get("time1", "")  # np. "2026-10-13 03:48:00"
            if not t1:
                continue
            parts = t1.split(" ")
            if len(parts) < 2:
                continue
            hour = parts[1][:5]  # "03:48"
            
            try:
                price = float(sch.get("price", 0.0))
            except (ValueError, TypeError):
                price = 0.0
                
            try:
                seats = int(sch.get("seats", 70))
            except (ValueError, TypeError):
                seats = 70
            
            courses[hour] = {
                "price": price,
                "seats": seats,
                "sold_out": (seats <= 0)
            }

        return courses
    except Exception as e:
        print(f"[!] Błąd zapytania POST do Neobusa ({from_id} -> {to_id}, {date_dmy}): {e}")
        return {}


def run_sniper():
    print(f"[*] Wczytywanie watchlisty z: {WATCHLIST_PATH}")
    if not os.path.isfile(WATCHLIST_PATH):
        print(f"[-] Brak pliku {WATCHLIST_PATH}.")
        return

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
