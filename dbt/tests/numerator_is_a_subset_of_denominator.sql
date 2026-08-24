-- THE INVARIANT THAT MATTERS MOST, asserted for all three measures at once.
--
-- Every member in a numerator must be in that measure's denominator. If this
-- fails, the rate can exceed 1 and the report reads as outstanding performance
-- rather than as a broken join.

{% set measures = ['fct_cdc_a1c', 'fct_bcs', 'fct_cis_dtap'] %}

{% for m in measures %}
select '{{ m }}' as model, patient_id
from {{ ref(m) }}
where in_numerator and not in_denominator
{% if not loop.last %}union all{% endif %}
{% endfor %}
