#!/usr/bin/env python3
"""Collect public ANGLERS fishing records and store normalized facts in SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://anglers.jp"
AREA_DISCOVERY_URL = f"{BASE_URL}/api/v2/areas/list_by_location.json"
DEFAULT_DATABASE = Path("data/fishing.db")
DEFAULT_SPOTS = Path("spots.json")
USER_AGENT = "FishingStatisticsCollector/1.0"

YEAR_MONTH_PATTERN = re.compile(r"(\d{4})年\s*(\d{1,2})月")
DAY_PATTERN = re.compile(r"(\d{1,2})日")
FISHING_PATH_PATTERN = re.compile(r"^/fishings/(\d+)$")
RESULT_ID_PATTERN = re.compile(r"/result/(\d+)/")


@dataclass(frozen=True)
class Spot:
    area_id: int
    prefecture: str
    spot_name: str
    lat: float
    lng: float

    @property
    def source_url(self) -> str:
        return f"{BASE_URL}/areas/{self.area_id}/fishings"


@dataclass(frozen=True)
class CatchRecord:
    source_fishing_id: str
    source_item_id: str
    fish_name: str
    caught_date: str


class RequestPacer:
    """Enforce a minimum interval with jitter between all HTTP requests."""

    def __init__(
        self,
        delay: float,
        jitter: float,
        *,
        clock=time.monotonic,
        sleeper=time.sleep,
        random_value=random.random,
    ) -> None:
        self.delay = delay
        self.jitter = jitter
        self.clock = clock
        self.sleeper = sleeper
        self.random_value = random_value
        self.last_request_at: float | None = None

    def wait(self) -> float:
        wait_seconds = 0.0
        if self.last_request_at is not None:
            interval = self.delay + self.random_value() * self.jitter
            elapsed = self.clock() - self.last_request_at
            wait_seconds = max(0.0, interval - elapsed)
            if wait_seconds > 0:
                self.sleeper(wait_seconds)
        self.last_request_at = self.clock()
        return wait_seconds

    def backoff(self, attempt: int, retry_after: str | None = None) -> float:
        retry_seconds = 0.0
        if retry_after:
            try:
                retry_seconds = max(0.0, float(retry_after))
            except ValueError:
                retry_seconds = 0.0
        exponential = max(self.delay, float(2**attempt))
        wait_seconds = max(retry_seconds, exponential) + self.random_value() * self.jitter
        self.sleeper(wait_seconds)
        return wait_seconds


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS spots (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_area_id TEXT NOT NULL,
            prefecture TEXT NOT NULL,
            spot_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            source_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, source_area_id)
        );

        CREATE TABLE IF NOT EXISTS catches (
            id INTEGER PRIMARY KEY,
            spot_id INTEGER NOT NULL REFERENCES spots(id),
            source TEXT NOT NULL,
            source_fishing_id TEXT NOT NULL,
            source_item_id TEXT NOT NULL,
            fish_name TEXT NOT NULL,
            caught_date TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            UNIQUE(source, source_item_id)
        );

        CREATE INDEX IF NOT EXISTS catches_spot_date_idx
            ON catches(spot_id, caught_date);
        CREATE INDEX IF NOT EXISTS catches_fish_date_idx
            ON catches(fish_name, caught_date);

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_area_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            pages_requested INTEGER NOT NULL,
            pages_collected INTEGER NOT NULL DEFAULT 0,
            records_found INTEGER NOT NULL DEFAULT 0,
            records_inserted INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_progress (
            source TEXT NOT NULL,
            source_area_id TEXT NOT NULL,
            next_page INTEGER NOT NULL DEFAULT 1,
            is_complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source, source_area_id)
        );
        """
    )
    connection.commit()


def load_spots(path: Path) -> list[Spot]:
    with path.open(encoding="utf-8") as source:
        items = json.load(source)

    spots = []
    for item in items:
        if not item.get("enabled", True):
            continue
        spots.append(
            Spot(
                area_id=int(item["area_id"]),
                prefecture=str(item["prefecture"]).strip(),
                spot_name=str(item["spot_name"]).strip(),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
            )
        )
    return spots


