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
PARALLEL_WORKERS = 6  # Stabilna liczba wątków chroniąca przed blokadą gniazd
TARGET_PROMO_PRICE = 50.00
MAX_SEATS_CAP = 65    # Założona maksymalna pojemność puli online

CSV_SANOK_WROCLAW = "ceny_sanok_wroclaw.csv"
CSV_WROCLAW_SANOK = "ceny_wroclaw_sanok.csv"
HORIZON_FILE = "chronos_boundary.txt"
README_FILE = "README.md"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STOPS = {
    "sanok": {"id": "17", "name": "SANOK D.A. Lipińskiego"},
    "wroclaw": {"id": "77", "name": "WROCŁAW PKS Polbus"}
}

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

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        try:
            s.get(GATEWAY_ENDPOINT, headers=HEADERS, timeout=8)
        except Exception:
            pass
        _thread_local.session = s
    return _thread_local.session


def normalize_time(t: str) -> str:
    return re.sub(r'[-]', ':', t.strip())


def query_raw_course_list(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1) -> dict:
    """Odpytuje API dla pojedynczej relacji i zwraca słownik dostępnych kursów: {dep_time: {hours, price}}."""
    session = get_session()
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": str(passengers),
        "date_there": date_str,
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    try:
        r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=6)
        if r.status_code != 200:
            return {}

        try:
            raw = r.json()
        except Exception:
            raw = json.loads(r.text)

        content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
        data = json.loads(content) if isinstance(content, str) else content

        courses = {}
        if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
            for it in data["ga4_data"][0].get("items", []):
                name = it.get("item_name", "")
                p_val = it.get("price") or it.get("discount", 0.0)
                try:
                    price = float(p_val)
                except (ValueError, TypeError):
                    price = 0.0

                m = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                if m:
                    dep = normalize_time(m.group(1))
                    arr = normalize_time(m.group(2))
                    h_str = f"{dep} -> {arr}"
                else:
                    dep = name
                    h_str = name

                if price > 0:
                    courses[dep] = {"hours": h_str, "price": price}

        return courses
    except Exception:
        return {}


def scan_day_departures(date_str: str) -> tuple:
    """Etap 1: Pobiera listę kursów z przypisaniem K1, K2, K3..."""
    sw = query_raw_course_list(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, 1)
    ws = query_raw_course_list(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, 1)

    # Sortowanie chronologiczne i przypisanie identyfikatorów K1, K2...
    sw_sorted = sorted(sw.items(), key=lambda x: x[0])
    ws_sorted = sorted(ws.items(), key=lambda x: x[0])

    courses_sw = []
    for idx, (dep, data) in enumerate(sw_sorted, 1):
        courses_sw.append({
            "route": "Sanok ➔ Wrocław",
            "course_code": f"K{idx}",
            "date": date_str,
            "departure": dep,
            "hours": data["hours"],
            "price": data["price"],
            "from_id": STOPS["sanok"]["id"],
            "from_name": STOPS["sanok"]["name"],
            "to_id": STOPS["wroclaw"]["id"],
            "to_name": STOPS["wroclaw"]["name"],
            "seats": 1
        })

    courses_ws = []
    for idx, (dep, data) in enumerate(ws_sorted, 1):
        courses_ws.append({
            "route": "Wrocław ➔ Sanok",
            "course_code": f"K{idx}",
            "date": date_str,
            "departure": dep,
            "hours": data["hours"],
            "price": data["price"],
            "from_id": STOPS["wroclaw"]["id"],
            "from_name": STOPS["wroclaw"]["name"],
            "to_id": STOPS["sanok"]["id"],
            "to_name": STOPS["sanok"]["name"],
            "seats": 1
        })

    return date_str, courses_sw, courses_ws


