import sqlite3
import unittest

from analysis import (
    RequestPacer,
    Spot,
    get_collection_progress,
    has_next_page,
    initialize_database,
    parse_fishing_page,
    save_collection_progress,
)


class FishingPageParserTest(unittest.TestCase):
    def test_parses_multiple_fish_from_one_fishing(self):
        html = """
        <div class="fishings-list-container">
          <h3>2026年06月</h3>
          <a href="/fishings/5350292">
            <h5 class="text-primary">13日(土)</h5>
            <div class="carousel-wrap">
              <img alt="キジハタ" src="https://example.test/result/10722071/a.jpg">
              <img alt="アジ" src="https://example.test/result/10722072/b.jpg">
            </div>
          </a>
        </div>
        """

        records = parse_fishing_page(html)

        self.assertEqual(2, len(records))
        self.assertEqual("2026-06-13", records[0].caught_date)
        self.assertEqual("5350292", records[0].source_fishing_id)
        self.assertEqual("10722071", records[0].source_item_id)
        self.assertEqual("アジ", records[1].fish_name)

    def test_detects_next_page(self):
        html = '<a rel="next" href="/areas/1202/fishings?page=2">次へ</a>'
        self.assertTrue(has_next_page(html))
        self.assertFalse(has_next_page("<p>最終ページ</p>"))

    def test_requires_fishing_list(self):
        with self.assertRaisesRegex(ValueError, "釣果一覧"):
            parse_fishing_page("<html></html>")

    def test_collection_progress_is_resumable(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        spot = Spot(1202, "富山県", "黒部漁港", 36.8, 137.4)

        self.assertEqual((1, False), get_collection_progress(connection, spot))
        save_collection_progress(
            connection,
            spot,
            next_page=12,
            is_complete=False,
        )
        self.assertEqual((12, False), get_collection_progress(connection, spot))
        save_collection_progress(
            connection,
            spot,
            next_page=13,
            is_complete=True,
        )
        self.assertEqual((13, True), get_collection_progress(connection, spot))

    def test_request_pacer_waits_between_requests(self):
        current_time = [100.0]
        sleeps = []

        def clock():
            return current_time[0]

        def sleeper(seconds):
            sleeps.append(seconds)
            current_time[0] += seconds

        pacer = RequestPacer(
            delay=2.0,
            jitter=1.0,
            clock=clock,
            sleeper=sleeper,
            random_value=lambda: 0.5,
        )

        self.assertEqual(0.0, pacer.wait())
        current_time[0] += 1.0
        self.assertEqual(1.5, pacer.wait())
        self.assertEqual([1.5], sleeps)

    def test_request_pacer_honors_retry_after(self):
        sleeps = []
        pacer = RequestPacer(
            delay=2.0,
            jitter=1.0,
            sleeper=sleeps.append,
            random_value=lambda: 0.5,
        )

        self.assertEqual(5.5, pacer.backoff(0, "5"))
        self.assertEqual([5.5], sleeps)


if __name__ == "__main__":
    unittest.main()
