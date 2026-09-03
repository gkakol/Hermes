import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"

MAX_CAPACITY = 70
DNI_TYG = {0: "Pn", 1: "Wt", 2: "Śr", 3: "Cz", 4: "Pt", 5: "Sb", 6: "Nd"}


def get_smooth_neobus_cmap():
    """Tworzy płynny, ciągły gradient z węzłami odpowiadającymi zadanym progom."""
    # Punkty odcięcia znormalizowane od 0.0 do 1.0 (dzielone przez MAX_CAPACITY = 70):
    # 0 -> Czarny
    # ~3.5 (5%) -> Czerwony
    # ~12 (17%) -> Pomarańczowy
    # ~24 (35%) -> Żółty
    # ~42 (60%) -> Zielony
    # ~60 (85%) -> Niebieski
    # 70 (100%) -> Głęboki błękit
    stops = [
        (0.00, "#111111"),  # 0 miejsc (czarny)
        (0.05, "#d92b2b"),  # 1-10% (czerwony)
        (0.18, "#f28500"),  # 10-25% (pomarańczowy)
        (0.35, "#f5c518"),  # 25-45% (żółty)
        (0.60, "#2ea44f"),  # 45-75% (zielony)
        (0.85, "#1e70bf"),  # 75-100% (błękit)
        (1.00, "#0b4d8c")   # Pełne 70 (głęboki niebieski)
    ]
    cmap = mcolors.LinearSegmentedColormap.from_list("smooth_neobus", stops, N=512)
    cmap.set_bad(color="white")
    return cmap


def generate_vertical_heatmap(csv_path: str, output_img: str, title: str):
    if not os.path.isfile(csv_path):
        return

    try:
        df = pd.read_csv(csv_path)
        if df.empty or "Wolne miejsca" not in df.columns:
            return

        df["Wolne miejsca"] = pd.to_numeric(df["Wolne miejsca"], errors="coerce")
        df = df.dropna(subset=["Wolne miejsca"])
        df["Wolne miejsca"] = df["Wolne miejsca"].astype(int)

        df = df.drop_duplicates(subset=["Data kursu", "Godzina"], keep="last")

        df["dt"] = pd.to_datetime(df["Data kursu"], format="%d.%m.%Y", errors="coerce")
        df = df.dropna(subset=["dt"]).sort_values(by=["dt", "Godzina"])

        df["label_data"] = df["dt"].dt.strftime("%d.%m") + " (" + df["dt"].dt.weekday.map(DNI_TYG) + ")"
        df["kurs_nr"] = df.groupby("Data kursu").cumcount() + 1

        pivot = df.pivot(index="label_data", columns="kurs_nr", values="Wolne miejsca")
        
        unique_order = df[["label_data", "dt"]].drop_duplicates().sort_values("dt")["label_data"].tolist()
        pivot = pivot.reindex(unique_order)

        n_rows = len(pivot)
        fig_h = max(8, n_rows * 0.35)
        fig, ax = plt.subplots(figsize=(6.5, fig_h), dpi=180)

        cmap = get_smooth_neobus_cmap()
        mat = np.ma.masked_invalid(pivot.values)

        # Ciągła skala vmin=0 do vmax=70
        im = ax.imshow(mat, cmap=cmap, vmin=0, vmax=MAX_CAPACITY, aspect="auto")

        for r in range(pivot.shape[0]):
            for c in range(pivot.shape[1]):
                val = pivot.iloc[r, c]
                if not np.isnan(val):
                    val_int = int(val)
                    # Kontrast tekstu: biały na bardzo ciemnym (czerwień/czerń) oraz bardzo jasnym/ciemnoniebieskim
                    txt_color = "white" if (val_int <= 6 or val_int >= 54) else "#111111"
                    ax.text(c, r, str(val_int), ha="center", va="center", fontsize=8.5, fontweight="bold", color=txt_color)

        ax.set_yticks(np.arange(len(pivot.index)))
        ax.set_yticklabels(pivot.index, fontsize=8)
        ax.set_xticks([])
        
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(left=False, bottom=False)

        cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.08, shrink=0.4)
        cbar.set_ticks([0, 5, 15, 28, 45, 65])
        cbar.set_ticklabels(['0 (brak)', '1-10%', '10-25%', '25-45%', '45-75%', '75-100%'])
        cbar.ax.tick_params(labelsize=8)
        cbar.outline.set_visible(False)

        clean_title = title.replace("➔", "->")
        plt.title(f"{clean_title}\n(Pojemnosc: do {MAX_CAPACITY} miejsc)", fontsize=11, pad=18, fontweight="bold", loc="center")

        plt.tight_layout()
        plt.savefig(output_img, dpi=180, bbox_inches="tight")
        plt.close()
        print(f"Wygenerowano płynną heatmapę: {output_img}")
    except Exception as e:
        print(f"[!] Błąd generowania heatmapy: {e}")


def main():
    print(f"=== GENEROWANIE PŁYNNYCH HEATMAP GRADIENTOWYCH (0..{MAX_CAPACITY}) ===")
    generate_vertical_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Wolne miejsca: Sanok -> Wroclaw")
    generate_vertical_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Wolne miejsca: Wroclaw -> Sanok")


if __name__ == "__main__":
    main()
