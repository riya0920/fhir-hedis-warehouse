-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    condition_id,
    patient_id,
    code_system,
    code,
    display,
    try_cast(onset_date as date) as onset_date,
    clinical_status,
    verification_status
from {{ source('wh', 'condition') }}
