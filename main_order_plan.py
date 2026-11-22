import os
import time
from datetime import datetime, timedelta, timezone

from ai_filter import make_candidate_id, mandatory_inclusion_reason
from discord_notify import send_discord
from filters import keyword_match
from g2b import BidCollectionError, fetch_order_plan_list
from history import BidHistory

KST = timezone(timedelta(hours=9))


def to_dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def get_order_plan_title(item: dict) -> str:
    title = item.get("bizNm") or item.get("prdctClsfcNoNm") or ""
    plan_no = item.get("orderPlanUntyNo", "번호없음")
    if not str(title).strip():
        return f"(사업명 누락 - 발주계획번호: {plan_no})"
    return str(title).strip()


def get_order_plan_org(item: dict) -> str:
    return str(item.get("orderInsttNm") or item.get("totlmngInsttNm") or "")


def get_order_plan_context(item: dict) -> str:
    values = [item.get("usgCntnts"), item.get("specCntnts"), item.get("rmrkCntnts")]
    return "\n".join(str(value) for value in values if value)[:2000]


def get_order_plan_search_text(item: dict) -> str:
    fields = (
        "bizNm",
        "prdctClsfcNoNm",
        "dtilPrdctClsfcNoNm",
        "usgCntnts",
        "specCntnts",
        "rmrkCntnts",
        "orderInsttNm",
        "totlmngInsttNm",
    )
    return " ".join(str(item.get(field, "")) for field in fields if item.get(field))


def get_record_id(item: dict, title: str, org: str) -> str:
    plan_no = item.get("orderPlanUntyNo")
    if plan_no:
        return f"order:{plan_no}-{item.get('orderPlanSno') or '1'}"
    return f"order:{make_candidate_id(title, org, item.get('nticeDt'))}"


def build_embed(item: dict, review: bool = False) -> dict:
    title = get_order_plan_title(item)
    org = get_order_plan_org(item) or "-"
    year = str(item.get("orderYear", ""))
    month = str(item.get("orderMnth", "")).zfill(2)
    order_date = f"{year}-{month}" if year else "-"

    raw_budget = item.get("sumOrderAmt")
    if raw_budget and str(raw_budget).isdigit():
        budget = f"{int(raw_budget):,}원"
    else:
        budget = str(raw_budget) if raw_budget else "-"

    fields = [
        {"name": "발주기관", "value": org, "inline": True},
        {"name": "발주예정", "value": order_date, "inline": True},
        {"name": "합계발주금액", "value": budget, "inline": False},
        {"name": "계약방법", "value": str(item.get("cntrctMthdNm") or "-"), "inline": True},
        {"name": "게시일시", "value": str(item.get("nticeDt") or "-"), "inline": True},
    ]
    url = item.get("orderPlanDtlUrl", "")
    if url:
        fields.append({"name": "링크", "value": str(url), "inline": False})

    return {
        "title": f"[발주계획] {title}",
        "color": 0xE67E22 if review else 0x9B59B6,
        "fields": fields,
    }


def send_items(webhook_url: str, items: list[dict], label: str, review: bool = False):
    chunks = [items[i:i + 10] for i in range(0, len(items), 10)]
    for index, chunk in enumerate(chunks, start=1):
        send_discord(
            webhook_url,
            content=f"{label} ({index}/{len(chunks)})",
            embeds=[build_embed(item, review) for item in chunk],
        )
        time.sleep(0.6)


def main():
    api_key = os.environ["G2B_API_KEY"].strip()
    webhook_url = os.environ["DISCORD_WEBHOOK_URL_ORDER_PLAN"].strip()

    now = datetime.now(KST)
    start = now - timedelta(days=1)

    try:
        items = fetch_order_plan_list(api_key, to_dt_str(start), to_dt_str(now))
    except BidCollectionError as e:
        send_discord(
            webhook_url,
            content=(
                "ODA 발주계획 수집에 실패했습니다.\n"
                "정상적인 0건이 아니므로 GitHub Actions 로그를 확인해 주세요.\n"
                f"오류: {e}"
            ),
        )
        raise

    print(f"최근 24시간 용역 발주계획: {len(items)}건")
    candidates = []

    for item in items:
        title = get_order_plan_title(item)
        org = get_order_plan_org(item)
        mandatory_reason = mandatory_inclusion_reason(title, org)
        if not mandatory_reason and not keyword_match(get_order_plan_search_text(item)):
            continue

        candidates.append({
            "id": get_record_id(item, title, org),
            "track": "order",
            "title": title,
            "org": org,
            "context": get_order_plan_context(item),
            "mandatory_reason": mandatory_reason,
            "item": item,
        })

    history = BidHistory()
    processed = history.process(candidates, now)
    filtered = []
    new_items = []
    changed_items = []
    forced_passed = 0
    ai_passed = 0
    ai_rejected = 0
    system_review = 0

    for candidate in candidates:
        item = candidate["item"]
        record_id = candidate["id"]
        result = processed.results[record_id]
        item["_filter_reason"] = result.reason

        if result.decision == "pass":
            filtered.append(item)
            if result.source == "mandatory":
                forced_passed += 1
            else:
                ai_passed += 1

            if record_id in processed.new_ids:
                new_items.append(item)
            elif record_id in processed.changed_ids:
                changed_items.append(item)
        elif result.decision == "review" and result.source == "system":
            system_review += 1
        else:
            ai_rejected += 1

    alert_count = len(new_items) + len(changed_items)
    summary_text = (
        f"조회기간: {start.strftime('%m월 %d일 %H:%M')} ~ "
        f"{now.strftime('%m월 %d일 %H:%M')} (최근 24시간)\n"
        f"전체 용역 발주계획: {len(items)}건\n"
        f"1차 후보: {len(candidates)}건\n"
        f"2차 AI 필터링 통과: {ai_passed}건\n"
        f"강제 포함: {forced_passed}건\n"
        f"신규·변경 알림: {alert_count}건"
    )
    if system_review:
        summary_text += f"\n처리 오류: {system_review}건"
    if alert_count:
        summary_text += f"\n\n🚀 신규·변경 공고 {alert_count}건을 전송합니다."
    else:
        summary_text += "\n\n오늘은 새로 알릴 발주계획이 없습니다."
    send_discord(
        webhook_url,
        content=f"📢 **ODA 발주계획 알림(일일 요약)**\n{summary_text}",
    )

    send_items(webhook_url, new_items, "🆕 신규 ODA 발주계획")
    send_items(webhook_url, changed_items, "🔄 변경된 ODA 발주계획")

    history.save()


if __name__ == "__main__":
    main()
