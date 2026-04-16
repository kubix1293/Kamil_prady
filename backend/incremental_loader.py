#!/usr/bin/env python3
"""
Incremental CSV → PostgreSQL loader for Daimler production data.

Run by cron every N minutes. For each CSV file in the input directory:
  1. Check file age — skip if older than FILE_MAX_AGE_DAYS
  2. Read loading state from csv_load_state table
  3. Load CSV, filter rows already loaded (date_time > last_loaded_dt)
  4. Insert new rows via INSERT ... ON CONFLICT DO NOTHING
  5. Update state in csv_load_state
  6. Optionally archive file after successful load
"""

import sys
import os
import re
import argparse
import glob
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from loguru import logger

# ── Load .env ────────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

# ── Configuration from .env ──────────────────────────────────────────────────
DB_DSN = os.getenv("PG_DSN", "host=localhost dbname=gedia user=loader password=loader")
SCHEMA = os.getenv("DB_SCHEMA", "public")
TABLE = os.getenv("DB_TABLE", "daimler_process_log")
STATE_TABLE = os.getenv("DB_STATE_TABLE", "csv_load_state")
CSV_INPUT_DIR = os.getenv("CSV_INPUT_DIR", "/srv/production/daimler")
CSV_ARCHIVE_DIR = os.getenv("CSV_ARCHIVE_DIR", "")
CSV_ERROR_DIR = os.getenv("CSV_ERROR_DIR", "")
FILE_MAX_AGE_DAYS = int(os.getenv("FILE_MAX_AGE_DAYS", "2"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", str(Path(__file__).resolve().parent / "logs"))

# Column name overrides: clean_col_name=db_col_name, comma-separated
_raw_overrides = os.getenv("COLUMN_OVERRIDES", "")
COLUMN_OVERRIDES: dict[str, str] = {}
for pair in _raw_overrides.split(","):
    pair = pair.strip()
    if "=" in pair:
        k, v = pair.split("=", 1)
        COLUMN_OVERRIDES[k.strip()] = v.strip()

STATE_TBL = f'"{SCHEMA}"."{STATE_TABLE}"'
TARGET = f'"{SCHEMA}"."{TABLE}"'

# ── Logging setup (loguru) ───────────────────────────────────────────────────
logger.remove()  # remove default stderr handler
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level: <7}</level> {message}",
)
os.makedirs(LOG_DIR, exist_ok=True)
logger.add(
    os.path.join(LOG_DIR, "loader_{time:YYYY-MM-DD}.log"),
    level=LOG_LEVEL,
    rotation="1 day",
    retention="30 days",
    encoding="utf-8",
)


# ── Helpers: column name mapping CSV → PostgreSQL ────────────────────────────
def clean_col(name: str) -> str:
    """Convert CSV header to a safe PostgreSQL column name."""
    name = re.sub(r'^\d+-', '', name)
    name = name.lower()
    name = re.sub(r'[^a-z0-9]+', '_', name)
    name = name.strip('_')
    name = re.sub(r'_+', '_', name)
    return name


def build_col_map(columns: list[str]) -> dict[str, str]:
    """Return {orig_name: pg_name}, handles duplicates with _N suffix.
    Applies COLUMN_OVERRIDES after automatic name generation."""
    seen: dict[str, int] = {}
    result: dict[str, str] = {}
    for col in columns:
        pg = clean_col(col)
        if pg in seen:
            seen[pg] += 1
            pg = f"{pg}_{seen[pg]}"
        else:
            seen[pg] = 0
        # Apply manual override if defined
        if pg in COLUMN_OVERRIDES:
            old_pg = pg
            pg = COLUMN_OVERRIDES[pg]
            logger.debug(f"Override kolumny: '{old_pg}' → '{pg}'")
        result[col] = pg
    return result


def quoted(name: str) -> str:
    """Wrap PostgreSQL identifier in double quotes."""
    return f'"{name}"'


# ── File age check ───────────────────────────────────────────────────────────
def is_file_too_old(file_path: str, max_age_days: int) -> bool:
    """
    Return True if file's modification time is older than max_age_days.
    If max_age_days <= 0, filtering is disabled (always returns False).
    """
    if max_age_days <= 0:
        return False
    mtime = os.path.getmtime(file_path)
    mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    return mtime_dt < cutoff