def load_discovered_spots(
    connection: sqlite3.Connection,
    area_id: int | None = None,
    start_area_id: int | None = None,
    end_area_id: int | None = None,
) -> list[Spot]:
    sql = """
        SELECT source_area_id, prefecture, spot_name, lat, lng
        FROM spots
        WHERE source = 'anglers'
    """
    parameters: tuple[object, ...] = ()
    if area_id is not None:
        sql += " AND source_area_id = ?"
        parameters = (str(area_id),)
    else:
        parameter_list: list[object] = []
        if start_area_id is not None:
            sql += " AND CAST(source_area_id AS INTEGER) >= ?"
            parameter_list.append(start_area_id)
        if end_area_id is not None:
            sql += " AND CAST(source_area_id AS INTEGER) <= ?"
            parameter_list.append(end_area_id)
        parameters = tuple(parameter_list)
    sql += " ORDER BY CAST(source_area_id AS INTEGER)"

    return [
        Spot(
            area_id=int(row["source_area_id"]),
            prefecture=row["prefecture"],
            spot_name=row["spot_name"],
            lat=float(row["lat"]),
            lng=float(row["lng"]),
        )
        for row in connection.execute(sql, parameters)
    ]


def parse_fishing_page(html: str) -> list[CatchRecord]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".fishings-list-container")
    if container is None:
        raise ValueError("釣果一覧が見つかりません。ページ構造が変更された可能性があります。")

    records: list[CatchRecord] = []
    current_year_month: tuple[int, int] | None = None

    for element in container.find_all(["h3", "a"]):
        if element.name == "h3":
            match = YEAR_MONTH_PATTERN.search(element.get_text(" ", strip=True))
            if match:
                current_year_month = (int(match.group(1)), int(match.group(2)))
            continue

        href = element.get("href", "")
        fishing_match = FISHING_PATH_PATTERN.fullmatch(href)
        if fishing_match is None or current_year_month is None:
            continue

        day_element = element.select_one("h5.text-primary")
        if day_element is None:
            continue
        day_match = DAY_PATTERN.search(day_element.get_text(" ", strip=True))
        if day_match is None:
            continue

        year, month = current_year_month
        caught_date = date(year, month, int(day_match.group(1))).isoformat()
        fishing_id = fishing_match.group(1)
        fish_images = element.select(".carousel-wrap img[alt]")

        for image_index, image in enumerate(fish_images):
            fish_name = image.get("alt", "").strip()
            if not fish_name:
                continue

            image_url = image.get("src", "")
            result_match = RESULT_ID_PATTERN.search(image_url)
            if result_match:
                item_id = result_match.group(1)
            else:
                fallback = f"{fishing_id}:{image_index}:{fish_name}:{caught_date}"
                item_id = hashlib.sha256(fallback.encode("utf-8")).hexdigest()

            records.append(
                CatchRecord(
                    source_fishing_id=fishing_id,
                    source_item_id=item_id,
                    fish_name=fish_name,
                    caught_date=caught_date,
                )
            )

    return records


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one('a[rel~="next"][href*="/fishings"]') is not None


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.8",
        }
    )
    return session


def fetch_page(
    session: requests.Session,
    url: str,
    timeout: float,
    pacer: RequestPacer,
    retries: int = 3,
) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            pacer.wait()
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt + 1 < retries:
                retry_after = None
                if error.response is not None:
                    retry_after = error.response.headers.get("Retry-After")
                pacer.backoff(attempt, retry_after)
    assert last_error is not None
    raise last_error


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, object],
    timeout: float,
    pacer: RequestPacer,
    retries: int = 3,
) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            pacer.wait()
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("エリアAPIが配列以外のデータを返しました。")
            return payload
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt + 1 < retries:
                retry_after = None
                if isinstance(error, requests.RequestException) and error.response is not None:
                    retry_after = error.response.headers.get("Retry-After")
                pacer.backoff(attempt, retry_after)
    assert last_error is not None
    raise last_error


