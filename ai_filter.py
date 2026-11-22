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
ALLOWED_DECISIONS = {"pass", "reject"}
CLASSIFIER_VERSION = 3

MANDATORY_PATTERNS = {
    "KOICA": re.compile(r"KOICA|한국국제협력단", re.IGNORECASE),
    "KOFIH": re.compile(r"KOFIH|한국국제보건의료재단", re.IGNORECASE),
}
EXIMBANK_PATTERN = re.compile(
    r"수출입\s*은행|Korea\s*Eximbank|KEXIM", re.IGNORECASE
)
EDCF_PATTERN = re.compile(r"EDCF|대외경제협력기금", re.IGNORECASE)
KSP_PATTERN = re.compile(r"(?<![A-Za-z])KSP(?![A-Za-z])|경제발전경험", re.IGNORECASE)

EXCLUDE_KEYWORDS = [
    "유지보수", "정비사업", "도로포장", "CCTV", "청사", "사무용품",
    "박람회", "전시회", "보행로 정비", "조경", "방역", "경비용역", "폐기물",
    "인도 및 차도", "자격시험", "도배", "건강검진", "차량 임차", "구내식당",
    "기자재 공급", "기자재 도입", "기자재 구매", "가구 제작", "시스템 감리",
    "감리 용역", "건축물 개보수", "버스 공급", "항공권", "보험 공급자", "단체 보험",
    "비자 발급", "비자발급", "임직원", "하자소송", "법률대리", "변호사 선임",
    "체육시설", "재외동포", "차세대동포", "해양법", "모국 초청", "채용 대행",
    "신규 채용", "렌터카", "행사 대행", "개막식", "전시물 제작", "세계기록유산",
    "인류무형유산", "달력", "정수기", "스포츠시설", "교량", "여행자보험", "대관 용역",
]
EXCLUDE_PATTERN = re.compile("|".join(map(re.escape, EXCLUDE_KEYWORDS)), re.IGNORECASE)
CORE_PATTERN = re.compile(
    r"(?<![A-Za-z])ODA(?![A-Za-z])|공적개발원조|국제개발협력|개발협력|"
    r"무상원조|유상원조|(?<![가-힣])수원국(?![가-힣])|"
    r"형성조사|종료평가|(?<![A-Za-z])PMC(?![A-Za-z])|대외경제협력기금|다자개발은행|세계은행|"
    r"아시아개발은행|아프리카개발은행|미주개발은행|아시아인프라투자은행",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """
너는 ODA 및 국제개발협력 컨설팅 기업의 입찰공고 분류기다.

수주 대상:
- ODA 사업의 기획·조사·평가·연구·성과관리·모니터링
- 형성조사, 타당성조사, 심층조사, 기획조사, 마스터플랜, 로드맵
- ODA 사업 수행(PMC), 개도국 역량강화, 초청연수
- 개도국 대상 정보시스템, 소프트웨어, 플랫폼 구축

합격 예시:
- 라오스 암 검진 역량강화 형성조사
- 우크라이나 공항 마스터플랜
- 다지역 유사 ODA 사업 평가
- 탄자니아 지적DB 플랫폼 구축
- 국제협력센터 초청연수 운영 대행

아래 즉시 탈락 기준에 하나라도 해당하면 무조건 reject하라.

[A. 하드웨어·물품·시설]
기자재·물품·가구 공급, 전시물 제작·설치, 버스·차량 공급, 건축물 신축·개보수,
토목·도로·포장·조경 공사, 시스템 감리 용역

[B. 단순 행정·운영 지원]
채용 대행, 렌터카·차량 임차, 항공권 구매, 비자 발급 대행, 보험 공급자 선정,
변호사 선임·하자소송, 시설 위탁운영, 구내식당, 경비·방역 용역

[C. 국내 행사·전시·문화·체육 용역]
국내 전시 기획·연출, 전시물 제작·설치, 국내 기념행사·특별전 운영,
개막식·부대행사 운영, 학술토론회·포럼 단순 행사대행, 스포츠 대회 운영

[D. 국제기구 명칭을 쓴 국내 사업]
유네스코 세계기록유산·인류무형유산 등재 신청·전략 수립, 유네스코 창의도시 행사,
유네스코 생태탐방로 조성, 유니세프·WHO 국내 캠페인 등 국내 행정·운영 사업

[E. 비ODA 목적 교류·연수]
재외동포·차세대동포 모국 초청, 해양법 교육, 특정 직군 단순 해외연수,
ODA 컨설팅과 무관한 단발성 교류 행사

[F. 동음이의어·국내 지명·일반명사 오인]
인도(보행로), 가나(출판사·식품), 차드(인명) 등 개발도상국명이 아닌 용례,
국내 지자체 사업에 국가명이 우연히 포함된 경우

판별 순서:
1. ODA·국제개발협력 또는 개발도상국과 관련된 공고만 pass로 분류
2. 위 즉시 탈락 기준을 확인하고 해당하면 reject로 분류
3. ODA와 무관한 국내 공고는 reject로 분류
4. 국가명이나 도시명이 있어도 내용이 국내 사업이면 reject로 분류
5. 정보가 부족하거나 관련성이 불명확하면 reject로 분류

기관명이나 국가명만으로 통과시키지 말고 각 공고를 독립적으로 판별하라.
반드시 다음 JSON 객체 형식으로만 응답하고, 입력된 모든 공고를 정확히 한 번씩 반환하라.
{"results":[{"id":"입력된 공고 ID","decision":"pass | reject","reason":"40자 이내 구체적 근거"}]}
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
    if KSP_PATTERN.search(title):
        return "[강제 포함] 입찰명에 KSP·경제발전경험 포함"
    search_text = f"{title} {org}"
    for name, pattern in MANDATORY_PATTERNS.items():
        if pattern.search(search_text):
            return f"[강제 포함] {name} 관련 공고"
    if EXIMBANK_PATTERN.search(search_text) and EDCF_PATTERN.search(search_text):
        return "[강제 포함] 수출입은행 EDCF 관련 공고"
    return None


def rule_based_classification(title: str, org: str) -> Classification | None:
    exclusion = EXCLUDE_PATTERN.search(title)
    if exclusion:
        return Classification(
            "reject", f"[규칙 제외] {exclusion.group(0)}", "rule"
        )
    core = CORE_PATTERN.search(f"{title} {org}")
    if core:
        return Classification(
            "pass", f"[ODA 핵심 규칙] {core.group(0)}", "rule"
        )
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
    pending = []
    for item in candidates:
        rule_result = rule_based_classification(item["title"], item["org"])
        if rule_result:
            results[item["id"]] = rule_result
        else:
            pending.append(item)

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
