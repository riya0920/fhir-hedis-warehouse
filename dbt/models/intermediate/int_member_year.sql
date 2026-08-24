-- One row per member per measurement year: the spine the measures join to.
--
-- AGE IS TAKEN AS OF 31 DECEMBER, which is the HEDIS convention and is the
-- reason the age-out edge cases behave as they do. Using age at the start of
-- the year, or age today, shifts whole cohorts across a band boundary.

{% set my_end = "date '" ~ var('my_end') ~ "'" %}

select
    p.patient_id,
    p.member_id,
    p.birth_date,
    p.gender,
    p.race_code,
    p.race_display,
    p.ethnicity_code,
    p.ethnicity_display,
    extract(year from {{ my_end }}) - extract(year from p.birth_date)
        - case when (extract(month from {{ my_end }}) * 100
                     + extract(day from {{ my_end }}))
                   < (extract(month from p.birth_date) * 100
                      + extract(day from p.birth_date))
               then 1 else 0 end                     as age_at_year_end,
    e.n_gaps,
    e.longest_gap_days,
    e.is_continuous
from {{ ref('stg_patient') }} p
join {{ ref('int_enrolment') }} e using (patient_id)
