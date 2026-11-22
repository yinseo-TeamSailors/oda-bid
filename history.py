from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_filter import CLASSIFIER_VERSION, Classification, classify_candidates

KST = timezone(timedelta(hours=9))
DEFAULT_HISTORY_PATH = "data/bid_history.json"


@dataclass
class ProcessResult:
    results: dict[str, Classification]
    new_ids: set[str]
    changed_ids: set[str]
    history: "BidHistory"


def _canonical_hash(value) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _classification_signature(candidate: dict) -> str:
    return _canonical_hash({
        "classifier_version": CLASSIFIER_VERSION,
        "title": candidate["title"],
        "org": candidate["org"],
        "context": candidate.get("context", ""),
        "mandatory_reason": candidate.get("mandatory_reason", ""),
    })


def parse_deadline(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def deadline_status(value, now: datetime) -> str:
    deadline = parse_deadline(value)
    if deadline is None:
        return "unknown"
    if deadline < now:
        return "expired"
    if deadline <= now + timedelta(days=3):
        return "imminent"
    return "later"


class BidHistory:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("ODA_HISTORY_PATH", DEFAULT_HISTORY_PATH))
        self.data = {"version": 1, "items": {}}
        if self.path.exists():
            with self.path.open(encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded.get("items"), dict):
                raise ValueError("공고 이력 파일의 items 형식이 올바르지 않습니다.")
            self.data = loaded

    def process(self, candidates: list[dict], now: datetime) -> ProcessResult:
        results = {}
        pending = []
        new_ids = set()
        changed_ids = set()

        for candidate in candidates:
            record_id = candidate["id"]
            previous = self.data["items"].get(record_id)
            raw_item = {
                key: value for key, value in candidate["item"].items()
                if not key.startswith("_")
            }
            snapshot_hash = _canonical_hash(raw_item)
            signature = _classification_signature(candidate)

            if previous is None:
                new_ids.add(record_id)
            elif previous.get("data_hash") != snapshot_hash:
                changed_ids.add(record_id)

            if candidate.get("mandatory_reason"):
                results[record_id] = Classification(
                    "pass", candidate["mandatory_reason"], "mandatory"
                )
                continue

            if (
                previous
                and previous.get("classification_signature") == signature
                and previous.get("source") == "deepseek"
                and previous.get("decision") in {"pass", "reject", "review"}
            ):
                results[record_id] = Classification(
                    previous["decision"], previous.get("reason", ""), "deepseek"
                )
            else:
                pending.append(candidate)

        if pending:
            results.update(classify_candidates(pending))

        for candidate in candidates:
            record_id = candidate["id"]
            previous = self.data["items"].get(record_id)
            result = results.get(record_id)
            if result is None:
                result = Classification(
                    "review", "분류 결과가 없어 사람 검토가 필요합니다.", "system"
                )
                results[record_id] = result

            raw_item = {
                key: value for key, value in candidate["item"].items()
                if not key.startswith("_")
            }
            self.data["items"][record_id] = {
                "track": candidate["track"],
                "first_seen": previous.get("first_seen") if previous else now.isoformat(),
                "last_seen": now.isoformat(),
                "data_hash": _canonical_hash(raw_item),
                "classification_signature": _classification_signature(candidate),
                "decision": result.decision,
                "reason": result.reason,
                "source": result.source,
                "data": raw_item,
            }

        self._prune(now)
        return ProcessResult(results, new_ids, changed_ids, self)

    def _prune(self, now: datetime):
        cutoff = now - timedelta(days=21)
        expired_ids = []
        for record_id, record in self.data["items"].items():
            try:
                last_seen = datetime.fromisoformat(record["last_seen"])
            except (KeyError, TypeError, ValueError):
                expired_ids.append(record_id)
                continue
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=KST)
            if last_seen < cutoff:
                expired_ids.append(record_id)
        for record_id in expired_ids:
            del self.data["items"][record_id]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        temporary_path.replace(self.path)
