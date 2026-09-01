import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

# =====================================================================
#                        KONFIGURACJA
# =====================================================================

DAYS_FORWARD_SEARCH = 120
MAX_WORKERS = 8
TARGET_PROMO_PRICE = 50.00
TICKET_TYPE = "normal"

CSV_SANOK_WROCLAW = "ceny_sanok_wroclaw.csv"
CSV_WROCLAW_SANOK = "ceny_wroclaw_sanok.csv"
HORIZON_FILE = "chronos_boundary.txt"
README_FILE = "README.md"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STOPS = {
    "sanok": {"id": "17", "name": "SANOK D.A. Lipińskiego"},
    "wroclaw": {"id": "77", "name": "WROCŁAW PKS Polbus"}
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://neobus.pl",
    "Referer": "https://neobus.pl/"
}


# =====================================================================
#                   DYNAMIKA DAT I BAZA CSV
# =====================================================================

def generate_dynamic_dates(days_count: int) -> list:
    today = date.today()
    return [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days_count)]


def load_known_seats(csv_filename: str) -> dict:
    known = {}
    if os.path.isfile(csv_filename):
        try:
            with open(csv_filename, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row.get("Data kursu"), row.get("Godzina kursu"))
                    val = row.get("Wolne miejsca", "").strip()
                    if val.isdigit():
                        known[key] = int(val)
        except Exception:
            pass
    return known


def load_previous_snapshot(csv_file: str) -> dict:
    prev = {}
    if not os.path.isfile(csv_file):
        return prev
    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("Data kursu"), row.get("Godzina kursu"))
                try:
                    price = float(row.get("Cena (PLN)", 0))
                    seats = row.get("Wolne miejsca", "B/D")
                    prev[key] = {
                        "price": price,
                        "seats": int(seats) if str(seats).isdigit() else None
                    }
                except Exception:
                    pass
    except Exception:
        pass
    return prev


