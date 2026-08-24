-- CDC-A1C: HbA1c testing for members 18-75 with diabetes.
--
-- Emitted as ONE ROW PER MEMBER with boolean flags rather than as a single
-- rate. A rate cannot be audited: when the number moves, the only way to find
-- out why is to ask which members entered or left, and that requires the
-- member-level table to still exist.

{% set my_start = "date '" ~ var('my_start') ~ "'" %}
{% set my_end   = "date '" ~ var('my_end')   ~ "'" %}

with diabetes as (
    select distinct patient_id
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'Diabetes'
),

hospice as (
    select distinct patient_id
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'Hospice Encounter'
),

a1c as (
    select distinct patient_id
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'HbA1c Laboratory Test'
      and event_date between {{ my_start }} and {{ my_end }}
)

select
    m.patient_id,
    'CDC-A1C'                                   as measure,
    d.patient_id is not null                    as has_diabetes,
    m.age_at_year_end between 18 and 75         as in_age_band,
    m.is_continuous                             as continuously_enrolled,
    h.patient_id is not null                    as excluded_hospice,
    (d.patient_id is not null
     and m.age_at_year_end between 18 and 75
     and m.is_continuous
     and h.patient_id is null)                  as in_denominator,
    (d.patient_id is not null
     and m.age_at_year_end between 18 and 75
     and m.is_continuous
     and h.patient_id is null
     and a.patient_id is not null)              as in_numerator
from {{ ref('int_member_year') }} m
left join diabetes d using (patient_id)
left join hospice  h using (patient_id)
left join a1c      a using (patient_id)
