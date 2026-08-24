-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    encounter_id,
    patient_id,
    code_system,
    code,
    display,
    try_cast(start_date as date) as start_date,
    try_cast(end_date as date) as end_date,
    class_code
from {{ source('wh', 'encounter') }}
