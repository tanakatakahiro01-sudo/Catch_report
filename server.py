#!/usr/bin/env python3
"""Serve the static site and aggregated fishing statistics API."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from fish_name_aliases import load_fish_name_aliases, normalize_fish_name

DEFAULT_DATABASE = Path("data/anglers_catches.db")
DEFAULT_STATISTICS_JSON = Path("data/statistics.json")
DEFAULT_DETAIL_STATISTICS_JSON = Path("data/detail_statistics.json")
DEFAULT_NEXT_6H_DIR = Path("data/next6h")
DEFAULT_DETAIL_SPOTS_DIR = Path("data/detail_spots")
PUBLIC_PATHS = {
    "/",
    "/index.html",
    "/robots.txt",
    "/styles.css",
    "/japan-map-data.js",
    "/app.js",
    "/data/fish_illustrations.json",
    "/data/affiliate_url.json",
    "/data/affiliate_fallbacks.json",
    "/data/statistics.json",
    "/data/detail_statistics.json",
}
PUBLIC_PREFIXES = ("/data/fish_slices/", "/data/next6h/", "/data/detail_spots/")
DEFAULT_MIN_SPOT_COUNT = 100


def seasonal_period(day: int) -> str:
    if day <= 10:
        return "early"
    if day <= 20:
        return "middle"
    return "late"


def stop_stale_local_servers(port: int) -> None:
    current_pid = os.getpid()
    try:
        result = subprocess.run(
            ["lsof", "-n", "-P", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return

    target_pids: list[int] = []
    current_entry: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        prefix, value = line[0], line[1:]
        if prefix == "p":
            if current_entry:
                pid_text = current_entry.get("pid", "")
                command = current_entry.get("command", "")
                if pid_text.isdigit():
                    pid = int(pid_text)
                    if pid != current_pid and command == "Python":
                        target_pids.append(pid)
            current_entry = {"pid": value}
        elif prefix == "c":
            current_entry["command"] = value

    if current_entry:
        pid_text = current_entry.get("pid", "")
        command = current_entry.get("command", "")
        if pid_text.isdigit():
            pid = int(pid_text)
            if pid != current_pid and command == "Python":
                target_pids.append(pid)

    for pid in target_pids:
        try:
            command_result = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        command_line = command_result.stdout.strip()
        if (
            f"python -m http.server {port}" not in command_line
            and not command_line.endswith("Python server.py")
            and "Python server.py " not in command_line
        ):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print(f"stopped_stale_server pid={pid}")
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"killed_stale_server pid={pid}")
            except (ProcessLookupError, PermissionError):
                pass


def query_statistics(database: Path) -> dict:
    if not database.exists():
        return {"spots": [], "catches": [], "metadata": {"oldest": None, "newest": None}}

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    aliases = load_fish_name_aliases()
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
                JOIN (
                    SELECT spot_id
                    FROM catches
                    GROUP BY spot_id
                    HAVING COUNT(*) >= ?
                ) AS visible ON visible.spot_id = s.id
                WHERE s.lat BETWEEN 20 AND 50
                  AND s.lng BETWEEN 120 AND 155
                ORDER BY s.prefecture, s.spot_name
                """,
                (DEFAULT_MIN_SPOT_COUNT,),
            )
        ]

        grouped_catches: dict[tuple[str, str, int], int] = {}
        for row in connection.execute(
            """
            SELECT
                c.spot_id,
                c.fish_name,
                CAST(strftime('%m', c.caught_date) AS INTEGER) AS month,
                COUNT(*) AS count
            FROM catches AS c
            JOIN spots AS s ON s.id = c.spot_id
            JOIN (
                SELECT spot_id
                FROM catches
                GROUP BY spot_id
                HAVING COUNT(*) >= ?
            ) AS visible ON visible.spot_id = s.id
            WHERE s.lat BETWEEN 20 AND 50
              AND s.lng BETWEEN 120 AND 155
            GROUP BY c.spot_id, c.fish_name, CAST(strftime('%Y', c.caught_date) AS INTEGER), month
            ORDER BY c.spot_id, c.fish_name, month
            """,
            (DEFAULT_MIN_SPOT_COUNT,),
        ):
            normalized_name = normalize_fish_name(str(row["fish_name"]), aliases)
            key = (str(row["spot_id"]), normalized_name, int(row["month"]))
            grouped_catches[key] = grouped_catches.get(key, 0) + int(row["count"])

        catches = [
            {
                "spot_id": spot_id,
                "fish_name": fish_name,
                "month": month,
                "count": count,
            }
            for (spot_id, fish_name, month), count in sorted(grouped_catches.items())
        ]

        period = connection.execute(
            "SELECT MIN(caught_date) AS oldest, MAX(caught_date) AS newest FROM catches"
        ).fetchone()
        return {
            "spots": spots,
            "catches": catches,
            "metadata": {
                "oldest": period["oldest"],
                "newest": period["newest"],
                "fish_aliases_applied": True,
            },
        }
    finally:
        connection.close()


