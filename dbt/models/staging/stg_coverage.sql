-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    patient_id,
    try_cast(span_start as date) as span_start,
    try_cast(span_end as date) as span_end
from {{ source('wh', 'coverage') }}
