import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
import threading
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
LATEST_DATE_FILE = "ostatnia_data_sprzedazy.txt"
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

_thread_local = threading.local()


def get_warmed_session() -> requests.Session:
    """Zwraca sesję z zainicjowanymi ciasteczkami omijającymi WAF."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        try:
            s.get("https://neobus.pl/", headers=HEADERS, timeout=8)
        except Exception:
            pass
        _thread_local.session = s
    return _thread_local.session


def normalize_time(t: str) -> str:
    return re.sub(r'[-]', ':', t.strip())


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


def save_route_to_csv(courses_list: list, csv_filename: str):
    if not courses_list:
        return

    file_exists = os.path.isfile(csv_filename)
    last_records = {}

    if file_exists:
        try:
            with open(csv_filename, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row.get("Data kursu"), row.get("Godzina kursu"))
                    try:
                        price = float(row.get("Cena (PLN)", 0))
                        seats = row.get("Wolne miejsca") or "B/D"
                        last_records[key] = (price, str(seats).strip())
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            print(f"[!] Ostrzeżenie przy odczycie {csv_filename}: {e}", flush=True)

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    records_to_add = []

    for c in courses_list:
        key = (c["date"], c["hours"])
        prev = last_records.get(key)
        curr_seats_str = str(c.get("seats", "B/D")).strip()

        is_new = prev is None
        price_changed = prev and abs(c["price"] - prev[0]) > 0.01
        seats_changed = prev and prev[1] != curr_seats_str

        if is_new or price_changed or seats_changed:
            records_to_add.append([
                timestamp,
                c["date"],
                c["hours"],
                f"{c['price']:.2f}",
                curr_seats_str
            ])

    if records_to_add:
        with open(csv_filename, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Data sprawdzenia", "Data kursu", "Godzina kursu", "Cena (PLN)", "Wolne miejsca"])
            writer.writerows(records_to_add)
        print(f"💾 [{csv_filename}] Zaktualizowano {len(records_to_add)} wierszy.", flush=True)
    else:
        print(f"⚡ [{csv_filename}] Ceny i stan miejsc bez zmian.", flush=True)


# =====================================================================
#                 FORMATOWANIE WSKAŹNIKÓW I RAPORTU
# =====================================================================

def render_progress_bar(seats: int) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    total = 90 if seats > 65 else (65 if seats > 50 else 50)
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def get_status_badge(seats) -> str:
    if isinstance(seats, int):
        if seats <= 5:
            return "🔴"
        elif seats <= 25:
            return "🟡"
        return "🟢"
    return "⚪"


def get_recent_history_changes(csv_filename: str, route_label: str, limit: int = 8) -> list:
    if not os.path.isfile(csv_filename):
        return []

    history_per_course = {}
    try:
        with open(csv_filename, mode="r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            for row in reader:
                key = (row.get("Data kursu"), row.get("Godzina kursu"))
                if key not in history_per_course:
                    history_per_course[key] = []
                history_per_course[key].append(row)
    except Exception:
        return []

    changes = []
    for (d_kurs, h_kurs), rows in history_per_course.items():
        if len(rows) > 1:
            prev = rows[-2]
            curr = rows[-1]

            p_price = float(prev.get("Cena (PLN)", 0))
            c_price = float(curr.get("Cena (PLN)", 0))
            p_seats = prev.get("Wolne miejsca", "B/D")
            c_seats = curr.get("Wolne miejsca", "B/D")

            price_str = (
                f"{p_price:.2f} zł ➔ **{c_price:.2f} zł**"
                if abs(c_price - p_price) > 0.01
                else f"{c_price:.2f} zł"
            )
            if p_seats != c_seats and c_seats != "B/D" and p_seats != "B/D":
                diff = int(c_seats) - int(p_seats)
                diff_str = f" ({diff:+d})" if diff != 0 else ""
                seats_str = f"{p_seats} ➔ **{c_seats} szt.**{diff_str}"
            else:
                seats_str = f"{c_seats} szt." if c_seats != "B/D" else "B/D"

            changes.append({
                "time": curr.get("Data sprawdzenia", ""),
                "route": route_label,
                "course": f"📅 {d_kurs} ({h_kurs})",
                "price_change": price_str,
                "seats_change": seats_str,
            })

    changes = sorted(changes, key=lambda x: x["time"], reverse=True)
    return changes[:limit]


def generate_markdown_readme(courses_san_wro: list, courses_wro_san: list):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_str}`  \n",
        "> 🟢 **Dużo miejsc (>25)** | 🟡 **Średnie obłożenie (6–25)** | 🔴 **Ostatnie miejsca (1–5)**\n\n",
        "## ⚡ 1. Ostatnie zarejestrowane zmiany cen i stanu miejsc\n\n",
        "| Data sprawdzenia | Trasa | Kurs | Zmiana ceny | Zmiana miejsc |\n",
        "| :--- | :--- | :--- | :--- | :--- |\n"
    ]

    recent_sw = get_recent_history_changes(CSV_SANOK_WROCLAW, "Sanok ➔ Wrocław", limit=6)
    recent_ws = get_recent_history_changes(CSV_WROCLAW_SANOK, "Wrocław ➔ Sanok", limit=6)
    recent_all = sorted(recent_sw + recent_ws, key=lambda x: x["time"], reverse=True)[:10]

    if recent_all:
        for r in recent_all:
            md.append(f"| `{r['time']}` | {r['route']} | {r['course']} | {r['price_change']} | {r['seats_change']} |\n")
    else:
        md.append("| - | - | Brak odnotowanych zmian w ostatnim cyklu | - | - |\n")

    md.extend([
        "\n---\n\n",
        "## 📊 2. Kalendarz Obłożenia Miejsc (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        "![Heatmapa Sanok -> Wrocław](heatmapa_sanok_wroclaw.png)\n\n",
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        "![Heatmapa Wrocław -> Sanok](heatmapa_wroclaw_sanok.png)\n\n",
        "---\n\n",
        "## 📍 3. Pełny Rozkład i Dostępność Kursów\n\n",
        "### 🧭 Sanok ➔ Wrocław\n\n",
        "| Data | Godzina odjazdu | Wolne miejsca | Cena | Zakup |\n",
        "| :--- | :--- | :--- | :--- | :---: |\n"
    ])

    for c in courses_san_wro:
        seats_val = c.get("seats", "B/D")
        badge = get_status_badge(seats_val)
        seats_bar = render_progress_bar(seats_val) if isinstance(seats_val, int) else "B/D"
        price_tag = f"🔥 **{c['price']:.2f} PLN**" if c["price"] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} PLN"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | {badge} `{seats_bar}` | {price_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 🧭 Wrocław ➔ Sanok\n\n",
        "| Data | Godzina odjazdu | Wolne miejsca | Cena | Zakup |\n",
        "| :--- | :--- | :--- | :--- | :---: |\n"
    ])

    for c in courses_wro_san:
        seats_val = c.get("seats", "B/D")
        badge = get_status_badge(seats_val)
        seats_bar = render_progress_bar(seats_val) if isinstance(seats_val, int) else "B/D"
        price_tag = f"🔥 **{c['price']:.2f} PLN**" if c["price"] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} PLN"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | {badge} `{seats_bar}` | {price_tag} | [Kup bilet](https://neobus.pl/) |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Wygenerowano README.md.", flush=True)


# =====================================================================
#                   POWIADOMIENIA DISCORD
# =====================================================================

def send_discord_message(content: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": content}, headers={"Content-Type": "application/json"}, timeout=10)
    except Exception:
        pass


def check_and_notify_new_schedule(active_dates: list):
    if not active_dates:
        return

    dt_dates = sorted(active_dates, key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    furthest = dt_dates[-1]

    prev = ""
    if os.path.isfile(LATEST_DATE_FILE):
        with open(LATEST_DATE_FILE, "r", encoding="utf-8") as f:
            prev = f.read().strip()

    if not prev:
        with open(LATEST_DATE_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)
        return

    d_furthest = datetime.strptime(furthest, "%d.%m.%Y")
    d_prev = datetime.strptime(prev, "%d.%m.%Y")

    if d_furthest > d_prev:
        msg = (
            f"📢 **NEOBUS OTWORZYŁ NOWĄ PULĘ BILETÓW!** @everyone\n\n"
            f"📅 Nowy zakres sprzedaży wydłużony do: **{furthest}** (wcześniej: {prev})\n"
            f"🚀 Bilety promocyjne za 1 zł: https://neobus.pl/"
        )
        send_discord_message(msg)
        with open(LATEST_DATE_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


# =====================================================================
#                    ZAPYTANIA API I POMIAR MIEJSC
# =====================================================================

def query_neobus(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1, retries: int = 2):
    session = get_warmed_session()
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": TICKET_TYPE,
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": str(passengers),
        "date_there": date_str,
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    for _ in range(retries):
        try:
            resp = session.post("https://neobus.pl/", data=payload, headers=HEADERS, timeout=8)
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
                            dep = normalize_time(match_hours.group(1))
                            arr = normalize_time(match_hours.group(2))
                            hours_str = f"{dep} -> {arr}"
                        else:
                            hours_str = name

                        if price > 0:
                            courses.append({"hours": hours_str, "price": price})
                return courses
        except Exception:
            time.sleep(0.15)
    return None


def scan_single_day_task(date_str: str, known_sw: dict, known_ws: dict) -> tuple:
    """Równoległe badanie obu relacji dla danego dnia."""
    sw_found = query_neobus(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, passengers=1)
    ws_found = query_neobus(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, passengers=1)

    res_sw = []
    if sw_found:
        for c in sw_found:
            k_seats = known_sw.get((date_str, c["hours"]))
            res_sw.append({
                "route": "Sanok ➔ Wrocław", "date": date_str, "hours": c["hours"], "price": c["price"],
                "from_id": STOPS["sanok"]["id"], "from_name": STOPS["sanok"]["name"],
                "to_id": STOPS["wroclaw"]["id"], "to_name": STOPS["wroclaw"]["name"],
                "known_seats": k_seats, "seats": "B/D"
            })

    res_ws = []
    if ws_found:
        for c in ws_found:
            k_seats = known_ws.get((date_str, c["hours"]))
            res_ws.append({
                "route": "Wrocław ➔ Sanok", "date": date_str, "hours": c["hours"], "price": c["price"],
                "from_id": STOPS["wroclaw"]["id"], "from_name": STOPS["wroclaw"]["name"],
                "to_id": STOPS["sanok"]["id"], "to_name": STOPS["sanok"]["name"],
                "known_seats": k_seats, "seats": "B/D"
            })

    return date_str, res_sw, res_ws


def get_fast_seat_count(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, target_hours: str, known_seats: int = None) -> int:
    # 1. Sprawdzenie znanego stanu z bazy CSV (Fast Exit)
    if known_seats and 1 <= known_seats <= 50:
        res = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=known_seats)
        if res is not None and any(c["hours"] == target_hours for c in res):
            if known_seats == 50:
                res_65 = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=65)
                if res_65 is not None and any(c["hours"] == target_hours for c in res_65):
                    res_90 = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=90)
                    return 90 if (res_90 and any(c["hours"] == target_hours for c in res_90)) else 65
                return 50
            res_plus = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=known_seats + 1)
            if res_plus is not None and not any(c["hours"] == target_hours for c in res_plus):
                return known_seats
            high = 50
        else:
            high = known_seats
    else:
        # Szybki check czy kurs nie jest pełny (50 miejsc)
        res_50 = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=50)
        if res_50 and any(c["hours"] == target_hours for c in res_50):
            res_65 = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=65)
            if res_65 and any(c["hours"] == target_hours for c in res_65):
                res_90 = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=90)
                return 90 if (res_90 and any(c["hours"] == target_hours for c in res_90)) else 65
            return 50
        high = 50

    # 2. Wyszukiwanie binarne z ograniczeniem do 5 kroków
    low = 1
    exact_seats = 1
    for _ in range(5):
        if low > high:
            break
        mid = (low + high) // 2
        res = query_neobus(from_id, from_name, to_id, to_name, date_str, passengers=mid)
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


# =====================================================================
#                           GŁÓWNY PROGRAM
# =====================================================================

def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("=== SENTINEL N3: SANOK ⇄ WROCŁAW (FAST PARALLEL ENGINE) ===", flush=True)
    print("==========================================================", flush=True)

    dates = generate_dynamic_dates(DAYS_FORWARD_SEARCH)
    total_days = len(dates)

    known_sw = load_known_seats(CSV_SANOK_WROCLAW)
    known_ws = load_known_seats(CSV_WROCLAW_SANOK)

    # 1. Równoległe pobieranie siatki połączeń z zachowaniem sesji
    print(f"\n📡 [ETAP 1/2] Równoległe skanowanie siatki połączeń ({total_days} dni)...", flush=True)
    courses_san_wro = []
    courses_wro_san = []
    done_days = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(scan_single_day_task, d, known_sw, known_ws) for d in dates]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            courses_san_wro.extend(res_sw)
            courses_wro_san.extend(res_ws)
            done_days += 1
            if done_days % 20 == 0 or done_days == total_days:
                pct = (done_days / total_days) * 100
                total_f = len(courses_san_wro) + len(courses_wro_san)
                print(f"  [📅 {done_days:03d}/{total_days} | {pct:5.1f}%] Przeskanowano... Znaleziono łącznie: {total_f} kursów", flush=True)

    all_courses = courses_san_wro + courses_wro_san
    total_count = len(all_courses)
    print(f"\n✅ Zakończono mapowanie. Znaleziono łącznie {total_count} aktywnych kursów.", flush=True)

    # 2. Bezpieczna weryfikacja horyzontu nowej puli
    all_active_dates = list({c["date"] for c in all_courses})
    check_and_notify_new_schedule(all_active_dates)

    # 3. Równoległe badanie miejsc z logowaniem na żywo
    print(f"\n🚀 [ETAP 2/2] Badanie miejsc dla {total_count} kursów ({MAX_WORKERS} wątków)...", flush=True)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(enrich_course_with_seats, course) for course in all_courses]
        done = 0
        for fut in as_completed(futures):
            res = fut.result()
            done += 1
            pct = (done / total_count) * 100
            print(f"  [💺 {done:03d}/{total_count} | {pct:5.1f}%] {res['route']} | {res['date']} ({res['hours']}) ➔ Miejsca: {res['seats']} | {res['price']:.2f} zł", flush=True)

    # 4. Chronologiczne sortowanie
    courses_san_wro.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["hours"]))
    courses_wro_san.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["hours"]))

    # 5. Zapis do plików CSV i generowanie README
    print("\n💾 Zapisywanie baz CSV...", flush=True)
    save_route_to_csv(courses_san_wro, CSV_SANOK_WROCLAW)
    save_route_to_csv(courses_wro_san, CSV_WROCLAW_SANOK)

    generate_markdown_readme(courses_san_wro, courses_wro_san)

    total_time = time.time() - start_t
    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO W CZASIE: {total_time:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
