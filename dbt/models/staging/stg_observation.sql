-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    observation_id,
    patient_id,
    code_system,
    code,
    display,
    try_cast(effective_date as date) as effective_date,
    value_num,
    value_unit,
    status
from {{ source('wh', 'observation') }}
