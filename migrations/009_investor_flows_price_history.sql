-- migrations/009_investor_flows_price_history.sql
--
-- 수급 강도 랭킹: 수급 표 4개와 DB 함수 2개, 주가 일별 이력 표 1개.
--   수급 (주인 features/flows)   : investor_flow_daily, investor_flow_runs, investor_flow_days,
--                                  investor_flow_stocks,
--                                  함수 rank_investor_flows, investor_flow_coverage
--   주가 (주인 features/prices)  : stock_price_daily
--   수급 표는 매일 수급 갱신(flows update)이 쓰고, 두 함수도 그 명령만 부른다. 주가 일별 이력은
--   매일 주가 갱신(prices update)이 쓴다. 다른 기능은 이 표들을 주인 기능의 공개 창구로만 읽는다.
--
-- 적용: Supabase SQL 편집기에서 한 번 실행한다(자동 적용 장치 없음). 데이터는 바꾸지 않는다.
-- 다시 실행해도 깨지지 않는다: create ... if not exists, create or replace function,
-- RLS·권한 문은 몇 번 실행해도 결과가 같다.
--
-- RLS: 모든 새 표에서 켜고 정책은 두지 않는다 = anon/authenticated 거부, service_role(서비스 키)만
-- 우회한다. 기존 표와 같다.

begin;

-- ============================================================
-- 1. 수급 (주인: features/flows)
-- ============================================================

-- 종목·거래일마다 한 행. 순매수 거래대금은 백만 원(KIS 단위 그대로)이고, 열은 화면의 주체 순서다.
-- 원자료 upsert는 키·net_*·fetched_at만 보낸다. 시총·창 행 수·순위·순위 시각은 아래
-- rank_investor_flows 함수만 쓴다: net_* 칸이 NOT NULL이라 시총만 담은 REST upsert는 충돌 처리 전에
-- NOT NULL 검사에서 거절되고, 행마다 UPDATE를 보내면 하루 2,500번 넘게 요청해야 한다.
create table if not exists public.investor_flow_daily (
  stock_code        text         not null,
  trade_date        date         not null,

  -- 순매수 거래대금, 백만 원 (KIS 단위 그대로)
  net_pe_fund       integer      not null,   -- 사모
  net_foreigner     integer      not null,   -- 외국인 (KRX '외국인' = 등록 외국인)
  net_institution   integer      not null,   -- 기관합계
  net_pension       integer      not null,   -- 연기금
  net_trust         integer      not null,   -- 투신
  net_fin_invest    integer      not null,   -- 금융투자
  net_insurance     integer      not null,   -- 보험
  net_bank          integer      not null,   -- 은행
  net_other_fin     integer      not null,   -- 기타금융
  net_other_corp    integer      not null,   -- 기타법인
  net_individual    integer      not null,   -- 개인

  -- 순위를 매길 때 rank_investor_flows가 정하고, 그 뒤로는 다시 쓰지 않는다(날 값 고정).
  market_cap        bigint,                  -- 그날 시총(원)
  window_days       smallint,                -- 그날 순위의 창 행 수 N
  rank_pe_fund      smallint,                -- 250일 순위. 비어 있으면 순위 없음
  rank_foreigner    smallint,
  rank_institution  smallint,
  rank_pension      smallint,
  rank_trust        smallint,
  rank_fin_invest   smallint,
  rank_insurance    smallint,
  rank_bank         smallint,
  rank_other_fin    smallint,
  rank_other_corp   smallint,
  rank_individual   smallint,
  ranked_at         timestamptz,             -- 순위를 매긴 시각. 비어 있으면 아직 매기지 않음

  fetched_at        timestamptz  not null default now(),   -- 마지막으로 받아 쓴 시각

  primary key (stock_code, trade_date)
);

-- 날 하나 읽기(순위표), 오래된 원자료 정리
create index if not exists ix_investor_flow_daily_trade_date
  on public.investor_flow_daily (trade_date);

-- flows update 실행 기록. 실행마다 한 행: running으로 시작해 ok / partial / failed로 마감한다.
-- 가장 최근 실행은 started_at 내림차순, 같으면 run_id 내림차순이다(주가와 같다).
create table if not exists public.investor_flow_runs (
  run_id         bigserial   primary key,
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  status         text        not null default 'running',
  as_of          date,                                -- 기준일(받은 가장 늦은 거래일)
  stocks_total   int,
  stocks_ok      int,
  stocks_failed  int,
  days_ranked    int,                                 -- 순위를 매긴 날 수
  message        text,

  constraint chk_investor_flow_runs_status check (status in ('running','ok','partial','failed'))
);

-- 마지막 실행 찾기
create index if not exists ix_investor_flow_runs_started
  on public.investor_flow_runs (started_at desc);

