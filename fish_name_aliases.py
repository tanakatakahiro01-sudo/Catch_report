"""魚種名の統合辞書ローダー。"""

from __future__ import annotations

import json
from pathlib import Path

ALIASES_FILE = Path(__file__).with_name("fish_name_aliases.json")


def load_fish_name_aliases(path: Path = ALIASES_FILE) -> dict[str, str]:
    """外部JSONから魚種名の統合辞書を読み込む。"""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return {str(key): str(value) for key, value in data.items()}


def normalize_fish_name(name: str, aliases: dict[str, str] | None = None) -> str:
    """辞書に登録済みの別名なら代表表記へ変換する。"""

    mapping = aliases if aliases is not None else load_fish_name_aliases()
    return mapping.get(name, name)
