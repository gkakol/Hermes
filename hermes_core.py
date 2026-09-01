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
PARALLEL_WORKERS = 8
TARGET_PROMO_PRICE = 50.00

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


def query_api_single_direction(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1) -> dict:
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


def scan_day_schedule(date_str: str) -> tuple:
    time.sleep(0.04)
    sw = query_api_single_direction(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, 1)
    ws = query_api_single_direction(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, 1)
    return date_str, sw, ws


def resolve_exact_seats_isolated(course: dict) -> dict:
    """Proste, izolowane i odporne na błędy badanie pojedynczego kursu."""
    from_id = course["from_id"]
    from_name = course["from_name"]
    to_id = course["to_id"]
    to_name = course["to_name"]
    d = course["date"]
    dep = course["departure"]

    # 1. Sprawdzamy stan standardowej pełnej puli (50 miejsc)
    r50 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=50)
    if dep in r50:
        # Pula jest pełna; sprawdzamy czy to piętrus / przedłużany (65+)
        r65 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=65)
        if dep in r65:
            r90 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=90)
            course["seats"] = 90 if dep in r90 else 65
        else:
            course["seats"] = 50
        return course

    # 2. Jeśli zostało <50 miejsc, sprawdzamy próg 25
    r25 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=25)
    if dep in r25:
        # Miejsca między 26 a 49 -> szybki test na 35
        r35 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=35)
        course["seats"] = 35 if dep in r35 else 25
        return course

    # 3. Końcówka miejsc (<25) -> badamy próg 10 i 5
    r10 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=10)
    if dep in r10:
        course["seats"] = 10
    else:
        r5 = query_api_single_direction(from_id, from_name, to_id, to_name, d, passengers=5)
        course["seats"] = 5 if dep in r5 else 1

    return course

def enrich_course_task(c: dict) -> dict:
    c["seats"] = resolve_course_seats(
        c["from_id"], c["from_name"], c["to_id"], c["to_name"],
        c["date"], c["departure"], c["price"]
    )
    return c


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
            c["hours"],
            f"{c['price']:.2f}",
            str(c.get("seats", "B/D"))
        ])

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["Data pomiaru", "Data kursu", "Godzina kursu", "Cena (PLN)", "Wolne miejsca"])
        w.writerows(rows)
    print(f"💾 [{csv_file}] Zaktualizowano bazę ({len(rows)} rekordów).", flush=True)


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


def render_bar(seats, total: int = 65) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def build_readme(courses_san_wro: list, courses_wro_san: list, deltas: list):
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Zakres:** 120+ dni rozkładowych\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n"
    ]

    if deltas:
        md.append("| Trasa | Data | Kurs | Aktualna Cena | Zmiana Ceny (Δ) | Wolne Miejsca | Zmiana Miejsc (Δ) |\n")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for d in deltas:
            p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else (f"🔴 `{d['price_diff']:+.2f} zł`" if d['price_diff'] > 0 else "0.00 zł")
            s_delta = f"📉 `{d['seats_diff']:+d} miejsc` (Wykupiono)" if d['seats_diff'] < 0 else (f"📈 `{d['seats_diff']:+d} miejsc` (Zwrot)" if d['seats_diff'] > 0 else "Bez zmian")
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
        cap = 90 if isinstance(s_val, int) and s_val > 65 else 65
        bar = render_bar(s_val, cap)
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 📍 Wrocław ➔ Sanok\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_wro_san:
        s_val = c.get("seats", "B/D")
        cap = 90 if isinstance(s_val, int) and s_val > 65 else 65
        bar = render_bar(s_val, cap)
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Zaktualizowano README.md.", flush=True)


def check_and_notify_horizon(active_dates: list):
    if not active_dates:
        return
    # Ścisłe sortowanie chronologiczne po dacie
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
            f"🚀 Sprawdź bilety na https://neobus.pl/"
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
    print("🚀 SENTINEL N3: ULTRA-STABLE SCANNER (SANOK ⇄ WROCŁAW)", flush=True)
    print("==========================================================", flush=True)

    prev_san_wro = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_wro_san = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = generate_dates(DAYS_FORWARD_SEARCH)
    total_days = len(dates)

    raw_courses_sw = []
    raw_courses_ws = []

    print(f"\n📡 [ETAP 1/2] Skanowanie kalendarza połączeń ({total_days} dni)...", flush=True)
    done_scan = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day_schedule, d) for d in dates]
        for fut in as_completed(futures):
            day_str, sw_dict, ws_dict = fut.result()
            done_scan += 1

            for dep, data in sw_dict.items():
                raw_courses_sw.append({
                    "route": "Sanok ➔ Wrocław", "date": day_str, "hours": data["hours"], "departure": dep,
                    "price": data["price"], "from_id": STOPS["sanok"]["id"], "from_name": STOPS["sanok"]["name"],
                    "to_id": STOPS["wroclaw"]["id"], "to_name": STOPS["wroclaw"]["name"], "seats": 1
                })

            for dep, data in ws_dict.items():
                raw_courses_ws.append({
                    "route": "Wrocław ➔ Sanok", "date": day_str, "hours": data["hours"], "departure": dep,
                    "price": data["price"], "from_id": STOPS["wroclaw"]["id"], "from_name": STOPS["wroclaw"]["name"],
                    "to_id": STOPS["sanok"]["id"], "to_name": STOPS["sanok"]["name"], "seats": 1
                })

            if done_scan % 20 == 0 or done_scan == total_days:
                pct = (done_scan / total_days) * 100
                print(f"  [📅 {done_scan:03d}/{total_days} | {pct:5.1f}%] Przeskanowano...", flush=True)

    all_raw = raw_courses_sw + raw_courses_ws
    total_courses = len(all_raw)
    print(f"\n✅ Znaleziono {total_courses} aktywnych kursów w rozkładzie.", flush=True)

    # Weryfikacja horyzontu dat
    all_dates = list({c["date"] for c in all_raw})
    check_and_notify_horizon(all_dates)

    # Dokładne badanie wolnych miejsc
    print(f"\n🔬 [ETAP 2/2] Równoległe badanie miejsc dla każdego kursu ({total_courses} zadań)...", flush=True)
    done_eval = 0
    courses_san_wro = []
    courses_wro_san = []

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(enrich_course_task, c): c for c in all_raw}
        for fut in as_completed(futures):
            res = fut.result()
            if res["route"] == "Sanok ➔ Wrocław":
                courses_san_wro.append(res)
            else:
                courses_wro_san.append(res)

            done_eval += 1
            if done_eval % 40 == 0 or done_eval == total_courses:
                pct = (done_eval / total_courses) * 100
                print(f"  [💺 {done_eval:03d}/{total_courses} | {pct:5.1f}%] Zbadano kurs: {res['route']} {res['date']} {res['hours']} ➔ {res['seats']} wolnych", flush=True)

    # Ścisłe sortowanie chronologiczne
    courses_san_wro.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_wro_san.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))

    # Zmiany względem poprzedniego pomiaru
    deltas = compute_deltas(courses_san_wro, prev_san_wro) + compute_deltas(courses_wro_san, prev_wro_san)

    # Zapis
    print("\n💾 Zapisywanie baz CSV i generowanie README.md...", flush=True)
    update_database(courses_san_wro, CSV_SANOK_WROCLAW)
    update_database(courses_wro_san, CSV_WROCLAW_SANOK)
    build_readme(courses_san_wro, courses_wro_san, deltas)

    elapsed = time.time() - start_t
    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO W CZASIE: {elapsed:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
