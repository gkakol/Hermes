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

DAYS_AHEAD = 7
PARALLEL_WORKERS = 3
MAX_CAPACITY = 70

CSV_CORRIDOR = "corridor_segments_7d.csv"
REPORT_MD = "CORRIDOR.md"

STOPS = {
    "SANOK": {"id": "17", "name": "SANOK D.A. Lipińskiego"},
    "NIEBYLEC": {"id": "19", "name": "NIEBYLEC"},
    "RZESZOW": {"id": "76", "name": "RZESZÓW D.A. PKS"},
    "KRAKOW_MDA": {"id": "71", "name": "KRAKÓW MDA"},
    "KRAKOW_AIRPORT": {"id": "72", "name": "KRAKÓW Lotnisko Balice"},
    "KATOWICE": {"id": "73", "name": "KATOWICE D.A. ul. Sądowa 5"},
    "GLIWICE": {"id": "123", "name": "GLIWICE Centrum Przesiadkowe ul. Składowa 8a"},
    "WROCLAW": {"id": "77", "name": "WROCŁAW PKS Polbus"}
}

# Macierz godzin odjazdów z poszczególnych węzłów (zgodnie z oficjalnym rozkładem jazdy N3)
# Kierunek: SANOK -> WROCŁAW
TIMETABLE_EAST_WEST = {
    "23:50": {"SANOK": "23:50", "NIEBYLEC": "00:50", "RZESZOW": "01:30", "KRAKOW_MDA": "03:35", "KRAKOW_AIRPORT": "04:00", "KATOWICE": "04:55", "GLIWICE": "05:22"},
    "03:00": {"SANOK": "03:00", "NIEBYLEC": "04:00", "RZESZOW": "04:40", "KRAKOW_MDA": "06:55", "KRAKOW_AIRPORT": "07:20", "KATOWICE": "08:30", "GLIWICE": "09:00"},
    "06:35": {"SANOK": "06:35", "NIEBYLEC": "07:40", "RZESZOW": "08:40", "KRAKOW_MDA": "10:55", "KRAKOW_AIRPORT": "11:30", "KATOWICE": "12:30", "GLIWICE": "13:00"},
    "10:10": {"SANOK": "10:10", "NIEBYLEC": "11:25", "RZESZOW": "12:20", "KRAKOW_MDA": "14:40", "KRAKOW_AIRPORT": "15:15", "KATOWICE": "16:25", "GLIWICE": "16:55"},
    "16:20": {"SANOK": "16:20", "NIEBYLEC": "17:30", "RZESZOW": "18:20", "KRAKOW_MDA": "20:35", "KRAKOW_AIRPORT": "21:00", "KATOWICE": "22:00", "GLIWICE": "22:27"}
}

# Kierunek: WROCŁAW -> SANOK
TIMETABLE_WEST_EAST = {
    "03:40": {"WROCLAW": "03:40", "GLIWICE": "05:40", "KATOWICE": "06:10", "KRAKOW_AIRPORT": "07:00", "KRAKOW_MDA": "07:40", "RZESZOW": "09:55", "NIEBYLEC": "10:34"},
    "07:45": {"WROCLAW": "07:45", "GLIWICE": "09:45", "KATOWICE": "10:15", "KRAKOW_AIRPORT": "11:05", "KRAKOW_MDA": "11:45", "RZESZOW": "13:50", "NIEBYLEC": "14:35"},
    "12:00": {"WROCLAW": "12:00", "GLIWICE": "14:00", "KATOWICE": "14:30", "KRAKOW_AIRPORT": "15:20", "KRAKOW_MDA": "16:15", "RZESZOW": "18:40", "NIEBYLEC": "19:20"},
    "15:35": {"WROCLAW": "15:35", "GLIWICE": "17:40", "KATOWICE": "18:15", "KRAKOW_AIRPORT": "19:05", "KRAKOW_MDA": "19:45", "RZESZOW": "21:55", "NIEBYLEC": "22:30"},
    "22:25": {"WROCLAW": "22:25", "GLIWICE": "00:20", "KATOWICE": "00:50", "KRAKOW_AIRPORT": "01:50", "KRAKOW_MDA": "02:20", "RZESZOW": "04:30", "NIEBYLEC": "05:00"}
}

SEGMENTS_EAST_WEST = [
    ("SANOK", "NIEBYLEC", "Sanok -> Niebylec"),
    ("NIEBYLEC", "RZESZOW", "Niebylec -> Rzeszow"),
    ("RZESZOW", "KRAKOW_MDA", "Rzeszow -> Krakow"),
    ("KRAKOW_MDA", "KRAKOW_AIRPORT", "Krakow -> Balice"),
    ("KRAKOW_AIRPORT", "KATOWICE", "Balice -> Katowice"),
    ("KATOWICE", "GLIWICE", "Katowice -> Gliwice"),
    ("GLIWICE", "WROCLAW", "Gliwice -> Wroclaw"),
]

