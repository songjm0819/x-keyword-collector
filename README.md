# X(트위터) 키워드 자동 수집 → 구글 시트 누적 저장

매일 한국시간(KST) 오전 7시에 자동 실행되어, **전일자(KST 00:00~23:59)** 동안
지정한 키워드가 포함된 X(트위터) 게시글을 수집하고 구글 시트에 누적 저장합니다.

- 구글 시트 문서명: `시트테스트`
- 탭 이름: `트위터누적`
- 새 데이터는 기존 데이터 아래로 계속 append (절대 덮어쓰지 않음)
- 같은 탭의 M\~N열에 `날짜 | 수집개수` 형태로 일별 집계도 자동 기록

---

## 1. 준비물

| 항목 | 설명 |
|---|---|
| twitterapi.io API 키 | [dashboard](https://twitterapi.io/dashboard)에서 발급 |
| 구글 서비스 계정 JSON | 아래 2번 참고 |
| 구글 시트 문서 ID | 시트 URL `https://docs.google.com/spreadsheets/d/문서ID/edit` 중 `문서ID` 부분 |

---

## 2. 구글 서비스 계정 만들기 (처음 한 번만)

1. [Google Cloud Console](https://console.cloud.google.com/) 접속 → 새 프로젝트 생성(또는 기존 프로젝트 사용)
2. **API 및 서비스 → 라이브러리**에서 다음 2개 API 활성화
   - Google Sheets API
   - Google Drive API
3. **API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → 서비스 계정** 생성
4. 생성된 서비스 계정 → **키 → 키 추가 → 새 키 만들기 → JSON** 선택 후 다운로드
   - 다운로드된 JSON 파일 안의 `client_email` 값을 복사해두기 (예: `xxx@xxx.iam.gserviceaccount.com`)
5. 구글 시트(`시트테스트` 문서) 열기 → 우측 상단 **공유** → 위에서 복사한 `client_email`을
   **편집자(Editor)** 권한으로 추가

---

## 3. GitHub 저장소 설정

1. 이 폴더(`x-keyword-collector`)를 새 GitHub 저장소에 업로드(push)
2. 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
   에서 아래 3개를 등록:

| Secret 이름 | 값 |
|---|---|
| `TWITTERAPI_IO_KEY` | twitterapi.io API 키 |
| `GOOGLE_SERVICE_ACCOUNT` | 다운로드한 서비스 계정 JSON 파일의 **전체 내용을 그대로 복사**해서 붙여넣기 |
| `SPREADSHEET_ID` | 구글 시트 문서 ID |

3. 키워드를 바꾸고 싶다면 `.github/workflows/daily-collect.yml` 파일의
   `SEARCH_KEYWORD: "생리대"` 부분을 원하는 키워드로 수정

---

## 4. 동작 확인

- GitHub 저장소 → **Actions** 탭 → `Daily X Keyword Collector` 워크플로우 선택
- **Run workflow** 버튼으로 즉시 1회 수동 실행 가능 (배포 직후 테스트 용도)
- 정상 동작 확인되면 이후로는 매일 KST 오전 7시에 자동 실행됨

---

## 5. 시트 컬럼 구성

| 수집일(KST) | 작성일시(KST) | 트윗ID | 작성자ID | 작성자명 | 본문 | 좋아요수 | 리트윗수 | 댓글수 | 조회수 | 트윗URL |
|---|---|---|---|---|---|---|---|---|---|---|

집계 영역(M\~N열): `날짜(KST) | 트윗 수집 개수`

---

## 6. 참고 / 주의사항

- twitterapi.io 응답이 일시적으로 비어있는 구간이 있을 수 있어, 하루 단위로
  구간을 나눠 수집하고 `until_time`을 슬라이딩하는 방식으로 누락을 최소화합니다.
- API 호출량(QPS)에 따라 요금이 발생하므로, 키워드의 일일 트윗량이 매우 많다면
  twitterapi.io 요금제를 확인하세요.
- 무료 요금제는 호출 속도 제한이 강해 트윗이 많은 키워드에서는 실행 시간이
  길어질 수 있습니다.
