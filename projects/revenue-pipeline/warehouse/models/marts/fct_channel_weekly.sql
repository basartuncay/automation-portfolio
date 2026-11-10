-- Per week and channel: revenue, new customers, spend, ROAS and CAC. Organic channels have no spend, so no ROAS/CAC.
with o as (
    select date_trunc('week', order_date) as week, channel, count(*) as orders, sum(net) as net,
           sum(case when is_first_order then 1 else 0 end) as new_customers
    from {{ ref('fct_orders') }} group by 1, 2
),
s as (select date_trunc('week', day) as week, channel, sum(spend) as spend from {{ ref('stg_ad_spend') }} group by 1, 2)
select
    coalesce(o.week, s.week) as week,
    coalesce(o.channel, s.channel) as channel,
    coalesce(o.orders, 0) as orders,
    coalesce(o.net, 0) as net,
    coalesce(o.new_customers, 0) as new_customers,
    s.spend,
    round(o.net / nullif(s.spend, 0), 2) as roas,
    round(s.spend / nullif(o.new_customers, 0), 2) as cac
from o
full outer join s on s.week = o.week and s.channel = o.channel
