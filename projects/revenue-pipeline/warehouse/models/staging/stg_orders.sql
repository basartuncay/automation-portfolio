-- One row per order, latest version wins (orders are re-delivered whenever they change, e.g. on a refund).
with ranked as (
    select payload, row_number() over (partition by id order by updated_at desc, _loaded_at desc) as rn
    from {{ source('raw', 'shop_orders') }}
)
select
    cast(json_extract(payload, '$.id') as bigint)                              as order_id,
    cast(json_extract_string(payload, '$.created_at') as timestamp)            as created_at,
    cast(cast(json_extract_string(payload, '$.created_at') as timestamp) as date) as order_date,
    cast(json_extract(payload, '$.customer.id') as bigint)                     as customer_id,
    cast(json_extract_string(payload, '$.total_price') as decimal(12, 2))      as total,
    json_extract_string(payload, '$.currency')                                 as currency,
    json_extract_string(payload, '$.financial_status')                         as financial_status,
    coalesce(nullif(regexp_extract(json_extract_string(payload, '$.landing_site'), 'utm_source=([a-z]+)', 1), ''), 'direct') as channel
from ranked
where rn = 1
