select
    customer_id,
    min(order_date)                                                 as first_order_date,
    date_trunc('month', min(order_date))                            as cohort_month,
    arg_min(channel, order_date)                                    as first_channel,
    count(*)                                                        as orders,
    sum(net)                                                        as net_revenue
from {{ ref('fct_orders') }}
group by 1
