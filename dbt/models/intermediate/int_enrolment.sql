-- CONTINUOUS ENROLMENT, THE MODEL EVERY MEASURE DEPENDS ON.
--
-- HEDIS allows ONE gap of at most 45 days in the measurement year. That is not
-- the same as "45 days missing in total": two 30-day gaps is two gaps and
-- fails, while one 45-day gap passes. Collapsing that to a total-days test is
-- the most common way to get this wrong, and it silently ENLARGES the
-- denominator with members who were not continuously covered.
--
-- The walk below mirrors the cursor loop in src/measures.py. It is deliberately
-- a SECOND, INDEPENDENT implementation: tests/test_dbt_parity.py asserts the
-- two agree member-for-member, which is worth more than either one alone.

{% set my_start = "date '" ~ var('my_start') ~ "'" %}
{% set my_end   = "date '" ~ var('my_end')   ~ "'" %}

with clipped as (
    -- clip every span to the measurement year, dropping those outside it
    select
        patient_id,
        greatest(span_start, {{ my_start }}) as s,
        least(span_end, {{ my_end }}) as e
    from {{ ref('stg_coverage') }}
    where span_start <= {{ my_end }}
      and span_end   >= {{ my_start }}
),

-- the running maximum END of all EARLIER spans is the cursor. A running max
-- rather than the previous row's end, because spans can overlap and a
-- contained span must not reopen a gap that an earlier longer one closed.
marked as (
    select
        patient_id,
        s,
        e,
        max(e) over (
            partition by patient_id
            order by s
            rows between unbounded preceding and 1 preceding
        ) as prev_max_e
    from clipped
),

leading_and_internal_gaps as (
    select
        patient_id,
        case
            when prev_max_e is null
                then case when s > {{ my_start }}
                          then date_diff('day', {{ my_start }}, s)
                          else 0 end
            else case when s > prev_max_e + interval 1 day
                      then date_diff('day', prev_max_e + interval 1 day, s)
                      else 0 end
        end as gap_days
    from marked
),

-- a member covered until November has a gap at the END of the year, and it is
-- invisible to any rule that only looks between spans
trailing_gap as (
    select
        patient_id,
        case when max(e) + interval 1 day <= {{ my_end }}
             then date_diff('day', max(e) + interval 1 day, {{ my_end }}) + 1
             else 0 end as gap_days
    from clipped
    group by 1
),

all_gaps as (
    select * from leading_and_internal_gaps
    union all
    select * from trailing_gap
),

per_member as (
    select
        patient_id,
        count(*) filter (where gap_days > 0) as n_gaps,
        coalesce(max(gap_days), 0)           as longest_gap_days
    from all_gaps
    group by 1
)

select
    p.patient_id,
    coalesce(m.n_gaps, 1) as n_gaps,
    -- A member with NO overlapping span at all is not "zero gaps"; they have
    -- one gap the length of the whole year. Defaulting to 0 here would enrol
    -- every member who was never covered.
    coalesce(m.longest_gap_days,
             date_diff('day', {{ my_start }}, {{ my_end }}) + 1)
        as longest_gap_days,
    coalesce(m.n_gaps, 1) <= 1
        and coalesce(m.longest_gap_days,
                     date_diff('day', {{ my_start }}, {{ my_end }}) + 1)
            <= {{ var('allowable_gap_days') }}
        as is_continuous
from {{ ref('stg_patient') }} p
left join per_member m using (patient_id)
