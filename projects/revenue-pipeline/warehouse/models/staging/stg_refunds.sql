with ranked as (
    select payload, row_number() over (partition by id order by _loaded_at desc) as rn
    from {{ source('raw', 'stripe_refunds') }}
)
select
    json_extract_string(payload, '$.id')                                       as refund_id,
    json_extract_string(payload, '$.charge')                                   as charge_id,
    cast(json_extract(payload, '$.amount') as bigint) / 100.0                  as amount,
    to_timestamp(cast(json_extract(payload, '$.created') as bigint))           as refunded_at
from ranked
where rn = 1
