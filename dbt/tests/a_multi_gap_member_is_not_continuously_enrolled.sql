-- Two short gaps must FAIL continuous enrolment even when their total is
-- inside the 45-day allowance. The rule permits ONE gap of up to 45 days, not
-- 45 days of absence, and the difference decides who is in the denominator.

select patient_id, n_gaps, longest_gap_days, is_continuous
from {{ ref('int_enrolment') }}
where n_gaps > 1
  and is_continuous
