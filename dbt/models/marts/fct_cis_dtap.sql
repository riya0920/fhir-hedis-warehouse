-- CIS-DTaP: children turning 2 in the measurement year, 4+ DTaP doses.
--
-- The numerator is a COUNT threshold, not an existence check, which is why the
-- events table is aggregated rather than DISTINCT-ed. Using `distinct
-- patient_id` here -- the shape every other measure in this project uses --
-- would pass a child with one dose.

with doses as (
    select patient_id, count(*) as n_doses
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'DTaP Vaccine'
    group by 1
)

select
    m.patient_id,
    'CIS-DTaP'                                  as measure,
    m.age_at_year_end = 2                       as turning_two,
    m.is_continuous                             as continuously_enrolled,
    coalesce(d.n_doses, 0)                      as n_doses,
    (m.age_at_year_end = 2 and m.is_continuous) as in_denominator,
    (m.age_at_year_end = 2
     and m.is_continuous
     and coalesce(d.n_doses, 0) >= 4)           as in_numerator
from {{ ref('int_member_year') }} m
left join doses d using (patient_id)
