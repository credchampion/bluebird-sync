"""Load Dallas County single-family homes into the homes table.

Source: Dallas Central Appraisal District public data products (certified roll, comma delimited).
Owner names and mailing addresses in the source files are never read into the output.
"""
import csv
import io
import os
import re
import tempfile
import urllib.request
import zipfile

import psycopg

URL = os.environ.get(
    "DCAD_URL",
    "https://www.dallascad.org/ViewPDFs.aspx?type=3&id="
    "%5C%5CDCAD.ORG%5CWEB%5CWEBDATA%5CWEBFORMS%5CDATA%20PRODUCTS%5CDCAD2026_CURRENT.ZIP",
)
COUNTY = "dallas"
MIN_ROWS = 400_000  # a short file means a bad download: abort and keep yesterday's data
COLS = ("county,tax_year,account,addr,addr_key,city_code,city,subdivision,sqft,year_built,"
        "market,appraised,bedrooms,bathrooms,pool,land_sqft")
SWAPS = {"AVENUE": "AVE", "STREET": "ST", "ROAD": "RD", "COURT": "CT", "DRIVE": "DR", "WEST": "W", "EAST": "E"}


def num(value):
    try:
        return int(float((value or "").strip() or 0))
    except ValueError:
        return 0


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def addr_key(addr):
    words = re.sub(r"[^A-Z0-9 ]", "", addr.upper()).split()
    return " ".join(SWAPS.get(w, w) for w in words)


def table(archive, name):
    member = next(n for n in archive.namelist() if n.upper() == name)
    return csv.DictReader(io.TextIOWrapper(archive.open(member), encoding="latin-1", newline=""))


def download(target):
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (data loader)"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        while chunk := resp.read(1 << 20):
            target.write(chunk)
    target.flush()


def rows(archive):
    homes = {}
    for r in table(archive, "ACCOUNT_APPRL_YEAR.CSV"):
        if r["DIVISION_CD"] == "RES" and r["SPTD_CODE"] == "A11":
            homes[r["ACCOUNT_NUM"]] = {"year": num(r["APPRAISAL_YR"]), "market": num(r["TOT_VAL"]),
                                       "appraised": num(r["HMSTD_CAP_VAL"]), "sqft": 0, "land": 0}
    for r in table(archive, "RES_DETAIL.CSV"):
        h = homes.get(r["ACCOUNT_NUM"])
        sqft = num(r["TOT_LIVING_AREA_SF"])
        if h is None or sqft <= h["sqft"]:
            continue  # keep the main (largest) building on the lot
        h.update(sqft=sqft, built=num(r["YR_BUILT"]), beds=num(r["NUM_BEDROOMS"]),
                 baths=num(r["NUM_FULL_BATHS"]), pool=r["POOL_IND"].strip().upper() == "Y")
    for r in table(archive, "ACCOUNT_INFO.CSV"):
        h = homes.get(r["ACCOUNT_NUM"])
        if h is None:
            continue
        unit = clean(r["UNIT_ID"])
        h["addr"] = clean(f'{r["STREET_NUM"]} {r["STREET_HALF_NUM"]} {r["FULL_STREET_NAME"]}'
                          + (f" #{unit}" if unit else ""))
        h["city"] = clean(re.sub(r"\(.*?\)", "", r["PROPERTY_CITY"])).upper()
        h["sub"] = clean(r["LEGAL1"])
    for r in table(archive, "LAND.CSV"):
        h = homes.get(r["ACCOUNT_NUM"])
        if h is None:
            continue
        try:
            area = float(r["AREA_SIZE"] or 0)
        except ValueError:
            area = 0
        h["land"] += int(area * 43560 if r["AREA_UOM_DESC"].upper().startswith("ACRE") else area)
    for account, h in homes.items():
        addr = h.get("addr", "")
        if h["sqft"] <= 400 or h["market"] <= 20000 or not addr[:1].isdigit():
            continue
        yield (COUNTY, h["year"], account, addr, addr_key(addr), None, h["city"] or None, h["sub"] or None,
               h["sqft"], h["built"], h["market"], h["appraised"], h["beds"], h["baths"], h["pool"],
               min(h["land"], 2_000_000_000))


def main():
    with tempfile.TemporaryFile() as tmp:
        download(tmp)
        archive = zipfile.ZipFile(tmp)
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn, conn.cursor() as cur:
            cur.execute("delete from public.homes where county = %s", (COUNTY,))
            count = 0
            with cur.copy(f"copy public.homes ({COLS}) from stdin") as copy:
                for row in rows(archive):
                    copy.write_row(row)
                    count += 1
            if count < MIN_ROWS:
                raise SystemExit(f"only {count} rows parsed; aborting so existing data is kept")
            print(f"loaded {count} Dallas homes")


if __name__ == "__main__":
    main()
