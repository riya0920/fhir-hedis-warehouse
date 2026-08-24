-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    procedure_id,
    patient_id,
    code_system,
    code,
    display,
    try_cast(performed_date as date) as performed_date,
    status
from {{ source('wh', 'procedure') }}
