import json
import os
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Automatyczne wykrycie położenia pliku watchlist.json (katalog główny lub docs/)
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


def fetch_neobus_courses(from_id, to_id, date_dmy):
    """
    Wysyła czyste żądanie AJAX POST dokładnie tak jak Hermes Core.
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
        "initial_stop": str(from_id).strip(),
        "final_stop": str(to_id).strip(),
        "passengers": "1",
        "date_there": str(date_dmy).strip(),  # format DD.MM.YYYY
        "date_return": ""
    }

    try:
        resp = session.post(url, data=payload, timeout=15)
        if resp.status_code != 200:
            print(f"[!] Neobus zwrócił status HTTP {resp.status_code}")
            return []

        data = resp.json()
        schedules = data.get("schedules", {})
        
        items = schedules if isinstance(schedules, list) else schedules.values()
        courses = []

        for sch in items:
            t1 = sch.get("time1", "")  # np. "2026-10-13 03:48:00"
            if not t1 or " " not in t1:
                continue
            time_part = t1.split(" ")[1][:5].strip()  # "03:48"
            
            try:
                price = float(sch.get("price", 0.0))
            except (ValueError, TypeError):
                price = 0.0
                
            try:
                seats = int(sch.get("seats", 70))
            except (ValueError, TypeError):
                seats = 70
            
            courses.append({
                "time": time_part,
                "price": price,
                "seats": seats,
                "sold_out": (seats <= 0)
            })

        return courses
    except Exception as e:
        print(f"[!] Błąd zapytania POST do Neobusa ({from_id} -> {to_id}, {date_dmy}): {e}")
        return []


def to_minutes(time_str):
    """Konwertuje 'HH:MM' na minuty od północy."""
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 9999


def run_sniper():
    print(f"[*] Wczytywanie watchlisty z: {WATCHLIST_PATH}")
    if not os.path.isfile(WATCHLIST_PATH):
        print(f"[-] Brak pliku {WATCHLIST_PATH}.")
        return

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    today = datetime.now(WARSAW_TZ).date()
    active_targets = []
    archived_targets = data.get("archived_targets", [])
    updated = False

    # 1. Filtrowanie i archiwizacja przeszłych kursów
    for t in data.get("targets", []):
        try:
            t_date = datetime.strptime(t["date"], "%d.%m.%Y").date()
        except Exception:
            continue

        if t_date < today:
            t["archived_at"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            archived_targets.append(t)
            updated = True
            print(f"[*] Zarchiwizowano przeterminowany kurs: {t.get('from_name')} -> {t.get('to_name')} ({t.get('date')} {t.get('hour')})")
        else:
            active_targets.append(t)

    # 2. Monitorowanie aktywnych kursów
    for target in active_targets:
        date_dmy = target["date"].strip()
        results = fetch_neobus_courses(target["from_id"], target["to_id"], date_dmy)
        
        target_hour = target["hour"].strip()
        if len(target_hour) == 4 and target_hour[1] == ':':
            target_hour = "0" + target_hour

        # Inteligentne dopasowanie:
        # Krok 1: Dokładne dopasowanie godziny
        matched_course = next((c for c in results if c["time"] == target_hour), None)

        # Krok 2: Jeśli brak, szukanie kursu z tolerancją do +/- 15 minut (różnice w przystankach pośrednich)
        if not matched_course and results:
            target_mins = to_minutes(target_hour)
            for c in results:
                if abs(to_minutes(c["time"]) - target_mins) <= 15:
                    matched_course = c
                    print(f"[*] Dopasowano z tolerancją czasową: cel {target_hour} -> znaleziono {c['time']}")
                    break

        if not matched_course:
            dostepne = [c["time"] for c in results]
            print(f"[-] Brak godziny {target_hour} dla {target.get('from_name')} -> {target.get('to_name')} ({date_dmy}). Znalezione w Neobusie: {dostepne}")
            continue

        curr_price = matched_course["price"]
        curr_seats = matched_course["seats"]
        
        # Inicjalizacja stawek przy pierwszym odczycie
        baseline_price = target.get("baseline_price", 0)
        if baseline_price <= 0:
            baseline_price = curr_price
            target["baseline_price"] = curr_price

        lowest_price = target.get("lowest_seen_price", 999)
        if lowest_price >= 999 or lowest_price <= 0:
            lowest_price = curr_price
            target["lowest_seen_price"] = curr_price

        seat_threshold = target.get("seat_threshold", 5)

        # Scenariusz A: Spadek ceny
        if 0 < curr_price < lowest_price:
            drop_pct = round(((baseline_price - curr_price) / baseline_price) * 100) if baseline_price > 0 else 0
            send_discord_alert(
                title="🔥 Okazja Cenowa // Neobus",
                description=f"Wykryto spadek ceny dla kursu **{target.get('from_name')} ➔ {target.get('to_name')}**!",
                color=0x10B981,
                fields=[
                    {"name": "Termin i Godzina", "value": f"📅 {target['date']} o **{matched_course['time']}**", "inline": True},
                    {"name": "Nowa Stawka", "value": f"**{curr_price:.2f} zł** (było: {lowest_price:.2f} zł)", "inline": True},
                    {"name": "Rabat", "value": f"-{drop_pct}%", "inline": True},
                    {"name": "Miejsca", "value": f"{curr_seats} os.", "inline": True},
                    {"name": "Rezerwacja", "value": "[Przejdź na Neobus.pl](https://neobus.pl)", "inline": False}
                ]
            )
            target["lowest_seen_price"] = curr_price
            updated = True

        # Scenariusz B: Kończące się miejsca
        last_recorded_seats = target.get("last_seats", 70)
        if 0 < curr_seats <= seat_threshold and last_recorded_seats > seat_threshold:
            send_discord_alert(
                title="⚠️ Ostatnie Miejsca w Autokarze!",
                description=f"Kończą się wolne fotele na kurs **{target.get('from_name')} ➔ {target.get('to_name')}**!",
                color=0xEF4444,
                fields=[
                    {"name": "Termin", "value": f"📅 {target['date']} {matched_course['time']}", "inline": True},
                    {"name": "Zostało foteli", "value": f"🚨 **{curr_seats} szt.**", "inline": True},
                    {"name": "Aktualna cena", "value": f"{curr_price:.2f} zł", "inline": True}
                ]
            )
            updated = True

        target["last_seats"] = curr_seats
        target["current_price"] = curr_price
        target["last_checked"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[✓] Odczytano kurs {target.get('from_name')} -> {target.get('to_name')} ({date_dmy} {matched_course['time']}): {curr_price:.2f} zł, {curr_seats} wolnych miejsc")

    data["targets"] = active_targets
    data["archived_targets"] = archived_targets
    data["updated_at"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[+] Zakończono cykl snajpera. Aktywne cele: {len(active_targets)}, Zarchiwizowane: {len(archived_targets)}")


if __name__ == "__main__":
    run_sniper()
