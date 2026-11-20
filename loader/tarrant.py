"""Nightly loader: Tarrant Appraisal District residential file -> Supabase `homes` table.

Reads the county's public pipe-delimited file, keeps single-family homes, and replaces
Tarrant's rows in one transaction. Owner names in the county file are never loaded.
"""
import csv
import io
import os
import re
import urllib.request
import zipfile

import psycopg

URL = os.environ.get(
    "TAD_URL",
    "https://www.tad.org/content/data-download/PropertyData_R_2025(Certified).ZIP",
)
COUNTY = "tarrant"
MIN_ROWS = 400_000  # a short file means a bad download: abort and keep yesterday's data
COLS = ("county,tax_year,account,addr,addr_key,city_code,subdivision,sqft,year_built,"
        "market,appraised,bedrooms,bathrooms,pool,land_sqft")


def num(value):
    try:
        return int(float(value.strip() or 0))
    except ValueError:
        return 0


def rows():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (county data loader)"})
    data = urllib.request.urlopen(req, timeout=300).read()
    archive = zipfile.ZipFile(io.BytesIO(data))
    with archive.open(archive.namelist()[0]) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1", newline=""),
                            delimiter="|", quoting=csv.QUOTE_NONE)
        ix = {name.strip(): i for i, name in enumerate(next(reader))}
        for f in reader:
            if len(f) < len(ix):
                continue
            addr = re.sub(r"\s+", " ", f[ix["Situs_Address"]]).strip()
            sqft, market = num(f[ix["Living_Area"]]), num(f[ix["Total_Value"]])
            if f[ix["Property_Class"]].strip() != "A1" or sqft <= 400 or market <= 20000 or not addr:
                continue
            legal = f[ix["LegalDescription"]].strip()
            m = re.match(r"^(.*?)\s+Block\s+", legal, re.I)
            yield (
                COUNTY, num(f[ix["Appraisal_Year"]]), f[ix["Account_Num"]].strip(), addr,
                addr.upper(), f[ix["City"]].strip(), (m.group(1) if m else legal).strip(),
                sqft, num(f[ix["Year_Built"]]), market, num(f[ix["Appraised_Value"]]),
                num(f[ix["Num_Bedrooms"]]), num(f[ix["Num_Bathrooms"]]),
                f[ix["Swimming_Pool_Ind"]].strip().upper() == "Y", num(f[ix["Land_SqFt"]]),
            )


def main():
    data = list(rows())
    if len(data) < MIN_ROWS:
        raise SystemExit(f"only {len(data)} rows parsed; aborting so existing data is kept")
    mark = (len(data), sum(r[9] for r in data), sum(r[10] for r in data), sum(r[7] for r in data))
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn, conn.cursor() as cur:
        cur.execute("select count(*), coalesce(sum(market::bigint), 0), coalesce(sum(appraised::bigint), 0), "
                    "coalesce(sum(sqft::bigint), 0) from public.homes where county = %s", (COUNTY,))
        if tuple(int(v) for v in cur.fetchone()) == mark:
            print("source unchanged; nothing to load")  # skipping avoids rewriting every row each night
            return
        cur.execute("delete from public.homes where county = %s", (COUNTY,))
        with cur.copy(f"copy public.homes ({COLS}) from stdin") as copy:
            for row in data:
                copy.write_row(row)
        print(f"loaded {len(data)} Tarrant homes")


if __name__ == "__main__":
    main()
