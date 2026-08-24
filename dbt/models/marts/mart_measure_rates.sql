-- The headline rates, with a WILSON interval rather than a normal one.
--
-- The normal approximation is wrong in exactly the places a measure report is
-- read hardest: small denominators and rates near 0 or 1, where it produces
-- bounds outside [0,1] and understates the width. A stratified report is full
-- of small denominators by construction, so the choice is not academic.

{% set measures = ['fct_cdc_a1c', 'fct_bcs', 'fct_cis_dtap'] %}

with unioned as (
{% for m in measures %}
    select measure, in_denominator, in_numerator from {{ ref(m) }}
    {% if not loop.last %}union all{% endif %}
{% endfor %}
),

counted as (
    select
        measure,
        count(*) filter (where in_denominator) as denominator,
        count(*) filter (where in_numerator)   as numerator
    from unioned
    group by 1
)

select
    measure,
    denominator,
    numerator,
    case when denominator = 0 then null
         else numerator::double / denominator end as rate,
    case when denominator = 0 then null else
        (numerator + 1.96 * 1.96 / 2
         - 1.96 * sqrt((numerator::double * (denominator - numerator))
                        / denominator + 1.96 * 1.96 / 4))
        / (denominator + 1.96 * 1.96)
    end as ci_low,
    case when denominator = 0 then null else
        (numerator + 1.96 * 1.96 / 2
         + 1.96 * sqrt((numerator::double * (denominator - numerator))
                        / denominator + 1.96 * 1.96 / 4))
        / (denominator + 1.96 * 1.96)
    end as ci_high
from counted
