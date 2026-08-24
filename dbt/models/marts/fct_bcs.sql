-- BCS: breast cancer screening, women 50-74.
--
-- THE 27-MONTH LOOKBACK IS THE DETAIL MOST OFTEN GOT WRONG. Screening is
-- biennial, so a 12-month lookback fails every woman correctly screened 18
-- months ago. The extra three months allow for scheduling slippage. It is
-- written as an explicit start date rather than an interval so the boundary is
-- visible in the compiled SQL.

{% set my_end = "date '" ~ var('my_end') ~ "'" %}

with lookback as (
    select
        make_date(extract(year from {{ my_end }})::int - 2, 10, 1) as lookback_start
),

mastectomy as (
    select distinct patient_id
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'Bilateral Mastectomy'
),

hospice as (
    select distinct patient_id
    from {{ ref('int_value_set_events') }}
    where value_set_name = 'Hospice Encounter'
),

mammogram as (
    select distinct e.patient_id
    from {{ ref('int_value_set_events') }} e
    cross join lookback l
    where e.value_set_name = 'Mammography'
      and e.event_date between l.lookback_start and {{ my_end }}
)

select
    m.patient_id,
    'BCS'                                       as measure,
    m.gender = 'female'                         as is_female,
    m.age_at_year_end between 50 and 74         as in_age_band,
    m.is_continuous                             as continuously_enrolled,
    (x.patient_id is not null
     or h.patient_id is not null)               as excluded,
    (m.gender = 'female'
     and m.age_at_year_end between 50 and 74
     and m.is_continuous
     and x.patient_id is null
     and h.patient_id is null)                  as in_denominator,
    (m.gender = 'female'
     and m.age_at_year_end between 50 and 74
     and m.is_continuous
     and x.patient_id is null
     and h.patient_id is null
     and g.patient_id is not null)              as in_numerator
from {{ ref('int_member_year') }} m
left join mastectomy x using (patient_id)
left join hospice    h using (patient_id)
left join mammogram  g using (patient_id)
