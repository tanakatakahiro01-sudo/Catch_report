from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATISTICS_JSON = ROOT / "data" / "statistics.json"
OUTPUT_JSON = ROOT / "data" / "point_month_top3.json"


def build_payload() -> dict[str, object]:
    statistics = json.loads(STATISTICS_JSON.read_text(encoding="utf-8"))
    spots = statistics.get("spots", [])
    catches = statistics.get("catches", [])

    spot_lookup = {str(spot["id"]): spot for spot in spots}
    grouped: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    monthly_totals: dict[tuple[str, int], int] = defaultdict(int)
    spot_totals: dict[str, int] = defaultdict(int)

    for record in catches:
        spot_id = str(record.get("spot_id"))
        month = record.get("month")
        fish_name = record.get("fish_name")
        count = int(record.get("count", 0))

        if not isinstance(month, int) or not (1 <= month <= 12):
            continue
        if not fish_name or count <= 0:
            continue

        key = (spot_id, month)
        grouped[key][str(fish_name)] += count
        monthly_totals[key] += count
        spot_totals[spot_id] += count

    payload: dict[str, object] = {
        "metadata": {
            "source_statistics": "data/statistics.json",
            "scope": "point x month",
            "metric": "count-based fish top3",
        },
        "spots": {},
    }

    spots_map: dict[str, object] = payload["spots"]  # type: ignore[assignment]
    for spot_id in sorted(spot_lookup, key=lambda value: (spot_lookup[value]["prefecture"], spot_lookup[value]["spot_name"], value)):
        spot = spot_lookup[spot_id]
        months: dict[str, object] = {}
        for month in range(1, 13):
            key = (spot_id, month)
            ranking = grouped.get(key)
            total = monthly_totals.get(key, 0)
            top_three = (
                [
                    {"fish_name": fish_name, "count": count}
                    for fish_name, count in ranking.most_common(3)
                ]
                if ranking
                else []
            )
            months[str(month)] = {
                "total_count": total,
                "top3": top_three,
            }

        spots_map[spot_id] = {
            "prefecture": str(spot["prefecture"]),
            "spot_name": str(spot["spot_name"]),
            "spot_total_count": int(spot_totals.get(spot_id, 0)),
            "months": months,
        }

    return payload


def main() -> int:
    OUTPUT_JSON.write_text(
        json.dumps(build_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated {OUTPUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