def upsert_spot(connection: sqlite3.Connection, spot: Spot) -> int:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO spots (
            source, source_area_id, prefecture, spot_name, lat, lng,
            source_url, created_at, updated_at
        )
        VALUES ('anglers', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_area_id) DO UPDATE SET
            prefecture = excluded.prefecture,
            spot_name = excluded.spot_name,
            lat = excluded.lat,
            lng = excluded.lng,
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        (
            str(spot.area_id),
            spot.prefecture,
            spot.spot_name,
            spot.lat,
            spot.lng,
            spot.source_url,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM spots WHERE source = 'anglers' AND source_area_id = ?",
        (str(spot.area_id),),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def discover_spots(
    connection: sqlite3.Connection,
    *,
    max_pages: int,
    timeout: float,
    center_lat: float,
    center_lng: float,
    pacer: RequestPacer,
) -> tuple[int, int]:
    session = build_session()
    discovered_ids: set[int] = set()
    pages_collected = 0
    try:
        for page_number in range(1, max_pages + 1):
            print(f"[ポイント発見] {page_number}ページ")
            items = fetch_json(
                session,
                AREA_DISCOVERY_URL,
                params={
                    "page": page_number,
                    "lat": center_lat,
                    "lng": center_lng,
                },
                timeout=timeout,
                pacer=pacer,
            )
            pages_collected += 1
            if not items:
                break

            with connection:
                for item in items:
                    area_id = int(item["id"])
                    prefecture = item.get("prefecture") or {}
                    lat = item.get("lat")
                    lng = item.get("lng")
                    if lat is None or lng is None:
                        print(f"[ポイント発見] area_id={area_id} は座標なしで登録")
                    spot = Spot(
                        area_id=area_id,
                        prefecture=str(prefecture.get("name") or "不明"),
                        spot_name=str(item["name"]).strip(),
                        lat=float(lat) if lat is not None else 0.0,
                        lng=float(lng) if lng is not None else 0.0,
                    )
                    upsert_spot(connection, spot)
                    discovered_ids.add(area_id)

            if len(items) < 20:
                break
    finally:
        session.close()

    return pages_collected, len(discovered_ids)


def insert_records(
    connection: sqlite3.Connection,
    spot_id: int,
    records: Iterable[CatchRecord],
) -> int:
    inserted = 0
    collected_at = datetime.now(timezone.utc).isoformat()
    for record in records:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO catches (
                spot_id, source, source_fishing_id, source_item_id,
                fish_name, caught_date, collected_at
            )
            VALUES (?, 'anglers', ?, ?, ?, ?, ?)
            """,
            (
                spot_id,
                record.source_fishing_id,
                record.source_item_id,
                record.fish_name,
                record.caught_date,
                collected_at,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def get_collection_progress(
    connection: sqlite3.Connection,
    spot: Spot,
) -> tuple[int, bool]:
    row = connection.execute(
        """
        SELECT next_page, is_complete
        FROM collection_progress
        WHERE source = 'anglers' AND source_area_id = ?
        """,
        (str(spot.area_id),),
    ).fetchone()
    if row is None:
        return 1, False
    return int(row["next_page"]), bool(row["is_complete"])


def save_collection_progress(
    connection: sqlite3.Connection,
    spot: Spot,
    *,
    next_page: int,
    is_complete: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO collection_progress (
            source, source_area_id, next_page, is_complete, updated_at
        )
        VALUES ('anglers', ?, ?, ?, ?)
        ON CONFLICT(source, source_area_id) DO UPDATE SET
            next_page = excluded.next_page,
            is_complete = excluded.is_complete,
            updated_at = excluded.updated_at
        """,
        (
            str(spot.area_id),
            next_page,
            int(is_complete),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def start_run(connection: sqlite3.Connection, spot: Spot, pages: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO collection_runs (
            source, source_area_id, started_at, pages_requested, status
        )
        VALUES ('anglers', ?, ?, ?, 'running')
        """,
        (str(spot.area_id), datetime.now(timezone.utc).isoformat(), pages),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    pages_collected: int,
    records_found: int,
    records_inserted: int,
    status: str,
    error_message: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE collection_runs
        SET finished_at = ?, pages_collected = ?, records_found = ?,
            records_inserted = ?, status = ?, error_message = ?
        WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            pages_collected,
            records_found,
            records_inserted,
            status,
            error_message,
            run_id,
        ),
    )
    connection.commit()


def collect_spot(
    connection: sqlite3.Connection,
    spot: Spot,
    *,
    pages: int,
    timeout: float,
    stop_before: date | None,
    pacer: RequestPacer,
    start_page: int = 1,
    save_progress: bool = False,
) -> tuple[int, int, int]:
    spot_id = upsert_spot(connection, spot)
    connection.commit()
    run_id = start_run(connection, spot, pages)
    session = build_session()
    pages_collected = 0
    records_found = 0
    records_inserted = 0

    try:
        page_number = start_page
        while pages == 0 or page_number < start_page + pages:
            url = spot.source_url
            if page_number > 1:
                url = f"{url}?page={page_number}"
            print(f"[{spot.spot_name}] {url}")

            html = fetch_page(session, url, timeout, pacer)
            records = parse_fishing_page(html)
            pages_collected += 1
            records_found += len(records)

            with connection:
                records_inserted += insert_records(connection, spot_id, records)

            next_page_exists = bool(records) and has_next_page(html)
            if save_progress:
                with connection:
                    save_collection_progress(
                        connection,
                        spot,
                        next_page=page_number + 1,
                        is_complete=not next_page_exists,
                    )

            if not next_page_exists:
                break

            oldest = min(date.fromisoformat(record.caught_date) for record in records)
            if stop_before is not None and oldest < stop_before:
                break
            page_number += 1

        finish_run(
            connection,
            run_id,
            pages_collected=pages_collected,
            records_found=records_found,
            records_inserted=records_inserted,
            status="completed",
        )
        return pages_collected, records_found, records_inserted
    except Exception as error:
        finish_run(
            connection,
            run_id,
            pages_collected=pages_collected,
            records_found=records_found,
            records_inserted=records_inserted,
            status="failed",
            error_message=str(error),
        )
        raise
    finally:
        session.close()


def print_status(connection: sqlite3.Connection) -> None:
    summary = connection.execute(
        """
        SELECT
            COUNT(DISTINCT spot_id) AS spots,
            COUNT(*) AS catches,
            MIN(caught_date) AS oldest,
            MAX(caught_date) AS newest
        FROM catches
        """
    ).fetchone()
    catalog = connection.execute(
        """
        SELECT
            COUNT(*) AS discovered,
            SUM(CASE WHEN p.is_complete = 1 THEN 1 ELSE 0 END) AS completed
        FROM spots AS s
        LEFT JOIN collection_progress AS p
          ON p.source = s.source AND p.source_area_id = s.source_area_id
        WHERE s.source = 'anglers'
        """
    ).fetchone()
    print(
        json.dumps(
            {
                "discovered_spots": catalog["discovered"],
                "full_collection_completed_spots": catalog["completed"] or 0,
                "spots": summary["spots"],
                "catches": summary["catches"],
                "oldest": summary["oldest"],
                "newest": summary["newest"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ANGLERSの公開釣果一覧を収集してSQLiteへ保存します。"
    )
    parser.add_argument(
        "command",
        choices=("collect", "collect-all", "discover", "init", "status", "update-all"),
        nargs="?",
        default="collect",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLiteデータベースの保存先",
    )
    parser.add_argument(
        "--spots",
        type=Path,
        default=DEFAULT_SPOTS,
        help="収集対象ポイント設定JSON",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="各ポイントで取得する最大ページ数。0は公開一覧の終端まで",
    )
    parser.add_argument(
        "--discovery-pages",
        type=int,
        default=1000,
        help="ポイント発見APIで取得する最大ページ数",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="すべてのHTTPリクエスト間で最低限待機する秒数",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=1.0,
        help="待機時間へ0秒から指定秒数までのランダムな揺らぎを追加",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTPリクエストのタイムアウト秒数",
    )
    parser.add_argument(
        "--stop-before",
        type=date.fromisoformat,
        help="この日付より古いレコードが現れたページで収集を終了する日付（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--area-id",
        type=int,
        help="指定したANGLERSエリアIDだけを処理",
    )
    parser.add_argument(
        "--start-area-id",
        type=int,
        help="このANGLERSエリアID以上のポイントだけを処理",
    )
    parser.add_argument(
        "--end-area-id",
        type=int,
        help="このANGLERSエリアID以下のポイントだけを処理",
    )
    parser.add_argument(
        "--restart-complete",
        action="store_true",
        help="公開釣行記の全ページ収集が完了済みのポイントも1ページ目から再処理",
    )
    parser.add_argument(
        "--center-lat",
        type=float,
        default=36.7,
        help="ポイント発見APIの距離順ソートに使う中心緯度",
    )
    parser.add_argument(
        "--center-lng",
        type=float,
        default=137.2,
        help="ポイント発見APIの距離順ソートに使う中心経度",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.pages < 0:
        print("--pages は0以上を指定してください。", file=sys.stderr)
        return 2
    if args.discovery_pages < 1:
        print("--discovery-pages は1以上を指定してください。", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay は0以上を指定してください。", file=sys.stderr)
        return 2
    if args.jitter < 0:
        print("--jitter は0以上を指定してください。", file=sys.stderr)
        return 2

    connection = connect_database(args.database)
    pacer = RequestPacer(args.delay, args.jitter)
    try:
        if args.command == "init":
            print(f"データベースを初期化しました: {args.database}")
            return 0
        if args.command == "status":
            print_status(connection)
            return 0
        if args.command == "discover":
            pages, spots = discover_spots(
                connection,
                max_pages=args.discovery_pages,
                timeout=args.timeout,
                center_lat=args.center_lat,
                center_lng=args.center_lng,
                pacer=pacer,
            )
            print(f"ポイント発見完了: {pages}ページ、{spots}ポイント")
            print_status(connection)
            return 0

        if args.command in {"collect-all", "update-all"}:
            spots = load_discovered_spots(
                connection,
                args.area_id,
                args.start_area_id,
                args.end_area_id,
            )
        else:
            spots = load_spots(args.spots)
            if args.area_id is not None:
                spots = [spot for spot in spots if spot.area_id == args.area_id]
        if not spots:
            print("有効な収集対象ポイントがありません。", file=sys.stderr)
            return 2

        total_inserted = 0
        skipped = 0
        failed = 0
        for spot in spots:
            start_page = 1
            save_progress = args.command == "collect-all"
            if save_progress:
                next_page, is_complete = get_collection_progress(connection, spot)
                if is_complete and not args.restart_complete:
                    skipped += 1
                    continue
                start_page = 1 if args.restart_complete else next_page

            try:
                pages, found, inserted = collect_spot(
                    connection,
                    spot,
                    pages=args.pages,
                    timeout=args.timeout,
                    stop_before=args.stop_before,
                    pacer=pacer,
                    start_page=start_page,
                    save_progress=save_progress,
                )
            except Exception as error:
                failed += 1
                print(f"[{spot.spot_name}] 収集失敗: {error}", file=sys.stderr)
                if args.command not in {"collect-all", "update-all"}:
                    raise
                continue

            total_inserted += inserted
            print(
                f"[{spot.spot_name}] {pages}ページ、{found}件検出、"
                f"{inserted}件を新規保存"
            )
        print(
            f"収集完了: 新規保存 {total_inserted}件、"
            f"完了済みスキップ {skipped}ポイント、失敗 {failed}ポイント"
        )
        print_status(connection)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
