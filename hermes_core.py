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

DAYS_FORWARD_SEARCH = 120
PARALLEL_WORKERS = 5
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
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://neobus.pl",
    "Referer": "https://neobus.pl/",
}

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=10))
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
    try:
        r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=7)
        if r.status_code != 200:
            return None

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
                    courses[normalize_time(m.group(1))] = {"hours": f"{normalize_time(m.group(1))} -> {normalize_time(m.group(2))}", "price": price}
        return courses
    except Exception:
        return None


def scan_day(date_str: str) -> tuple:
    time.sleep(0.04)
    sw = query_api(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, 1) or {}
    ws = query_api(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, 1) or {}

    courses_sw = [
        {"route": "Sanok ➔ Wrocław", "date": date_str, "departure": dep, "hours": data["hours"], "price": data["price"], "from_id": STOPS["sanok"]["id"], "from_name": STOPS["sanok"]["name"], "to_id": STOPS["wroclaw"]["id"], "to_name": STOPS["wroclaw"]["name"]}
        for dep, data in sw.items()
    ]
    courses_ws = [
        {"route": "Wrocław ➔ Sanok", "date": date_str, "departure": dep, "hours": data["hours"], "price": data["price"], "from_id": STOPS["wroclaw"]["id"], "from_name": STOPS["wroclaw"]["name"], "to_id": STOPS["sanok"]["id"], "to_name": STOPS["sanok"]["name"]}
        for dep, data in ws.items()
    ]
    return date_str, courses_sw, courses_ws


def resolve_seats(c: dict) -> dict:
    time.sleep(0.03)
    f_id, f_name, t_id, t_name = c["from_id"], c["from_name"], c["to_id"], c["to_name"]
    d, dep = c["date"], c["departure"]

    # 1. Sprawdzamy czy kurs jest pełny (50 miejsc)
    r50 = query_api(f_id, f_name, t_id, t_name, d, passengers=50)
    if r50 is None:
        c["seats"] = "B/D"
        return c

    if dep in r50:
        r65 = query_api(f_id, f_name, t_id, t_name, d, passengers=65)
        c["seats"] = 65 if (r65 and dep in r65) else 50
        return c

    # 2. Jeśli mniej niż 50, badamy próg 25
    r25 = query_api(f_id, f_name, t_id, t_name, d, passengers=25)
    if r25 and dep in r25:
        c["seats"] = 25
    else:
        r10 = query_api(f_id, f_name, t_id, t_name, d, passengers=10)
        c["seats"] = 10 if (r10 and dep in r10) else 1

    return c


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


def compute_deltas(current_courses: list, prev_dict: dict) -> list:
    deltas = []
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
                "route": c["route"],
                "date": c["date"],
                "time": c["departure"],
                "curr_price": c["price"],
                "price_diff": price_diff,
                "curr_seats": curr_seats,
                "seats_diff": seats_diff
            })
    return deltas


def render_bar(seats, total: int = 65) -> str:
    if not isinstance(seats, int):
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    return f"[{'█' * filled}{'░' * (10 - filled)}] {seats}/{total}"


def build_readme(courses_san_wro: list, courses_wro_san: list, deltas: list):
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Horyzont:** 120+ dni\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n"
    ]

    if deltas:
        md.append("| Trasa | Data | Godzina | Aktualna Cena | Zmiana Ceny (Δ) | Wolne Miejsca | Zmiana Miejsc (Δ) |\n")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for d in deltas:
            p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else (f"🔴 `{d['price_diff']:+.2f} zł`" if d['price_diff'] > 0 else "0.00 zł")
            s_delta = f"📉 `{d['seats_diff']:+d}`" if d['seats_diff'] < 0 else (f"📈 `+{d['seats_diff']}`" if d['seats_diff'] > 0 else "0")
            s_str = f"{d['curr_seats']}" if d['curr_seats'] is not None else "B/D"
            md.append(f"| {d['route']} | 📅 **{d['date']}** | ⏰ **{d['time']}** | **{d['curr_price']:.2f} zł** | {p_delta} | `{s_str}` | {s_delta} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i dostępności miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 2. Mapy Obłożenia (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        "![Heatmapa Sanok -> Wrocław](heatmapa_sanok_wroclaw.png)\n\n",
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        "![Heatmapa Wrocław -> Sanok](heatmapa_wroclaw_sanok.png)\n\n",
        "---\n\n",
        "## 📋 3. Pełny Wykaz Kursów\n\n",
        "### 📍 Sanok ➔ Wrocław\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_san_wro:
        bar = render_bar(c.get("seats"))
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 📍 Wrocław ➔ Sanok\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_wro_san:
        bar = render_bar(c.get("seats"))
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

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
    print("==========================================================", flush=True)
    print("🚀 SENTINEL N3: LIGHTWEIGHT SCANNER (SANOK ⇄ WROCŁAW)", flush=True)
    print("==========================================================", flush=True)

    prev_sw = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_ws = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = [(date.today() + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(DAYS_FORWARD_SEARCH)]
    total_days = len(dates)
    all_raw = []

    print(f"\n📡 [1/2] Skanowanie kalendarza połączeń ({total_days} dni)...", flush=True)
    done_scan = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day, d) for d in dates]
        for fut in as_completed(futures):
            _, res_sw, res_ws = fut.result()
            all_raw.extend(res_sw + res_ws)
            done_scan += 1
            if done_scan % 30 == 0 or done_scan == total_days:
                print(f"  [📅 {done_scan:03d}/{total_days} | {(done_scan/total_days)*100:5.1f}%] Skanowanie...", flush=True)

    total_courses = len(all_raw)
    print(f"\n✅ Znaleziono {total_courses} aktywnych kursów.", flush=True)
    check_and_notify_horizon(list({c["date"] for c in all_raw}))

    print(f"\n🔬 [2/2] Weryfikacja wolnych foteli ({total_courses} zadań)...", flush=True)
    done_eval = 0
    courses_sw, courses_ws = [], []

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(resolve_seats, c) for c in all_raw]
        for fut in as_completed(futures):
            res = fut.result()
            (courses_sw if res["route"] == "Sanok ➔ Wrocław" else courses_ws).append(res)
            done_eval += 1
            if done_eval % 40 == 0 or done_eval == total_courses:
                print(f"  [💺 {done_eval:03d}/{total_courses} | {(done_eval/total_courses)*100:5.1f}%] {res['date']} {res['departure']} ➔ {res.get('seats')} wolnych", flush=True)

    courses_sw.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_ws.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))

    deltas = compute_deltas(courses_sw, prev_sw) + compute_deltas(courses_ws, prev_ws)

    update_database(courses_sw, CSV_SANOK_WROCLAW)
    update_database(courses_ws, CSV_WROCLAW_SANOK)
    build_readme(courses_sw, courses_ws, deltas)

    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO W CZASIE: {time.time() - start_t:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
