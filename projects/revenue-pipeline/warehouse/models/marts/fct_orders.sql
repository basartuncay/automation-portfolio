-- One row per order. Refunds are attributed to the order's date, so a late refund changes that past day's net revenue.
select
    o.order_id,
    o.order_date,
    o.customer_id,
    o.channel,
    o.total                                                         as gross,
    p.refunded,
    o.total - p.refunded                                            as net,
    row_number() over (partition by o.customer_id order by o.created_at, o.order_id) = 1 as is_first_order
from {{ ref('stg_orders') }} o
join {{ ref('int_order_payments') }} p using (order_id)
