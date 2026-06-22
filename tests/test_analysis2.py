import sqlite3
import unittest

from analysis2 import (
    archive_catch_exists,
    describe_request_error,
    initialize_database,
    is_recoverable_http_error,
    parse_japanese_caught_at,
    sample_percent_units,
    sample_progress_label,
    should_sample_catch,
    update_boundary_outside_count,
)
import requests


class CatchSamplingTest(unittest.TestCase):
    def test_parse_japanese_caught_at_returns_none_for_invalid_year(self):
        self.assertEqual(
            (None, None),
            parse_japanese_caught_at("0000年 6月 15日 12:34"),
        )

    def test_recoverable_http_error_accepts_client_and_server_errors(self):
        response = requests.Response()
        response.status_code = 500
        error = requests.HTTPError(response=response)
        self.assertTrue(is_recoverable_http_error(error))

        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError(response=response)
        self.assertTrue(is_recoverable_http_error(error))

    def test_describe_request_error_formats_timeout_and_status_code(self):
        self.assertEqual(
            "通信失敗(ReadTimeout)",
            describe_request_error(requests.ReadTimeout("timed out")),
        )

        response = requests.Response()
        response.status_code = 503
        error = requests.HTTPError(response=response)
        self.assertEqual("503", describe_request_error(error))

    def test_ten_percent_samples_one_in_each_ten_ids(self):
        sampled = [
            catch_id
            for catch_id in range(1000, 1100)
            if should_sample_catch(str(catch_id), 10)
        ]

        self.assertEqual(10, len(sampled))
        self.assertEqual(list(range(1000, 1100, 10)), sampled)

    def test_twenty_five_percent_samples_evenly(self):
        sampled = [
            catch_id
            for catch_id in range(100, 120)
            if should_sample_catch(str(catch_id), 25)
        ]

        self.assertEqual([100, 104, 108, 112, 116], sampled)

    def test_full_collection_samples_every_id(self):
        self.assertTrue(should_sample_catch("4662463", 100))
        self.assertTrue(should_sample_catch("4662464", 100))

    def test_percentage_is_stored_to_two_decimal_places(self):
        self.assertEqual(1234, sample_percent_units(12.34))
        self.assertEqual("12.34", sample_progress_label(12.34))

    def test_boundary_margin_resets_when_in_period_record_appears(self):
        count = update_boundary_outside_count("期間外（開始日前）", 0)
        count = update_boundary_outside_count("期間外（開始日前）", count)
        self.assertEqual(2, count)

        count = update_boundary_outside_count("2025-06-17", count)
        self.assertEqual(0, count)
        self.assertEqual(
            1,
            update_boundary_outside_count("期間外（開始日前）", count),
        )

    def test_boundary_margin_ignores_unparseable_records(self):
        self.assertEqual(
            2,
            update_boundary_outside_count("魚種が未設定の釣果", 2),
        )

    def test_archive_catch_exists_detects_collected_catch(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO spots (
                source, source_area_id, prefecture, spot_name, lat, lng,
                source_url, created_at, updated_at
            )
            VALUES (
                'anglers', '1', '富山県', 'テスト', 36.0, 137.0,
                'https://anglers.jp/areas/1/fishings', 'now', 'now'
            )
            """
        )
        spot_id = connection.execute("SELECT id FROM spots").fetchone()["id"]
        connection.execute(
            """
            INSERT INTO catches (
                spot_id, source, source_item_id, fish_name, caught_date,
                collected_at
            )
            VALUES (?, 'anglers', '12345', 'アジ', '2026-06-16', 'now')
            """,
            (spot_id,),
        )

        self.assertTrue(archive_catch_exists(connection, "12345"))
        self.assertFalse(archive_catch_exists(connection, "12346"))


if __name__ == "__main__":
    unittest.main()
