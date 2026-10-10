# research_desk/features/flows/ — 매일 수급 순위: KIS 투자자별 순매수를 받아 250일 순위를 매기는 명령과, 수급 랭킹·기업 수급 주소

## 맡는 일

- 표 네 개와 DB 함수 두 개의 유일한 주인(마이그레이션 009): `investor_flow_daily`(종목·거래일마다 한 행. 11개 주체의 순매수 `net_<키>`(백만 원)와, 순위를 매길 때 그날 값으로 고정되는 `market_cap`·`window_days`·`rank_<키>`·`ranked_at`), `investor_flow_runs`(실행마다 한 행), `investor_flow_days`(순위를 매긴 날, 최근 60개), `investor_flow_stocks`(종목별 자료 시작일), `rank_investor_flows`·`investor_flow_coverage`.
- 명령 `flows update [--codes 005930,080220]`(`jobs.py`). 매일 실행은 `scripts/run-prices.ps1`이 주가 갱신이 0으로 끝났을 때만 이어서 부른다(스크립트와 그 테스트는 `features/prices` 쪽에 있다).
- 웹 주소 `GET /api/flows`(순위를 매긴 날 하나의 순위표), `GET /api/stocks/{code}/flows`(기업 화면 수급 탭)(`router.py` → `service.py`).
- 창구 이름은 셋이다: `register_jobs`, `latest_run()`(상태 줄이 읽는다), `web_router()`.
- 계산(`logic.py`): 주체 대응과 기관합계 항등식, 거래일 거르기, 250일 순위의 PC 기준 계산(`window_ranks`), 기준일, 거래일 달력과 빠진 기간 채우기 계획, 정리 기준, 그날 시총, 실행 상태, 순위표(그룹·TS·CS·정렬).
- 설정(`settings.py`): 이 칸 전용은 `FLOWS_MAX_CALLS_PER_SEC` 하나다. KIS 계정 값(`KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_BASE_URL`, `KIS_TOKEN_CACHE`)은 주가 갱신과 같은 변수라 `core.settings`의 공용 읽기 함수로 읽는다.

## 맡지 않는 일

- KIS HTTP 호출·접근 토큰·재시도·호출 간격·키 가리기. 모두 `core.kis.KisClient`(`investor_daily(code, anchor)`)가 한다. 이 칸은 httpx를 import하지 않는다(`R9 외부 도구`).
- DB 연결 만들기는 `core.db.supabase_client`로만. 주가 표와 `reports` 표는 직접 읽지 않는다(`R11 표 주인`). 그날 시총·주가 갱신 진행 여부·수정 종가는 `prices` 창구(`snapshots`, `latest_run`, `daily_closes`), 리포트 수는 `coverage.report_counts`로만 받는다. 두 창구 모두 함수 안에서 import한다 — coverage 창구는 라우터를 묶고 있어 FastAPI를 불러오고, 이 창구는 모든 명령에서 import된다.
- 과거 날짜의 시총 만들기. 그날 주가 스냅샷의 시총만 쓴다. 예전 시총을 쓰거나 주가로 거꾸로 계산하지 않는다(사용자가 버린 안이다 — 그사이 주식 수가 바뀐 종목은 값이 틀린다).
- 상태 줄의 밀림 판정(`features/freshness`). 이 칸은 `latest_run()` 값만 준다.
- 위 두 창구 밖의 기능(`peers`, `freshness`, `reports` 등), `collector`·`tagger`·`web`·`cli` import. `freshness`가 이 칸에 기대므로 그쪽을 import하면 `R6 순환 금지`다.

## 늘 지켜야 할 것

**창구 import는 가볍다.** `cli.py`가 모든 명령에서 `research_desk.features.flows`를 import한다. 창구 최상위는 `jobs.register`(→ `register_jobs`)와 `service.latest_run`만 묶고, 그 모듈들(`jobs.py`, `service.py`, `logic.py`, `store.py`, `settings.py`)의 최상위에 FastAPI·httpx·supabase·다른 기능 창구를 두지 않는다. supabase-py(`core.db`)는 `service.connect()` 안에서, KIS 클라이언트는 `jobs.kis_client()` 안에서 import한다. `tests/test_window.py`와 입구 테스트(`research_desk/tests/test_cli.py`)가 새 프로세스로 확인한다. 라우터는 `web_router()`로만 받는다(한 번 부르면 `research_desk.features.flows.router`는 하위 모듈이 된다).

