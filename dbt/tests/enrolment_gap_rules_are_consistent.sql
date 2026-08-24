-- is_continuous must follow from n_gaps and longest_gap_days, not drift from
-- them. Two columns that are supposed to agree, and one derived expression --
-- if they ever disagree, one of the three has been edited alone.

select patient_id, n_gaps, longest_gap_days, is_continuous
from {{ ref('int_enrolment') }}
where is_continuous
   != (n_gaps <= 1 and longest_gap_days <= {{ var('allowable_gap_days') }})
