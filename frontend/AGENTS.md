# frontend/ — Research Desk 화면(React + Vite). 서버의 `/api/*`만 부르고, 빌드 결과를 웹 서버가 내준다

## 맡는 일

- 화면 전체. 한 페이지 앱이고 주소의 `?view=`로 보기를 고른다: `reports`(기본, 기업 리서치 — 리포트 라이브러리 / 발행 추이 · 발행처 / 유사 기업 세 탭), `compare`(보고서 비교. 두 건을 고르기 전에는 리포트 목록이 보인다), `market`(리서치 커버리지), `review`(리포트 검토), `themes`(테마로 기업 찾기, `&q=<검색어>`). `?stock=<코드>`가 지금 기업이다.
- 파일 지도.
  - `src/main.jsx`: 메뉴·보기 전환·기업 검색·관심 기업·최근 기업, 기업 리서치 화면(리포트 라이브러리와 필터, 리포트 상세의 핵심 요약·실적 전망·투자 논리·밸류에이션 탭, "선택한 N건 분석" 진행), 보고서 비교 화면(`Comparison`). 한 파일이다.
  - `src/Coverage.jsx`: 커버리지 화면 `Coverage`(`GET /api/market`)와 기업 발행 추이 `StockActivity`(`GET /api/stocks/{code}/activity`).
  - `src/ReviewQueue.jsx`: 검토 화면(`/api/review…`).
  - `src/peers/`: `Peers.jsx`(유사 기업 탭, `GET /api/stocks/{code}/peers`), `ThemeSearch.jsx`(테마 검색, `POST /api/peers/search`), `PeerTable.jsx`(두 화면이 같이 쓰는 표 부품), `labels.js`(응답 코드 → 한국어 표기, 숫자 형식), `peers.css`.
  - `src/freshness/`: `StatusLine.jsx`(모든 화면 맨 위 상태 줄, `GET /api/freshness`), `freshness.css`.
  - `src/api.js`: 모든 호출이 거치는 `api(path, options)`, 커버리지의 기간·단위 목록, CSV 내려받기.
  - `src/styles.css`·`theme.css`·`migration.css`, `index.html`, `vite.config.js`(개발 서버가 `/api`를 8520으로 넘기는 설정).
- 빌드 결과 `dist/`(git 무시)는 웹 서버가 `/`(`dist/index.html`)와 `/assets`(`dist/assets/`)로 내준다.

## 맡지 않는 일

- 서버의 규칙. 분석 대상 판정, 분류, 비교 수치 계산, 리포트 수·반응·후보 판정은 서버가 하고 화면은 응답을 보여 준다. 화면에서 하는 계산은 받은 목록을 거르는 것뿐이다(라이브러리의 발행처·유형·기간·검색어·분석 상태 필터, 유사 기업 표의 후보·리포트 없음·덜 오름·같은 산업분류 숨기기·거래대금 하한 필터). 응답에 없는 값을 화면에서 만들어 내지 않는다.
- 비밀 값과 경로. 화면 코드에는 키·DB 주소·파일 경로가 없다. PDF는 응답의 `pdf_url`로만 연다.
- 파이썬 코드. 화면은 `/api` 주소와 응답 모양에만 기댄다. 서버 폴더 이름(`coverage`)과 보기 이름(`market`)·주소(`/api/market`)가 달라도 맞추려고 보기 이름이나 주소를 바꾸지 않는다.
- 서버 쪽 테스트가 고정한 주소·응답 키·문장을 바꾸는 일. 화면이 새 키나 새 문장을 쓰려면 서버와 그 테스트가 먼저 바뀌어야 한다.

## 늘 지켜야 할 것