**단위.** `net_*`는 KIS 단위 그대로 백만 원이고 NOT NULL 정수다. 시총은 원, 웹 응답의 금액(`amount`, `series.flows`)은 원(× 1,000,000), 순위표 하한 `min_cap`은 억 원이다. 비율(`pct`)은 % 실수이고 여기서 반올림하지 않는다. 날짜는 `YYYY-MM-DD`, 시각은 오프셋이 붙은 ISO 글자로 저장한다.

**주체 11개(`logic.INVESTORS`).** 키·이름·KIS 필드 대응은 이 한 곳에만 둔다. 외국인 = `frgn_reg`(등록 외국인), 기관합계·연기금에는 `etc_orgt`를 더한다(masterdb에서 KRX와 대조한 대응). 키를 바꾸거나 더하면 DB 열(`net_<키>`, `rank_<키>`), DB 함수, 웹 `investor` 값, 화면 사본(`frontend/src/flows/labels.js`)이 함께 바뀌어야 한다 — 새 마이그레이션이 먼저다.

**명령의 시작.**
- 준비 확인(4, KIS·DB를 부르지 않고 아무것도 쓰지 않음): DB 설정 → KIS 키 → 종목표 읽기. 문구는 `수급 갱신을 시작하지 않았습니다. 이유: …` 한 줄. 종목표 버전 불일치는 보지 않는다. `FLOWS_MAX_CALLS_PER_SEC`가 0보다 큰 수가 아니면 `ValueError`(1), 이것도 호출 전이다.
- 시작 거절(1, `--codes`도): 가장 최근 주가 실행이나 가장 최근 수급 실행이 `running`이고 시작한 지 `RUN_ALIVE_FOR`(2시간)가 안 됐다. 더 오래된 `running`은 죽은 실행으로 보고 무시한다(그대로 둔다). 이때 KIS 클라이언트는 만들지 않는다.
- 기준일은 `logic.anchor_day(now)`(한국 시간 평일 18:00 이후면 오늘, 아니면 그 전 평일). KIS는 15:40 전에는 당일이 들어간 요청을 거절한다. KIS 클라이언트는 실행마다 하나, 접근 토큰은 종목을 묻기 전에 한 번(`ensure_token`) 받는다.

**전체 실행(`jobs._run`).** 실행 기록 `running` → 토큰(못 받으면 아무 종목도 묻지 않고 `failed`, 모든 종목을 실패로 셈) → 기준 종목 005930을 310거래일까지 거슬러 받아 달력을 만든다(이번 실행의 응답으로만, 저장된 날은 쓰지 않음. 못 받으면 다른 종목을 묻지 않고 `failed`) → 보관 범위의 저장 현황(`investor_flow_coverage`)과 자료 시작일을 한 번씩 읽음 → 종목마다 최신 30거래일 묶음과 `logic.next_fill_anchor`가 요구하는 더 오래된 묶음 → 필요한 묶음을 다 받았을 때만 그 종목의 원자료를 한 번에 upsert → 상태(`logic.run_status`, 20%) → `ok`·`partial`만 순위·날 목록·정리 → 실행 기록 마감 → 요약 한 줄.
- 원자료 upsert는 키·11개 금액·`fetched_at`만 보낸다(`store.RAW_COLUMNS`). 시총·창 일수·순위 칸을 보내면 이미 매긴 날의 값이 지워진다. 묶음 하나라도 못 받은 종목은 아무것도 저장하지 않는다 — 앞부분만 받은 짧은 창으로 순위가 고정되면 영영 틀린다.
- 받지 못한 종목이 20%를 넘는 순간 그 실행은 `failed`로 정해지고 남은 종목은 묻지 않는다.
- 순위는 기준 거래일 D(받은 가장 늦은 거래일) 하루만 매긴다: `prices.snapshots()`에서 `as_of`가 D인 시총만 모아(`logic.day_market_caps`) `rank_investor_flows(D, caps)`를 한 번 부른다. 매긴 종목이 있으면 D를 날 목록에 넣고(이미 있으면 종목 수만 고침), 없으면 넣지 않는다.
- 한 번 매긴 행은 다시 쓰지 않는다(함수가 `ranked_at`이 빈 행만 쓴다 — 날 값 고정). 이 고정을 우회하는 코드를 넣지 않는다.
- 정리: `logic.raw_cutoff(anchor)`(기준일 − 470일) 전의 원자료, 최근 60개(`KEEP_RANKED_DAYS`) 밖의 날. `failed`면 순위도 정리도 하지 않는다.
- 예외·Ctrl+C면 실행 기록을 `failed`로 닫고 예외를 그대로 올린다(오류는 1, Ctrl+C는 인터프리터의 중단 코드). 종목 단위로 이미 저장한 원자료는 남고, 다음 실행이 나머지를 받는다.

