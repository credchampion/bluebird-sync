"""Replace one county's rows in small steps, so the table never holds two full copies at once."""
import hashlib
import os

import psycopg

STEPS = 8
# The same bucket number, worked out in SQL here and in Python below.
BUCKET = "('x' || substr(md5(account), 1, 2))::bit(8)::int %% 8"


def bucket(account):
    return int(hashlib.md5(account.encode()).hexdigest()[:2], 16) % STEPS


def replace_county(county, data, cols, account_ix, marks):
    """Swap in new rows for a county. marks = column positions of (market, appraised, sqft).

    Returns False, and touches nothing, when the source matches what is already stored.
    """
    mark = (len(data),) + tuple(sum(row[i] for row in data) for i in marks)
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        conn.execute("set statement_timeout = 0")  # a busy database can make one step slow
        stored = conn.execute(
            "select count(*), coalesce(sum(market::bigint), 0), coalesce(sum(appraised::bigint), 0), "
            "coalesce(sum(sqft::bigint), 0) from public.homes where county = %s", (county,)).fetchone()
        if tuple(int(v) for v in stored) == mark:
            return False
        groups = [[] for _ in range(STEPS)]
        for row in data:
            groups[bucket(row[account_ix])].append(row)
        for step, rows in enumerate(groups):
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(f"delete from public.homes where county = %s and {BUCKET} = %s", (county, step))
                with cur.copy(f"copy public.homes ({cols}) from stdin") as copy:
                    for row in rows:
                        copy.write_row(row)
            conn.execute("vacuum public.homes")  # lets the next step reuse the space just freed
    return True
