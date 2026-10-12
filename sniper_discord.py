import json
import os
import re
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Automatyczne wykrycie lokalizacji watchlist.json (główny folder lub docs/)
if os.path.isfile(os.path.join(BASE_DIR, "watchlist.json")):
    WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
elif os.path.isfile(os.path.join(BASE_DIR, "docs", "watchlist.json")):
    WATCHLIST_PATH = os.path.join(BASE_DIR, "docs", "watchlist.json")
else:
    WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
WARSAW_TZ = ZoneInfo("Europe/Warsaw")

GATEWAY_ENDPOINT = "https://neobus.pl/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pl,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://neobus.pl",
    "Referer": "https://neobus.pl/",
}

MAX_CAPACITY = 70


def get_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.4, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    try:
        s.get(GATEWAY_ENDPOINT, headers=HEADERS, timeout=8)
    except Exception:
        pass
    return s


session = get_session()


def normalize_time(t: str) -> str:
    return re.sub(r'[-]', ':', t.strip())


def send_discord_alert(title, description, color, fields=None):
    if not DISCORD_WEBHOOK_URL:
        print("[!] Brak DISCORD_WEBHOOK_URL, pomijam powiadomienie.")
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


def query_api(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1):
    """Dokładne zapytanie z corridor_radar wyciągające kursy z ga4_data."""
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": str(passengers),
        "date_there": str(date_str).strip(),
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    for attempt in range(2):
        try:
            r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                time.sleep(0.15)
                continue

            raw = r.json() if hasattr(r, "json") else json.loads(r.text)
            content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
            data = json.loads(content) if isinstance(content, str) else content

            courses = {}
            if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
                for it in data["ga4_data"][0].get("items", []):
                    name = it.get("item_name", "")
                    price = float(it.get("price") or it.get("discount", 0.0) or 0.0)
                    m = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                    if m and price > 0:
                        dep_hour = normalize_time(m.group(1))
                        courses[dep_hour] = {
                            "hours": f"{dep_hour} -> {normalize_time(m.group(2))}",
                            "price": price
                        }
            return courses
        except Exception:
            time.sleep(0.15)
    return None


def match_departure(available_deps: list, target_dep: str) -> str:
    """Dopasowuje najbliższą godzinę odjazdu w tolerancji do 25 min."""
    if not available_deps or not target_dep:
        return None
    if target_dep in available_deps:
        return target_dep

    try:
        t_target = datetime.strptime(target_dep, "%H:%M")
    except Exception:
        return None

    best_dep = None
    min_diff = 999999

    for d in available_deps:
        try:
            t_curr = datetime.strptime(d, "%H:%M")
            diff = abs((t_curr - t_target).total_seconds())
            if diff < min_diff and diff <= 25 * 60:
                min_diff = diff
                best_dep = d
        except Exception:
            pass
    return best_dep


