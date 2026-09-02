import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"

DNI_TYG = {0: "Pn", 1: "Wt", 2: "Śr", 3: "Cz", 4: "Pt", 5: "Sb", 6: "Nd"}


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

        # Skala kolorów od 1 do 90:
        # Ciemny bordowy -> Czerwony -> Pomarańczowy -> Żółty -> Kość słoniowa (90)
        colors = ["#7a001e", "#c41230", "#f45d22", "#fca338", "#ffdc73", "#fffae0"]
        cmap = mcolors.LinearSegmentedColormap.from_list("neobus_theme", colors, N=256)
        cmap.set_bad(color="white")

        mat = np.ma.masked_invalid(pivot.values)
        im = ax.imshow(mat, cmap=cmap, vmin=1, vmax=90, aspect="auto")

        for r in range(pivot.shape[0]):
            for c in range(pivot.shape[1]):
                val = pivot.iloc[r, c]
                if not np.isnan(val):
                    val_int = int(val)
                    txt_color = "white" if val_int <= 20 else "#222222"
                    ax.text(c, r, str(val_int), ha="center", va="center", fontsize=9, fontweight="medium", color=txt_color)

        ax.set_yticks(np.arange(len(pivot.index)))
        ax.set_yticklabels(pivot.index, fontsize=8.5)
        ax.set_xticks([])
        
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(left=False, bottom=False)

        cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.08, shrink=0.4)
        cbar.ax.tick_params(labelsize=8)
        cbar.outline.set_visible(False)

        clean_title = title.replace("➔", "->")
        plt.title(f"{clean_title}\n(Pojemnosc: do 90 miejsc)", fontsize=11, pad=18, fontweight="bold", loc="center")

        plt.tight_layout()
        plt.savefig(output_img, dpi=180, bbox_inches="tight")
        plt.close()
        print(f"Wygenerowano heatmapę 90-miejsc: {output_img}")
    except Exception as e:
        print(f"[!] Błąd generowania heatmapy: {e}")


def main():
    print("=== GENEROWANIE PIONOWYCH HEATMAP (1..90) ===")
    generate_vertical_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Wolne miejsca: Sanok -> Wroclaw")
    generate_vertical_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Wolne miejsca: Wroclaw -> Sanok")


if __name__ == "__main__":
    main()