-- 순위를 매긴 날 목록. 화면은 이 목록에 오른 날만 보여 준다.
create table if not exists public.investor_flow_days (
  trade_date     date        primary key,
  ranked_at      timestamptz not null,
  stocks_ranked  int         not null,                -- 그날 사모 순위가 있는 종목 수
  run_id         bigint                               -- 그날을 기록한 실행 (investor_flow_runs.run_id)
);

-- 종목별 수집 상태.
create table if not exists public.investor_flow_stocks (
  stock_code   text        primary key,
  listed_from  date,                                  -- 자료 시작일. 상장 전 날을 만났을 때만 정한다
  updated_at   timestamptz not null default now()
);

-- ============================================================
-- 2. 주가 일별 이력 (주인: features/prices)
-- ============================================================

-- 수정 종가 일별 이력. 종목·거래일마다 한 행. 008의 stock_price_snapshot(종목당 최신 한 행)은
-- 그대로 두고, 수급 탭의 주가선에 쓸 이력을 여기에 둔다.
create table if not exists public.stock_price_daily (
  stock_code  text    not null,
  trade_date  date    not null,
  close       bigint  not null,                       -- 수정 종가(원)

  primary key (stock_code, trade_date)
);

-- 오래된 이력 정리
create index if not exists ix_stock_price_daily_trade_date
  on public.stock_price_daily (trade_date);

-- ============================================================
-- 3. RLS: 정책 없음 = anon/authenticated 거부, service_role만 우회 (기존 표와 같음)
-- ============================================================
alter table public.investor_flow_daily  enable row level security;
alter table public.investor_flow_runs   enable row level security;
alter table public.investor_flow_days   enable row level security;
alter table public.investor_flow_stocks enable row level security;
alter table public.stock_price_daily    enable row level security;

-- ============================================================
-- 4. DB 함수 (주인: features/flows, 서비스 키 전용)
-- ============================================================
-- 둘 다 flows update만 부른다. REST의 한 응답 1000행 한도는 RPC에도 걸린다.

-- rank_investor_flows: 날 p_day의 250일 순위와 그날 시총을 한 번에 쓴다(날 값 고정).
--   p_caps = {"종목코드": 시총(원 정수), …}. 값이 null인 종목은 목록에 없는 것으로 본다.
--   trade_date = p_day이고 ranked_at이 비었고 종목코드가 p_caps에 있는 행마다 다음을 쓴다.
--     market_cap  = p_caps의 그 종목 값
--     window_days = 창의 행 수 N. 창 = 그 종목의 trade_date <= p_day인 행 중 최근 p_window행(그날 포함)
--     rank_<키>   = 1 + (창 안에서 그 주체 순매수가 그날 값보다 큰 행의 수). 같은 값은 같은 순위이고
--                   더 큰 날만 센다. 순매수가 0 이하인 날도 매긴다. N < p_min_days면 11개 모두 비운다
--     ranked_at   = now()
--   p_caps에 없는 종목의 행은 바꾸지 않는다: 순위를 매기지 않은 채 남아, 나중에 시총과 함께 다시
--   부르면 그때 매긴다. 이미 순위를 매긴 행은 시총도 순위도 바꾸지 않는다. 바꾼 행 수를 돌려준다.
--   PC 쪽 기준 계산은 research_desk/features/flows/logic.py의 window_ranks다(같은 입력에 같은 값).
--   한 문장 UPDATE다: 그날 행마다 그 종목의 기본 키 범위에서 최근 p_window행만 읽는다.
--   인자가 비었거나 범위 밖이면(p_window는 1~32767) 아무것도 쓰지 않고 오류를 낸다.
--   함수 모양(이름·인자·돌려주는 값)은 고정이다.
create or replace function public.rank_investor_flows(
  p_day       date,
  p_caps      jsonb default '{}'::jsonb,
  p_window    int   default 250,
  p_min_days  int   default 60
)
returns int
language plpgsql
volatile
security invoker
set search_path = public
as $$
declare
  v_changed int;
