-- A RULE NO ROW EXERCISES IS NOT TESTED, IT IS ONLY WRITTEN DOWN.
--
-- The first dbt build of this project revealed that NO member in the corpus
-- had more than one enrolment gap, so the "at most one gap" half of the HEDIS
-- rule never fired. Every measure would have produced identical numbers if
-- that clause had been deleted.
--
-- EC6-two-short-gaps was planted for exactly this: two 20-day gaps, 40 days
-- total, inside the 45-day allowance but in TWO gaps, so it must FAIL. An
-- implementation that sums total gap-days passes it and quietly enlarges every
-- denominator.
--
-- This test fails if the corpus ever stops containing such a member, because
-- at that point the clause is unverified again and nobody would notice.

with multi_gap as (
    select count(*) as n
    from {{ ref('int_enrolment') }}
    where n_gaps > 1
)

select 'no multi-gap member: the one-gap rule is unexercised' as failure
from multi_gap
where n = 0