def query_detail_statistics(database: Path) -> dict:
    if not database.exists():
        return {
            "spot_month_top3": {},
            "metadata": {"oldest": None, "newest": None},
        }

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    aliases = load_fish_name_aliases()
    try:
        period = connection.execute(
            "SELECT MIN(caught_date) AS oldest, MAX(caught_date) AS newest FROM catches"
        ).fetchone()
        oldest = period["oldest"]
        newest = period["newest"]
        if newest is None or oldest is None:
            return {
                "spot_month_top3": {},
                "metadata": {"oldest": None, "newest": None},
            }

        monthly_counts: dict[tuple[str, int, str], int] = {}
        for row in connection.execute(
            """
            SELECT
                c.spot_id,
                c.fish_name,
                CAST(strftime('%m', c.caught_date) AS INTEGER) AS month,
                COUNT(*) AS count
            FROM catches AS c
            JOIN spots AS s ON s.id = c.spot_id
            JOIN (
                SELECT spot_id
                FROM catches
                GROUP BY spot_id
                HAVING COUNT(*) >= ?
            ) AS visible ON visible.spot_id = s.id
            WHERE s.lat BETWEEN 20 AND 50
              AND s.lng BETWEEN 120 AND 155
            GROUP BY c.spot_id, c.fish_name, CAST(strftime('%Y', c.caught_date) AS INTEGER), month
            ORDER BY c.spot_id, c.fish_name, month
            """,
            (DEFAULT_MIN_SPOT_COUNT,),
        ):
            normalized_name = normalize_fish_name(str(row["fish_name"]), aliases)
            key = (str(row["spot_id"]), int(row["month"]), normalized_name)
            monthly_counts[key] = monthly_counts.get(key, 0) + int(row["count"])

        twenty_minute_counts: dict[tuple[str, str], list[int]] = {}
        for row in connection.execute(
            """
            SELECT
                c.spot_id,
                c.fish_name,
                CAST(substr(c.caught_at, 12, 2) AS INTEGER) AS hour,
                CAST(substr(c.caught_at, 15, 2) AS INTEGER) AS minute,
                COUNT(*) AS count
            FROM catches AS c
            JOIN spots AS s ON s.id = c.spot_id
            JOIN (
                SELECT spot_id
                FROM catches
                GROUP BY spot_id
                HAVING COUNT(*) >= ?
            ) AS visible ON visible.spot_id = s.id
            WHERE s.lat BETWEEN 20 AND 50
              AND s.lng BETWEEN 120 AND 155
              AND c.caught_at IS NOT NULL
              AND length(c.caught_at) >= 16
            GROUP BY c.spot_id, c.fish_name, hour, minute
            ORDER BY c.spot_id, c.fish_name, hour, minute
            """,
            (DEFAULT_MIN_SPOT_COUNT,),
        ):
            normalized_name = normalize_fish_name(str(row["fish_name"]), aliases)
            key = (str(row["spot_id"]), normalized_name)
            buckets = twenty_minute_counts.setdefault(key, [0] * 72)
            hour = int(row["hour"])
            minute = int(row["minute"])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                bucket_index = hour * 3 + min(2, minute // 20)
                buckets[bucket_index] += int(row["count"])

        ranking_by_spot_month: dict[tuple[str, int], list[dict[str, object]]] = {}
        for (spot_id, month, fish_name), count in monthly_counts.items():
            ranking_by_spot_month.setdefault((spot_id, month), []).append(
                {"fish_name": fish_name, "count": count}
            )

        payload: dict[str, dict[str, list[dict[str, object]]]] = {}
        for (spot_id, month), items in ranking_by_spot_month.items():
            top_items = sorted(
                items,
                key=lambda item: (-int(item["count"]), str(item["fish_name"])),
            )[:3]
            payload.setdefault(spot_id, {})[str(month)] = [
                {
                    "fish_name": str(item["fish_name"]),
                    "count": int(item["count"]),
                }
                for item in top_items
            ]
        return {
            "spot_month_top3": payload,
            "spot_fish_time_counts": {
                spot_id: {
                    fish_name: counts
                    for (source_spot_id, fish_name), counts in sorted(twenty_minute_counts.items())
                    if source_spot_id == spot_id
                }
                for spot_id in sorted({spot_id for spot_id, _fish_name in twenty_minute_counts})
            },
            "metadata": {
                "oldest": oldest,
                "newest": newest,
                "fish_aliases_applied": True,
                "detail_period_scope": "all_time",
            },
        }
    finally:
        connection.close()


def write_json(output: Path, payload: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def next_6h_filename(month: int, period: str, hour: int) -> str:
    return f"{month:02d}-{period}-{hour:02d}.json"


def query_next_6h_statistics(database: Path) -> dict[tuple[int, str, int], dict[str, list[list[object]]]]:
    if not database.exists():
        return {}

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    aliases = load_fish_name_aliases()
    try:
        counts: dict[tuple[int, str, int, str, str], int] = {}
        for row in connection.execute(
            """
            SELECT
                c.spot_id,
                c.fish_name,
                substr(c.caught_at, 1, 16) AS caught_at,
                COUNT(*) AS count
            FROM catches AS c
            JOIN spots AS s ON s.id = c.spot_id
            JOIN (
                SELECT spot_id
                FROM catches
                GROUP BY spot_id
                HAVING COUNT(*) >= ?
            ) AS visible ON visible.spot_id = s.id
            WHERE s.lat BETWEEN 20 AND 50
              AND s.lng BETWEEN 120 AND 155
              AND c.caught_at IS NOT NULL
              AND length(c.caught_at) >= 16
            GROUP BY c.spot_id, c.fish_name, caught_at
            ORDER BY c.spot_id, c.fish_name, caught_at
            """,
            (DEFAULT_MIN_SPOT_COUNT,),
        ):
            caught_at = str(row["caught_at"])
            try:
                caught_dt = datetime.strptime(caught_at.replace("T", " "), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            normalized_name = normalize_fish_name(str(row["fish_name"]), aliases)
            count = int(row["count"])
            for offset in range(6):
                start_dt = caught_dt - timedelta(hours=offset)
                key = (
                    start_dt.month,
                    seasonal_period(start_dt.day),
                    start_dt.hour,
                    str(row["spot_id"]),
                    normalized_name,
                )
                counts[key] = counts.get(key, 0) + count

        grouped: dict[tuple[int, str, int, str], list[dict[str, object]]] = {}
        for (month, period, hour, spot_id, fish_name), count in counts.items():
            grouped.setdefault((month, period, hour, spot_id), []).append(
                {"fish_name": fish_name, "count": count}
            )

        payload: dict[tuple[int, str, int], dict[str, list[list[object]]]] = {}
        for (month, period, hour, spot_id), items in grouped.items():
            top_items = sorted(
                items,
                key=lambda item: (-int(item["count"]), str(item["fish_name"])),
            )[:3]
            payload.setdefault((month, period, hour), {})[spot_id] = [
                [str(item["fish_name"]), int(item["count"])]
                for item in top_items
            ]
        return payload
    finally:
        connection.close()


def export_next_6h_statistics(database: Path, output_dir: Path) -> int:
    payload = query_next_6h_statistics(database)
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*.json"):
        old_file.unlink()

    periods = ("early", "middle", "late")
    written = 0
    for month in range(1, 13):
        for period in periods:
            for hour in range(24):
                data = payload.get((month, period, hour), {})
                write_json(output_dir / next_6h_filename(month, period, hour), data)
                written += 1
    return written


def export_detail_spot_statistics(detail_payload: dict, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*.json"):
        old_file.unlink()

    monthly = detail_payload.get("spot_month_top3", {})
    time_counts = detail_payload.get("spot_fish_time_counts", {})
    metadata = detail_payload.get("metadata", {})
    spot_ids = sorted(set(monthly) | set(time_counts), key=lambda value: int(value))
    for spot_id in spot_ids:
        write_json(
            output_dir / f"{spot_id}.json",
            {
                "spot_month_top3": monthly.get(spot_id, {}),
                "spot_fish_time_counts": time_counts.get(spot_id, {}),
                "metadata": metadata,
            },
        )
    return len(spot_ids)


def export_statistics(
    database: Path,
    output: Path,
    detail_output: Path | None = None,
    next_6h_output_dir: Path | None = None,
    detail_spots_output_dir: Path | None = None,
) -> None:
    payload = query_statistics(database)
    write_json(output, payload)
    print(f"statistics_json={output}")
    print(f"spots={len(payload['spots'])}")
    print(f"catch_groups={len(payload['catches'])}")
    metadata = payload.get("metadata", {})
    print(f"period={metadata.get('oldest')}..{metadata.get('newest')}")
    effective_detail_output = detail_output
    if effective_detail_output is None and output.name == DEFAULT_STATISTICS_JSON.name:
        effective_detail_output = output.with_name(DEFAULT_DETAIL_STATISTICS_JSON.name)
    if effective_detail_output is not None:
        detail_payload = query_detail_statistics(database)
        write_json(effective_detail_output, detail_payload)
        print(f"detail_statistics_json={effective_detail_output}")
        print(f"detail_spots={len(detail_payload.get('spot_month_top3', {}))}")
        if detail_spots_output_dir is not None:
            written = export_detail_spot_statistics(detail_payload, detail_spots_output_dir)
            print(f"detail_spots_dir={detail_spots_output_dir}")
            print(f"detail_spot_files={written}")
    if next_6h_output_dir is not None:
        written = export_next_6h_statistics(database, next_6h_output_dir)
        print(f"next_6h_dir={next_6h_output_dir}")
        print(f"next_6h_files={written}")


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
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

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
            if request_path == "/api/detail-statistics":
                try:
                    payload = query_detail_statistics(database)
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
    parser.add_argument(
        "--export-detail-statistics",
        type=Path,
        default=None,
        help="詳細ページ用の時間帯別集計JSONを書き出して終了する保存先。",
    )
    parser.add_argument(
        "--export-next-6h-dir",
        type=Path,
        default=None,
        help="これから6時間Top3の分割JSONを書き出すディレクトリ。",
    )
    parser.add_argument(
        "--export-detail-spots-dir",
        type=Path,
        default=None,
        help="ポイント別の詳細集計JSONを書き出すディレクトリ。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.export_statistics is not None:
        export_statistics(
            args.database,
            args.export_statistics,
            args.export_detail_statistics,
            args.export_next_6h_dir
            or args.export_statistics.with_name(DEFAULT_NEXT_6H_DIR.name),
            args.export_detail_spots_dir
            or args.export_statistics.with_name(DEFAULT_DETAIL_SPOTS_DIR.name),
        )
        return
    print_startup_summary(args.database)
    stop_stale_local_servers(args.port)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.database),
    )
    print(f"Serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