**`--codes`는 아무것도 쓰지 않는다.** 원자료·자료 시작일·순위·실행 기록 어디에도. 토큰을 먼저 받고(못 받으면 stdout 없이 stderr 한 줄, 1), 종목마다 250거래일치(9번 안팎)를 받아 `logic.window_ranks`로 이 PC에서 순위를 계산해 JSON으로 낸다(`institution_gap` 포함). 종목표에 없는 코드는 묻지 않는다. 이 경로에 쓰기를 넣지 않는다 — 상태 줄과 다음 매일 실행의 전제다.

**DB 함수와 PC 계산은 같은 규칙이다.** `rank_investor_flows`(SQL)와 `logic.window_ranks`(파이썬)는 같은 입력에 같은 순위를 내야 한다: 창 = 그날 이하 최근 250행, 순위 = 1 + 더 큰 날의 수, 같은 값은 같은 순위, 0 이하인 날도 매김, 창이 60행 미만이면 순위 없음. 한쪽을 바꾸면 다른 쪽도 바꾸고, 임시 로컬 PostgreSQL에 Supabase 역할(`anon`, `authenticated`, `service_role`)을 먼저 만든 뒤 마이그레이션을 적용해 짧은 이력·같은 값·0 이하·거래정지가 섞인 가짜 행으로 두 결과를 다시 대조한다. 단위 테스트는 SQL에 닿지 않는다(가짜 DB는 RPC를 파이썬 기준 계산으로 흉내 낸다). 함수 모양(이름·인자·돌려주는 값)은 고정이고, 바꾸려면 새 마이그레이션이다. 함수는 이 기능만 부른다(구조 검사는 `.rpc(…)`를 보지 않으므로 리뷰에서 지킨다).

**DB 읽기·쓰기(`store.py`).** 1000행을 넘을 수 있는 읽기(날 하나의 행, 저장 현황 함수, 자료 시작일, 날 목록)는 1000행(`PAGE`)씩, 쪽마다 새 쿼리를 만든다(postgrest-py의 `range()`는 offset·limit을 덧붙인다). 원자료 upsert는 500행(`UPSERT_ROWS`)씩 `(stock_code, trade_date)`로 충돌 처리하고, `RAW_COLUMNS` 밖의 열은 요청 전에 `ValueError`다. 날 하나의 순위 매긴 종목 수는 서버가 센다(`count='exact'`). "가장 최근 실행"은 `started_at` 내림차순, 같으면 `run_id` 내림차순. 요청끼리는 원자적이지 않다 — 실패하면 그 전에 쓴 것은 남고 실행은 `failed`로 닫힌다.

**웹 답(`router.py`, `service.py`).**
- 확인 순서: 형식 오류 422(FastAPI, 준비 전) → 준비 503(DB 설정 → 종목표) → 순위표만: 순위를 매긴 날이 없음 503(요청마다 확인, 기억하지 않음) → 404 고정 문장(`그날의 수급 순위가 없습니다.` / `종목을 찾을 수 없습니다.` / `이 종목은 수급 자료가 없습니다.`). 화면이 `이 종목은 수급 자료가 없습니다.`를 글자 그대로 비교한다 — 바꾸면 `frontend/src/flows/labels.js`도 바꾼다.
- 순위표는 그날 행을 읽어 `logic.ranking_table`로 거르고·점수 매기고·정렬하고 자른다. 점수는 저장하지 않고 요청마다 계산한다. 리포트 수는 돌려줄 행의 코드로 `coverage.report_counts(codes, days=365)`를 한 번 부른다(행이 없으면 부르지 않는다).
- 수급 탭은 그 종목의 최근 250행(`STOCK_ROWS`)에서 순위를 매긴 가장 늦은 날의 11개 주체 표와 기간(`PERIODS`: 21·63·126·250거래일)만큼의 계열을 만들고, 수정 종가는 `prices.daily_closes([code], since=첫 날)` 한 번으로 받는다.
- 두 창구의 `NotReady`(`리포트`, `주가`)는 바꾸지 않고 그대로 올린다. 준비를 마친 뒤의 DB 오류는 `NotReady`로 바꾸지 않는다. 웹은 KIS도 AI도 부르지 않는다.
- 핸들러 이름(`flows_ranking`, `stock_flows`)과 인자를 바꾸지 않는다(`/openapi.json`을 고정한 테스트가 있다).