# ── State read/write ─────────────────────────────────────────────────────────
def get_load_state(cur, file_path: str):
    cur.execute(
        f'SELECT "last_loaded_dt", "rows_total" FROM {STATE_TBL} WHERE "file_path" = %s',
        (file_path,),
    )
    row = cur.fetchone()
    if row:
        return row[0], row[1]
    return None, 0


def set_load_state(cur, file_path, last_dt, rows_total, status="ok", error=None):
    cur.execute(
        f"""
        INSERT INTO {STATE_TBL}
            ("file_path", "last_loaded_dt", "rows_total",
             "last_run_at", "last_run_status", "error_msg")
        VALUES (%s, %s, %s, now(), %s, %s)
        ON CONFLICT ("file_path") DO UPDATE SET
            "last_loaded_dt"  = EXCLUDED."last_loaded_dt",
            "rows_total"      = EXCLUDED."rows_total",
            "last_run_at"     = now(),
            "last_run_status" = EXCLUDED."last_run_status",
            "error_msg"       = EXCLUDED."error_msg"
        """,
        (file_path, last_dt, rows_total, status, error),
    )


# ── Date parsing ─────────────────────────────────────────────────────────────
def parse_datetime(val: str):
    """'20260319_02:31:02' → datetime"""
    try:
        return datetime.strptime(str(val), "%Y%m%d_%H:%M:%S")
    except Exception:
        return None


# ── File move helper ─────────────────────────────────────────────────────────
def move_file(src: str, dest_dir: str):
    """Move file to destination directory (archive or error)."""
    if not dest_dir:
        return
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(src))
        dest = os.path.join(dest_dir, f"{base}_{datetime.now():%Y%m%d_%H%M%S}{ext}")
    os.rename(src, dest)
    logger.info(f"Przeniesiono plik → {dest}")


