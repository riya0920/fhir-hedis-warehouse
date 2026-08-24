-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    immunization_id,
    patient_id,
    code_system,
    code,
    display,
    try_cast(occurrence_date as date) as occurrence_date,
    status
from {{ source('wh', 'immunization') }}