**비밀 값.** 키·시크릿은 `FlowsSettings`의 표시 글자에 나오지 않는다(`repr=False`). 출력과 실행 기록 메시지의 KIS 오류 문장은 클라이언트가 가린 것만 쓴다. 종목표 원인 문장은 경로를 담을 수 있으므로 명령 출력(이 PC 터미널)에만 쓰고 웹 응답에는 고정 문장만 쓴다.

## 이 칸의 방식

| 파일 | 역할 |
|---|---|
| `__init__.py` | 창구: `register_jobs`, `latest_run`, `web_router()` |
| `jobs.py` | 명령 등록과 실행. 테스트가 바꿔 끼우는 이름: `utc_now`, `kis_client` |
| `logic.py` | 순수 계산(DB·네트워크·설정·파일 없음). 조정 가능한 값(250, 60, 310, 470, 60개, 18:00, 20%, 005930, 정렬 기본값과 한도)은 이 모듈의 상수다 |
| `store.py` | 네 표와 두 DB 함수의 읽기·쓰기. 첫 인자는 supabase-py 클라이언트 |
| `service.py` | DB·종목표 준비(처음 쓸 때, 잠금 안에서 한 번, 실패는 기억하지 않음), `latest_run`, 웹 답, 프로세스 전역 `_service` |
| `router.py` | 두 주소. 요청 값 검사까지만 하고 일은 서비스에 넘긴다. 주체·정렬·기간 값은 `logic`·`service`의 목록에서 만든다 |
| `settings.py` | `FLOWS_MAX_CALLS_PER_SEC`와 공용 KIS 값 읽기 |

## 테스트

- 실행: 저장소 루트에서 `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider research_desk/features/flows`.
- `tests/fakes.py`의 `FakeSupabase`는 네 표와 두 DB 함수를 마이그레이션 009처럼 흉내 낸다: 한 응답 1000행(RPC도), NOT NULL·CHECK, 처음 쓰는 행의 기본값, 같은 쿼리에 두 번째 `range()`를 부르면 거절, `rank_investor_flows`는 `logic.window_ranks`로, `fail_next`로 DB 장애. `FakeKis`는 가짜 시장(평일)의 KIS 클라이언트, `FakePrices`는 주가 창구다. `tests/web_world.py`는 웹 답용 작은 세상(종목표, 순위를 매긴 날 세 개, 손으로 계산한 점수)이다. 실제 KIS·DB·`.env`에는 닿지 않는다.
- 꼭 덮을 것: 주체 대응과 `etc_orgt`·항등식 차이, 상장 전 행·거래정지일, 250일 순위(같은 값, N < 60, 60 ≤ N < 250, 창 250 자르기), 기준일(18:00 경계, 주말·월요일 아침), 빠진 기간 채우기 계획(새 종목, 끊긴 채우기, 오래 꺼 둔 경우, 자료 시작일에서 멈춤), 종목 단위 저장(묶음 하나를 못 받으면 아무것도 저장하지 않음, 원자료 upsert에 순위·시총 칸이 없음), 기준 종목 실패, 그날 시총(스냅샷 기준일이 D일 때만), 순위를 매길 날과 날 목록, 20% 경계(정확히 20%는 `partial`), 토큰 실패(한 번만 묻고 종목 요청 없음), 시작 거절(2시간 넘은 `running`은 무시, `--codes`도), 정리(470일, 60개), `--codes`가 아무것도 쓰지 않음, 종료 코드 0·1·2·4와 준비 문제 때 KIS·DB 미접촉, 순위표(그룹, TS·CS·종합 점수, 평균 등수, M = 1, 네 정렬과 같은 값 처리, `limit`·`total`), 두 주소의 422 → 503 → 404 순서와 고정 문장·응답 키, 창구 import가 무거운 것을 부르지 않음, 서비스를 만들 때 아무것도 읽지 않음, `.env`에 더한 값 반영.
- DB 함수 자체는 이 테스트가 덮지 못한다. 위 "DB 함수와 PC 계산은 같은 규칙이다"의 대조로 확인한다.
