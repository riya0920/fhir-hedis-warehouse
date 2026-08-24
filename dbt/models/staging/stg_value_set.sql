-- Staging is a rename-and-cast layer only. No business logic lives
-- here, so that a change in measure definition never requires
-- touching the layer that reads the source.
select
    value_set_name,
    code_system,
    code
from {{ source('wh', 'value_set') }}
