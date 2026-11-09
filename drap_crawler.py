import requests
import sqlite3
from bs4 import BeautifulSoup
import time
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ============================================================
# CONFIG
# ============================================================

DB_FILE = "drap_master.db"

BASE_URL = "https://eapp.dra.gov.pk/product_view_web.php"

MAX_WORKERS = 10

TIMEOUT = 60

MAX_RETRIES = 3


# ============================================================
# DATABASE
# ============================================================

conn = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db_lock = Lock()

cur = conn.cursor()


# ============================================================
# PRODUCT DETAILS
# ============================================================

cur.execute("""
CREATE TABLE IF NOT EXISTS product_details (

    registration_no TEXT PRIMARY KEY,

    brand_name TEXT,
    registration_date TEXT,
    meeting_no TEXT,
    used_for TEXT,

    product_category TEXT,
    dosage_form TEXT,
    route_of_admin TEXT,

    finished_product_spec TEXT,
    shelf_life TEXT,
    storage_condition TEXT,

    manufacturing_type TEXT,
    variation_status TEXT,

    label_claim TEXT,

    pack_size TEXT,
    presentation TEXT,
    approved_price TEXT,
    pricing_type TEXT,

    status TEXT,

    raw_html TEXT,

    scraped_at TEXT
)
""")


# ============================================================
# COMPOSITIONS
# ============================================================

cur.execute("""
CREATE TABLE IF NOT EXISTS product_compositions (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    registration_no TEXT,

    generic_name TEXT,
    operator TEXT,
    strength TEXT,
    unit TEXT,

    FOREIGN KEY(registration_no)
        REFERENCES product_details(registration_no)
)
""")


# ============================================================
# SCRAPE STATUS
# ============================================================

cur.execute("""
CREATE TABLE IF NOT EXISTS scrape_status (

    registration_no TEXT PRIMARY KEY,

    status TEXT,

    attempts INTEGER DEFAULT 0,

    error TEXT,

    updated_at TEXT
)
""")

conn.commit()

# ============================================================
# PERFORMANCE INDEXES
# ============================================================
# The original batch query used TRIM() on BOTH sides of the JOIN,
# which prevented SQLite from efficiently using scrape_status's
# PRIMARY KEY index. Products are also ordered by TRIM(registration_no).
# These indexes make batch selection fast even after tens of thousands
# of records have been scraped.

cur.execute("""
CREATE INDEX IF NOT EXISTS idx_products_registration_trim
ON products(TRIM(registration_no))
""")

conn.commit()


# ============================================================
# HELPERS
# ============================================================

def clean(value):

    if value is None:
        return None

    value = value.strip()

    value = re.sub(r"\s+", " ", value)

    return value if value else None


# ============================================================
# FETCH PAGE
# ============================================================