def resolve_course_binary_search(course: dict) -> dict:
    """Etap 2: Dziel i zwyciężaj w przedziale 1..MAX_SEATS_CAP niezależnie dla kursu."""
    from_id = course["from_id"]
    from_name = course["from_name"]
    to_id = course["to_id"]
    to_name = course["to_name"]
    d = course["date"]
    dep = course["departure"]

    # 1. Szybkie sprawdzenie sufitu (65 miejsc)
    check_max = query_raw_course_list(from_id, from_name, to_id, to_name, d, passengers=MAX_SEATS_CAP)
    if dep in check_max:
        course["seats"] = MAX_SEATS_CAP
        return course

    # 2. Algorytm Dziel i Zwyciężaj (Binary Search 1..64)
    low = 1
    high = MAX_SEATS_CAP - 1
    exact_seats = 1

    while low <= high:
        mid = (low + high) // 2
        res = query_raw_course_list(from_id, from_name, to_id, to_name, d, passengers=mid)
        if dep in res:
            exact_seats = mid
            low = mid + 1      # Próbujemy znaleźć więcej wolnych miejsc
        else:
            high = mid - 1     # Za dużo pasażerów, szukamy w dolnej połowie
        time.sleep(0.02)

    course["seats"] = exact_seats
    return course


def generate_dates(days_count: int) -> list:
    today = date.today()
    return [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days_count)]


def load_previous_snapshot(csv_file: str) -> dict:
    prev = {}
    if not os.path.isfile(csv_file):
        return prev
    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Klucz: (Data, Kod kursu, Godzina)
                key = (row.get("Data kursu"), row.get("Kod kursu"), row.get("Godzina kursu"))
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


def update_database(courses: list, csv_file: str):
    if not courses:
        return
    file_exists = os.path.isfile(csv_file)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for c in courses:
        rows.append([
            ts,
            c["date"],
            c["course_code"],
            c["hours"],
            f"{c['price']:.2f}",
            str(c.get("seats", 1))
        ])

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["Data pomiaru", "Data kursu", "Kod kursu", "Godzina kursu", "Cena (PLN)", "Wolne miejsca"])
        w.writerows(rows)
    print(f"💾 [{csv_file}] Zapisano {len(rows)} rekordów.", flush=True)


def compute_deltas(current_courses: list, prev_dict: dict) -> list:
    deltas = []
    for c in current_courses:
        key = (c["date"], c["course_code"], c["hours"])
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
                "course_code": c["course_code"],
                "hours": c["hours"],
                "curr_price": curr_price,
                "price_diff": price_diff,
                "curr_seats": curr_seats,
                "seats_diff": seats_diff
            })
    return deltas


