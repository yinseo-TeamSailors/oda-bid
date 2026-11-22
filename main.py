import os
import time
from datetime import datetime, timedelta, timezone

from ai_filter import make_candidate_id, mandatory_inclusion_reason
from discord_notify import send_discord
from filters import keyword_match
from g2b import BidCollectionError, fetch_bid_list
from history import BidHistory, deadline_status

KST = timezone(timedelta(hours=9))


def to_dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def get_record_id(item: dict, title: str, org: str) -> str:
    notice_no = item.get("bidNtceNo")
    if notice_no:
        return f"bid:{notice_no}-{item.get('bidNtceOrd') or '000'}"
    return f"bid:{make_candidate_id(title, org, item.get('bidNtceDt'))}"


def get_deadline(item: dict):
    return item.get("bidNtceEndDt") or item.get("bidQlfctRgstDt")


def build_embed(item: dict, review: bool = False) -> dict:
    title = item.get("bidNtceNm", "(제목 없음)")
    org = item.get("dminsttNm", "-")
    deadline = get_deadline(item) or "-"

    raw_budget = item.get("asignBdgtAmt")
    if raw_budget and str(raw_budget).isdigit():
        budget = f"{int(raw_budget):,}원"
    else:
        budget = str(raw_budget) if raw_budget else "-"

    fields = [
        {"name": "수요기관", "value": str(org), "inline": True},
        {"name": "입찰 마감", "value": str(deadline), "inline": True},
        {"name": "예산금액", "value": str(budget), "inline": False},
    ]
    url = item.get("bidNtceDtlUrl", "")
    if url:
        fields.append({"name": "링크", "value": str(url), "inline": False})
    reason = item.get("_filter_reason")
    if reason:
        fields.append({"name": "필터링 근거", "value": str(reason), "inline": False})

    return {
        "title": str(title),
        "color": 0xE67E22 if review else 0x3498DB,
        "fields": fields,
    }


def send_items(webhook_url: str, items: list[dict], label: str, review: bool = False):
    chunk_size = 10 if review else 1
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    for index, chunk in enumerate(chunks, start=1):
        send_discord(
            webhook_url,
            content=f"{label} ({index}/{len(chunks)})",
            embeds=[build_embed(item, review) for item in chunk],
        )
        time.sleep(0.6)


def main():
    api_key = os.environ["G2B_API_KEY"].strip()
    webhook_url = os.environ["DISCORD_WEBHOOK_URL_BID"].strip()

    now = datetime.now(KST)
    start = now - timedelta(days=3)

    try:
        items = fetch_bid_list(api_key, to_dt_str(start), to_dt_str(now))
    except BidCollectionError as e:
        send_discord(
            webhook_url,
            content=(
                "ODA 입찰공고 수집에 실패했습니다.\n"
                "정상적인 0건이 아니므로 GitHub Actions 로그를 확인해 주세요.\n"
                f"오류: {e}"
            ),
        )
        raise

    print(f"최근 3일 본공고: {len(items)}건")
    candidates = []

    for item in items:
        title = str(item.get("bidNtceNm", ""))
        org = str(item.get("dminsttNm", ""))
        mandatory_reason = mandatory_inclusion_reason(title, org)
        if not mandatory_reason and not keyword_match(f"{title} {org}"):
            continue

        candidates.append({
            "id": get_record_id(item, title, org),
            "track": "bid",
            "title": title,
            "org": org,
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
    expired = 0

    for candidate in candidates:
        item = candidate["item"]
        record_id = candidate["id"]
        result = processed.results[record_id]
        item["_filter_reason"] = result.reason

        if result.decision == "pass":
            if deadline_status(get_deadline(item), now) == "expired":
                expired += 1
                continue
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

    summary_text = (
        f"- 조회기간: {start.strftime('%m월 %d일 %H:%M')} ~ "
        f"{now.strftime('%m월 %d일 %H:%M')} (최근 3일)\n"
        f"- 수집 상태: 정상\n"
        f"- 전체 공고: {len(items)}건\n"
        f"- 관련 공고(마감 전): {len(filtered)}건 "
        f"(강제 포함 {forced_passed}건, AI 통과 {ai_passed}건)\n"
        f"- 신규 알림: {len(new_items)}건\n"
        f"- 변경 알림: {len(changed_items)}건\n"
        f"- AI 분류 제외: {ai_rejected}건\n"
        f"- 마감 경과: {expired}건"
    )
    if system_review:
        summary_text += f"\n- 처리 오류: {system_review}건"
    send_discord(
        webhook_url,
        content=f"ODA 입찰공고 알림(일일 요약)\n{summary_text}",
    )

    send_items(webhook_url, new_items, "신규 ODA 입찰공고")
    send_items(webhook_url, changed_items, "변경된 ODA 입찰공고")
    if not any((new_items, changed_items)):
        send_discord(webhook_url, content="오늘은 새로 알릴 입찰공고가 없습니다.")

    history.save()


if __name__ == "__main__":
    main()
