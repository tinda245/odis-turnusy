import csv
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# --- NASTAVENÍ ---
DOBA_BEHU_HODIN = 5.8
INTERVAL_SEKUND = 45
VYSTUPNI_SOUBOR = "turnusy_hotovo.csv"
DIAGNOSTIKA_SOUBOR = "ukazka_dat.json"
DB_SOUBOR = "odis_data.db"
MAPA_URL = "https://mpvnet.cz/odis/map"


def inicializuj_db():
    conn = sqlite3.connect(DB_SOUBOR)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS odjete_spoje (
            datum TEXT,
            vozidlo TEXT,
            linka TEXT,
            spoj TEXT,
            zacatek TEXT,
            konec TEXT,
            PRIMARY KEY (datum, vozidlo, linka, spoj)
        )
    """)
    conn.commit()
    return conn


def vyexportuj_turnusy_do_csv(conn):
    c = conn.cursor()
    c.execute("""
        SELECT datum, vozidlo, linka, spoj, zacatek, konec
        FROM odjete_spoje
        ORDER BY datum, vozidlo, zacatek
    """)
    radky = c.fetchall()

    turnusy = {}
    for datum, vozidlo, linka, spoj, zacatek, konec in radky:
        klic = (datum, vozidlo)
        if klic not in turnusy:
            turnusy[klic] = []
        turnusy[klic].append(f"{zacatek[:5]}-{konec[:5]} (L{linka}/{spoj})")

    with open(VYSTUPNI_SOUBOR, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Datum", "ID Vozidla / Kurzu", "Počet spojů", "Celý denní turnus (Od-Do Linka/Spoj)"])
        for (datum, vozidlo), spoje in turnusy.items():
            writer.writerow([datum, vozidlo, len(spoje), " | ".join(spoje)])


def uloz_na_github(zprava):
    try:
        subprocess.run(["git", "config", "user.name", "ODIS-Automat"], check=False)
        subprocess.run(["git", "config", "user.email", "automat@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", VYSTUPNI_SOUBOR, DB_SOUBOR, DIAGNOSTIKA_SOUBOR], check=False)
        subprocess.run(["git", "commit", "-m", zprava], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as e:
        print(f"Chyba při ukládání na GitHub: {e}")


def extrahuj_vozy_z_objektu(obj):
    """Rekurzivně prohledá jakoukoliv JSON strukturu a vytáhne objekty, které vypadají jako autobus."""
    nalezeno = []

    def hledej(o):
        if isinstance(o, dict):
            # Pokud objekt obsahuje linku / spoj nebo pozici vozidla
            ma_linku = any(k in o for k in ("line", "linka", "l", "route", "ln", "lineName"))
            ma_spoj = any(k in o for k in ("conn", "spoj", "cn", "trip", "c", "course"))
            ma_vuz = any(k in o for k in ("id", "reg", "rz", "vId", "evc", "car", "label", "num"))

            if ma_linku and (ma_spoj or ma_vuz):
                nalezeno.append(o)
            else:
                for v in o.values():
                    hledej(v)
        elif isinstance(o, list):
            for polozka in o:
                hledej(polozka)

    hledej(obj)
    return nalezeno


def spust_automat():
    conn = inicializuj_db()
    c = conn.cursor()
    pocet_cyklu = int((DOBA_BEHU_HODIN * 3600) / INTERVAL_SEKUND)
    ulozit_kazdych_cyklu = 20  # Každých 15 minut

    zachycene_odpovedi = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="cs-CZ",
            timezone_id="Europe/Prague"
        )
        page = context.new_page()

        def zachyt_odpoved(response):
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type or "javascript" in content_type:
                    text = response.text()
                    if text and (text.strip().startswith("{") or text.strip().startswith("[")):
                        d = json.loads(text)
                        zachycene_odpovedi.append({"url": response.url, "data": d})
            except Exception:
                pass

        page.on("response", zachyt_odpoved)

        print("Načítám mapu ODIS...")
        try:
            page.goto(MAPA_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)

            # Odklikneme případné cookies nebo potvrzovací okno
            for selector in ["button:has-text('Souhlasím')", "button:has-text('Rozumím')", ".cookie-accept"]:
                try:
                    if page.is_visible(selector, timeout=2000):
                        page.click(selector)
                        print("Potvrzen dialog.")
                except Exception:
                    pass

            # Simulujeme pohyb v mapě (zoom dovnitř/ven), aby se vyžádalo načtení vozů
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(3000)

        except Exception as e:
            print(f"Upozornění při startu stránky: {e}")

        for i in range(pocet_cyklu):
            nyni = datetime.now(ZoneInfo("Europe/Prague"))
            datum_str = nyni.strftime("%Y-%m-%d")
            cas_str = nyni.strftime("%H:%M:%S")

            try:
                # Drobný pohyb myší na mapě každé kolo, aby server věděl, že uživatel žije
                page.mouse.move(500 + (i % 50), 400 + (i % 50))
                page.wait_for_timeout(2000)

                zaznamenano = 0
                while zachycene_odpovedi:
                    balicek = zachycene_odpovedi.pop(0)
                    url = balicek["url"]
                    data = balicek["data"]

                    # Uložíme si ukázku prvního smysluplného JSONu, ať vidíme strukturu
                    if not os.path.exists(DIAGNOSTIKA_SOUBOR) and ("map" in url or "data" in url):
                        with open(DIAGNOSTIKA_SOUBOR, "w", encoding="utf-8") as f:
                            json.dump(balicek, f, ensure_ascii=False, indent=2)

                    vozidla = extrahuj_vozy_z_objektu(data)

                    for idx, v in enumerate(vozidla):
                        vuz_id = str(
                            v.get("id") or v.get("reg") or v.get("rz") or v.get("vId")
                            or v.get("evc") or v.get("label") or v.get("num")
                            or v.get("car") or v.get("kurz") or f"vuz_{idx}"
                        )
                        linka = str(
                            v.get("line") or v.get("linka") or v.get("l")
                            or v.get("route") or v.get("ln") or v.get("lineName") or ""
                        )
                        spoj = str(
                            v.get("conn") or v.get("spoj") or v.get("cn")
                            or v.get("trip") or v.get("c") or v.get("course") or "?"
                        )

                        if linka:
                            c.execute("""
                                INSERT INTO odjete_spoje (datum, vozidlo, linka, spoj, zacatek, konec)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(datum, vozidlo, linka, spoj)
                                DO UPDATE SET konec = excluded.konec
                            """, (datum_str, vuz_id, linka, spoj, cas_str, cas_str))
                            zaznamenano += 1

                conn.commit()
                vyexportuj_turnusy_do_csv(conn)
                print(f"[{cas_str}] Cyklus {i+1}/{pocet_cyklu}: Zpracováno {zaznamenano} záznamů vozů.")

                # Uložení na GitHub po prvním cyklu a pak každých 15 minut
                if i == 0 or (i + 1) % ulozit_kazdych_cyklu == 0:
                    uloz_na_github(f"Aktualizace turnusů ({cas_str})")

            except Exception as e:
                print(f"[{cas_str}] Chyba v cyklu: {e}")

            time.sleep(INTERVAL_SEKUND)

        browser.close()

    uloz_na_github("Uložení po dokončení bloku")
    conn.close()


if __name__ == "__main__":
    spust_automat()