def render_bar(seats, total: int = MAX_SEATS_CAP) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def build_readme(courses_san_wro: list, courses_wro_san: list, deltas: list):
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Horyzont:** 120+ dni\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n"
    ]

    if deltas:
        md.append("| Trasa | Data | Kurs | Aktualna Cena | Zmiana Ceny (Δ) | Wolne Miejsca | Zmiana Miejsc (Δ) |\n")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for d in deltas:
            p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else (f"🔴 `{d['price_diff']:+.2f} zł`" if d['price_diff'] > 0 else "0.00 zł")
            s_delta = f"📉 `{d['seats_diff']:+d}` (Kupiono)" if d['seats_diff'] < 0 else (f"📈 `{d['seats_diff']:+d}` (Zwrot/Nowa pula)" if d['seats_diff'] > 0 else "Bez zmian")
            s_str = f"{d['curr_seats']}" if d['curr_seats'] is not None else "B/D"
            md.append(f"| {d['route']} | 📅 **{d['date']}** | **{d['course_code']}** ({d['hours']}) | **{d['curr_price']:.2f} zł** | {p_delta} | `{s_str}` | {s_delta} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i dostępności miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 2. Mapy Obłożenia (Heatmapy Kursów K1, K2...)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        "![Heatmapa Sanok -> Wrocław](heatmapa_sanok_wroclaw.png)\n\n",
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        "![Heatmapa Wrocław -> Sanok](heatmapa_wroclaw_sanok.png)\n\n",
        "---\n\n",
        "## 📋 3. Pełny Wykaz Kursów\n\n",
        "### 📍 Sanok ➔ Wrocław\n\n",
        "| Data | Kod | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_san_wro:
        bar = render_bar(c["seats"])
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | `{c['course_code']}` | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 📍 Wrocław ➔ Sanok\n\n",
        "| Data | Kod | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_wro_san:
        bar = render_bar(c["seats"])
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | `{c['course_code']}` | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Zaktualizowano README.md.", flush=True)


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

    d_furthest = datetime.strptime(furthest, "%d.%m.%Y")
    d_prev = datetime.strptime(prev, "%d.%m.%Y")

    if d_furthest > d_prev:
        msg = (
            f"📢 **OTWARTO NOWĄ PULĘ BILETÓW!** @everyone\n\n"
            f"📅 Sprzedaż wydłużona do: **{furthest}** (wcześniej: {prev})\n"
            f"🚀 Sprawdź promocyjne bilety na https://neobus.pl/"
        )
        if DISCORD_WEBHOOK_URL:
            try:
                requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": msg}, timeout=8)
            except Exception:
                pass
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("🚀 SENTINEL N3: ISOLATED BINARY SEARCH ENGINE (1..65)", flush=True)
    print("==========================================================", flush=True)

    prev_san_wro = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_wro_san = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = generate_dates(DAYS_FORWARD_SEARCH)
    total_days = len(dates)

    all_raw_courses = []

    # ETAP 1: Skanowanie kalendarza i indeksacja K1, K2...
    print(f"\n📡 [ETAP 1/2] Skanowanie kalendarza połączeń ({total_days} dni)...", flush=True)
    done_scan = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day_departures, d) for d in dates]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            all_raw_courses.extend(res_sw)
            all_raw_courses.extend(res_ws)
            done_scan += 1
            if done_scan % 20 == 0 or done_scan == total_days:
                pct = (done_scan / total_days) * 100
                print(f"  [📅 {done_scan:03d}/{total_days} | {pct:5.1f}%] Przeskanowano...", flush=True)

    total_courses = len(all_raw_courses)
    print(f"\n✅ Znaleziono {total_courses} aktywnych kursów.", flush=True)

    all_dates = list({c["date"] for c in all_raw_courses})
    check_and_notify_horizon(all_dates)

    # ETAP 2: Dokładne badanie wolnych miejsc metodą Dziel i Zwyciężaj (każdy kurs oddzielnie)
    print(f"\n🔬 [ETAP 2/2] Dziel i Zwyciężaj: Badanie wolnych miejsc ({total_courses} kursów)...", flush=True)
    done_eval = 0
    courses_san_wro = []
    courses_wro_san = []

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(resolve_course_binary_search, c): c for c in all_raw_courses}
        for fut in as_completed(futures):
            res = fut.result()
            if res["route"] == "Sanok ➔ Wrocław":
                courses_san_wro.append(res)
            else:
                courses_wro_san.append(res)

            done_eval += 1
            pct = (done_eval / total_courses) * 100
            print(f"  [💺 {done_eval:03d}/{total_courses} | {pct:5.1f}%] {res['route']} | {res['date']} {res['course_code']} ({res['hours']}) ➔ {res['seats']}/{MAX_SEATS_CAP} wolnych", flush=True)

    # Sortowanie chronologiczne
    courses_san_wro.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["course_code"]))
    courses_wro_san.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["course_code"]))

    # Wyliczanie zmian
    deltas = compute_deltas(courses_san_wro, prev_san_wro) + compute_deltas(courses_wro_san, prev_wro_san)

    # Zapis danych
    print("\n💾 Zapisywanie baz CSV i generowanie README.md...", flush=True)
    update_database(courses_san_wro, CSV_SANOK_WROCLAW)
    update_database(courses_wro_san, CSV_WROCLAW_SANOK)
    build_readme(courses_san_wro, courses_wro_san, deltas)

    elapsed = time.time() - start_t
    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO POMYŚLNIE W CZASIE: {elapsed:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