SEGMENTS_WEST_EAST = [
    ("WROCLAW", "GLIWICE", "Wroclaw -> Gliwice"),
    ("GLIWICE", "KATOWICE", "Gliwice -> Katowice"),
    ("KATOWICE", "KRAKOW_AIRPORT", "Katowice -> Balice"),
    ("KRAKOW_AIRPORT", "KRAKOW_MDA", "Balice -> Krakow"),
    ("KRAKOW_MDA", "RZESZOW", "Krakow -> Rzeszow"),
    ("RZESZOW", "NIEBYLEC", "Rzeszow -> Niebylec"),
    ("NIEBYLEC", "SANOK", "Niebylec -> Sanok"),
]

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


def match_departure(available_deps: list, target_dep: str) -> str:
    """Dopasowuje najbliższą godzinę odjazdu (w tolerancji do 25 min)."""
    if not available_deps or not target_dep:
        return None
    if target_dep in available_deps:
        return target_dep

    t_target = datetime.strptime(target_dep, "%H:%M")
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


def probe_seats_accurate(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, target_dep: str) -> int:
    """Precyzyjny pomiar wolnych foteli dla konkretnego kursu węzłowego."""
    day_courses = query_api(from_id, from_name, to_id, to_name, date_str, passengers=1)
    if not day_courses:
        return 0

    matched = match_departure(list(day_courses.keys()), target_dep)
    if not matched:
        return 0

    # 1. Sprawdzamy czy kurs ma >= 50 miejsc
    res_50 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=50)
    if res_50 and matched in res_50:
        res_70 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=MAX_CAPACITY)
        return MAX_CAPACITY if (res_70 and matched in res_70) else 50

    # 2. Rzetelny binary search w zakresie 1..49
    low, high = 1, 49
    exact = 1

    while low <= high:
        mid = (low + high) // 2
        res = query_api(from_id, from_name, to_id, to_name, date_str, passengers=mid)
        if res is None:
            break
        if matched in res:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def scan_day_corridor(date_str: str) -> list:
    print(f"\n📅 [DZIEŃ {date_str}] Analiza odcinków...", flush=True)
    day_start = time.time()
    results = []

    # 1. Sanok -> Wrocław
    sw_main = query_api(STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], date_str, 1) or {}
    print(f"  ↳ Sanok ➔ Wrocław: {len(sw_main)} kursów {list(sw_main.keys())}", flush=True)

    for dep, data in sw_main.items():
        sched = TIMETABLE_EAST_WEST.get(dep, {})
        seg_results = {}
        min_seats = 999
        bottleneck = ""

        for f_key, t_key, label in SEGMENTS_EAST_WEST:
            expected_node_dep = sched.get(f_key, dep)
            f_info, t_info = STOPS[f_key], STOPS[t_key]
            
            seats = probe_seats_accurate(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, expected_node_dep)
            seg_results[label] = seats
            if seats < min_seats:
                min_seats = seats
                bottleneck = label

        results.append({
            "direction": "Sanok ➔ Wrocław",
            "date": date_str,
            "main_dep": dep,
            "main_hours": data["hours"],
            "segments": seg_results,
            "bottleneck_seg": bottleneck,
            "bottleneck_seats": min_seats
        })
        print(f"    🔎 [{date_str} {dep}] Wąskie gardło: {bottleneck} ({min_seats} wolnych)", flush=True)

    # 2. Wrocław -> Sanok
    ws_main = query_api(STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], date_str, 1) or {}
    print(f"  ↳ Wrocław ➔ Sanok: {len(ws_main)} kursów {list(ws_main.keys())}", flush=True)

    for dep, data in ws_main.items():
        sched = TIMETABLE_WEST_EAST.get(dep, {})
        seg_results = {}
        min_seats = 999
        bottleneck = ""

        for f_key, t_key, label in SEGMENTS_WEST_EAST:
            expected_node_dep = sched.get(f_key, dep)
            f_info, t_info = STOPS[f_key], STOPS[t_key]
            
            seats = probe_seats_accurate(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, expected_node_dep)
            seg_results[label] = seats
            if seats < min_seats:
                min_seats = seats
                bottleneck = label

        results.append({
            "direction": "Wrocław ➔ Sanok",
            "date": date_str,
            "main_dep": dep,
            "main_hours": data["hours"],
            "segments": seg_results,
            "bottleneck_seg": bottleneck,
            "bottleneck_seats": min_seats
        })
        print(f"    🔎 [{date_str} {dep}] Wąskie gardło: {bottleneck} ({min_seats} wolnych)", flush=True)

    print(f"  ⏱️ Zakończono {date_str} w {time.time() - day_start:.1f} s", flush=True)
    return results


