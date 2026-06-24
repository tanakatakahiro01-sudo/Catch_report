from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATISTICS_JSON = ROOT / "data" / "statistics.json"
OUTPUT_JSON = ROOT / "data" / "spot_water_types.json"

LAKE_KEYWORDS = [
    "管理釣り場",
    "管理釣場",
    "釣り堀",
    "つり堀",
    "つりぼり",
    "へら鮒",
    "ヘラブナ",
    "へらぶな",
    "鱒釣り場",
    "ます釣り場",
    "養魚場",
    "養殖場",
    "養鱒場",
    "トラウト",
    "trout",
    "Trout",
    "レイク",
    "ポンド",
    "湖",
    "池",
    "沼",
    "ダム",
    "ワンド",
    "貯水池",
    "ため池",
    "調整池",
    "フィッシングエリア",
    "FISHING AREA",
    "Fishing Area",
]

SEA_KEYWORDS = [
    "河口",
    "海釣",
    "海づり",
    "海上釣",
    "海洋釣り場",
    "海洋",
    "海峡",
    "干潟",
    "一文字",
    "ケーソン",
    "フェリー",
    "ワーフ",
    "マリーナ",
    "遊漁センター",
    "魚つり公園",
    "海つり公園",
    "海釣センター",
    "沿岸",
    "ビーチ",
    "灯台",
    "岬",
    "崎",
    "漁港",
    "港",
    "湾",
    "磯",
    "堤防",
    "防波堤",
    "海岸",
    "サーフ",
    "沖",
    "島",
    "鼻",
    "浜",
    "浦",
    "海浜",
    "護岸",
    "波止",
    "埠頭",
    "岸壁",
    "突堤",
    "海",
    "灘",
    "水道",
]

RIVER_KEYWORDS = [
    "渓流",
    "河川",
    "用水路",
    "水路",
    "運河",
    "沢",
    "堰堤",
    "堰",
    "支流",
    "川",
]

FRESHWATER_FALLBACK_KEYWORDS = [
    "フィッシングパーク",
    "FISHING PARK",
    "Fishing Park",
    "フィッシングクラブ",
    "フィッシュランド",
    "ガーデン",
    "プール",
    "釣パラダイス",
    "へら鮒センター",
]


def infer_water_type(spot_name: str) -> tuple[str, str]:
    name = spot_name.strip()

    for keyword in LAKE_KEYWORDS:
        if keyword in name:
            return "湖（池）", keyword

    for keyword in SEA_KEYWORDS:
        if keyword in name:
            return "海", keyword

    for keyword in RIVER_KEYWORDS:
        if keyword in name:
            return "川", keyword

    for keyword in FRESHWATER_FALLBACK_KEYWORDS:
        if keyword in name:
            return "湖（池）", keyword

    return "海", "default"


def main() -> int:
    payload = json.loads(STATISTICS_JSON.read_text(encoding="utf-8"))
    spots = payload.get("spots", [])

    result: dict[str, dict[str, str]] = {}
    for spot in sorted(spots, key=lambda item: (item["prefecture"], item["spot_name"], str(item["id"]))):
        water_type, matched_rule = infer_water_type(str(spot["spot_name"]))
        result[str(spot["id"])] = {
            "prefecture": str(spot["prefecture"]),
            "spot_name": str(spot["spot_name"]),
            "water_type": water_type,
            "matched_rule": matched_rule,
        }

    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated {OUTPUT_JSON} ({len(result)} spots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