def fetch_product(reg_no):

    session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml"
    }

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.get(
                BASE_URL,
                params={"reg_no": reg_no},
                headers=headers,
                timeout=TIMEOUT
            )

            if response.status_code == 200:
                return response.text, attempt

            print(
                f"[{reg_no}] HTTP {response.status_code} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )

        except Exception as e:

            print(
                f"[{reg_no}] ERROR: {e} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    return None, MAX_RETRIES


# ============================================================
# PARSE PRODUCT
# ============================================================

def parse_product(html, reg_no):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # DRAP returns the normal search page when
    # the registration number does not exist.
    if not soup.select_one(".product-record"):
        return None

    reg_no = str(reg_no).strip()

    product = {
        "registration_no": reg_no,
        "brand_name": None,
        "registration_date": None,
        "meeting_no": None,
        "used_for": None,
        "product_category": None,
        "dosage_form": None,
        "route_of_admin": None,
        "finished_product_spec": None,
        "shelf_life": None,
        "storage_condition": None,
        "manufacturing_type": None,
        "variation_status": None,
        "label_claim": None,
        "pack_size": None,
        "presentation": None,
        "approved_price": None,
        "pricing_type": None,
        "status": None,
        "raw_html": html,
        "compositions": []
    }

    # ========================================================
    # GENERAL INFORMATION
    # ========================================================

    mapping = {
        "Brand Name": "brand_name",
        "Registration No": "registration_no",
        "Registration Date": "registration_date",
        "Meeting No": "meeting_no",
        "Used For": "used_for",
        "Product Category": "product_category",
        "Dosage Form": "dosage_form",
        "Route of Admin": "route_of_admin",
        "Finished Product Spec": "finished_product_spec",
        "Shelf Life": "shelf_life",
        "Storage Condition": "storage_condition",
        "Manufacturing Type": "manufacturing_type",
        "Variation Status": "variation_status",
        "Label Claim (Ref Unit)": "label_claim"
    }

    labels = soup.select(".record-label")

    for label in labels:

        key = clean(
            label.get_text(
                " ",
                strip=True
            )
        )

        value_element = label.find_next_sibling(
            class_="record-value"
        )

        if not value_element:
            continue

        value = clean(
            value_element.get_text(
                " ",
                strip=True
            )
        )

        if not value:
            continue

        if key in mapping:
            product[mapping[key]] = value

    # ========================================================
    # STATUS
    # ========================================================

    status_badges = soup.select(
        ".product-record .badge-status"
    )

    for badge in status_badges:

        text = clean(
            badge.get_text(
                " ",
                strip=True
            )
        )

        if text and product["status"] is None:
            product["status"] = text

    # ========================================================
    # TABLES
    # ========================================================

    for table in soup.select(
        "table.registry-table"
    ):

        headers = [
            clean(
                th.get_text(
                    " ",
                    strip=True
                )
            ).lower()
            for th in table.select("thead th")
        ]

        # ----------------------------------------------------
        # COMPOSITION
        # ----------------------------------------------------

        if headers == [
            "generic name",
            "operator",
            "strength",
            "unit"
        ]:

            for row in table.select(
                "tbody tr"
            ):

                cells = [
                    clean(
                        td.get_text(
                            " ",
                            strip=True
                        )
                    )
                    for td in row.select("td")
                ]

                if len(cells) != 4:
                    continue

                product["compositions"].append({
                    "generic_name": cells[0],
                    "operator": cells[1],
                    "strength": cells[2],
                    "unit": cells[3]
                })

        # ----------------------------------------------------
        # PRICING
        # ----------------------------------------------------

        if headers == [
            "pack size",
            "presentation",
            "approved price",
            "pricing type"
        ]:

            rows = table.select(
                "tbody tr"
            )

            for row in rows:

                cells = [
                    clean(
                        td.get_text(
                            " ",
                            strip=True
                        )
                    )
                    for td in row.select("td")
                ]

                if len(cells) != 4:
                    continue

                product["pack_size"] = cells[0]
                product["presentation"] = cells[1]
                product["approved_price"] = cells[2]
                product["pricing_type"] = cells[3]

                break

    return product


# ============================================================
# SAVE STATUS
# ============================================================

def save_status(
    reg_no,
    status,
    attempts,
    error=None
):

    with db_lock:

        conn.execute("""
        INSERT OR REPLACE INTO scrape_status
        (
            registration_no,
            status,
            attempts,
            error,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """, (
            reg_no,
            status,
            attempts,
            error,
            datetime.now().isoformat()
        ))

        conn.commit()


# ============================================================
# SAVE PRODUCT
# ============================================================

def save_product(
    product,
    attempts
):

    reg_no = product["registration_no"]

    with db_lock:

        conn.execute("""
        INSERT OR REPLACE INTO product_details
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """, (
            reg_no,

            product["brand_name"],
            product["registration_date"],
            product["meeting_no"],
            product["used_for"],

            product["product_category"],
            product["dosage_form"],
            product["route_of_admin"],

            product["finished_product_spec"],
            product["shelf_life"],
            product["storage_condition"],

            product["manufacturing_type"],
            product["variation_status"],

            product["label_claim"],

            product["pack_size"],
            product["presentation"],
            product["approved_price"],
            product["pricing_type"],

            product["status"],

            product["raw_html"],

            datetime.now().isoformat()
        ))

        # Delete existing compositions
        conn.execute("""
        DELETE FROM product_compositions
        WHERE registration_no = ?
        """, (reg_no,))

        for composition in product["compositions"]:

            conn.execute("""
            INSERT INTO product_compositions
            (
                registration_no,
                generic_name,
                operator,
                strength,
                unit
            )
            VALUES (?, ?, ?, ?, ?)
            """, (
                reg_no,
                composition["generic_name"],
                composition["operator"],
                composition["strength"],
                composition["unit"]
            ))

        conn.execute("""
        INSERT OR REPLACE INTO scrape_status
        (
            registration_no,
            status,
            attempts,
            error,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """, (
            reg_no,
            "completed",
            attempts,
            None,
            datetime.now().isoformat()
        ))

        conn.commit()


# ============================================================
# PROCESS ONE PRODUCT
# ============================================================

def process_product(row):

    reg_no = str(
        row["registration_no"]
    ).strip()

    search_name = row["product_name"]

    print(
        f"[START] {reg_no} | {search_name}"
    )

    # --------------------------------------------------------
    # INVALID DRAP ID
    # --------------------------------------------------------

    if reg_no == "0":

        save_status(
            reg_no,
            "not_found",
            0,
            "Invalid DRAP registration number: 0"
        )

        print(
            f"[NOT FOUND] {reg_no}"
        )

        return "not_found"

    # --------------------------------------------------------
    # FETCH
    # --------------------------------------------------------

    html, attempts = fetch_product(
        reg_no
    )

    if html is None:

        save_status(
            reg_no,
            "failed",
            attempts,
            "Unable to fetch page"
        )

        print(
            f"[FAILED] {reg_no}"
        )

        return "failed"

    # --------------------------------------------------------
    # PARSE
    # --------------------------------------------------------

    try:

        product = parse_product(
            html,
            reg_no
        )

        if product is None:

            save_status(
                reg_no,
                "not_found",
                attempts,
                "DRAP returned empty product lookup page"
            )

            print(
                f"[NOT FOUND] {reg_no}"
            )

            return "not_found"

        # ----------------------------------------------------
        # SAVE
        # ----------------------------------------------------

        save_product(
            product,
            attempts
        )

        print(
            f"[OK] {reg_no} | "
            f"{product['brand_name']} | "
            f"Composition: "
            f"{len(product['compositions'])}"
        )

        return "completed"

    except Exception as e:

        save_status(
            reg_no,
            "parse_failed",
            attempts,
            str(e)
        )

        print(
            f"[PARSE FAILED] {reg_no}: {e}"
        )

        return "parse_failed"


# ============================================================
# MAIN
# ============================================================

def main():

    total_batch = 0

    while True:

        # ----------------------------------------------------
        # GET NEXT BATCH OF 100
        # ----------------------------------------------------

        rows = conn.execute("""
            SELECT
                TRIM(p.registration_no) AS registration_no,
                p.product_name
            FROM products p
            LEFT JOIN scrape_status s
                ON s.registration_no = TRIM(p.registration_no)
            WHERE
                s.registration_no IS NULL
            ORDER BY TRIM(p.registration_no)
            LIMIT 100
        """).fetchall()

        # ----------------------------------------------------
        # NOTHING LEFT
        # ----------------------------------------------------

        if not rows:

            print()
            print("=" * 70)
            print("ALL PRODUCTS SCRAPED")
            print("=" * 70)
            break

        total_batch += 1

        print()
        print("=" * 70)
        print(f"BATCH {total_batch}")
        print("=" * 70)
        print(f"Products in batch : {len(rows)}")
        print(f"Workers            : {MAX_WORKERS}")
        print("=" * 70)
        print()

        # Convert rows to dictionaries
        rows = [
            {
                "registration_no": row[0],
                "product_name": row[1]
            }
            for row in rows
        ]

        # ----------------------------------------------------
        # BATCH COUNTERS
        # ----------------------------------------------------

        batch_completed = 0
        batch_not_found = 0
        batch_failed = 0

        # ----------------------------------------------------
        # 5 PARALLEL WORKERS
        # ----------------------------------------------------

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = [
                executor.submit(
                    process_product,
                    row
                )
                for row in rows
            ]

            for future in as_completed(futures):

                try:

                    result = future.result()

                    if result == "completed":
                        batch_completed += 1

                    elif result == "not_found":
                        batch_not_found += 1

                    elif result in (
                        "failed",
                        "parse_failed"
                    ):
                        batch_failed += 1

                except Exception as e:

                    print(
                        "WORKER CRASHED:",
                        e
                    )

                    batch_failed += 1

        # ----------------------------------------------------
        # BATCH SUMMARY
        # ----------------------------------------------------

        total_completed = conn.execute("""
            SELECT COUNT(*)
            FROM scrape_status
            WHERE status = 'completed'
        """).fetchone()[0]

        total_not_found = conn.execute("""
            SELECT COUNT(*)
            FROM scrape_status
            WHERE status = 'not_found'
        """).fetchone()[0]

        total_failed = conn.execute("""
            SELECT COUNT(*)
            FROM scrape_status
            WHERE status IN (
                'failed',
                'parse_failed'
            )
        """).fetchone()[0]

        print()
        print("-" * 70)
        print(f"BATCH {total_batch} FINISHED")
        print("-" * 70)

        print(
            f"Batch completed : {batch_completed}"
        )

        print(
            f"Batch not found : {batch_not_found}"
        )

        print(
            f"Batch failed    : {batch_failed}"
        )

        print()

        print(
            f"Total completed : {total_completed:,}"
        )

        print(
            f"Total not found : {total_not_found:,}"
        )

        print(
            f"Total failed    : {total_failed:,}"
        )

        print("-" * 70)

        # ----------------------------------------------------
        # STOP IF SOMETHING IS GOING VERY WRONG
        # ----------------------------------------------------

        if batch_failed > 0:

            print()
            print(
                "WARNING: This batch had failures."
            )

            print(
                "The scraper will continue, "
                "but failed records will remain "
                "for retry on the next run."
            )

        # ----------------------------------------------------
        # SMALL PAUSE BETWEEN BATCHES
        # ----------------------------------------------------

        time.sleep(2)

    # ========================================================
    # FINAL DATABASE SUMMARY
    # ========================================================

    print()
    print("=" * 70)
    print("SCRAPER FINISHED")
    print("=" * 70)

    completed = conn.execute("""
        SELECT COUNT(*)
        FROM scrape_status
        WHERE status = 'completed'
    """).fetchone()[0]

    not_found = conn.execute("""
        SELECT COUNT(*)
        FROM scrape_status
        WHERE status = 'not_found'
    """).fetchone()[0]

    failed = conn.execute("""
        SELECT COUNT(*)
        FROM scrape_status
        WHERE status IN (
            'failed',
            'parse_failed'
        )
    """).fetchone()[0]

    details = conn.execute("""
        SELECT COUNT(*)
        FROM product_details
    """).fetchone()[0]

    compositions = conn.execute("""
        SELECT COUNT(*)
        FROM product_compositions
    """).fetchone()[0]

    print(
        f"Completed       : {completed:,}"
    )

    print(
        f"Not found       : {not_found:,}"
    )

    print(
        f"Failed          : {failed:,}"
    )

    print(
        f"Product details : {details:,}"
    )

    print(
        f"Compositions    : {compositions:,}"
    )

    print("=" * 70)

    conn.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()