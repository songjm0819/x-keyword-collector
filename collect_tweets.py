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
TAB_NAME = "트위터누적"          # 본문 데이터 탭(워크시트) 이름
SUMMARY_TAB_NAME = "일별집계"    # 날짜별 수집 개수를 기록하는 별도 탭 (본문 탭과 완전히 분리)

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
    collected = []
    seen_ids = set()
    current_until = until_ts
    calls = 0
    empty_streak = 0  # 연속 빈 응답 카운터

    while current_until > since_ts and calls < MAX_CALLS_PER_WINDOW:
        query = f"{keyword} since_time:{since_ts} until_time:{current_until}"
        data = api_get(api_key, {"queryType": "Latest", "query": query})
        tweets = data.get("tweets", [])
        calls += 1

        if not tweets:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"[INFO] 연속 빈 응답 3회 -> 수집 종료 (until={current_until})")
                break
            # 빈 응답이면 1시간 앞으로 슬라이딩해서 계속 탐색
            current_until -= 3600
            continue

        empty_streak = 0

        for t in tweets:
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            if t.get("isRetweet", False):
                continue
            seen_ids.add(tid)
            collected.append(t)

        try:
            earliest = min(parse_twitter_time(t["createdAt"]) for t in tweets)
        except Exception:
            break

        if earliest < current_until:
            current_until = earliest - 1
        else:
            break

        # 20개 미만이어도 종료하지 않고 since_ts까지 계속 탐색
        # (twitterapi.io가 특정 시간대에서 일시적으로 적게 반환해도 누락 없이 탐색)

    print(f"[INFO] API 호출 횟수: {calls}회")
    return collected


def get_existing_tweet_ids(ws, date_str: str) -> set:
    """
    시트에서 해당 날짜(date_str)에 이미 저장된 트윗ID를 읽어서 반환.
    중복 수집 방지용 — 이미 있는 ID는 추가하지 않음.
    """
    try:
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return set()
        existing_ids = set()
        for row in all_values[1:]:  # 헤더 제외
            if len(row) >= 3 and row[0] == date_str:
                tid = row[2]  # C열 = 트윗ID
                if tid:
                    existing_ids.add(tid)
        return existing_ids
    except Exception as e:
        print(f"[WARN] 기존 트윗ID 조회 실패: {e}", file=sys.stderr)
        return set()


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


def get_worksheet(gc, spreadsheet_id: str):
    """본문 데이터 탭('트위터누적')을 가져오거나 없으면 생성."""
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=1000, cols=len(HEADER))

    # A1 셀을 기준으로 헤더 존재 여부 확인 (다른 열에 값이 있어도 영향 없음)
    a1_value = ws.acell("A1").value
    if not a1_value:
        ws.update(f"A1:{chr(ord('A') + len(HEADER) - 1)}1", [HEADER])

    return ws


def get_summary_worksheet(gc, spreadsheet_id: str):
    """집계 전용 탭('일별집계')을 가져오거나 없으면 생성. 본문 탭과 완전히 분리되어 서로 영향 없음."""
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(SUMMARY_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SUMMARY_TAB_NAME, rows=1000, cols=2)

    a1_value = ws.acell("A1").value
    if not a1_value:
        ws.update("A1:B1", [["날짜(KST)", "트윗 수집 개수"]])

    return ws


def append_rows(ws, rows: list[list]):
    if not rows:
        return
    # table_range="A1"로 명시 -> 항상 A열 기준으로 데이터가 있는 마지막 줄 다음에 추가됨.
    # (다른 열에 값이 있어도 A~K열 기준 위치만 보고 판단하므로 칼럼이 밀릴 일이 없음)
    ws.append_rows(rows, value_input_option="USER_ENTERED", table_range="A1")


def write_daily_count_summary(summary_ws, date_str: str, count: int):
    """
    집계 전용 탭의 A, B열에 '날짜 | 수집개수'를 한 줄씩 누적 기록.
    본문 탭(트위터누적)과는 완전히 다른 탭이라 서로 영향을 주지 않음.
    """
    summary_ws.append_rows([[date_str, count]], value_input_option="USER_ENTERED", table_range="A1")


def get_gspread_client():
    sa_json = env_or_die("GOOGLE_SERVICE_ACCOUNT")
    try:
        creds_info = json.loads(sa_json)
    except json.JSONDecodeError:
        print("[ERROR] GOOGLE_SERVICE_ACCOUNT 값이 올바른 JSON이 아닙니다.", file=sys.stderr)
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)


def main():
    api_key = env_or_die("TWITTERAPI_IO_KEY")
    spreadsheet_id = env_or_die("SPREADSHEET_ID")
    keyword = os.environ.get("SEARCH_KEYWORD", "생리대")

    day_start, day_end, date_str = get_target_day_kst()
    since_ts = int(day_start.astimezone(timezone.utc).timestamp())
    until_ts = int(day_end.astimezone(timezone.utc).timestamp())

    print(f"[INFO] 수집 대상(KST): {date_str} 00:00:00 ~ 23:59:59")
    print(f"[INFO] 키워드: {keyword}")

    tweets = fetch_day_tweets(api_key, keyword, since_ts, until_ts)
    print(f"[INFO] 수집된 트윗 수: {len(tweets)}")

    gc = get_gspread_client()
    ws = get_worksheet(gc, spreadsheet_id)

    # 중복 방지: 오늘 이미 저장된 트윗ID를 조회해서 새 트윗만 필터링
    existing_ids = get_existing_tweet_ids(ws, date_str)
    new_tweets = [t for t in tweets if t.get("id") not in existing_ids]
    print(f"[INFO] 이미 저장된 트윗: {len(existing_ids)}개, 새로 추가할 트윗: {len(new_tweets)}개")

    rows = [standardize_row(t, date_str) for t in new_tweets]
    append_rows(ws, rows)

    summary_ws = get_summary_worksheet(gc, spreadsheet_id)
    write_daily_count_summary(summary_ws, date_str, len(tweets))

    print("[DONE] 구글 시트 저장 완료")


if __name__ == "__main__":
    main()
  
