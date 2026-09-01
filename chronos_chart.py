import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

ARCHIVE_FILE = "oracle_pulse.csv"
OUT_WEST = "chronos_westbound.png"
OUT_EAST = "chronos_eastbound.png"


def create_fallback_chart(out_img: str, title: str):
    """Tworzy pusty wykres informacyjny, jeśli brakuje jeszcze danych."""
    plt.figure(figsize=(10, 4))
    plt.text(0.5, 0.5, "Zbieranie danych telemetrycznych...", horizontalalignment='center', verticalalignment='center', fontsize=12, color='gray')
    plt.title(title, fontsize=12, pad=10)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(out_img, dpi=150)
    plt.close()
    print(f"📊 Utworzono placeholder: {out_img}")


def build_corridor_heatmap(df: pd.DataFrame, direction_name: str, out_img: str):
    if df.empty:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
        return

    subset = df[df["Direction"] == direction_name].copy()
    if subset.empty:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
        return

    # Zamiana ewentualnych B/D na 0 do celów wizualizacji
    subset["Onboard Passengers"] = pd.to_numeric(subset["Onboard Passengers"], errors="coerce").fillna(0).astype(int)
    subset["Time_Label"] = subset["Date"] + " " + subset["Origin Departure"]

    pivot = subset.pivot_table(
        index="Segment",
        columns="Time_Label",
        values="Onboard Passengers",
        aggfunc="last"
    ).fillna(0)

    if pivot.empty or pivot.shape[1] == 0:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
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
        create_fallback_chart(OUT_WEST, "Magistrala N3: Sanok ➔ Wrocław")
        create_fallback_chart(OUT_EAST, "Magistrala N3: Wrocław ➔ Sanok")
        return

    try:
        df = pd.read_csv(ARCHIVE_FILE)
        build_corridor_heatmap(df, "Sanok ➔ Wrocław", OUT_WEST)
        build_corridor_heatmap(df, "Wrocław ➔ Sanok", OUT_EAST)
    except Exception as e:
        print(f"[!] Błąd generatora wykresów: {e}")
        create_fallback_chart(OUT_WEST, "Magistrala N3: Sanok ➔ Wrocław")
        create_fallback_chart(OUT_EAST, "Magistrala N3: Wrocław ➔ Sanok")


if __name__ == "__main__":
    main()
