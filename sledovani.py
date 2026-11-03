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


def najdi_seznam_vozidel(data):
    """Prohledá JSON odpověď a vrátí seznam objektů představujících vozidla."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        nejlepsi_seznam = []
        for hodnota in data.values():
            if isinstance(hodnota, list) and len(hodnota) > len(nejlepsi_seznam):
                if len(hodnota) > 0 and isinstance(hodnota[0], dict):
                    nejlepsi_seznam = hodnota
        if nejlepsi_seznam:
            return nejlepsi_seznam
        # Pokud jsou vozidla uložena jako slovník slovníků
        Slovníky = [v for v in data.values() if isinstance(v, dict)]
        if len(Slovníky) > 3:
            return Slovníky
    return []


def spust_automat():
    conn = inicializuj_db()
    c = conn.cursor()
    pocet_cyklu = int((DOBA_BEHU_HODIN * 3600) / INTERVAL_SEKUND)
    ulozit_kazdych_cyklu = 20  # Každých 15 minut

    zachycene_odpovedi = []

    with sync_playwright() as p:
        # Spustí neviditelný prohlížeč s velkou obrazovkou (4K), aby mapa obsáhla celý kraj
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 3840, "height": 2160})
        page = context.new_page()

        def pri_odpovedi(response):
            try:
                if response.status == 200 and (
                    "mapData" in response.url
                    or response.request.resource_type in ("xhr", "fetch")
                ):
                    text = response.text()
                    if text and (text.strip().startswith("{") or text.strip().startswith("[")):
                        data = json.loads(text)
                        zachycene_odpovedi.append({
                            "url": response.url,
                            "dotaz_payload": response.request.post_data,
                            "data": data
                        })
            except Exception:
                pass

        page.on("response", pri_odpovedi)

        print("Otevírám mapu ODIS v prohlížeči...")
        try:
            page.goto(MAPA_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
        except Exception as e:
            print(f"Upozornění při načítání stránky: {e}")

        for i in range(pocet_cyklu):
            nyni = datetime.now(ZoneInfo("Europe/Prague"))
            datum_str = nyni.strftime("%Y-%m-%d")
            cas_str = nyni.strftime("%H:%M:%S")

            try:
                # Pokud mapa sama neposlala nová data, obnovíme ji
                if not zachycene_odpovedi:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(7000)

                zaznamenano = 0
                while zachycene_odpovedi:
                    polozka = zachycene_odpovedi.pop(0)
                    data = polozka["data"]

                    # Uložení prvního zachyceného JSONu pro kontrolu struktury
                    if not os.path.exists(DIAGNOSTIKA_SOUBOR):
                        with open(DIAGNOSTIKA_SOUBOR, "w", encoding="utf-8") as f:
                            json.dump(polozka, f, ensure_ascii=False, indent=2)

                    vozidla = najdi_seznam_vozidel(data)
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

                # Uloží na GitHub hned po 1. cyklu (pro okamžitou kontrolu) a pak každých 15 minut
                if i == 0 or (i + 1) % ulozit_kazdych_cyklu == 0:
                    uloz_na_github(f"Aktualizace turnusů ({cas_str})")

            except Exception as e:
                print(f"[{cas_str}] Chyba v cyklu: {e}")

            time.sleep(INTERVAL_SEKUND)

        browser.close()

    uloz_na_github("Uložení po dokončení 6h bloku")
    conn.close()


if __name__ == "__main__":
    spust_automat()
