{% test rate_in_unit_interval(model, column_name) %}
-- A rate above 1 means members are in the numerator but not the denominator,
-- which is the failure that makes a measure report worse than useless: it
-- looks like exceptional performance.
select *
from {{ model }}
where {{ column_name }} is not null
  and ({{ column_name }} < 0 or {{ column_name }} > 1)
{% endtest %}
