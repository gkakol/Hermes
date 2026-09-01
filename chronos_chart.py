import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

ARCHIVE_FILE = "oracle_pulse.csv"
OUT_WEST = "chronos_westbound.png"
OUT_EAST = "chronos_eastbound.png"


def build_corridor_heatmap(df: pd.DataFrame, direction_name: str, out_img: str):
    subset = df[df["Direction"] == direction_name].copy()
    if subset.empty:
        return

    subset = subset[subset["Onboard Passengers"].astype(str).str.isdigit()]
    if subset.empty:
        return

    subset["Onboard Passengers"] = subset["Onboard Passengers"].astype(int)
    subset["Time_Label"] = subset["Date"] + " " + subset["Origin Departure"]

    pivot = subset.pivot_table(
        index="Segment",
        columns="Time_Label",
        values="Onboard Passengers",
        aggfunc="last"
    )

    if pivot.empty:
        return

    plt.figure(figsize=(16, 7))
    sns.heatmap(pivot, cmap="YlGnBu", annot=True, fmt="d", cbar_kws={'label': 'Pasażerowie na pokładzie'})
    plt.title(f"Obłożenie Odcinków Magistrali N3: {direction_name}", fontsize=14, pad=15)
    plt.xlabel("Kurs (Data i godzina startu)", fontsize=11)
    plt.ylabel("Odcinek pomiarowy", fontsize=11)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(out_img, dpi=200)
    plt.close()
    print(f"📊 Wygenerowano mapę korytarza: {out_img}")


def main():
    print("=== CHRONOS HEATMAP GENERATOR ===")
    if not os.path.isfile(ARCHIVE_FILE):
        print("[i] Brak archiwum oracle_pulse.csv.")
        return

    try:
        df = pd.read_csv(ARCHIVE_FILE)
        if df.empty or "Direction" not in df.columns:
            return

        build_corridor_heatmap(df, "Sanok ➔ Wrocław", OUT_WEST)
        build_corridor_heatmap(df, "Wrocław ➔ Sanok", OUT_EAST)
    except Exception as e:
        print(f"[!] Błąd generatora wykresów: {e}")


if __name__ == "__main__":
    main()
