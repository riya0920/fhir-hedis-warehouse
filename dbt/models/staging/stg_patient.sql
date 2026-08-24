-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    patient_id,
    member_id,
    try_cast(birth_date as date) as birth_date,
    gender,
    race_code,
    race_display,
    ethnicity_code,
    ethnicity_display
from {{ source('wh', 'patient') }}