begin
  if p_day is null then
    raise exception 'rank_investor_flows: p_day(순위를 매길 날)가 비어 있습니다';
  end if;
  if p_caps is null or jsonb_typeof(p_caps) <> 'object' then
    raise exception 'rank_investor_flows: p_caps는 {"종목코드": 시총} 모양의 JSON 객체여야 합니다';
  end if;
  if p_window is null or p_window < 1 or p_window > 32767 then
    raise exception 'rank_investor_flows: p_window는 1~32767이어야 합니다(받은 값: %)', p_window;
  end if;
  if p_min_days is null then
    raise exception 'rank_investor_flows: p_min_days가 비어 있습니다';
  end if;

  update public.investor_flow_daily d
     set market_cap       = r.market_cap,
         window_days      = r.n::smallint,
         rank_pe_fund     = case when r.n >= p_min_days then r.rank_pe_fund::smallint end,
         rank_foreigner   = case when r.n >= p_min_days then r.rank_foreigner::smallint end,
         rank_institution = case when r.n >= p_min_days then r.rank_institution::smallint end,
         rank_pension     = case when r.n >= p_min_days then r.rank_pension::smallint end,
         rank_trust       = case when r.n >= p_min_days then r.rank_trust::smallint end,
         rank_fin_invest  = case when r.n >= p_min_days then r.rank_fin_invest::smallint end,
         rank_insurance   = case when r.n >= p_min_days then r.rank_insurance::smallint end,
         rank_bank        = case when r.n >= p_min_days then r.rank_bank::smallint end,
         rank_other_fin   = case when r.n >= p_min_days then r.rank_other_fin::smallint end,
         rank_other_corp  = case when r.n >= p_min_days then r.rank_other_corp::smallint end,
         rank_individual  = case when r.n >= p_min_days then r.rank_individual::smallint end,
         ranked_at        = now()
    from (
      -- 그날의 순위를 매길 행마다 그 종목의 창(최근 p_window행)을 읽어 주체별로 센다.
      select t.stock_code,
             (p_caps ->> t.stock_code)::numeric::bigint as market_cap,
             w.*
        from public.investor_flow_daily t
        cross join lateral (
          select count(*)                                                            as n,
                 1 + count(*) filter (where x.net_pe_fund     > t.net_pe_fund)     as rank_pe_fund,
                 1 + count(*) filter (where x.net_foreigner   > t.net_foreigner)   as rank_foreigner,
                 1 + count(*) filter (where x.net_institution > t.net_institution) as rank_institution,
                 1 + count(*) filter (where x.net_pension     > t.net_pension)     as rank_pension,
                 1 + count(*) filter (where x.net_trust       > t.net_trust)       as rank_trust,
                 1 + count(*) filter (where x.net_fin_invest  > t.net_fin_invest)  as rank_fin_invest,
                 1 + count(*) filter (where x.net_insurance   > t.net_insurance)   as rank_insurance,
                 1 + count(*) filter (where x.net_bank        > t.net_bank)        as rank_bank,
                 1 + count(*) filter (where x.net_other_fin   > t.net_other_fin)   as rank_other_fin,
                 1 + count(*) filter (where x.net_other_corp  > t.net_other_corp)  as rank_other_corp,
                 1 + count(*) filter (where x.net_individual  > t.net_individual)  as rank_individual
            from (
              select h.net_pe_fund, h.net_foreigner, h.net_institution, h.net_pension,
                     h.net_trust, h.net_fin_invest, h.net_insurance, h.net_bank,
                     h.net_other_fin, h.net_other_corp, h.net_individual
                from public.investor_flow_daily h
               where h.stock_code = t.stock_code
                 and h.trade_date <= p_day
               order by h.trade_date desc
               limit p_window
            ) x
        ) w
       where t.trade_date = p_day
         and t.ranked_at is null
         and p_caps ->> t.stock_code is not null
    ) r
   where d.stock_code = r.stock_code
     and d.trade_date = p_day
     and d.ranked_at is null;   -- 동시에 부른 호출이 먼저 매긴 행도 다시 쓰지 않는다

  get diagnostics v_changed = row_count;
  return v_changed;
end;
$$;

-- investor_flow_coverage: trade_date >= p_since인 행을 종목마다 센 값(행 수, 첫날, 마지막 날).
--   종목코드 순으로 정렬해서, 부르는 쪽이 1000행씩 나눠 읽어도 쪽이 일정하다.
create or replace function public.investor_flow_coverage(p_since date)
returns table (stock_code text, days int, first_day date, last_day date)
language sql
stable
security invoker
set search_path = public
as $$
  select f.stock_code,
         count(*)::int     as days,
         min(f.trade_date) as first_day,
         max(f.trade_date) as last_day
    from public.investor_flow_daily f
   where f.trade_date >= p_since
   group by f.stock_code
   order by f.stock_code;
$$;

-- 서비스 역할만 실행한다. Postgres는 새 함수의 실행 권한을 PUBLIC에, Supabase는 anon·authenticated에도
-- 주므로 거둬들인다.
revoke execute on function public.rank_investor_flows(date, jsonb, int, int)
  from public, anon, authenticated;
revoke execute on function public.investor_flow_coverage(date)
  from public, anon, authenticated;
grant execute on function public.rank_investor_flows(date, jsonb, int, int)
  to service_role;
grant execute on function public.investor_flow_coverage(date)
  to service_role;

-- PostgREST가 새 표와 함수를 바로 보게 한다.
notify pgrst, 'reload schema';

commit;