def save_corridor_csv(records: list):
    if not records:
        print("[WARN] Brak rekordów do zapisu!", flush=True)
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    base_headers = ["Data pomiaru", "Kierunek", "Data kursu", "Godzina", "Wąskie gardło (odcinek)", "Wolne miejsca (min)"]
    segment_headers = set()

    rows = []
    for r in records:
        row = {
            "Data pomiaru": ts,
            "Kierunek": r["direction"],
            "Data kursu": r["date"],
            "Godzina": r["main_dep"],
            "Wąskie gardło (odcinek)": r["bottleneck_seg"],
            "Wolne miejsca (min)": r["bottleneck_seats"]
        }
        for seg_name, s_val in r["segments"].items():
            row[seg_name] = s_val
            segment_headers.add(seg_name)
        rows.append(row)

    all_fieldnames = base_headers + sorted(list(segment_headers))

    with open(CSV_CORRIDOR, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"💾 Zapisano {len(rows)} wierszy w {CSV_CORRIDOR}", flush=True)


def build_corridor_report(records: list):
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = [
        "# 🛣️ Sentinel N3: Radar Węzłów i Odcinków (Horyzont 7 Dni)\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 🎯 **Zakres:** Kolejne 7 dni\n\n",
        "> Raport identyfikuje **wąskie gardła** na trasie N3. ",
        "Wskazuje odcinek pośredni z najmniejszą liczbą dostępnych foteli, który blokuje możliwość zakupu biletu na całej trasie.\n\n",
        "## 🔍 1. Wykaz Wąskich Gardeł Kursów\n\n",
        "| Data | Kierunek | Odjazd | Wąskie gardło (Odcinek krytyczny) | Dostępne fotele | Status |\n",
        "| :--- | :--- | :---: | :--- | :---: | :---: |\n"
    ]

    for r in sorted(records, key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["main_dep"])):
        b_seats = r["bottleneck_seats"]
        b_seg = r["bottleneck_seg"]

        if b_seats <= 6:
            badge = f"🔴 **{b_seats} szt.**"
            status = "⚠️ Krytyczny korek"
        elif b_seats <= 17:
            badge = f"🟠 **{b_seats} szt.**"
            status = "Końcówka biletów"
        elif b_seats <= 31:
            badge = f"🟡 `{b_seats} szt.`"
            status = "Średnie obłożenie"
        else:
            badge = f"🟢 `{b_seats} szt.`"
            status = "Duża dostępność"

        md.append(f"| 📅 **{r['date']}** | {r['direction']} | ⏰ **{r['main_dep']}** | **{b_seg}** | {badge} | {status} |\n")

    md.extend([
        "\n---\n\n",
        "## 📊 2. Szczegółowe obłożenie segmentów trasy\n\n"
    ])

    for r in records:
        md.append(f"### 🚌 {r['direction']} | 📅 {r['date']} {r['main_hours']}\n")
        md.append(f"> 🚨 Odcinek blokujący: **{r['bottleneck_seg']}** ({r['bottleneck_seats']} wolnych miejsc)\n\n")
        md.append("| Segment trasy | Wolne miejsca |\n| :--- | :---: |\n")
        for seg_name, s_val in r["segments"].items():
            bar_color = "🔴" if s_val <= 6 else ("🟠" if s_val <= 17 else ("🟡" if s_val <= 31 else "🟢"))
            md.append(f"| {seg_name} | {bar_color} **{s_val}** / {MAX_CAPACITY} |\n")
        md.append("\n")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.writelines(md)

    print(f"📄 Wygenerowano raport w {REPORT_MD}", flush=True)


def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("🛣️ SENTINEL CORRIDOR RADAR (7-DAYS ENGINE)", flush=True)
    print("==========================================================", flush=True)

    today = date.today()
    dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(DAYS_AHEAD)]
    print(f"📡 Planowane dni do zbadania ({len(dates)}): {dates}", flush=True)

    all_records = []
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day_corridor, d) for d in dates]
        for fut in as_completed(futures):
            try:
                all_records.extend(fut.result())
            except Exception as e:
                print(f"[ERROR] Błąd w wątku dnia: {e}", flush=True)

    print(f"\n📊 Zebrano dane dla {len(all_records)} kursów. Zapisywanie...", flush=True)
    save_corridor_csv(all_records)
    build_corridor_report(all_records)

    print("==========================================================", flush=True)
    print(f"⏱️ CAŁKOWITY CZAS WYKONANIA: {time.time() - start_t:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
