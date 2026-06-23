"""
X(트위터) 키워드 자동 수집 -> 구글 시트 누적 저장
------------------------------------------------
매일 실행 시:
  1) 한국시간(KST) 기준 "어제" 00:00 ~ 23:59:59 구간을 계산
  2) twitterapi.io Advanced Search API로 해당 구간의 키워드 트윗을 모두 수집
     (until_time 슬라이딩 방식 - cursor 미사용, 무한루프 방지)
  3) 구글 시트 '트위터누적' 탭에 다음날 데이터가 기존 데이터 아래로 이어붙도록 append
  4) 같은 시트의 집계 영역(또는 별도 셀)에 '날짜별 수집 개수'를 자동 기록

환경변수로 받는 값 (GitHub Actions Secrets 로 주입):
  TWITTERAPI_IO_KEY        : twitterapi.io API 키
  GOOGLE_SERVICE_ACCOUNT   : 구글 서비스 계정 JSON 전체 내용(문자열)
  SPREADSHEET_ID           : 구글 시트 문서 ID (URL 중간의 긴 문자열)
  SEARCH_KEYWORD            (선택) : 검색 키워드. 미지정 시 기본값 "생리대"
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials


# ========= 기본 설정 =========
API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"

KST = timezone(timedelta(hours=9))

SHEET_TITLE = "시트테스트"      # 구글 시트 문서(스프레드시트) 이름 -- 단, 실제 접근은 SPREADSHEET_ID 로 함
TAB_NAME = "트위터누적"          # 탭(워크시트) 이름

HEADER = [
    "수집일(KST)",      # 이 트윗을 수집 대상으로 삼은 "어제" 날짜
    "작성일시(KST)",
    "트윗ID",
    "작성자ID",
    "작성자명",
    "본문",
    "좋아요수",
    "리트윗수",
    "댓글수",
    "조회수",
    "트윗URL",
]

MAX_CALLS_PER_WINDOW = 200   # 안전장치: 한 구간(하루)에서 무한루프 방지용 호출 상한
REQUEST_TIMEOUT = 60
RETRY_COUNT = 3
RETRY_SLEEP_SEC = 2


def env_or_die(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[ERROR] 필수 환경변수 누락: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def get_target_day_kst() -> tuple[datetime, datetime, str]:
    """
    한국시간 기준 '어제' 00:00:00 ~ 23:59:59 구간을 반환.
    GitHub Actions 서버는 UTC로 동작하므로, 현재 시각을 KST로 변환한 뒤 하루를 뺀다.
    """
    now_kst = datetime.now(timezone.utc).astimezone(KST)
    yesterday_kst = now_kst - timedelta(days=1)
    day_start = yesterday_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
    date_str = day_start.strftime("%Y-%m-%d")
    return day_start, day_end, date_str


def parse_twitter_time(s: str) -> int:
    # 예: "Wed Apr 17 12:34:56 +0000 2026"
    return int(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").timestamp())


def api_get(api_key: str, params: dict) -> dict:
    headers = {"x-api-key": api_key}
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.get(API_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_SLEEP_SEC)
    print(f"[WARN] API 호출 실패(재시도 종료): {last_err}", file=sys.stderr)
    return {"tweets": []}


def fetch_day_tweets(api_key: str, keyword: str, since_ts: int, until_ts: int) -> list[dict]:
    """
    [since_ts, until_ts] 구간의 트윗을 until_time 슬라이딩 방식으로 전부 수집.
    - 한 번 호출에 최대 20개 반환 -> 가장 이른 트윗 시각 - 1초를 다음 until_time으로 사용
    - 20개 미만 응답이면 해당 구간 종료
    - 동일 구간에서 진행이 멈추면(이전 until_time과 동일) 무한루프 방지를 위해 종료
    """
    collected = []
    seen_ids = set()
    current_until = until_ts
    calls = 0

    while current_until > since_ts and calls < MAX_CALLS_PER_WINDOW:
        query = f"{keyword} since_time:{since_ts} until_time:{current_until}"
        data = api_get(api_key, {"queryType": "Latest", "query": query})
        tweets = data.get("tweets", [])
        calls += 1

        if not tweets:
            break

        new_in_batch = 0
        for t in tweets:
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            # 일부 환경에서 -filter:retweets 가 완전히 걸러주지 않으므로 코드에서 한 번 더 확인
            if t.get("isRetweet", False):
                continue
            seen_ids.add(tid)
            collected.append(t)
            new_in_batch += 1

        try:
            earliest = min(parse_twitter_time(t["createdAt"]) for t in tweets)
        except Exception:
            break

        if earliest < current_until:
            current_until = earliest - 1
        else:
            # 더 이상 진행되지 않음 -> 무한루프 가능성, 종료
            break

        if len(tweets) < 20:
            break

    return collected


def standardize_row(t: dict, collected_date_str: str) -> list:
    author = t.get("author", {}) or {}
    created_at_raw = t.get("createdAt", "")
    try:
        created_kst = (
            datetime.strptime(created_at_raw, "%a %b %d %H:%M:%S %z %Y")
            .astimezone(KST)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception:
        created_kst = created_at_raw

    tweet_id = t.get("id", "")
    username = author.get("userName", "")
    tweet_url = f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else ""

    return [
        collected_date_str,
        created_kst,
        tweet_id,
        author.get("id", ""),
        author.get("name", ""),
        t.get("text", ""),
        t.get("likeCount", 0),
        t.get("retweetCount", 0),
        t.get("replyCount", 0),
        t.get("viewCount", 0),
        tweet_url,
    ]


def get_worksheet():
    sa_json = env_or_die("GOOGLE_SERVICE_ACCOUNT")
    spreadsheet_id = env_or_die("SPREADSHEET_ID")

    try:
        creds_info = json.loads(sa_json)
    except json.JSONDecodeError:
        print("[ERROR] GOOGLE_SERVICE_ACCOUNT 값이 올바른 JSON이 아닙니다.", file=sys.stderr)
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=1000, cols=len(HEADER) + 2)

    # 헤더가 없으면(빈 시트) 헤더 작성
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(HEADER, value_input_option="USER_ENTERED")

    return ws


def append_rows(ws, rows: list[list]):
    if not rows:
        return
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_daily_count_summary(ws, date_str: str, count: int):
    """
    같은 탭의 우측 별도 영역(예: M, N열)에 '날짜 | 수집개수' 형태로 한 줄씩 누적 기록.
    """
    summary_col_a = "M"
    summary_col_b = "N"

    header_cell = ws.acell(f"{summary_col_a}1").value
    if not header_cell:
        ws.update(f"{summary_col_a}1:{summary_col_b}1", [["날짜(KST)", "트윗 수집 개수"]])

    existing_dates = ws.col_values(13)  # M열 = 13번째
    next_row = len(existing_dates) + 1
    if next_row < 2:
        next_row = 2

    ws.update(f"{summary_col_a}{next_row}:{summary_col_b}{next_row}", [[date_str, count]])


def main():
    api_key = env_or_die("TWITTERAPI_IO_KEY")
    keyword = os.environ.get("SEARCH_KEYWORD", "생리대")

    day_start, day_end, date_str = get_target_day_kst()
    since_ts = int(day_start.astimezone(timezone.utc).timestamp())
    until_ts = int(day_end.astimezone(timezone.utc).timestamp())

    print(f"[INFO] 수집 대상(KST): {date_str} 00:00:00 ~ 23:59:59")
    print(f"[INFO] 키워드: {keyword}")

    tweets = fetch_day_tweets(api_key, keyword, since_ts, until_ts)
    print(f"[INFO] 수집된 트윗 수: {len(tweets)}")

    rows = [standardize_row(t, date_str) for t in tweets]

    ws = get_worksheet()
    append_rows(ws, rows)
    write_daily_count_summary(ws, date_str, len(tweets))

    print("[DONE] 구글 시트 저장 완료")


if __name__ == "__main__":
    main()