- **서버 문장을 글자 그대로 비교하는 곳.** `src/peers/Peers.jsx`의 `NO_PEER_DATA`(`이 종목은 유사 기업 자료가 없습니다.`)는 404의 `detail`과 같을 때만 빈 안내 화면("신규 상장으로 사업보고서가 아직 없거나…")이 되고, `NO_SEGMENT`(`그 사업부문이 없습니다.`)는 422의 `detail`과 같을 때만 "기본 부문으로 보기" 버튼이 된다. 한 글자라도 다르면 일반 오류 화면과 "다시 시도" 버튼이다. 서버 문장을 바꾸면 이 상수도 같이 바꾼다.
- **`api()`의 오류 처리.** 응답이 2xx가 아니면 `detail`이 문자열일 때 그 문장을, 아니면(FastAPI 검증 오류처럼 `detail`이 목록) `요청을 완료하지 못했습니다.`를 `Error`로 던진다. 그래서 사용자에게 보여야 할 오류는 서버가 문자열 `detail`로 내야 한다. 응답 본문이 JSON이 아니면(`Invalid host header` 400 같은 일반 글자) `response.json()`에서 예외가 난다. `AbortError`는 오류로 보여 주지 않는다 — 화면마다 `AbortController`로 이전 요청을 취소한다.
- **서버 값 목록의 사본.** `src/ReviewQueue.jsx`의 `reasons`(`foreign` 해외 상장사, `fund` 펀드 · ETF, `digital` 디지털자산, `private` 비상장사, `ir_self` 기업 IR 자료)는 서버의 분석 대상 외 사유 5개를 옮겨 적은 것이다. 서버에 사유가 더해지거나 바뀌어도 화면은 모른다 — 함께 고친다. `src/peers/labels.js`의 `TIER`·`REACTION`·`COVERAGE`·`REASON`·`FLAG`도 같다: 서버에 새 코드 값이 생기면 여기에 더한다(없으면 코드 글자가 그대로 보인다). 모르는 값은 `—`, 매출 비중 -1은 `비중 미상`.
- **상태 줄의 문장.** 주가 쪽 문장은 서버의 `prices.note`를 그대로 쓰고, 리포트 쪽은 응답에 문장이 없어 화면이 만든다: `latest_at`이 있으면 `리포트가 {M}월 {D}일 이후 들어오지 않았습니다`, 없으면 `리포트 자료가 없습니다`. 주소가 실패하면 회색 `자료 기준일 확인 불가`(실패 문장은 마우스를 올리면 보이는 `title`). 앱이 뜰 때와 "다시 확인" 버튼에서만 묻는다.
- **분석 실행.** "선택한 N건 분석"과 리포트 하나의 분석 버튼은 같은 `runAnalysis`다. 고른 리포트 중 `report_type`이 `단일종목`인 것만, 한 번에 한 건씩 차례로 `POST /api/reports/{id}/analyze`를 보내고, 받은 리포트로 목록의 그 행을 바로 바꾼다. 진행 상태는 `waiting → running → done`(`analysis_reused`면 `cached`) / `error`(오류 문장 보관) / `cancelled`(중단 버튼 뒤 남은 건). 중단은 지금 보내진 건이 끝난 뒤부터 멈춘다. 여러 건을 동시에 보내지 않는다.
- **비교.** 수치는 `GET /api/compare?left=&right=`로 받고, 해석문은 "비교 분석" 버튼의 `POST /api/compare/analyze`로만 만든다. 두 건의 앞·뒤 순서는 서버가 정하므로 고른 순서와 상관없다. 메뉴의 "보고서 비교"를 고른 건 없이 누르면, 상세 분석이 있는 리포트가 2건 이상이면 그중에서, 아니면 요약이 있는 리포트 중에서 첫 건과 같은 발행처의 다음 건(없으면 둘째 건)을 골라 준다. 둘 다 2건 미만이면 `목록에서 비교할 보고서 두 개를 선택해 주세요.`, 고른 건이 2건이 아니면(1건, 3건 이상) `비교할 단일종목 보고서 두 건을 선택해 주세요.`를 띄운다.
- **PDF 쪽 링크.** 원문 근거는 `pdf_url#page=N`으로 연다. `N`은 분석 결과의 `evidence.page`(PDF의 물리적 쪽 번호, 1부터)다. 검토 화면의 쪽 그림은 `/api/review/{id}/pages/{n}`(1~3쪽)이다.
- **브라우저 저장.** `localStorage`의 `research:lastStock`(마지막에 연 기업, 처음 값 `016360`)·`research:recent`(최근 연 기업 5개). `sessionStorage`의 `research:review`(검토의 건너뛴 id 목록 `skipped`, 처리 건수 `counts{verify, oos, retag}`, 마지막 작업 `last`와 그 `undo_token`). 건너뛰기는 서버에 보내지 않으므로 탭을 닫으면 사라진다. "마지막 작업 되돌리기"는 `last`가 건너뛰기면 서버를 부르지 않고 목록에서만 빼고, 처리였으면 `POST /api/review/undo/{token}`을 부른다 — 서버가 다시 켜진 뒤에는 서버의 거절 문장이 그대로 보인다.
- **개발 서버.** `npm --prefix frontend run dev`(5173)로 띄운 화면은 `vite.config.js`의 프록시로 `/api`를 8520에 넘긴다. 서버는 출처 `127.0.0.1:5173`·`localhost:5173`의 쓰기 요청을 허용하므로 다른 포트로 띄우면 쓰기(검토 처리, 관심 기업, 분석)가 403이 된다.
- **빌드 결과.** `dist/index.html`과 `dist/assets/`가 함께 있어야 한다. `dist`는 있는데 `dist/assets`가 없으면 웹 서버가 켜지지 않는다(빌드를 중간에 멈춘 경우) — `npm --prefix frontend run build`를 끝까지 다시 돌린다. 다시 빌드하면 서버를 다시 켜지 않아도 새 화면이 나간다(`/`는 요청마다 `index.html`을 읽는다).
- 사용자 문구는 한국어다. 서버가 보낸 문장은 고치지 않고 그대로 보여 준다.

