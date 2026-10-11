-- migrations/010_summary_input_mode.sql
--
-- 리포트 1건 재무 분석 결과(report_summaries)에 "무엇을 읽고 분석했는지" 칸 하나를 더한다.
--   input_mode text NOT NULL DEFAULT 'text'
--     'text'       = PDF의 글자를 읽어 분석한 결과 (지금까지 저장된 모든 행)
--     'page_image' = PDF 쪽을 그림으로 AI에 보내 분석한 결과 (새 기능)
--   제약 chk_summary_input_mode: 위 두 값만 받는다. 빈 값(NULL)도 받지 않는다.
--
-- 데이터: 기존 행에 기본값 'text'가 채워지는 것 말고는 어떤 행도 바꾸지 않는다. 다른 표는 건드리지 않는다.
--   (상수 기본값이라 표를 다시 쓰지 않고, 제약을 붙일 때 기존 행을 한 번 훑어 확인만 한다.)
--
-- 적용 순서: 새 코드로 웹앱을 켜기 **전에** Supabase SQL 편집기에서 한 번 실행한다(자동 적용 장치 없음).
--   먼저 켜면 새 코드가 분석 결과를 저장할 때마다 input_mode를 함께 보내는데 DB에 그 칸이 없어
--   분석 저장이 모두 실패한다. 옛 코드는 이 칸을 보내지 않으므로 적용 뒤에도 그대로 돈다(기본값 'text').
--
-- 다시 실행해도 깨지지 않는다: 칸은 add column if not exists, 제약은 이미 있으면 건너뛴다
-- (있는 것을 지우고 다시 만들지 않는다). 전체가 한 트랜잭션이라 중간에 실패하면 아무것도 바뀌지 않는다.

BEGIN;

ALTER TABLE public.report_summaries
  ADD COLUMN IF NOT EXISTS input_mode text NOT NULL DEFAULT 'text';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.report_summaries'::regclass
      AND conname = 'chk_summary_input_mode'
  ) THEN
    ALTER TABLE public.report_summaries
      ADD CONSTRAINT chk_summary_input_mode
      CHECK (input_mode IN ('text','page_image'));
  END IF;
END
$$;

NOTIFY pgrst, 'reload schema';

COMMIT;