def probe_seats_accurate(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, matched_dep: str) -> int:
    """Precyzyjny pomiar wolnych foteli z corridor_radar."""
    # 1. Sprawdzamy czy kurs ma >= 50 miejsc
    res_50 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=50)
    if res_50 and matched_dep in res_50:
        res_70 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=MAX_CAPACITY)
        return MAX_CAPACITY if (res_70 and matched_dep in res_70) else 50

    # 2. Binary search w zakresie 1..49
    low, high = 1, 49
    exact = 1

    while low <= high:
        mid = (low + high) // 2
        res = query_api(from_id, from_name, to_id, to_name, date_str, passengers=mid)
        if res is None:
            break
        if matched_dep in res:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def run_sniper():
    print(f"[*] Wczytywanie watchlisty z: {WATCHLIST_PATH}", flush=True)
    if not os.path.isfile(WATCHLIST_PATH):
        print(f"[-] Brak pliku {WATCHLIST_PATH}.", flush=True)
        return

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    today = datetime.now(WARSAW_TZ).date()
    active_targets = []
    archived_targets = data.get("archived_targets", [])
    updated = False

    # 1. Archiwizacja kursów z przeszłości
    for t in data.get("targets", []):
        try:
            t_date = datetime.strptime(t["date"], "%d.%m.%Y").date()
        except Exception:
            continue

        if t_date < today:
            t["archived_at"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            archived_targets.append(t)
            updated = True
            print(f"[*] Zarchiwizowano przeterminowany kurs: {t.get('from_name')} -> {t.get('to_name')} ({t.get('date')} {t.get('hour')})", flush=True)
        else:
            active_targets.append(t)

    # 2. Monitorowanie aktywnych celów
    for target in active_targets:
        date_str = target["date"].strip()
        from_id = target["from_id"]
        from_name = target.get("from_name", "")
        to_id = target["to_id"]
        to_name = target.get("to_name", "")
        target_hour = target["hour"].strip()

        # Pobranie kursów za pomocą mechanizmu GA4
        day_courses = query_api(from_id, from_name, to_id, to_name, date_str, passengers=1)
        if not day_courses:
            print(f"[-] Brak odpowiedzi API dla {from_name} -> {to_name} na {date_str}", flush=True)
            continue

        # Dopasowanie godziny z tolerancją
        matched_hour = match_departure(list(day_courses.keys()), target_hour)
        if not matched_hour:
            print(f"[-] Brak godziny {target_hour} dla {from_name} -> {to_name} ({date_str}). Znalezione: {list(day_courses.keys())}", flush=True)
            continue

        curr_price = day_courses[matched_hour]["price"]
        
        # Precyzyjny odczyt wolnych foteli
        curr_seats = probe_seats_accurate(from_id, from_name, to_id, to_name, date_str, matched_hour)

        # Inicjalizacja stawek
        baseline_price = target.get("baseline_price", 0)
        if baseline_price <= 0:
            baseline_price = curr_price
            target["baseline_price"] = curr_price

        lowest_price = target.get("lowest_seen_price", 999)
        if lowest_price >= 999 or lowest_price <= 0:
            lowest_price = curr_price
            target["lowest_seen_price"] = curr_price

        seat_threshold = target.get("seat_threshold", 5)

        # Alert A: Spadek ceny
        if 0 < curr_price < lowest_price:
            drop_pct = round(((baseline_price - curr_price) / baseline_price) * 100) if baseline_price > 0 else 0
            send_discord_alert(
                title="🔥 Okazja Cenowa // Neobus",
                description=f"Wykryto spadek ceny dla kursu **{from_name} ➔ {to_name}**!",
                color=0x10B981,
                fields=[
                    {"name": "Termin i Godzina", "value": f"📅 {date_str} o **{matched_hour}**", "inline": True},
                    {"name": "Nowa Stawka", "value": f"**{curr_price:.2f} zł** (było: {lowest_price:.2f} zł)", "inline": True},
                    {"name": "Rabat", "value": f"-{drop_pct}%", "inline": True},
                    {"name": "Miejsca", "value": f"{curr_seats} os.", "inline": True},
                    {"name": "Rezerwacja", "value": "[Przejdź na Neobus.pl](https://neobus.pl)", "inline": False}
                ]
            )
            target["lowest_seen_price"] = curr_price
            updated = True

        # Alert B: Ostatnie fotele
        last_recorded_seats = target.get("last_seats", 70)
        if 0 < curr_seats <= seat_threshold and last_recorded_seats > seat_threshold:
            send_discord_alert(
                title="⚠️ Ostatnie Miejsca w Autokarze!",
                description=f"Kończą się wolne fotele na kurs **{from_name} ➔ {to_name}**!",
                color=0xEF4444,
                fields=[
                    {"name": "Termin", "value": f"📅 {date_str} {matched_hour}", "inline": True},
                    {"name": "Zostało foteli", "value": f"🚨 **{curr_seats} szt.**", "inline": True},
                    {"name": "Aktualna cena", "value": f"{curr_price:.2f} zł", "inline": True}
                ]
            )
            updated = True

        target["last_seats"] = curr_seats
        target["current_price"] = curr_price
        target["last_checked"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[✓] Odczytano kurs {from_name} -> {to_name} ({date_str} {matched_hour}): {curr_price:.2f} zł, {curr_seats} miejsc", flush=True)

    data["targets"] = active_targets
    data["archived_targets"] = archived_targets
    data["updated_at"] = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[+] Zakończono cykl snajpera. Aktywne cele: {len(active_targets)}, Zarchiwizowane: {len(archived_targets)}", flush=True)


if __name__ == "__main__":
    run_sniper()