## 이 칸의 방식

- 상태 관리는 React의 `useState`·`useEffect`·`useMemo`·`useRef`뿐이고, 화면 사이 전달은 `main.jsx`가 넘기는 props다(예: `onStock`, `onChanged`; 비교 화면은 `ids`·`reports`·`onBack`·`onChange`·`onAnalyze`·`busy`). 라우터·상태 라이브러리를 쓰지 않는다. 아이콘은 `lucide-react`.
- 보기 전환은 `main.jsx`의 `view` 상태 하나이고 바뀔 때마다 주소의 `?view=`를 바꾼다(`history.replaceState`, 새로고침 없음). 기업을 고르면(`pickStock`) `reports` 보기로 돌아가고 `?stock=`을 바꾼다. 테마 검색 결과와 유사 기업 표에서 회사를 누르면 그 회사의 유사 기업 탭(`stockMode = "peers"`)이 열린다.
- 새 화면은 `src/<기능>/` 폴더(유사 기업·상태 줄과 같은 방식)에 두고, `main.jsx`에는 import 한 줄, 메뉴 항목 하나, 보기 분기 한 줄을 더한다. 스타일은 그 폴더의 css에 둔다. 서버 코드 값의 표기는 그 폴더의 `labels.js` 같은 한 곳에 모은다.
- `main.jsx` 분할이 남아 있다(메뉴·보기 전환 틀 / 공용 부품 / 기능별 폴더). 나눌 때 보기 이름과 서버 주소는 그대로 두고, 분할 전후 화면이 같은지 캡처로 비교한다.
- 빌드와 실행을 한 번에: `powershell -File scripts\start-workspace.ps1`(`node_modules`가 없으면 `npm ci`, 그다음 `npm run build`, 그다음 웹 서버). Node.js 22.12 이상(또는 20.19 이상).

## 테스트

- 자동 테스트가 없다(만들어져 있고 실제 화면으로만 확인한 상태). 바꾼 뒤의 확인 순서: `npm --prefix frontend run build`가 오류 없이 끝남 → `python -m research_desk web`으로 켬 → 다섯 보기(기업 리서치·보고서 비교·리서치 커버리지·리포트 검토·테마로 기업 찾기)와 기업 화면의 세 탭(리포트 라이브러리 / 발행 추이 · 발행처 / 유사 기업), 상태 줄을 한 번씩 연다.
- 서버 문장을 비교하는 자리(`NO_PEER_DATA`, `NO_SEGMENT`)나 값 목록 사본(`reasons`, `labels.js`)을 고쳤으면 그 경우를 실제로 띄워 본다: 유사 기업 자료가 없는 종목(신규 상장), 없는 부문 번호, 검토 화면의 제외 사유 선택.
- 서버 주소·응답 키·문장은 서버 쪽 테스트가 고정한다. 화면이 새 키나 문장을 쓰게 되면 서버 쪽 변경과 함께 다룬다.
