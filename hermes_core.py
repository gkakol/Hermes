import csv
from datetime import date, datetime, timedelta
import json
import os
import re
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
PARALLEL_WORKERS = 4
TARGET_PROMO_PRICE = 50.00
MAX_CAPACITY = 90

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
        retries = Retry(total=3, backoff_factor=0.4, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=10)
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


def query_api(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1):
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
    for attempt in range(2):
        try:
            r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=7)
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
                        courses[normalize_time(m.group(1))] = {
                            "hours": f"{normalize_time(m.group(1))} -> {normalize_time(m.group(2))}",
                            "price": price
                        }
            return courses
        except Exception:
            time.sleep(0.15)
    return None


def query_day_seats_map(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int):
    data = query_api(from_id, from_name, to_id, to_name, date_str, passengers=passengers)
    if data is None:
        return None
    return set(data.keys())


def resolve_course_adaptive(from_id, from_name, to_id, to_name, date_str, dep, prev_val: int) -> int:
    """Ultra-szybkie badanie bazujące na poprzednim stanie (Adaptive Heuristic)."""
    # 1. Jeśli znamy poprzednią liczbę miejsc (np. 48)
    if prev_val and isinstance(prev_val, int) and 1 < prev_val <= MAX_CAPACITY:
        # Sprawdzamy czy stan się utrzymał
        map_prev = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, prev_val)
        if map_prev is not None and dep in map_prev:
            # Stan nienaruszony lub lekki zwrot (+2)
            if prev_val < MAX_CAPACITY:
                map_plus = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, min(MAX_CAPACITY, prev_val + 2))
                if map_plus is not None and dep in map_plus:
                    return min(MAX_CAPACITY, prev_val + 2)
            return prev_val
        else:
            # Ktoś kupił bilety -> szukamy szybko w dół od prev_val
            low, high = 1, prev_val - 1
    else:
        # Brak historii: szybki check standardowego sufitu (50 / 90)
        map_50 = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, 50)
        if map_50 is not None and dep in map_50:
            map_90 = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, 90)
            if map_90 is not None and dep in map_90:
                return 90
            return 50
        low, high = 1, 49

    # Binary search dla wąskiego zakresu
    exact = 1
    while low <= high:
        mid = (low + high) // 2
        day_map = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, mid)
        if day_map is None:
            return prev_val if (prev_val and isinstance(prev_val, int)) else 1
        if dep in day_map:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def process_day_unified(date_str: str, prev_sw: dict, prev_ws: dict) -> tuple:
    time.sleep(0.02)
    sw_raw = query_api(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, 1) or {}
    ws_raw = query_api(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, 1) or {}

    res_sw = []
    for dep, data in sw_raw.items():
        prev_s = prev_sw.get((date_str, dep), {}).get("seats")
        seats = resolve_course_adaptive(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, dep, prev_s)
        res_sw.append({
            "route": "Sanok ➔ Wrocław", "date": date_str, "departure": dep, "hours": data["hours"],
            "price": data["price"], "seats": seats
        })

    res_ws = []
    for dep, data in ws_raw.items():
        prev_s = prev_ws.get((date_str, dep), {}).get("seats")
        seats = resolve_course_adaptive(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, dep, prev_s)
        res_ws.append({
            "route": "Wrocław ➔ Sanok", "date": date_str, "departure": dep, "hours": data["hours"],
            "price": data["price"], "seats": seats
        })

    return date_str, res_sw, res_ws


def load_previous_snapshot(csv_file: str) -> dict:
    prev = {}
    if not os.path.isfile(csv_file):
        return prev
    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row.get("Data kursu"), row.get("Godzina"))
                seats = row.get("Wolne miejsca", "B/D")
                prev[key] = {
                    "price": float(row.get("Cena (PLN)", 0)),
                    "seats": int(seats) if str(seats).isdigit() else None
                }
    except Exception:
        pass
    return prev


def update_database(courses: list, csv_file: str):
    if not courses:
        return
    file_exists = os.path.isfile(csv_file)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [[ts, c["date"], c["departure"], f"{c['price']:.2f}", str(c.get("seats", "B/D"))] for c in courses]

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["Data pomiaru", "Data kursu", "Godzina", "Cena (PLN)", "Wolne miejsca"])
        w.writerows(rows)


def compute_deltas(current_courses: list, prev_dict: dict, check_ts: str) -> list:
    deltas = []
    parts = check_ts.split(" ")
    date_part, time_part = parts[0], parts[1] if len(parts) > 1 else ""

    for c in current_courses:
        prev = prev_dict.get((c["date"], c["departure"]))
        if not prev:
            continue

        curr_seats = c.get("seats") if isinstance(c.get("seats"), int) else None
        prev_seats = prev.get("seats")
        price_diff = round(c["price"] - prev.get("price", 0.0), 2)
        seats_diff = (curr_seats - prev_seats) if (curr_seats is not None and prev_seats is not None) else 0

        if abs(price_diff) >= 0.01 or seats_diff != 0:
            deltas.append({
                "check_date": date_part, "check_time": time_part,
                "route": c["route"].replace("➔", "→"), "date": c["date"], "hours": c["hours"],
                "curr_price": c["price"], "price_diff": price_diff,
                "prev_seats": prev_seats, "curr_seats": curr_seats, "seats_diff": seats_diff
            })
    return deltas


