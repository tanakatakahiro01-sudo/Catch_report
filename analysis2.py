#!/usr/bin/env python3
"""Collect public ANGLERS catch records and store normalized facts in SQLite."""
from __future__ import annotations


"""
使い方

1. 全国・全期間の詳細な釣果を集める
python3 analysis2.py collect --database data/anglers_catches.db --delay 10 --jitter 5
公開サイトマップを先頭から処理し、釣果詳細ページからポイントも自動登録します。

2. まず直近約4か月の簡易情報だけを素早く集める
python3 analysis2.py collect --database data/anglers_catches.db --pages 0 --delay 1 --jitter 1 --detail-mode skip
魚種、日付、ポイント、元ページURLだけを釣行一覧から保存します。

3. 状況確認
python3 analysis2.py status --database data/anglers_catches.db
件数、最古日、最新日、サイズあり件数などが表示されます。

少数で試す場合
python3 analysis2.py collect --database data/anglers_catches.db --catch-limit 10 --delay 10 --jitter 5

途中で止まった場合

同じコマンドをもう一度実行すれば、続きから再開します。

python3 analysis2.py collect --database data/anglers_catches.db --delay 10 --jitter 5

`recent`は`collect --detail-mode skip`の別名です。
"""
#!/usr/bin/env python3
"""Collect ANGLERS catch records via accessible /fishings index pages."""


import argparse
import gzip
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://anglers.jp"
AREA_DISCOVERY_URL = f"{BASE_URL}/api/v2/areas/list_by_location.json"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap"
DEFAULT_DATABASE = Path("data/anglers_catches.db")
DEFAULT_SPOTS = Path("spots.json")
USER_AGENT = "FishingStatisticsCollector/1.0"
SAMPLE_PERCENT_SCALE = 100
SAMPLE_BUCKETS = 100 * SAMPLE_PERCENT_SCALE

YEAR_MONTH_PATTERN = re.compile(r"(\d{4})年\s*(\d{1,2})月")
DAY_PATTERN = re.compile(r"(\d{1,2})日")
FISHING_PATH_PATTERN = re.compile(r"^/fishings/(\d+)$")
RESULT_ID_PATTERN = re.compile(r"/result/(\d+)/")
CATCH_PATH_PATTERN = re.compile(r"/catches/(\d+)")
AREA_PATH_PATTERN = re.compile(r"^/areas/(\d+)$")
SIZE_CM_PATTERN = re.compile(r"([\d.]+)\s*cm")
WEIGHT_G_PATTERN = re.compile(r"([\d.]+)\s*g")
QUANTITY_PATTERN = re.compile(r"(\d+)\s*匹")
CAUGHT_AT_PATTERN = re.compile(
    r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}):(\d{2})"
)
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
ARCHIVE_BEFORE_START = "期間外（開始日前）"


@dataclass(frozen=True)
class Spot:
    area_id: int
    prefecture: str
    spot_name: str
    lat: float
    lng: float

    @property
    def fishings_url(self) -> str:
        return f"{BASE_URL}/areas/{self.area_id}/fishings"


@dataclass(frozen=True)
class CatchCandidate:
    source_catch_id: str
    fallback_fish_name: str | None
    fallback_caught_date: str | None
    source_url: str


@dataclass(frozen=True)
class CatchRecord:
    source_catch_id: str
    fish_name: str
    caught_at: str | None
    caught_date: str
    prefecture: str | None
    area_name: str | None
    size_cm: float | None
    weight_g: float | None
    quantity: int | None
    source_url: str


class ArchiveCatchSkipped(ValueError):
    """Raised when a public catch cannot be associated with a map area."""


def is_recoverable_http_error(error: requests.HTTPError) -> bool:
    if error.response is None:
        return False
    return 400 <= error.response.status_code < 600


def describe_request_error(error: requests.RequestException) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return str(error.response.status_code)
    return f"通信失敗({type(error).__name__})"


