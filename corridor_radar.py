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
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
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
        except Exception as e:
            time.sleep(0.15)
    return None


def query_segment_day_map(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int):
    data = query_api(from_id, from_name, to_id, to_name, date_str, passengers=passengers)
    if data is None:
        return None
    return set(data.keys())


def probe_seats_fast(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, dep: str) -> int:
    """Szybkie badanie miejsc na segmencie: najpierw próg 50, potem 70, a w dół binary search."""
    r50 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=50)
    if r50 and (dep in r50 or len(r50) > 0):
        r70 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=MAX_CAPACITY)
        return MAX_CAPACITY if (r70 and (dep in r70 or len(r70) > 0)) else 50

    low, high = 1, 49
    exact = 1
    while low <= high:
        mid = (low + high) // 2
        res = query_api(from_id, from_name, to_id, to_name, date_str, passengers=mid)
        if res is None:
            break
        if dep in res or len(res) > 0:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def scan_day_corridor(date_str: str) -> list:
    print(f"\n📅 [DZIEŃ {date_str}] Rozpoczęcie analizy korytarza...", flush=True)
    day_start = time.time()
    results = []

    # 1. Kursy Sanok -> Wrocław
    sw_main = query_api(STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], date_str, 1) or {}
    print(f"  ↳ Sanok ➔ Wrocław: wykryto {len(sw_main)} kursów {list(sw_main.keys())}", flush=True)

    for dep, data in sw_main.items():
        seg_results = {}
        min_seats = 999
        bottleneck = ""

        for f_key, t_key, label in SEGMENTS_EAST_WEST:
            f_info, t_info = STOPS[f_key], STOPS[t_key]
            seats = probe_seats_fast(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, dep)
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
        print(f"    🔎 [{date_str} {dep}] Wąskie gardło: {bottleneck} (wolne: {min_seats} szt.)", flush=True)

    # 2. Kursy Wrocław -> Sanok
    ws_main = query_api(STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], date_str, 1) or {}
    print(f"  ↳ Wrocław ➔ Sanok: wykryto {len(ws_main)} kursów {list(ws_main.keys())}", flush=True)

    for dep, data in ws_main.items():
        seg_results = {}
        min_seats = 999
        bottleneck = ""

        for f_key, t_key, label in SEGMENTS_WEST_EAST:
            f_info, t_info = STOPS[f_key], STOPS[t_key]
            seats = probe_seats_fast(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, dep)
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
        print(f"    🔎 [{date_str} {dep}] Wąskie gardło: {bottleneck} (wolne: {min_seats} szt.)", flush=True)

    print(f"  ⏱️ Zakończono {date_str} w {time.time() - day_start:.1f} s", flush=True)
    return results


def save_corridor_csv(records: list):
    """Zapis do CSV z pełną, bezpieczną unią wszystkich nagłówków z obu kierunków."""
    if not records:
        print("[WARN] Brak rekordów do zapisania w CSV!", flush=True)
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # Zbuduj pełną listę kolumn ze wszystkich wierszy (unika błędu KeyError / ValueError)
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

    file_exists = os.path.isfile(CSV_CORRIDOR)
    with open(CSV_CORRIDOR, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"💾 Pomyślnie zapisano {len(rows)} wierszy w {CSV_CORRIDOR} z kolumnami: {all_fieldnames}", flush=True)


def build_corridor_report(records: list):
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = [
        "# 🛣️ Sentinel N3: Radar Węzłów i Odcinków (Horyzont 7 Dni)\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 🎯 **Zakres:** Kolejne 7 dni\n\n",
        "> Raport identyfikuje **wąskie gardła** na trasie N3. ",
        "Oznacza to, że kurs może być zablokowany na pełnej trasie (Sanok ⇄ Wrocław) z powodu wyprzedania jednego krytycznego odcinka pośredniego.\n\n",
        "## 🔍 1. Wykaz Wąskich Gardeł Kursów\n\n",
        "| Data | Kierunek | Odjazd | Wąskie gardło (Odcinek krytyczny) | Wolne fotele | Status |\n",
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
        "## 📊 2. Szczegółowe obłożenie poszczególnych segmentów\n\n"
    ])

    for r in records:
        md.append(f"### 🚌 {r['direction']} | 📅 {r['date']} {r['main_hours']}\n")
        md.append(f"> 🚨 Odcinek krytyczny: **{r['bottleneck_seg']}** ({r['bottleneck_seats']} wolnych miejsc)\n\n")
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
    print("🛣️ SENTINEL CORRIDOR RADAR (7-DAYS SEGMENT ENGINE)", flush=True)
    print("==========================================================", flush=True)

    today = date.today()
    dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(DAYS_AHEAD)]
    print(f"📡 Planowane dni do zbadania ({len(dates)}): {dates}", flush=True)

    all_records = []
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day_corridor, d) for d in dates]
        for fut in as_completed(futures):
            try:
                day_res = fut.result()
                all_records.extend(day_res)
            except Exception as e:
                print(f"[ERROR] Błąd przetwarzania wątku dnia: {e}", flush=True)

    print(f"\n📊 Łącznie zebrano dane dla {len(all_records)} kursów. Rozpoczynanie zapisu...", flush=True)
    save_corridor_csv(all_records)
    build_corridor_report(all_records)

    print("==========================================================", flush=True)
    print(f"⏱️ CAŁKOWITY CZAS WYKONANIA: {time.time() - start_t:.2f} s", flush=True)
    print(f"📁 Wyniki: {CSV_CORRIDOR} | Raport: {REPORT_MD}", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
