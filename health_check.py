"""Is the channel still publishing?

A pipeline that dies quietly is worse than one that crashes loudly. The
workflow that runs this opens a GitHub issue when it exits non-zero, so a
stalled channel surfaces on its own instead of waiting for someone to think
to check the log.

Exit 0 = healthy, 1 = stalled, 2 = the log itself is unreadable.
"""

from __future__ import annotations

import csv
import datetime
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USED_LOG = ROOT / "used_log.csv"

# Two days of silence. The schedule publishes twice daily, so this tolerates
# one fully failed day -- transient Gemini overload has already cost us a
# slot once -- without sitting quiet through a real outage.
STALE_DAYS = 2

RECENT_WINDOW = 7


def parse(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    if not USED_LOG.exists():
        print("used_log.csv가 없습니다.")
        return 2

    try:
        rows = parse(USED_LOG)
    except Exception as exc:  # noqa: BLE001
        print(f"로그를 읽을 수 없습니다: {exc}")
        return 2

    uploaded = [r for r in rows if r.get("youtube_video_id")]
    if not uploaded:
        print("업로드 기록이 하나도 없습니다.")
        return 1

    today = datetime.date.today()
    last_date = max(
        datetime.date.fromisoformat(r["date"]) for r in uploaded if r.get("date")
    )
    silent_days = (today - last_date).days

    cutoff = today - datetime.timedelta(days=RECENT_WINDOW)
    recent = [
        r for r in rows
        if r.get("date") and datetime.date.fromisoformat(r["date"]) >= cutoff
    ]
    recent_ok = sum(1 for r in recent if r.get("youtube_video_id"))
    stages = Counter(
        r["status"].split(":", 1)[1]
        for r in recent
        if r.get("status", "").startswith("failed:")
    )

    print(f"총 발행 {len(uploaded)}편, 마지막 발행 {last_date} ({silent_days}일 전)")
    print(f"최근 {RECENT_WINDOW}일: 성공 {recent_ok}편, 실패 {sum(stages.values())}건")
    if stages:
        print("실패 단계별: " + ", ".join(f"{k} {v}건" for k, v in stages.most_common()))

    # A topic published twice is the signature of the exhaustion bug the
    # refill was built to prevent; worth catching even while healthy.
    dupes = [t for t, n in Counter(r["topic_id"] for r in uploaded).items() if n > 1]
    if dupes:
        print(f"경고: 중복 발행된 주제 {dupes[:5]}")

    if silent_days > STALE_DAYS:
        print(f"\n발행이 {silent_days}일째 멈춰 있습니다.")
        return 1

    print("\n정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
