-- What was actually paid and refunded per order, from the payment processor (not the store's own status field).
with refunds_per_charge as (
    select charge_id, sum(amount) as refunded, max(refunded_at) as last_refund_at
    from {{ ref('stg_refunds') }}
    group by 1
)
select
    o.order_id,
    c.charge_id,
    coalesce(c.amount, 0)            as paid,
    coalesce(r.refunded, 0)          as refunded,
    r.last_refund_at
from {{ ref('stg_orders') }} o
left join {{ ref('stg_charges') }} c on c.order_id = o.order_id
left join refunds_per_charge r on r.charge_id = c.charge_id