# ── Main loading function ───────────────────────────────────────────────────
def load_file(file_path: str, dry_run: bool = False, max_age_days: int | None = None):
    file_path = os.path.abspath(file_path)
    file_name = os.path.basename(file_path)

    # Determine effective max age
    effective_max_age = max_age_days if max_age_days is not None else FILE_MAX_AGE_DAYS

    # --- File age check ---
    if is_file_too_old(file_path, effective_max_age):
        age_days = (
            datetime.now(tz=timezone.utc)
            - datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
        ).days
        logger.info(
            f"Pomijam (za stary: {age_days} dni, limit: {effective_max_age}): {file_path}"
        )
        return

    logger.info(f"Przetwarzanie: {file_path}")

    # --- Read CSV ---
    try:
        df = pd.read_csv(
            file_path,
            sep=';',
            skiprows=1,
            encoding='utf-8-sig',
            on_bad_lines='skip',
            index_col=False,
            engine='python',
        )
    except Exception as e:
        logger.error(f"Błąd odczytu CSV: {e}")
        move_file(file_path, CSV_ERROR_DIR)
        return

    if df.empty:
        logger.info("Plik pusty, pomijam.")
        return

    # --- Parse Date-Time ---
    if 'Date-Time' not in df.columns:
        logger.error(f"Brak kolumny 'Date-Time' w pliku {file_name}")
        move_file(file_path, CSV_ERROR_DIR)
        return

    df['date_time'] = df['Date-Time'].apply(parse_datetime)
    invalid_dt = df['date_time'].isna().sum()

    if invalid_dt:
        logger.warning(f"{invalid_dt} wierszy ma nieprawidłowy Date-Time – zostaną pominięte")

    df = df[df['date_time'].notna()].copy()
    df.drop(columns=['Date-Time'], inplace=True)

    # --- Column mapping CSV → pg_name ---
    col_map = build_col_map(df.columns.tolist())
    col_map.pop('date_time', None)
    df.rename(columns=col_map, inplace=True)

    # Metadata columns
    df['source_file'] = file_name
    df['loaded_at'] = datetime.utcnow()

    total_in_file = len(df)

    # --- Connect to DB ---
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        last_dt, prev_rows = get_load_state(cur, file_path)
        logger.info(
            f"Stan DB: last_loaded_dt={last_dt}, prev_rows={prev_rows}, "
            f"wierszy w pliku={total_in_file}"
        )

        # --- Incremental filter ---
        if last_dt is not None:
            new_rows = df[df['date_time'] > last_dt]
        else:
            new_rows = df

        if new_rows.empty:
            logger.info("Brak nowych wierszy. Koniec.")
            set_load_state(cur, file_path, last_dt, total_in_file)
            conn.commit()
            return

        logger.info(f"Nowych wierszy do wstawienia: {len(new_rows)}")

        if dry_run:
            logger.info("[DRY RUN] Nie zapisuję do bazy. Podgląd pierwszych 10 wierszy:")
            preview = new_rows.head(10).to_string(max_cols=8)
            for line in preview.split('\n'):
                logger.info(f"  {line}")
            conn.rollback()
            return

        # --- Target columns (id is BIGSERIAL — skip) ---
        target_pg_cols = [c for c in new_rows.columns if c != 'id']
        cols_sql = ", ".join(quoted(c) for c in target_pg_cols)

        rows = [
            tuple(None if pd.isna(v) else v for v in row)
            for row in new_rows[target_pg_cols].itertuples(index=False)
        ]

        insert_sql = f"""
            INSERT INTO {TARGET} ({cols_sql})
            VALUES %s
            ON CONFLICT ("date_time", "recipe_mlf") DO NOTHING
        """
        execute_values(cur, insert_sql, rows, page_size=500)

        new_last_dt = new_rows['date_time'].max()
        set_load_state(cur, file_path, new_last_dt, total_in_file)
        conn.commit()
        logger.info(
            f"OK – wstawiono maks. {len(new_rows)} wierszy, "
            f"nowy last_loaded_dt={new_last_dt}"
        )

    except Exception as e:
        conn.rollback()
        logger.error(f"Błąd ładowania: {e}")
        try:
            cur2 = conn.cursor()
            set_load_state(
                cur2, file_path,
                last_dt if 'last_dt' in dir() else None,
                total_in_file, status="error", error=str(e)[:500],
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        cur.close()
        conn.close()


# ── Scan directory for CSV files ─────────────────────────────────────────────
def scan_and_load(
    input_dir: str,
    dry_run: bool = False,
    max_age_days: int | None = None,
    exclude: list[str] | None = None,
):
    """Find all CSV files in input_dir and load each one."""
    csv_pattern = os.path.join(input_dir, "*.csv")
    files = sorted(glob.glob(csv_pattern))

    if exclude:
        exclude_set = set(exclude)
        before = len(files)
        files = [f for f in files if os.path.basename(f) not in exclude_set]
        skipped = before - len(files)
        if skipped:
            logger.info(f"Pominięto {skipped} plików z listy wykluczeń: {exclude_set}")

    if not files:
        logger.info(f"Brak plików CSV w {input_dir}")
        return

    logger.info(f"Znaleziono {len(files)} plików CSV w {input_dir}")

    for f in files:
        try:
            load_file(f, dry_run=dry_run, max_age_days=max_age_days)
        except Exception as e:
            logger.error(f"Błąd przetwarzania {f}: {e}")
            move_file(f, CSV_ERROR_DIR)
            continue


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Incremental CSV → PostgreSQL loader")
    parser.add_argument(
        "files", nargs="*",
        help="Ścieżki do plików CSV (jeśli puste, skanuje CSV_INPUT_DIR)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Nie zapisuj do bazy")
    parser.add_argument("--all", action="store_true", help="Skanuj cały katalog CSV_INPUT_DIR")
    parser.add_argument(
        "--max-age", type=int, default=None,
        help="Maks. wiek pliku w dniach (nadpisuje FILE_MAX_AGE_DAYS z .env). 0 = brak limitu.",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=[],
        help="Nazwy plików CSV do pominięcia (np. --exclude plik1.csv plik2.csv)",
    )
    args = parser.parse_args()

    if args.all or not args.files:
        scan_and_load(
            CSV_INPUT_DIR,
            dry_run=args.dry_run,
            max_age_days=args.max_age,
            exclude=args.exclude,
        )
    else:
        for f in args.files:
            if not os.path.exists(f):
                logger.error(f"Plik nie istnieje: {f}")
                continue
            load_file(f, dry_run=args.dry_run, max_age_days=args.max_age)
