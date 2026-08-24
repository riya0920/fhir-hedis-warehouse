-- Every clinical event that falls in a value set, unioned to ONE grain:
-- (patient_id, value_set_name, event_date).
--
-- Unioning here rather than in each measure means a new measure joins to one
-- table instead of re-deriving the value-set join against four source tables,
-- and a mistake in that join gets made once instead of four times.

{% set sources = [
    ('stg_condition',    'onset_date'),
    ('stg_observation',  'effective_date'),
    ('stg_procedure',    'performed_date'),
    ('stg_encounter',    'start_date'),
    ('stg_immunization', 'occurrence_date'),
] %}

with events as (
{% for model, date_col in sources %}
    select
        patient_id,
        code_system,
        code,
        {{ date_col }} as event_date,
        '{{ model }}'  as source_model
    from {{ ref(model) }}
    {% if not loop.last %}union all{% endif %}
{% endfor %}
)

select
    e.patient_id,
    v.value_set_name,
    e.code_system,
    e.code,
    e.event_date,
    e.source_model
from events e
join {{ ref('stg_value_set') }} v
  on v.code_system = e.code_system
 and v.code        = e.code
where e.event_date is not null
