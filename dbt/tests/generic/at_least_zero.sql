{% test at_least_zero(model, column_name) %}
-- A negative gap count means the enrolment walk ran backwards.
select *
from {{ model }}
where {{ column_name }} < 0
{% endtest %}
