-- Rates by race and ethnicity.
--
-- MISSINGNESS IS A STRATUM, not a row to drop. Race is unrecorded for a
-- sizeable minority of members, and silently excluding them reports a rate for
-- the population whose race happens to be recorded -- which is a different and
-- usually healthier population. It is emitted as 'Unknown' so the reader can
-- see how much of the panel it covers.

{% set measures = ['fct_cdc_a1c', 'fct_bcs', 'fct_cis_dtap'] %}

with unioned as (
{% for m in measures %}
    select patient_id, measure, in_denominator, in_numerator from {{ ref(m) }}
    {% if not loop.last %}union all{% endif %}
{% endfor %}
),

joined as (
    select
        u.measure,
        coalesce(nullif(trim(m.race_display), ''), 'Unknown')      as race,
        coalesce(nullif(trim(m.ethnicity_display), ''), 'Unknown') as ethnicity,
        u.in_denominator,
        u.in_numerator
    from unioned u
    join {{ ref('int_member_year') }} m using (patient_id)
)

select
    measure,
    'race'      as dimension,
    race        as stratum,
    count(*) filter (where in_denominator) as denominator,
    count(*) filter (where in_numerator)   as numerator,
    case when count(*) filter (where in_denominator) = 0 then null
         else count(*) filter (where in_numerator)::double
              / count(*) filter (where in_denominator) end as rate
from joined
group by 1, 2, 3

union all

select
    measure,
    'ethnicity' as dimension,
    ethnicity   as stratum,
    count(*) filter (where in_denominator) as denominator,
    count(*) filter (where in_numerator)   as numerator,
    case when count(*) filter (where in_denominator) = 0 then null
         else count(*) filter (where in_numerator)::double
              / count(*) filter (where in_denominator) end as rate
from joined
group by 1, 2, 3