class RequestPacer:
    def __init__(self, delay: float, jitter: float) -> None:
        self.delay = delay
        self.jitter = jitter
        self.last_request_at: float | None = None

    def wait(self) -> None:
        if self.last_request_at is not None:
            interval = self.delay + random.random() * self.jitter
            elapsed = time.monotonic() - self.last_request_at
            wait_seconds = max(0.0, interval - elapsed)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        self.last_request_at = time.monotonic()

    def backoff(self, attempt: int, retry_after: str | None = None) -> None:
        retry_seconds = 0.0
        if retry_after:
            try:
                retry_seconds = max(0.0, float(retry_after))
            except ValueError:
                retry_seconds = 0.0
        wait_seconds = max(retry_seconds, float(2**attempt), self.delay)
        wait_seconds += random.random() * self.jitter
        time.sleep(wait_seconds)


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
            source_item_id TEXT NOT NULL,
            fish_name TEXT NOT NULL,
            caught_date TEXT NOT NULL,
            caught_at TEXT,
            prefecture TEXT,
            area_name TEXT,
            size_cm REAL,
            weight_g REAL,
            quantity INTEGER,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            UNIQUE(source, source_item_id)
        );

        CREATE INDEX IF NOT EXISTS catches_spot_date_idx
            ON catches(spot_id, caught_date);

        CREATE INDEX IF NOT EXISTS catches_fish_date_idx
            ON catches(fish_name, caught_date);

        CREATE TABLE IF NOT EXISTS collection_progress (
            source TEXT NOT NULL,
            source_area_id TEXT NOT NULL,
            next_page INTEGER NOT NULL DEFAULT 1,
            is_complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source, source_area_id)
        );

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

        CREATE TABLE IF NOT EXISTS sitemap_progress (
            source TEXT NOT NULL,
            sitemap_number INTEGER NOT NULL,
            next_url_index INTEGER NOT NULL DEFAULT 0,
            is_complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source, sitemap_number)
        );

        """
    )

    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(catches)")
    }
    columns_to_add = {
        "caught_at": "TEXT",
        "prefecture": "TEXT",
        "area_name": "TEXT",
        "size_cm": "REAL",
        "weight_g": "REAL",
        "quantity": "INTEGER",
        "source_url": "TEXT",
    }
    for column_name, column_type in columns_to_add.items():
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE catches ADD COLUMN {column_name} {column_type}")

    connection.commit()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
            "Connection": "keep-alive",
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


def fetch_content(
    session: requests.Session,
    url: str,
    timeout: float,
    pacer: RequestPacer,
    retries: int = 3,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            pacer.wait()
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as error:
            last_error = error
            if error.response is not None and error.response.status_code == 404:
                raise
            if attempt + 1 < retries:
                retry_after = None
                if error.response is not None:
                    retry_after = error.response.headers.get("Retry-After")
                pacer.backoff(attempt, retry_after)

    assert last_error is not None
    raise last_error


def load_spots(path: Path) -> list[Spot]:
    with path.open(encoding="utf-8") as source:
        items = json.load(source)

    spots: list[Spot] = []
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
    params: list[object] = []

    if area_id is not None:
        sql += " AND source_area_id = ?"
        params.append(str(area_id))
    if start_area_id is not None:
        sql += " AND CAST(source_area_id AS INTEGER) >= ?"
        params.append(start_area_id)
    if end_area_id is not None:
        sql += " AND CAST(source_area_id AS INTEGER) <= ?"
        params.append(end_area_id)

    sql += " ORDER BY CAST(source_area_id AS INTEGER)"

    return [
        Spot(
            area_id=int(row["source_area_id"]),
            prefecture=row["prefecture"],
            spot_name=row["spot_name"],
            lat=float(row["lat"]),
            lng=float(row["lng"]),
        )
        for row in connection.execute(sql, tuple(params))
    ]


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
            spot.fishings_url,
            now,
            now,
        ),
    )

    row = connection.execute(
        """
        SELECT id
        FROM spots
        WHERE source = 'anglers'
          AND source_area_id = ?
        """,
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
                params={"page": page_number, "lat": center_lat, "lng": center_lng},
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


def has_next_fishing_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one('a[rel~="next"][href*="/fishings"]') is not None


def parse_fishing_index(html: str) -> list[CatchCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".fishings-list-container")

    if container is None:
        raise ValueError("釣行記一覧が見つかりません。ページ構造が変更された可能性があります。")

    candidates: list[CatchCandidate] = []
    seen: set[str] = set()
    current_year_month: tuple[int, int] | None = None

    for element in container.find_all(["h3", "a"]):
        if element.name == "h3":
            match = YEAR_MONTH_PATTERN.search(element.get_text(" ", strip=True))
            if match:
                current_year_month = (int(match.group(1)), int(match.group(2)))
            continue

        href = element.get("href", "")
        if FISHING_PATH_PATTERN.fullmatch(href) is None:
            continue

        fallback_date: str | None = None
        day_element = element.select_one("h5.text-primary")
        if day_element is not None and current_year_month is not None:
            day_match = DAY_PATTERN.search(day_element.get_text(" ", strip=True))
            if day_match:
                year, month = current_year_month
                fallback_date = date(year, month, int(day_match.group(1))).isoformat()

        for image_index, image in enumerate(element.select(".carousel-wrap img[alt]")):
            fish_name = image.get("alt", "").strip() or None
            image_url = image.get("src", "")
            result_match = RESULT_ID_PATTERN.search(image_url)

            if result_match:
                catch_id = result_match.group(1)
            else:
                raw = f"{href}:{image_index}:{fish_name}:{fallback_date}:{image_url}"
                catch_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()

            if catch_id in seen:
                continue

            seen.add(catch_id)

            # result id is usually identical to catch detail id.
            if catch_id.isdigit():
                source_url = f"{BASE_URL}/catches/{catch_id}"
            else:
                source_url = f"{BASE_URL}{href}"

            candidates.append(
                CatchCandidate(
                    source_catch_id=catch_id,
                    fallback_fish_name=fish_name,
                    fallback_caught_date=fallback_date,
                    source_url=source_url,
                )
            )

    return candidates


def extract_field_from_text(page_text: str, label: str) -> str | None:
    labels = [
        "釣れた日",
        "魚種",
        "サイズ",
        "重さ",
        "匹数",
        "都道府県",
        "エリア",
        "マップの中心",
        "ルアー",
        "タックル",
        "状況",
        "天気",
        "潮位",
        "水温",
    ]

    start = page_text.find(label)
    if start < 0:
        return None

    start += len(label)
    end_candidates = []

    for next_label in labels:
        if next_label == label:
            continue
        pos = page_text.find(next_label, start)
        if pos >= 0:
            end_candidates.append(pos)

    end = min(end_candidates) if end_candidates else len(page_text)
    value = page_text[start:end].strip()
    value = re.sub(r"\n+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def parse_japanese_caught_at(text: str) -> tuple[str, str] | tuple[None, None]:
    match = CAUGHT_AT_PATTERN.search(text)
    if match is None:
        return None, None

    year, month, day, hour, minute = map(int, match.groups())
    caught_dt = datetime(year, month, day, hour, minute)
    return caught_dt.isoformat(timespec="minutes"), caught_dt.date().isoformat()


def parse_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if match is None:
        return None
    return float(match.group(1))


def parse_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    if match is None:
        return None
    return int(match.group(1))


def clean_field(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value.strip())
    return value or None


def parse_catch_detail_or_fallback(
    html: str | None,
    candidate: CatchCandidate,
    spot: Spot,
) -> CatchRecord:
    if html is None:
        if candidate.fallback_fish_name is None or candidate.fallback_caught_date is None:
            raise ValueError(f"詳細なしで保存できる最低項目が不足しています: {candidate.source_url}")

        return CatchRecord(
            source_catch_id=candidate.source_catch_id,
            fish_name=candidate.fallback_fish_name,
            caught_at=None,
            caught_date=candidate.fallback_caught_date,
            prefecture=spot.prefecture,
            area_name=spot.spot_name,
            size_cm=None,
            weight_g=None,
            quantity=None,
            source_url=candidate.source_url,
        )

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)

    caught_at_text = extract_field_from_text(page_text, "釣れた日")
    fish_name = extract_field_from_text(page_text, "魚種")
    size_text = extract_field_from_text(page_text, "サイズ") or ""
    weight_text = extract_field_from_text(page_text, "重さ") or ""
    quantity_text = extract_field_from_text(page_text, "匹数") or ""
    prefecture = extract_field_from_text(page_text, "都道府県")
    area_name = extract_field_from_text(page_text, "エリア")

    caught_at: str | None = None
    caught_date: str | None = None

    if caught_at_text:
        caught_at, caught_date = parse_japanese_caught_at(caught_at_text)

    if caught_date is None:
        caught_date = candidate.fallback_caught_date

    if fish_name is None:
        fish_name = candidate.fallback_fish_name

    if caught_date is None:
        raise ValueError(f"釣れた日を取得できません: {candidate.source_url}")

    if fish_name is None:
        raise ValueError(f"魚種を取得できません: {candidate.source_url}")

    return CatchRecord(
        source_catch_id=candidate.source_catch_id,
        fish_name=clean_field(fish_name) or "",
        caught_at=caught_at,
        caught_date=caught_date,
        prefecture=clean_field(prefecture) or spot.prefecture,
        area_name=clean_field(area_name) or spot.spot_name,
        size_cm=parse_float(SIZE_CM_PATTERN, size_text),
        weight_g=parse_float(WEIGHT_G_PATTERN, weight_text),
        quantity=parse_int(QUANTITY_PATTERN, quantity_text),
        source_url=candidate.source_url,
    )


def parse_sitemap_urls(payload: bytes) -> list[str]:
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)

    root = ET.fromstring(payload)
    return [
        element.text.strip()
        for element in root.findall(".//{*}loc")
        if element.text and element.text.strip()
    ]


def years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, month=2, day=28)


def sample_percent_units(sample_percent: float) -> int:
    return round(sample_percent * SAMPLE_PERCENT_SCALE)


def should_sample_catch(catch_id: str, sample_percent: float) -> bool:
    units = sample_percent_units(sample_percent)
    if units >= SAMPLE_BUCKETS:
        return True

    numeric_id = int(catch_id)
    return (numeric_id * units) % SAMPLE_BUCKETS < units


def sample_progress_label(sample_percent: float) -> str:
    units = sample_percent_units(sample_percent)
    if units >= SAMPLE_BUCKETS:
        return "100"
    return f"{units / SAMPLE_PERCENT_SCALE:g}"


def update_boundary_outside_count(result: str, current_count: int) -> int:
    if result == ARCHIVE_BEFORE_START:
        return current_count + 1
    if ISO_DATE_PATTERN.fullmatch(result):
        return 0
    return current_count


def caught_date_from_detail_html(html: str) -> date:
    soup = BeautifulSoup(html, "html.parser")
    fields = field_values_from_catch_page(soup)
    caught_element = fields.get("釣れた日")
    if caught_element is None:
        raise ValueError("釣れた日がありません。")
    _, caught_date_value = parse_japanese_caught_at(
        caught_element.get_text(" ", strip=True)
    )
    if caught_date_value is None:
        raise ValueError("釣れた日を解析できません。")
    return date.fromisoformat(caught_date_value)


def find_recent_start(
    session: requests.Session,
    sitemap_urls: list[str],
    *,
    cutoff: date,
    timeout: float,
    pacer: RequestPacer,
) -> tuple[int, int]:
    sitemap_cache: dict[int, list[str]] = {}
    date_cache: dict[str, date] = {}

    def catch_urls_for(sitemap_number: int) -> list[str]:
        if sitemap_number not in sitemap_cache:
            urls = parse_sitemap_urls(
                fetch_content(
                    session,
                    sitemap_urls[sitemap_number - 1],
                    timeout,
                    pacer,
                )
            )
            sitemap_cache[sitemap_number] = [
                url for url in urls if CATCH_PATH_PATTERN.search(url) is not None
            ]
        return sitemap_cache[sitemap_number]

    def caught_date_for(url: str) -> date:
        if url not in date_cache:
            date_cache[url] = caught_date_from_detail_html(
                fetch_page(session, url, timeout, pacer)
            )
        return date_cache[url]

    low = 1
    high = len(sitemap_urls)
    last_catch_sitemap = 0
    while low <= high:
        middle = (low + high) // 2
        if catch_urls_for(middle):
            last_catch_sitemap = middle
            low = middle + 1
        else:
            high = middle - 1

    if last_catch_sitemap == 0:
        raise ValueError("サイトマップに釣果URLがありません。")

    low = 1
    high = last_catch_sitemap
    first_sitemap = last_catch_sitemap
    while low <= high:
        middle = (low + high) // 2
        urls = catch_urls_for(middle)
        if urls and caught_date_for(urls[-1]) >= cutoff:
            first_sitemap = middle
            high = middle - 1
        else:
            low = middle + 1

    urls = catch_urls_for(first_sitemap)
    low = 0
    high = len(urls)
    while low < high:
        middle = (low + high) // 2
        if caught_date_for(urls[middle]) >= cutoff:
            high = middle
        else:
            low = middle + 1

    return first_sitemap, low


def field_values_from_catch_page(soup: BeautifulSoup) -> dict[str, BeautifulSoup]:
    heading = soup.find(["h2", "h3"], string=lambda value: value and "釣果データ" in value)
    if heading is None:
        raise ValueError("釣果データ欄が見つかりません。")

    data_list = heading.find_next("dl")
    if data_list is None:
        raise ValueError("釣果データの一覧が見つかりません。")

    fields: dict[str, BeautifulSoup] = {}
    for label_element in data_list.find_all("dt"):
        value_element = label_element.find_next_sibling("dd")
        if value_element is None:
            continue
        label = label_element.get_text(" ", strip=True)
        fields[label] = value_element
    return fields


def parse_leaflet_coordinates(soup: BeautifulSoup) -> tuple[float, float]:
    leaflet = soup.select_one('[data-react-class="commons/Leaflet"][data-react-props]')
    if leaflet is None:
        return 0.0, 0.0

    try:
        props = json.loads(leaflet.get("data-react-props", "{}"))
        return float(props.get("latitude") or 0.0), float(props.get("longitude") or 0.0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0, 0.0


def parse_archive_catch(html: str, catch_id: str) -> tuple[Spot, CatchRecord]:
    soup = BeautifulSoup(html, "html.parser")
    fields = field_values_from_catch_page(soup)

    caught_text = fields.get("釣れた日")
    fish_element = fields.get("魚種")
    area_element = fields.get("エリア")
    if caught_text is None:
        raise ArchiveCatchSkipped("釣れた日が未設定の釣果")
    if fish_element is None:
        raise ArchiveCatchSkipped("魚種が未設定の釣果")
    if area_element is None:
        raise ArchiveCatchSkipped("エリアが未設定の釣果")

    caught_at, caught_date = parse_japanese_caught_at(
        caught_text.get_text(" ", strip=True)
    )
    if caught_date is None:
        raise ArchiveCatchSkipped("釣れた日を解析できない釣果")

    fish_links = fish_element.select('a[href^="/fishes/"]')
    fish_name = (
        fish_links[-1].get_text(" ", strip=True)
        if fish_links
        else fish_element.get_text(" ", strip=True)
    )
    if not fish_name:
        raise ArchiveCatchSkipped("魚種が未設定の釣果")

    area_link = area_element.find("a", href=AREA_PATH_PATTERN)
    if area_link is None:
        raise ArchiveCatchSkipped("公開エリアIDを持たない釣果")

    area_match = AREA_PATH_PATTERN.fullmatch(area_link.get("href", ""))
    assert area_match is not None
    area_id = int(area_match.group(1))
    area_name = area_link.get_text(" ", strip=True)

    prefecture_element = fields.get("都道府県")
    prefecture = (
        prefecture_element.get_text(" ", strip=True)
        if prefecture_element is not None
        else "不明"
    )
    lat, lng = parse_leaflet_coordinates(soup)

    size_text = fields.get("サイズ")
    weight_text = fields.get("重さ")
    quantity_text = fields.get("匹数")
    source_url = f"{BASE_URL}/catches/{catch_id}"

    spot = Spot(
        area_id=area_id,
        prefecture=prefecture or "不明",
        spot_name=area_name,
        lat=lat,
        lng=lng,
    )
    record = CatchRecord(
        source_catch_id=catch_id,
        fish_name=fish_name,
        caught_at=caught_at,
        caught_date=caught_date,
        prefecture=prefecture or None,
        area_name=area_name,
        size_cm=parse_float(
            SIZE_CM_PATTERN,
            size_text.get_text(" ", strip=True) if size_text is not None else "",
        ),
        weight_g=parse_float(
            WEIGHT_G_PATTERN,
            weight_text.get_text(" ", strip=True) if weight_text is not None else "",
        ),
        quantity=parse_int(
            QUANTITY_PATTERN,
            quantity_text.get_text(" ", strip=True)
            if quantity_text is not None
            else "",
        ),
        source_url=source_url,
    )
    return spot, record


def get_sitemap_progress(
    connection: sqlite3.Connection,
    sitemap_number: int,
    progress_source: str,
) -> tuple[int, bool]:
    row = connection.execute(
        """
        SELECT next_url_index, is_complete
        FROM sitemap_progress
        WHERE source = ?
          AND sitemap_number = ?
        """,
        (progress_source, sitemap_number),
    ).fetchone()
    if row is None:
        return 0, False
    return int(row["next_url_index"]), bool(row["is_complete"])


def save_sitemap_progress(
    connection: sqlite3.Connection,
    sitemap_number: int,
    *,
    progress_source: str,
    next_url_index: int,
    is_complete: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO sitemap_progress (
            source, sitemap_number, next_url_index, is_complete, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source, sitemap_number) DO UPDATE SET
            next_url_index = excluded.next_url_index,
            is_complete = excluded.is_complete,
            updated_at = excluded.updated_at
        """,
        (
            progress_source,
            sitemap_number,
            next_url_index,
            int(is_complete),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_resume_sitemap(
    connection: sqlite3.Connection,
    progress_source: str,
) -> int | None:
    rows = connection.execute(
        """
        SELECT sitemap_number, is_complete
        FROM sitemap_progress
        WHERE source = ?
        ORDER BY sitemap_number
        """,
        (progress_source,),
    ).fetchall()
    if not rows:
        return None

    expected = int(rows[0]["sitemap_number"])
    for row in rows:
        sitemap_number = int(row["sitemap_number"])
        if sitemap_number > expected:
            return expected
        if not bool(row["is_complete"]):
            return sitemap_number
        expected = sitemap_number + 1
    return expected


def archive_catch_exists(connection: sqlite3.Connection, catch_id: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM catches
        WHERE source = 'anglers'
          AND source_item_id = ?
        LIMIT 1
        """,
        (catch_id,),
    ).fetchone()
    return row is not None


def store_archive_catch(
    connection: sqlite3.Connection,
    html: str,
    catch_id: str,
    *,
    archive_after: date | None,
    archive_before: date | None,
) -> tuple[bool, str]:
    parsed_spot, record = parse_archive_catch(html, catch_id)
    caught_date = date.fromisoformat(record.caught_date)

    if archive_after is not None and caught_date < archive_after:
        return False, ARCHIVE_BEFORE_START
    if archive_before is not None and caught_date >= archive_before:
        return False, "期間外（終了日以降）"

    existing_spot = load_discovered_spots(connection, area_id=parsed_spot.area_id)
    spot = existing_spot[0] if existing_spot else parsed_spot
    spot_id = upsert_spot(connection, spot)
    inserted = insert_catch_records(connection, spot_id, [record])
    return inserted > 0, record.caught_date


def collect_archive_catch_ids(
    connection: sqlite3.Connection,
    catch_ids: list[int],
    *,
    timeout: float,
    pacer: RequestPacer,
    archive_after: date | None,
    archive_before: date | None,
) -> tuple[int, int]:
    session = build_session()
    processed = 0
    inserted = 0
    try:
        for catch_id in catch_ids:
            url = f"{BASE_URL}/catches/{catch_id}"
            print(f"[履歴釣果] {url}")
            if archive_catch_exists(connection, str(catch_id)):
                processed += 1
                print(f"[履歴釣果] {catch_id}: 取得済み")
                continue

            html = fetch_page(session, url, timeout, pacer)
            try:
                with connection:
                    was_inserted, result = store_archive_catch(
                        connection,
                        html,
                        str(catch_id),
                        archive_after=archive_after,
                        archive_before=archive_before,
                    )
            except ArchiveCatchSkipped as error:
                was_inserted = False
                result = str(error)
            processed += 1
            inserted += int(was_inserted)
            print(f"[履歴釣果] {catch_id}: {result}")
    finally:
        session.close()
    return processed, inserted


def collect_archive_sitemaps(
    connection: sqlite3.Connection,
    *,
    sitemap_start: int,
    sitemap_end: int,
    catch_limit: int,
    timeout: float,
    pacer: RequestPacer,
    archive_after: date | None,
    archive_before: date | None,
    restart_complete: bool,
    recent_years: int | None,
    recent_sitemap_margin: int,
    recent_boundary_margin: int,
    sample_percent: float,
) -> tuple[int, int, int]:
    session = build_session()
    processed = 0
    inserted = 0
    completed_sitemaps = 0

    try:
        sitemap_urls = parse_sitemap_urls(
            fetch_content(session, SITEMAP_INDEX_URL, timeout, pacer)
        )
        first_url_index = 0
        scope_source = (
            f"anglers:recent-years:{recent_years}:"
            f"tail-margin:{recent_sitemap_margin}:"
            f"outside-margin:{recent_boundary_margin}"
            if recent_years is not None
            else "anglers"
        )
        sample_label = sample_progress_label(sample_percent)
        progress_source = (
            scope_source
            if sample_percent_units(sample_percent) >= SAMPLE_BUCKETS
            else f"{scope_source}:sample-percent:{sample_label}"
        )
        if sample_percent_units(sample_percent) < SAMPLE_BUCKETS:
            print(
                f"[抽出収集] 投稿IDを基準に{sample_label}%を周期抽出します。"
            )
        if recent_years is not None:
            assert archive_after is not None
            resume_sitemap = get_resume_sitemap(connection, progress_source)
            if resume_sitemap is None or restart_complete:
                estimated_sitemap, estimated_url_index = find_recent_start(
                    session,
                    sitemap_urls,
                    cutoff=archive_after,
                    timeout=timeout,
                    pacer=pacer,
                )
                sitemap_start = estimated_sitemap
                first_url_index = estimated_url_index
                if recent_sitemap_margin:
                    print(
                        f"[期間探索] 本収集はサイトマップ"
                        f"{estimated_sitemap}の{estimated_url_index + 1}件目から開始し、"
                        f"直前のサイトマップを末尾から確認します。"
                    )
                else:
                    print(
                        f"[期間探索] サイトマップ{estimated_sitemap}の"
                        f"{estimated_url_index + 1}件目から開始します。"
                    )

                consecutive_outside = 0
                boundary_confirmed = False
                for margin_offset in range(1, recent_sitemap_margin + 1):
                    boundary_sitemap = estimated_sitemap - margin_offset
                    if boundary_sitemap < 1:
                        break

                    boundary_url = sitemap_urls[boundary_sitemap - 1]
                    boundary_urls = parse_sitemap_urls(
                        fetch_content(session, boundary_url, timeout, pacer)
                    )
                    boundary_catch_urls = [
                        url
                        for url in boundary_urls
                        if CATCH_PATH_PATTERN.search(url) is not None
                    ]
                    print(
                        f"[境界確認] サイトマップ{boundary_sitemap}を"
                        f"末尾から確認します。"
                    )

                    for url_index in range(len(boundary_catch_urls) - 1, -1, -1):
                        if catch_limit > 0 and processed >= catch_limit:
                            return processed, inserted, completed_sitemaps

                        url = boundary_catch_urls[url_index]
                        catch_match = CATCH_PATH_PATTERN.search(url)
                        assert catch_match is not None
                        catch_id = catch_match.group(1)
                        if not should_sample_catch(catch_id, sample_percent):
                            continue

                        print(
                            f"[境界確認 {boundary_sitemap}] "
                            f"{url_index + 1}/{len(boundary_catch_urls)} {url}"
                        )
                        if archive_catch_exists(connection, catch_id):
                            print(f"[履歴釣果] {catch_id}: 取得済み")
                            continue

                        try:
                            html = fetch_page(session, url, timeout, pacer)
                            with connection:
                                was_inserted, result = store_archive_catch(
                                    connection,
                                    html,
                                    catch_id,
                                    archive_after=archive_after,
                                    archive_before=archive_before,
                                )
                        except ArchiveCatchSkipped as error:
                            result = str(error)
                            was_inserted = False
                        except requests.RequestException as error:
                            if (
                                isinstance(error, requests.HTTPError)
                                and not is_recoverable_http_error(error)
                            ):
                                raise
                            result = describe_request_error(error)
                            was_inserted = False

                        processed += 1
                        inserted += int(was_inserted)
                        print(f"[履歴釣果] {catch_id}: {result}")

                        previous_outside = consecutive_outside
                        consecutive_outside = update_boundary_outside_count(
                            result,
                            consecutive_outside,
                        )
                        if result == ARCHIVE_BEFORE_START:
                            print(
                                f"[境界確認] 連続期間外 "
                                f"{consecutive_outside}/{recent_boundary_margin}件"
                            )
                        elif ISO_DATE_PATTERN.fullmatch(result):
                            if previous_outside:
                                print(
                                    f"[境界確認] 期間内の釣果を確認したため、"
                                    f"連続期間外件数をリセットします。"
                                )
                            consecutive_outside = 0

                        if consecutive_outside >= recent_boundary_margin:
                            boundary_confirmed = True
                            print(
                                f"[境界確認] 開始日前の釣果が"
                                f"{recent_boundary_margin}件連続したため、"
                                f"逆順確認を終了します。"
                            )
                            break

                    if boundary_confirmed:
                        break
            else:
                sitemap_start = resume_sitemap
                print(
                    f"[再開] 保存済み進捗に基づき、"
                    f"サイトマップ{sitemap_start}から再開します。"
                )
        last_sitemap = len(sitemap_urls) if sitemap_end == 0 else min(
            sitemap_end, len(sitemap_urls)
        )

        for sitemap_number in range(sitemap_start, last_sitemap + 1):
            next_index, is_complete = get_sitemap_progress(
                connection, sitemap_number, progress_source
            )
            if is_complete and not restart_complete:
                continue
            if restart_complete:
                next_index = 0
            if sitemap_number == sitemap_start:
                next_index = max(next_index, first_url_index)

            sitemap_url = sitemap_urls[sitemap_number - 1]
            print(f"[サイトマップ {sitemap_number}] {sitemap_url}")
            urls = parse_sitemap_urls(
                fetch_content(session, sitemap_url, timeout, pacer)
            )
            catch_urls = [
                url for url in urls if CATCH_PATH_PATTERN.search(url) is not None
            ]

            if next_index >= len(catch_urls):
                with connection:
                    save_sitemap_progress(
                        connection,
                        sitemap_number,
                        progress_source=progress_source,
                        next_url_index=len(catch_urls),
                        is_complete=True,
                    )
                completed_sitemaps += 1
                continue

            for url_index in range(next_index, len(catch_urls)):
                if catch_limit > 0 and processed >= catch_limit:
                    return processed, inserted, completed_sitemaps

                url = catch_urls[url_index]
                catch_match = CATCH_PATH_PATTERN.search(url)
                assert catch_match is not None
                catch_id = catch_match.group(1)
                if not should_sample_catch(catch_id, sample_percent):
                    continue
                print(
                    f"[サイトマップ {sitemap_number}] "
                    f"{url_index + 1}/{len(catch_urls)} {url}"
                )
                if archive_catch_exists(connection, catch_id):
                    with connection:
                        save_sitemap_progress(
                            connection,
                            sitemap_number,
                            progress_source=progress_source,
                            next_url_index=url_index + 1,
                            is_complete=(url_index + 1 == len(catch_urls)),
                        )
                    processed += 1
                    print(f"[履歴釣果] {catch_id}: 取得済み")
                    continue

                try:
                    html = fetch_page(session, url, timeout, pacer)
                    with connection:
                        was_inserted, result = store_archive_catch(
                            connection,
                            html,
                            catch_id,
                            archive_after=archive_after,
                            archive_before=archive_before,
                        )
                        save_sitemap_progress(
                            connection,
                            sitemap_number,
                            progress_source=progress_source,
                            next_url_index=url_index + 1,
                            is_complete=(url_index + 1 == len(catch_urls)),
                        )
                except ArchiveCatchSkipped as error:
                    with connection:
                        save_sitemap_progress(
                            connection,
                            sitemap_number,
                            progress_source=progress_source,
                            next_url_index=url_index + 1,
                            is_complete=(url_index + 1 == len(catch_urls)),
                        )
                    result = str(error)
                    was_inserted = False
                except requests.RequestException as error:
                    if (
                        isinstance(error, requests.HTTPError)
                        and not is_recoverable_http_error(error)
                    ):
                        raise
                    with connection:
                        save_sitemap_progress(
                            connection,
                            sitemap_number,
                            progress_source=progress_source,
                            next_url_index=url_index + 1,
                            is_complete=(url_index + 1 == len(catch_urls)),
                        )
                    result = describe_request_error(error)
                    was_inserted = False

                processed += 1
                inserted += int(was_inserted)
                print(f"[履歴釣果] {catch_id}: {result}")

            with connection:
                save_sitemap_progress(
                    connection,
                    sitemap_number,
                    progress_source=progress_source,
                    next_url_index=len(catch_urls),
                    is_complete=True,
                )
            completed_sitemaps += 1

    finally:
        session.close()

    return processed, inserted, completed_sitemaps


def insert_catch_records(
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
                spot_id,
                source,
                source_item_id,
                fish_name,
                caught_date,
                caught_at,
                prefecture,
                area_name,
                size_cm,
                weight_g,
                quantity,
                source_url,
                collected_at
            )
            VALUES (?, 'anglers', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spot_id,
                record.source_catch_id,
                record.fish_name,
                record.caught_date,
                record.caught_at,
                record.prefecture,
                record.area_name,
                record.size_cm,
                record.weight_g,
                record.quantity,
                record.source_url,
                collected_at,
            ),
        )
        inserted += cursor.rowcount

    return inserted


def get_collection_progress(connection: sqlite3.Connection, spot: Spot) -> tuple[int, bool]:
    row = connection.execute(
        """
        SELECT next_page, is_complete
        FROM collection_progress
        WHERE source = 'anglers'
          AND source_area_id = ?
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
            source,
            source_area_id,
            next_page,
            is_complete,
            updated_at
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
            source,
            source_area_id,
            started_at,
            pages_requested,
            status
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
        SET finished_at = ?,
            pages_collected = ?,
            records_found = ?,
            records_inserted = ?,
            status = ?,
            error_message = ?
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
    start_page: int,
    detail_mode: str,
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
            url = spot.fishings_url
            if page_number > 1:
                url = f"{url}?page={page_number}"

            print(f"[{spot.spot_name}] {url}")

            html = fetch_page(session, url, timeout, pacer)
            candidates = parse_fishing_index(html)

            if not candidates:
                with connection:
                    save_collection_progress(
                        connection,
                        spot,
                        next_page=page_number,
                        is_complete=True,
                    )
                break

            page_records: list[CatchRecord] = []

            for candidate in candidates:
                detail_html: str | None = None

                if detail_mode in {"try", "required"} and candidate.source_catch_id.isdigit():
                    try:
                        detail_html = fetch_page(session, candidate.source_url, timeout, pacer)
                    except Exception as error:
                        if detail_mode == "required":
                            print(
                                f"[{spot.spot_name}] 詳細取得失敗: {candidate.source_url} {error}",
                                file=sys.stderr,
                            )
                            continue

                try:
                    record = parse_catch_detail_or_fallback(detail_html, candidate, spot)
                    page_records.append(record)
                except Exception as error:
                    print(
                        f"[{spot.spot_name}] 釣果解析失敗: {candidate.source_url} {error}",
                        file=sys.stderr,
                    )

            pages_collected += 1
            records_found += len(page_records)

            with connection:
                records_inserted += insert_catch_records(connection, spot_id, page_records)

            if not page_records:
                with connection:
                    save_collection_progress(
                        connection,
                        spot,
                        next_page=page_number + 1,
                        is_complete=False,
                    )
                page_number += 1
                continue

            oldest = min(date.fromisoformat(record.caught_date) for record in page_records)
            reached_stop_date = stop_before is not None and oldest < stop_before
            next_page_exists = has_next_fishing_page(html)

            with connection:
                save_collection_progress(
                    connection,
                    spot,
                    next_page=page_number + 1,
                    is_complete=(reached_stop_date or not next_page_exists),
                )

            if reached_stop_date or not next_page_exists:
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
            MAX(caught_date) AS newest,
            COUNT(size_cm) AS size_count,
            COUNT(weight_g) AS weight_count,
            COUNT(quantity) AS quantity_count
        FROM catches
        """
    ).fetchone()

    catalog = connection.execute(
        """
        SELECT COUNT(*) AS discovered
        FROM spots
        WHERE source = 'anglers'
        """
    ).fetchone()

    completed = connection.execute(
        """
        SELECT COUNT(*) AS completed
        FROM collection_progress
        WHERE source = 'anglers'
          AND is_complete = 1
        """
    ).fetchone()

    archive = connection.execute(
        """
        SELECT
            COUNT(*) AS started,
            SUM(CASE WHEN is_complete = 1 THEN 1 ELSE 0 END) AS completed,
            SUM(next_url_index) AS processed_urls
        FROM sitemap_progress
        WHERE source LIKE 'anglers%'
        """
    ).fetchone()

    archive_positions = [
        {
            "scope": row["source"],
            "sitemap": int(row["sitemap_number"]),
            "next_url_index": int(row["next_url_index"]),
            "is_complete": bool(row["is_complete"]),
        }
        for row in connection.execute(
            """
            SELECT source, sitemap_number, next_url_index, is_complete
            FROM sitemap_progress
            WHERE source LIKE 'anglers%'
            ORDER BY source, sitemap_number
            """
        )
    ]

    print(
        json.dumps(
            {
                "discovered_spots": catalog["discovered"] or 0,
                "completed_spots": completed["completed"] or 0,
                "spots": summary["spots"] or 0,
                "catches": summary["catches"] or 0,
                "oldest": summary["oldest"],
                "newest": summary["newest"],
                "size_count": summary["size_count"] or 0,
                "weight_count": summary["weight_count"] or 0,
                "quantity_count": summary["quantity_count"] or 0,
                "archive_sitemaps_started": archive["started"] or 0,
                "archive_sitemaps_completed": archive["completed"] or 0,
                "archive_processed_urls": archive["processed_urls"] or 0,
                "archive_positions": archive_positions,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ANGLERSの公開釣果を収集してSQLiteへ保存します。"
    )

    parser.add_argument(
        "command",
        choices=("collect", "recent", "discover", "init", "status"),
        nargs="?",
        default="collect",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--spots", type=Path, default=DEFAULT_SPOTS)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--discovery-pages", type=int, default=1000)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--jitter", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--stop-before", type=date.fromisoformat)
    parser.add_argument("--area-id", type=int)
    parser.add_argument("--start-area-id", type=int)
    parser.add_argument("--end-area-id", type=int)
    parser.add_argument("--restart-complete", action="store_true")
    parser.add_argument(
        "--catch-id",
        type=int,
        action="append",
        default=[],
        help="collectで指定した釣果IDだけを取得します。複数回指定できます。",
    )
    parser.add_argument(
        "--sitemap-start",
        type=int,
        default=1,
        help="collectを開始するサイトマップ番号です。",
    )
    parser.add_argument(
        "--sitemap-end",
        type=int,
        default=0,
        help="collectを終了するサイトマップ番号です。0は最後までです。",
    )
    parser.add_argument(
        "--catch-limit",
        type=int,
        default=0,
        help="1回のcollectで取得する釣果ページ数です。0は無制限です。",
    )
    parser.add_argument(
        "--archive-after",
        type=date.fromisoformat,
        help="この日付以降の履歴だけを保存します。",
    )
    parser.add_argument(
        "--archive-before",
        type=date.fromisoformat,
        help="この日付より前の履歴だけを保存します。",
    )
    parser.add_argument(
        "--recent-years",
        type=int,
        help="詳細収集の対象を今日から直近N年に限定します。",
    )
    parser.add_argument(
        "--recent-sitemap-margin",
        type=int,
        default=1,
        help=(
            "直近N年の推定境界より前を末尾から確認するサイトマップ数です。"
            "既定値は1です。"
        ),
    )
    parser.add_argument(
        "--recent-boundary-margin",
        type=int,
        default=10,
        help=(
            "逆順確認を終了するために必要な連続期間外件数です。"
            "期間内の釣果が現れると0件へ戻します。既定値は10です。"
        ),
    )
    parser.add_argument(
        "--sample-percent",
        type=float,
        default=100.0,
        help=(
            "サイトマップ詳細収集で開く投稿の割合です。"
            "URL末尾の投稿IDを基準に周期抽出します。既定値は100です。"
        ),
    )
    parser.add_argument("--center-lat", type=float, default=36.7)
    parser.add_argument("--center-lng", type=float, default=137.2)
    parser.add_argument(
        "--detail-mode",
        choices=("try", "skip", "required"),
        default="try",
        help=(
            "try: 詳細ページ取得を試し、失敗したら一覧情報で保存。"
            " skip: 詳細ページを取らず、一覧情報だけ保存。"
            " required: 詳細ページを取れたものだけ保存。"
        ),
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> int:
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
    if args.sitemap_start < 1:
        print("--sitemap-start は1以上を指定してください。", file=sys.stderr)
        return 2
    if args.sitemap_end < 0:
        print("--sitemap-end は0以上を指定してください。", file=sys.stderr)
        return 2
    if args.sitemap_end and args.sitemap_end < args.sitemap_start:
        print("--sitemap-end は --sitemap-start 以上を指定してください。", file=sys.stderr)
        return 2
    if args.catch_limit < 0:
        print("--catch-limit は0以上を指定してください。", file=sys.stderr)
        return 2
    if args.recent_years is not None and args.recent_years < 1:
        print("--recent-years は1以上を指定してください。", file=sys.stderr)
        return 2
    if args.recent_sitemap_margin < 0:
        print(
            "--recent-sitemap-margin は0以上で指定してください。",
            file=sys.stderr,
        )
        return 2
    if args.recent_boundary_margin < 1:
        print(
            "--recent-boundary-margin は1以上で指定してください。",
            file=sys.stderr,
        )
        return 2
    if (
        not math.isfinite(args.sample_percent)
        or args.sample_percent <= 0
        or args.sample_percent > 100
    ):
        print(
            "--sample-percent は0より大きく100以下で指定してください。",
            file=sys.stderr,
        )
        return 2
    if sample_percent_units(args.sample_percent) < 1:
        print(
            "--sample-percent は0.01以上で指定してください。",
            file=sys.stderr,
        )
        return 2
    if args.recent_years is not None and args.archive_after is not None:
        print(
            "--recent-years と --archive-after は同時に指定できません。",
            file=sys.stderr,
        )
        return 2
    if (
        args.archive_after is not None
        and args.archive_before is not None
        and args.archive_after >= args.archive_before
    ):
        print("--archive-after は --archive-before より前にしてください。", file=sys.stderr)
        return 2
    return 0


def load_collection_targets(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
) -> list[Spot]:
    spots = load_discovered_spots(
        connection,
        area_id=args.area_id,
        start_area_id=args.start_area_id,
        end_area_id=args.end_area_id,
    )

    if spots:
        return spots

    if args.area_id is None and args.start_area_id is None and args.end_area_id is None:
        if args.spots.exists():
            return load_spots(args.spots)

    return []


def main() -> int:
    args = parse_args()
    validation_result = validate_args(args)
    if validation_result != 0:
        return validation_result

    connection = connect_database(args.database)
    pacer = RequestPacer(args.delay, args.jitter)
    archive_after = (
        years_ago(date.today(), args.recent_years)
        if args.recent_years is not None
        else args.archive_after
    )
    if (
        archive_after is not None
        and args.archive_before is not None
        and archive_after >= args.archive_before
    ):
        print(
            "収集開始日は --archive-before より前にしてください。",
            file=sys.stderr,
        )
        connection.close()
        return 2

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

        simple_collection = (
            args.command == "recent"
            or (args.command == "collect" and args.detail_mode == "skip")
        )

        if args.command == "collect" and not simple_collection:
            if args.pages != 1:
                print(
                    "[注意] 詳細収集はサイトマップを処理するため、"
                    "--pagesは使用しません。"
                )
            if args.catch_id:
                processed, inserted = collect_archive_catch_ids(
                    connection,
                    args.catch_id,
                    timeout=args.timeout,
                    pacer=pacer,
                    archive_after=archive_after,
                    archive_before=args.archive_before,
                )
                scope_label = (
                    f"直近{args.recent_years}年"
                    if args.recent_years is not None
                    else "全期間"
                )
                print(
                    f"{scope_label}の詳細収集完了: "
                    f"{processed}件確認、{inserted}件を新規保存"
                )
            else:
                processed, inserted, completed_sitemaps = collect_archive_sitemaps(
                    connection,
                    sitemap_start=args.sitemap_start,
                    sitemap_end=args.sitemap_end,
                    catch_limit=args.catch_limit,
                    timeout=args.timeout,
                    pacer=pacer,
                    archive_after=archive_after,
                    archive_before=args.archive_before,
                    restart_complete=args.restart_complete,
                    recent_years=args.recent_years,
                    recent_sitemap_margin=args.recent_sitemap_margin,
                    recent_boundary_margin=args.recent_boundary_margin,
                    sample_percent=args.sample_percent,
                )
                scope_label = (
                    f"直近{args.recent_years}年"
                    if args.recent_years is not None
                    else "全期間"
                )
                print(
                    f"{scope_label}の詳細収集完了: "
                    f"{processed}件確認、{inserted}件を新規保存、"
                    f"{completed_sitemaps}サイトマップ完了"
                )
            print_status(connection)
            return 0

        assert simple_collection
        print(
            "[簡易収集] 詳細ページを開かず、魚種・日付・ポイント・"
            "元ページURLを保存します。対象期間は公開一覧にある直近約4か月です。"
        )
        spots = load_collection_targets(connection, args)
        if not spots:
            print(
                "有効な収集対象ポイントがありません。先に discover を実行してください。",
                file=sys.stderr,
            )
            return 2

        print(f"収集対象ポイント: {len(spots)}件")

        total_inserted = 0
        skipped = 0
        failed = 0

        for spot in spots:
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
                    detail_mode=args.detail_mode,
                )

            except Exception as error:
                failed += 1
                print(f"[{spot.spot_name}] 収集失敗: {error}", file=sys.stderr)
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
