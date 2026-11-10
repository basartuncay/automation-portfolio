-- Stripe charges in euros (Stripe sends cents), latest version per charge.
with ranked as (
    select payload, row_number() over (partition by id order by _loaded_at desc) as rn
    from {{ source('raw', 'stripe_charges') }}
)
select
    json_extract_string(payload, '$.id')                                       as charge_id,
    cast(json_extract_string(payload, '$.metadata.order_id') as bigint)        as order_id,
    cast(json_extract(payload, '$.amount') as bigint) / 100.0                  as amount,
    to_timestamp(cast(json_extract(payload, '$.created') as bigint))           as charged_at
from ranked
where rn = 1