def save_route_to_csv(courses_list: list, csv_filename: str):
    if not courses_list:
        return
    file_exists = os.path.isfile(csv_filename)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for c in courses_list:
        rows.append([
            ts,
            c["date"],
            c["hours"],
            f"{c['price']:.2f}",
            str(c.get("seats", "B/D"))
        ])

    with open(csv_filename, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Data sprawdzenia", "Data kursu", "Godzina kursu", "Cena (PLN)", "Wolne miejsca"])
        writer.writerows(rows)
    print(f"💾 [{csv_filename}] Zapisano {len(rows)} wierszy.", flush=True)


def compute_deltas(current_courses: list, prev_dict: dict) -> list:
    deltas = []
    for c in current_courses:
        key = (c["date"], c["hours"])
        prev = prev_dict.get(key)
        if not prev:
            continue

        curr_seats = c.get("seats") if isinstance(c.get("seats"), int) else None
        prev_seats = prev.get("seats")
        curr_price = c["price"]
        prev_price = prev.get("price", 0.0)

        price_diff = round(curr_price - prev_price, 2)
        seats_diff = (curr_seats - prev_seats) if (curr_seats is not None and prev_seats is not None) else 0

        if abs(price_diff) >= 0.01 or seats_diff != 0:
            deltas.append({
                "route": c["route"],
                "date": c["date"],
                "hours": c["hours"],
                "curr_price": curr_price,
                "price_diff": price_diff,
                "curr_seats": curr_seats,
                "seats_diff": seats_diff
            })
    return deltas


# =====================================================================
#                 FORMATOWANIE WSKAŹNIKÓW I RAPORTU
# =====================================================================

def render_progress_bar(seats: int) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    total = 90 if seats > 65 else 65
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def build_readme(courses_san_wro: list, courses_wro_san: list, deltas: list):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_str}` | 📡 **Zakres:** 120+ dni rozkładowych\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n"
    ]

    if deltas:
        md.append("| Trasa | Data | Kurs | Aktualna Cena | Zmiana Ceny (Δ) | Wolne Miejsca | Zmiana Miejsc (Δ) |\n")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for d in deltas:
            p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else (f"🔴 `{d['price_diff']:+.2f} zł`" if d['price_diff'] > 0 else "0.00 zł")
            s_delta = f"📉 `{d['seats_diff']:+d} miejsc`" if d['seats_diff'] < 0 else (f"📈 `{d['seats_diff']:+d} miejsc`" if d['seats_diff'] > 0 else "Bez zmian")
            s_str = f"{d['curr_seats']}" if d['curr_seats'] is not None else "B/D"
            md.append(f"| {d['route']} | 📅 **{d['date']}** | ⏰ {d['hours']} | **{d['curr_price']:.2f} zł** | {p_delta} | `{s_str}` | {s_delta} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i zajętości miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 2. Mapy Obłożenia i Dostępności Miejsc (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        "![Heatmapa Sanok -> Wrocław](heatmapa_sanok_wroclaw.png)\n\n",
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        "![Heatmapa Wrocław -> Sanok](heatmapa_wroclaw_sanok.png)\n\n",
        "---\n\n",
        "## 📋 3. Pełny Rozkład i Dostępność Kursów\n\n",
        "### 📍 Sanok ➔ Wrocław\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_san_wro:
        s_val = c.get("seats", "B/D")
        bar = render_progress_bar(s_val) if isinstance(s_val, int) else "B/D"
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 📍 Wrocław ➔ Sanok\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_wro_san:
        s_val = c.get("seats", "B/D")
        bar = render_progress_bar(s_val) if isinstance(s_val, int) else "B/D"
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Wygenerowano README.md.", flush=True)


def check_and_notify_horizon(active_dates: list):
    if not active_dates:
        return

    dt_dates = sorted(active_dates, key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    furthest = dt_dates[-1]

    prev = ""
    if os.path.isfile(HORIZON_FILE):
        with open(HORIZON_FILE, "r", encoding="utf-8") as f:
            prev = f.read().strip()

    if not prev:
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)
        return

    if datetime.strptime(furthest, "%d.%m.%Y") > datetime.strptime(prev, "%d.%m.%Y"):
        msg = (
            f"📢 **NEOBUS OTWORZYŁ NOWĄ PULĘ BILETÓW!** @everyone\n\n"
            f"📅 Nowy zakres sprzedaży wydłużony do: **{furthest}** (wcześniej: {prev})\n"
            f"🚀 Bilety promocyjne: https://neobus.pl/"
        )
        if DISCORD_WEBHOOK_URL:
            try:
                requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": msg}, timeout=10)
            except Exception:
                pass
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


# =====================================================================
#                    ZAPYTANIA API I SPRAWDZANIE MIEJSC
# =====================================================================

def query_neobus(session: requests.Session, from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1, retries: int = 3):
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": TICKET_TYPE,
        "initial_stop": from_id,
        "final_stop": to_id,
        "passengers": str(passengers),
        "date_there": date_str,
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    for _ in range(retries):
        try:
            resp = session.post("https://neobus.pl/", data=payload, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
                data = json.loads(content) if isinstance(content, str) else content

                courses = []
                if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
                    for it in data["ga4_data"][0].get("items", []):
                        name = it.get("item_name", "")
                        price = it.get("price") or it.get("discount", 0.0)
                        try:
                            price = float(price)
                        except Exception:
                            price = 0.0

                        match_hours = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                        if match_hours:
                            h1 = match_hours.group(1).replace("-", ":")
                            h2 = match_hours.group(2).replace("-", ":")
                            hours_str = f"{h1} -> {h2}"
                            dep = h1
                        else:
                            hours_str = name
                            dep = name

                        if price > 0:
                            courses.append({"hours": hours_str, "departure": dep, "price": price})
                return courses
        except Exception:
            time.sleep(0.3)
    return None


def get_fast_seat_count(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, target_hours: str, known_seats: int = None) -> int:
    session = requests.Session()

    # 1. Sprawdzenie autokarów piętrowych (90 miejsc)
    res_90 = query_neobus(session, from_id, from_name, to_id, to_name, date_str, passengers=90)
    if res_90 is not None and any(c["hours"] == target_hours for c in res_90):
        return 90

    # 2. Fast-Path z wykorzystaniem znanej liczby miejsc z poprzedniego zapisu w bazie
    if known_seats and 1 <= known_seats <= 65:
        res = query_neobus(session, from_id, from_name, to_id, to_name, date_str, passengers=known_seats)
        if res is not None and any(c["hours"] == target_hours for c in res):
            if known_seats == 65:
                return 65
            res_plus = query_neobus(session, from_id, from_name, to_id, to_name, date_str, passengers=known_seats + 1)
            if res_plus is not None and not any(c["hours"] == target_hours for c in res_plus):
                return known_seats
            high = 65
        else:
            high = known_seats
    else:
        # Standardowa pula autokaru
        res_65 = query_neobus(session, from_id, from_name, to_id, to_name, date_str, passengers=65)
        if res_65 is not None and any(c["hours"] == target_hours for c in res_65):
            return 65
        high = 64

    low = 1
    exact_seats = 1

    # 3. Wyszukiwanie binarne
    while low <= high:
        mid = (low + high) // 2
        res = query_neobus(session, from_id, from_name, to_id, to_name, date_str, passengers=mid)
        if res is None:
            continue
        if any(c["hours"] == target_hours for c in res):
            exact_seats = mid
            low = mid + 1
        else:
            high = mid - 1

    return exact_seats


def enrich_course_with_seats(course: dict) -> dict:
    course["seats"] = get_fast_seat_count(
        course["from_id"],
        course["from_name"],
        course["to_id"],
        course["to_name"],
        course["date"],
        course["hours"],
        course.get("known_seats")
    )
    return course


def check_route_base(route_label: str, from_id: str, from_name: str, to_id: str, to_name: str, dates_list: list, known_dict: dict):
    session = requests.Session()
    courses = []
    empty_days = 0

    for d in dates_list:
        found = query_neobus(session, from_id, from_name, to_id, to_name, d, passengers=1)
        if found:
            empty_days = 0
            for c in found:
                k_seats = known_dict.get((d, c["hours"]))
                courses.append({
                    "route": route_label,
                    "date": d,
                    "hours": c["hours"],
                    "departure": c["departure"],
                    "price": c["price"],
                    "from_id": from_id,
                    "from_name": from_name,
                    "to_id": to_id,
                    "to_name": to_name,
                    "known_seats": k_seats,
                    "seats": "B/D"
                })
        else:
            empty_days += 1

        if empty_days >= 8:
            print(f"🛑 [{route_label}] Koniec dostępnego rozkładu od {d}.", flush=True)
            break

    return courses


# =====================================================================
#                           GŁÓWNY PROGRAM
# =====================================================================

def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("🚀 SENTINEL N3: SANOK ⇄ WROCŁAW (KLASYCZNA ARCHITEKTURA)", flush=True)
    print("==========================================================", flush=True)

    dates = generate_dynamic_dates(DAYS_FORWARD_SEARCH)

    known_san_wro = load_known_seats(CSV_SANOK_WROCLAW)
    known_wro_san = load_known_seats(CSV_WROCLAW_SANOK)

    prev_san_wro = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_wro_san = load_previous_snapshot(CSV_WROCLAW_SANOK)

    # 1. Pobieranie siatki połączeń
    print("\n📡 [ETAP 1/2] Skanowanie siatki połączeń...", flush=True)
    courses_san_wro = check_route_base("Sanok ➔ Wrocław", STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], dates, known_san_wro)
    courses_wro_san = check_route_base("Wrocław ➔ Sanok", STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], dates, known_wro_san)

    all_courses = courses_san_wro + courses_wro_san
    total_count = len(all_courses)
    print(f"✅ Znaleziono {total_count} aktywnych kursów w rozkładzie.", flush=True)

    # 2. Sprawdzenie otwarcia nowej puli
    all_active_dates = list({c["date"] for c in all_courses})
    check_and_notify_horizon(all_active_dates)

    # 3. Równoległe badanie miejsc z logowaniem na żywo
    print(f"\n🔬 [ETAP 2/2] Badanie wolnych miejsc dla {total_count} kursów ({MAX_WORKERS} wątków)...", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(enrich_course_with_seats, course): course for course in all_courses}
        for fut in as_completed(futures):
            res = fut.result()
            done += 1
            pct = (done / total_count) * 100
            print(f"  [💺 {done:03d}/{total_count} | {pct:5.1f}%] {res['route']} | {res['date']} {res['hours']} ➔ {res['seats']} wolnych | {res['price']:.2f} zł", flush=True)

    # Sortowanie chronologiczne
    courses_san_wro.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_wro_san.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))

    # 4. Wyliczanie delty zmian
    deltas = compute_deltas(courses_san_wro, prev_san_wro) + compute_deltas(courses_wro_san, prev_wro_san)

    # 5. Zapis do plików CSV
    print("\n💾 Utrwalanie baz CSV...", flush=True)
    save_route_to_csv(courses_san_wro, CSV_SANOK_WROCLAW)
    save_route_to_csv(courses_wro_san, CSV_WROCLAW_SANOK)

    # 6. Generowanie raportu README.md
    build_readme(courses_san_wro, courses_wro_san, deltas)

    total_time = time.time() - start_t
    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO POMYŚLNIE W CZASIE: {total_time:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
