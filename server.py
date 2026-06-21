#!/usr/bin/env python3
"""Serve the static site and aggregated fishing statistics API."""

from __future__ import annotations

import argparse
import json
import sqlite3
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DATABASE = Path("data/anglers_catches.db")
DEFAULT_STATISTICS_JSON = Path("data/statistics.json")
PUBLIC_PATHS = {
    "/",
    "/index.html",
    "/robots.txt",
    "/styles.css",
    "/japan-map-data.js",
    "/app.js",
    "/data/fish_illustrations.json",
    "/data/statistics.json",
}
PUBLIC_PREFIXES = ("/data/fish_slices/",)
DEFAULT_MIN_SPOT_COUNT = 100


def query_statistics(database: Path) -> dict:
    if not database.exists():
        return {"spots": [], "catches": [], "metadata": {"oldest": None, "newest": None}}

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        spots = [
            {
                "id": str(row["id"]),
                "prefecture": row["prefecture"],
                "spot_name": row["spot_name"],
                "lat": row["lat"],
                "lng": row["lng"],
            }
            for row in connection.execute(
                """
                SELECT DISTINCT s.id, s.prefecture, s.spot_name, s.lat, s.lng
                FROM spots AS s
                JOIN catches AS c ON c.spot_id = s.id
                WHERE s.lat BETWEEN 20 AND 50
                  AND s.lng BETWEEN 120 AND 155
                ORDER BY s.prefecture, s.spot_name
                """
            )
        ]

        catches = [
            {
                "spot_id": str(row["spot_id"]),
                "fish_name": row["fish_name"],
                "month": row["month"],
                "count": row["count"],
            }
            for row in connection.execute(
                """
                SELECT
                    c.spot_id,
                    s.prefecture,
                    s.spot_name,
                    s.lat,
                    s.lng,
                    c.fish_name,
                    CAST(strftime('%Y', c.caught_date) AS INTEGER) AS year,
                    CAST(strftime('%m', c.caught_date) AS INTEGER) AS month,
                    COUNT(*) AS count
                FROM catches AS c
                JOIN spots AS s ON s.id = c.spot_id
                WHERE s.lat BETWEEN 20 AND 50
                  AND s.lng BETWEEN 120 AND 155
                GROUP BY c.spot_id, c.fish_name, year, month
                ORDER BY c.spot_id, c.fish_name, year, month
                """
            )
        ]

        period = connection.execute(
            "SELECT MIN(caught_date) AS oldest, MAX(caught_date) AS newest FROM catches"
        ).fetchone()
        return {
            "spots": spots,
            "catches": catches,
            "metadata": {"oldest": period["oldest"], "newest": period["newest"]},
        }
    finally:
        connection.close()


def export_statistics(database: Path, output: Path) -> None:
    payload = query_statistics(database)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"statistics_json={output}")
    print(f"spots={len(payload['spots'])}")
    print(f"catch_groups={len(payload['catches'])}")
    metadata = payload.get("metadata", {})
    print(f"period={metadata.get('oldest')}..{metadata.get('newest')}")


def print_startup_summary(database: Path) -> None:
    if not database.exists():
        print(f"database={database} not found")
        return

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        period = connection.execute(
            "SELECT MIN(caught_date) AS oldest, MAX(caught_date) AS newest FROM catches"
        ).fetchone()
        total_catches = connection.execute(
            "SELECT COUNT(*) AS count FROM catches"
        ).fetchone()["count"]
        total_spots = connection.execute(
            "SELECT COUNT(DISTINCT spot_id) AS count FROM catches"
        ).fetchone()["count"]
        visible_spots = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT spot_id
                FROM catches
                GROUP BY spot_id
                HAVING COUNT(*) >= ?
            )
            """,
            (DEFAULT_MIN_SPOT_COUNT,),
        ).fetchone()["count"]
        print(f"database={database}")
        print(f"period={period['oldest']}..{period['newest']}")
        print(f"catches={total_catches}")
        print(f"spots={total_spots}")
        print(
            f"visible_spots(min_count={DEFAULT_MIN_SPOT_COUNT})={visible_spots}"
        )
    finally:
        connection.close()


def make_handler(database: Path):
    class FishingRequestHandler(SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            request_path = urlparse(self.path).path
            if request_path in PUBLIC_PATHS or request_path.startswith(PUBLIC_PREFIXES):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:
            request_path = urlparse(self.path).path
            if request_path == "/api/statistics":
                try:
                    payload = query_statistics(database)
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (sqlite3.Error, OSError) as error:
                    body = json.dumps(
                        {"error": str(error)}, ensure_ascii=False
                    ).encode("utf-8")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return
            if request_path in PUBLIC_PATHS or request_path.startswith(PUBLIC_PREFIXES):
                super().do_GET()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return FishingRequestHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="釣果統計サイトを起動します。")
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けホスト")
    parser.add_argument("--port", type=int, default=8000, help="待ち受けポート")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLiteデータベースの保存先",
    )
    parser.add_argument(
        "--export-statistics",
        type=Path,
        default=None,
        help="集計済みJSONを書き出して終了する保存先。静的公開用。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.export_statistics is not None:
        export_statistics(args.database, args.export_statistics)
        return
    print_startup_summary(args.database)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.database),
    )
    print(f"Serving on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
