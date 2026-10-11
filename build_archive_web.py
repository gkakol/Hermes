import csv
import glob
import json
import os
from datetime import datetime, date

DOCS_DIR = "docs"
ARCHIVE_DIR = "archive"
OUTPUT_JSON = os.path.join(DOCS_DIR, "data.json")

DNI_TYGODNIA = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]


def parse_date_safe(d_str):
    try:
        return datetime.strptime(d_str.strip(), "%d.%m.%Y").date()
    except Exception:
        return None


def parse_datetime_safe(dt_str):
    try:
        return datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def process_all_data():
    os.makedirs(DOCS_DIR, exist_ok=True)
    today = date.today()

    # Zbieramy pliki archiwalne oraz bieżące
    all_files = glob.glob(os.path.join(ARCHIVE_DIR, "arch_*.csv")) + [
        "ceny_sanok_wroclaw.csv",
        "ceny_wroclaw_sanok.csv"
    ]

    courses_map = {}

    for path in all_files:
        if not os.path.isfile(path):
            continue
        route_name = "Sanok ➔ Wrocław" if "sanok_wroclaw" in path else "Wrocław ➔ Sanok"
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d_kurs = row.get("Data kursu")
                    godz = row.get("Godzina")
                    if not d_kurs or not godz:
                        continue

                    key = (route_name, d_kurs, godz)
                    meas_dt = parse_datetime_safe(row.get("Data pomiaru", ""))
                    price = float(row.get("Cena (PLN)", 0.0) or 0.0)
                    seats_raw = row.get("Wolne miejsca", "")
                    seats = int(seats_raw) if str(seats_raw).isdigit() else None

                    if key not in courses_map:
                        courses_map[key] = {
                            "last_meas_dt": meas_dt,
                            "current_price": price,
                            "min_price": price if price > 0 else 999.0,
                            "max_price": price,
                            "current_seats": seats,
                            "samples": 1
                        }
                    else:
                        c = courses_map[key]
                        c["samples"] += 1
                        if price > 0 and price < c["min_price"]:
                            c["min_price"] = price
                        if price > c["max_price"]:
                            c["max_price"] = price
                        # Najświeższy pomiar
                        if meas_dt and (c["last_meas_dt"] is None or meas_dt > c["last_meas_dt"]):
                            c["last_meas_dt"] = meas_dt
                            c["current_price"] = price
                            c["current_seats"] = seats
        except Exception as e:
            print(f"[!] Błąd w {path}: {e}")

    records = []
    weekday_stats = {d: {"total": 0, "sold_out": 0} for d in DNI_TYGODNIA}

    for (route, d_kurs, godz), data in courses_map.items():
        dt_obj = parse_date_safe(d_kurs)
        if not dt_obj:
            continue

        day_name = DNI_TYGODNIA[dt_obj.weekday()]
        seats = data["current_seats"]
        is_past = dt_obj < today
        is_sold_out = (seats is not None and seats <= 3)

        if is_past:
            weekday_stats[day_name]["total"] += 1
            if is_sold_out:
                weekday_stats[day_name]["sold_out"] += 1

        min_p = data["min_price"] if data["min_price"] < 900 else data["current_price"]

        records.append({
            "route": route,
            "date": d_kurs,
            "day": day_name,
            "hour": godz,
            "price": data["current_price"],
            "min_price": min_p,
            "max_price": data["max_price"],
            "seats": seats if seats is not None else -1,
            "is_past": is_past,
            "sold_out": is_sold_out
        })

    # Sortowanie: najpierw po dacie rosnąco/malejąco
    records.sort(
        key=lambda r: (datetime.strptime(r["date"], "%d.%m.%Y"), r["hour"]),
        reverse=True
    )

    total_courses = len(records)
    past_courses = sum(1 for r in records if r["is_past"])
    future_courses = total_courses - past_courses
    sold_out_cnt = sum(1 for r in records if r["is_past"] and r["sold_out"])

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_courses": total_courses,
        "past_courses": past_courses,
        "future_courses": future_courses,
        "sold_out_cnt": sold_out_cnt,
        "analytics": {
            "days": DNI_TYGODNIA,
            "sold_out_rates": [
                round((weekday_stats[d]["sold_out"] / weekday_stats[d]["total"] * 100), 1)
                if weekday_stats[d]["total"] > 0 else 0
                for d in DNI_TYGODNIA
            ]
        },
        "records": records
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Zapisano {OUTPUT_JSON} (Łącznie kursów: {total_courses}, w tym {future_courses} aktywnych).")


if __name__ == "__main__":
    process_all_data()
