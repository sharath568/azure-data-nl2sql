"""Gold and adversarial question sets (YAML)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nl2sql.semantic import bundled_path


@dataclass
class GoldItem:
    id: str
    question: str
    sql: str
    tags: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AdversarialItem:
    id: str
    question: str
    attack: str = ""
    expect_blocked_by: str | None = None
    tags: list[str] = field(default_factory=list)


def _items(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected a list under 'items'")
    seen = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict) or "question" not in it:
            raise ValueError(f"{path}: item {i} needs at least a 'question'")
        it.setdefault("id", f"q{i + 1:02d}")
        if it["id"] in seen:
            raise ValueError(f"{path}: duplicate id {it['id']}")
        seen.add(it["id"])
    return items


def load_gold(path: str | Path | None = None) -> list[GoldItem]:
    path = Path(path) if path else bundled_path("gold.yaml")
    out = []
    for it in _items(path):
        if not it.get("sql"):
            raise ValueError(f"{path}: gold item {it['id']} has no sql")
        out.append(
            GoldItem(
                id=str(it["id"]),
                question=str(it["question"]),
                sql=" ".join(str(it["sql"]).split()),
                tags=[str(t) for t in it.get("tags", [])],
                notes=str(it.get("notes", "")),
            )
        )
    return out


def load_adversarial(path: str | Path | None = None) -> list[AdversarialItem]:
    path = Path(path) if path else bundled_path("adversarial.yaml")
    return [
        AdversarialItem(
            id=str(it["id"]),
            question=str(it["question"]),
            attack=str(it.get("attack", "")),
            expect_blocked_by=it.get("expect_blocked_by"),
            tags=[str(t) for t in it.get("tags", [])],
        )
        for it in _items(path)
    ]
