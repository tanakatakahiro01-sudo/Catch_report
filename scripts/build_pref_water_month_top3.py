from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATISTICS_JSON = ROOT / "data" / "statistics.json"
WATER_TYPES_JSON = ROOT / "data" / "spot_water_types.json"
OUTPUT_JSON = ROOT / "data" / "prefecture_water_type_month_top3.json"
WATER_TYPE_ORDER = ("海", "川", "湖（池）")


def build_payload() -> dict[str, object]:
    statistics = json.loads(STATISTICS_JSON.read_text(encoding="utf-8"))
    water_types = json.loads(WATER_TYPES_JSON.read_text(encoding="utf-8"))

    grouped: dict[tuple[str, str, int], Counter[str]] = defaultdict(Counter)
    monthly_totals: dict[tuple[str, str, int], int] = defaultdict(int)

    for record in statistics.get("catches", []):
        spot_id = str(record.get("spot_id"))
        month = record.get("month")
        fish_name = record.get("fish_name")
        count = int(record.get("count", 0))

        if not isinstance(month, int) or not (1 <= month <= 12):
            continue
        if not fish_name or count <= 0:
            continue

        spot_meta = water_types.get(spot_id)
        if not spot_meta:
            continue

        prefecture = str(spot_meta["prefecture"])
        water_type = str(spot_meta["water_type"])
        key = (prefecture, water_type, month)
        grouped[key][str(fish_name)] += count
        monthly_totals[key] += count

    prefectures = sorted({pref for pref, _, _ in grouped})
    payload: dict[str, object] = {
        "metadata": {
            "source_statistics": "data/statistics.json",
            "source_water_types": "data/spot_water_types.json",
            "scope": "prefecture x water_type x month",
            "metric": "count-based fish top3",
            "water_type_order": list(WATER_TYPE_ORDER),
        },
        "prefectures": {},
    }

    prefecture_map: dict[str, object] = payload["prefectures"]  # type: ignore[assignment]
    for prefecture in prefectures:
        water_type_map: dict[str, object] = {}
        for water_type in WATER_TYPE_ORDER:
            months: dict[str, object] = {}
            has_any = False
            for month in range(1, 13):
                key = (prefecture, water_type, month)
                ranking = grouped.get(key)
                total = monthly_totals.get(key, 0)
                if ranking:
                    has_any = True
                    top_three = [
                        {"fish_name": fish_name, "count": count}
                        for fish_name, count in ranking.most_common(3)
                    ]
                else:
                    top_three = []
                months[str(month)] = {
                    "total_count": total,
                    "top3": top_three,
                }
            if has_any:
                water_type_map[water_type] = months
        prefecture_map[prefecture] = water_type_map

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
