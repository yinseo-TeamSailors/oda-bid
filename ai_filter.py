from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
ALLOWED_DECISIONS = {"pass", "reject", "review"}
CLASSIFIER_VERSION = 1

MANDATORY_PATTERNS = {
    "KOICA": re.compile(r"KOICA|한국국제협력단", re.IGNORECASE),
    "KOFIH": re.compile(r"KOFIH|한국국제보건의료재단", re.IGNORECASE),
}
EXIMBANK_PATTERN = re.compile(
    r"수출입\s*은행|Korea\s*Eximbank|KEXIM", re.IGNORECASE
)
EDCF_PATTERN = re.compile(r"EDCF|대외경제협력기금", re.IGNORECASE)
SYSTEM_PROMPT = """
너는 ODA 및 국제개발협력 컨설팅 기업의 입찰공고 분류기다.

수주 대상:
- ODA 사업의 기획·조사·평가·연구·성과관리·모니터링
- 형성조사, 타당성조사, 심층조사, 기획조사, 마스터플랜, 로드맵
- ODA 사업 수행(PMC), 개도국 역량강화, 초청연수
- 개도국 대상 정보시스템, 소프트웨어, 플랫폼 구축

위 예시에 없더라도 ODA·국제개발협력과 관련된 용역은 검토하라.
물품, 시설, 행정, 운영, 행사, 전시, 문화, 체육, 교류, 연수 등 특정 업무 유형만으로 즉시 배제하지 마라.
기관명이나 국가명만으로 통과시키지 말고 각 공고를 독립적으로 판별하라.
제목·수요기관·추가 설명만으로 관련성이나 수주 가능성이 불명확하면 review로 분류하라.
반드시 다음 JSON 객체 형식으로만 응답하고, 입력된 모든 공고를 정확히 한 번씩 반환하라.
{"results":[{"id":"입력된 공고 ID","decision":"pass | reject | review","reason":"40자 이내 구체적 근거"}]}
""".strip()


@dataclass(frozen=True)
class Classification:
    decision: str
    reason: str
    source: str


class BatchClassificationError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def make_candidate_id(*parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def mandatory_inclusion_reason(title: str, org: str) -> str | None:
    search_text = f"{title} {org}"
    for name, pattern in MANDATORY_PATTERNS.items():
        if pattern.search(search_text):
            return f"[강제 포함] {name} 관련 공고"
    if EXIMBANK_PATTERN.search(search_text) and EDCF_PATTERN.search(search_text):
        return "[강제 포함] 수출입은행 EDCF 관련 공고"
    return None


def _positive_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return min(max(value, 1), maximum)


def _request_batch(batch: list[dict]) -> list[dict]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise BatchClassificationError(
            "DEEPSEEK_API_KEY가 설정되지 않았습니다.", retryable=False
        )

    public_bids = []
    for item in batch:
        public_item = {"id": item["id"], "title": item["title"], "org": item["org"]}
        if item.get("context"):
            public_item["context"] = item["context"]
        public_bids.append(public_item)
    body = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps({"bids": public_bids}, ensure_ascii=False),
            },
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(
                DEEPSEEK_ENDPOINT, headers=headers, json=body, timeout=60
            )
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise BatchClassificationError(
                    f"DeepSeek HTTP {response.status_code}"
                )
            if not response.ok:
                raise BatchClassificationError(
                    f"DeepSeek HTTP {response.status_code}", retryable=False
                )

            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            result = json.loads(text)
            rows = result.get("results")
            if not isinstance(rows, list):
                raise BatchClassificationError("DeepSeek 응답에 results 배열이 없습니다.")
            return rows
        except (
            requests.exceptions.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            BatchClassificationError,
        ) as e:
            last_error = e
            print(f"DeepSeek 배치 오류({len(batch)}건): {e}")
            if attempt + 1 < 3 and getattr(e, "retryable", True):
                time.sleep(10 * (2**attempt))
            else:
                break

    raise BatchClassificationError(f"DeepSeek 배치 요청 실패: {last_error}")


def _validate_rows(rows: list[dict], batch: list[dict]) -> tuple[dict, list[dict]]:
    expected = {item["id"]: item for item in batch}
    validated = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        bid_id = row.get("id")
        decision = row.get("decision")
        if bid_id not in expected or bid_id in validated:
            continue
        if decision not in ALLOWED_DECISIONS:
            continue
        reason = str(row.get("reason", "")).strip()[:100] or "판단 근거 없음"
        validated[bid_id] = Classification(decision, reason, "deepseek")

    missing = [item for bid_id, item in expected.items() if bid_id not in validated]
    return validated, missing


def _system_review(message: str) -> Classification:
    return Classification("review", message, "system")


def _classify_batch_with_recovery(batch: list[dict]) -> dict[str, Classification]:
    try:
        rows = _request_batch(batch)
    except BatchClassificationError as e:
        return {
            item["id"]: _system_review(f"AI 호출 실패: {e}")
            for item in batch
        }

    results, missing = _validate_rows(rows, batch)
    if not missing:
        return results

    print(f"DeepSeek 결과 누락: {len(missing)}건 재요청")
    try:
        retry_rows = _request_batch(missing)
    except BatchClassificationError as e:
        for item in missing:
            results[item["id"]] = _system_review(f"AI 재호출 실패: {e}")
        return results

    retry_results, missing = _validate_rows(retry_rows, missing)
    results.update(retry_results)

    for item in missing:
        try:
            individual_rows = _request_batch([item])
            individual, still_missing = _validate_rows(individual_rows, [item])
            if still_missing:
                results[item["id"]] = _system_review("AI 응답에서 공고 결과가 누락됐습니다.")
            else:
                results.update(individual)
        except BatchClassificationError as e:
            results[item["id"]] = _system_review(f"AI 개별 재호출 실패: {e}")

    return results


def classify_candidates(candidates: list[dict]) -> dict[str, Classification]:
    results = {}
    pending = list(candidates)

    batch_size = _positive_int_env("ODA_BATCH_SIZE", 10, 20)
    max_workers = _positive_int_env("ODA_MAX_WORKERS", 1, 2)
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]

    if not batches:
        return results

    if max_workers == 1:
        for batch in batches:
            results.update(_classify_batch_with_recovery(batch))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for batch_result in executor.map(_classify_batch_with_recovery, batches):
                results.update(batch_result)

    return results