def render_bar(seats, total: int = MAX_CAPACITY) -> str:
    if not isinstance(seats, int):
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    return f"[{'█' * filled}{'░' * (10 - filled)}] {seats}/{total}"


def build_readme(all_courses: list, deltas: list, now_ts: str):
    today = date.today()

    # Filtrowanie tylko kursów bieżących i przyszłych
    future_courses = []
    for c in all_courses:
        try:
            d_dt = datetime.strptime(c["date"], "%d.%m.%Y").date()
            if d_dt >= today and isinstance(c.get("seats"), int):
                future_courses.append(c)
        except Exception:
            pass

    # Sortowanie: najmniej wolnych miejsc na samej górze (TOP 50 najbardziej obleganych)
    top_50 = sorted(future_courses, key=lambda x: (x["seats"], x["price"]))[:50]

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Horyzont:** 120+ dni\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n",
        "> Poniżej prezentowane są różnice względem poprzedniego sprawdzenia (np. ubytek foteli lub zmiana ceny).\n\n"
    ]

    if deltas:
        md.append("| Data sprawdzenia | Trasa | Kurs | Zmiana ceny | Zmiana miejsc |\n")
        md.append("| :--- | :--- | :--- | :---: | :---: |\n")
        for d in deltas:
            col_ts = f"`{d['check_date']}`<br>`{d['check_time']}`"
            from_to = d['route'].split("→")
            col_route = f"{from_to[0].strip()} →<br>{from_to[1].strip()}" if len(from_to) == 2 else d['route']
            col_course = f"📅 {d['date']} ({d['hours']})"

            if abs(d['price_diff']) >= 0.01:
                p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else f"🔴 `{d['price_diff']:+.2f} zł`"
                col_price = f"**{d['curr_price']:.2f} zł**<br>({p_delta})"
            else:
                col_price = f"{d['curr_price']:.2f} zł"

            p_s = d['prev_seats'] if d['prev_seats'] is not None else "?"
            c_s = d['curr_seats'] if d['curr_seats'] is not None else "?"
            diff_s = f"({d['seats_diff']:+d})" if d['seats_diff'] != 0 else "(0)"
            col_seats = f"{p_s} → **{c_s} szt.**<br>`{diff_s}`"

            md.append(f"| {col_ts} | {col_route} | {col_course} | {col_price} | {col_seats} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i dostępności miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🔥 2. TOP 50 Najbardziej Obleganych Kursów (Końcówki biletów)\n\n",
        "> Zestawienie aktualnych kursów o najmniejszej liczbie wolnych miejsc siedzących.\n\n",
        "| Poz. | Trasa | Data | Godzina | Wolne miejsca | Cena | Status |\n",
        "| :---: | :--- | :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for rank, c in enumerate(top_50, 1):
        bar = render_bar(c["seats"])
        r_tag = c["route"].replace("➔", "→")
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        alert_seats = f"🔴 **{c['seats']} szt.**" if c['seats'] <= 10 else f"**{c['seats']} szt.**"
        md.append(f"| {rank:02d} | {r_tag} | 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` ({alert_seats}) | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 3. Mapy Obłożenia (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        '<p align="center"><img src="heatmapa_sanok_wroclaw.png" alt="Heatmapa Sanok -> Wrocław" width="520"></p>\n\n',
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        '<p align="center"><img src="heatmapa_wroclaw_sanok.png" alt="Heatmapa Wrocław -> Sanok" width="520"></p>\n\n'
    ])

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)


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
        msg = f"📢 **OTWARTO NOWĄ PULĘ BILETÓW!**\n\n📅 Sprzedaż wydłużona do: **{furthest}** (wcześniej: {prev})\n🔗 https://neobus.pl/"
        if DISCORD_WEBHOOK_URL:
            try:
                requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": msg}, timeout=8)
            except Exception:
                pass
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


def main():
    start_t = time.time()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("==========================================================", flush=True)
    print("🚀 SENTINEL N3: ADAPTIVE PROBE ENGINE (SANOK ⇄ WROCŁAW)", flush=True)
    print("==========================================================", flush=True)

    prev_sw = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_ws = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = [(date.today() + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(DAYS_FORWARD_SEARCH)]
    total_days = len(dates)
    courses_sw, courses_ws = [], []

    print(f"\n📡 Skanowanie i adaptacyjny pomiar foteli ({total_days} dni)...", flush=True)
    done_days = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(process_day_unified, d, prev_sw, prev_ws) for d in dates]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            courses_sw.extend(res_sw)
            courses_ws.extend(res_ws)
            done_days += 1
            if done_days % 15 == 0 or done_days == total_days:
                print(f"  [⚡ {done_days:03d}/{total_days} | {(done_days/total_days)*100:5.1f}%] Przetworzono {day_str}...", flush=True)

    courses_sw.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_ws.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))

    all_active = courses_sw + courses_ws
    deltas = compute_deltas(courses_sw, prev_sw, now_ts) + compute_deltas(courses_ws, prev_ws, now_ts)

    update_database(courses_sw, CSV_SANOK_WROCLAW)
    update_database(courses_ws, CSV_WROCLAW_SANOK)
    build_readme(all_active, deltas, now_ts)
    check_and_notify_horizon(list({c["date"] for c in all_active}))

    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO POMYŚLNIE W CZASIE: {time.time() - start_t:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
