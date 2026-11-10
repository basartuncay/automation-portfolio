-- One row per day, with no gaps (days without orders or spend still appear).
with days as (
    select cast(range as date) as day
    from range((select min(order_date) from {{ ref('fct_orders') }}), (select max(order_date) from {{ ref('fct_orders') }}) + interval 1 day, interval 1 day)
),
o as (
    select order_date as day, count(*) as orders, sum(gross) as gross, sum(refunded) as refunds, sum(net) as net,
           sum(case when is_first_order then 1 else 0 end) as new_customers
    from {{ ref('fct_orders') }} group by 1
),
s as (select day, sum(spend) as ad_spend from {{ ref('stg_ad_spend') }} group by 1)
select
    d.day,
    coalesce(o.orders, 0)          as orders,
    coalesce(o.gross, 0)           as gross,
    coalesce(o.refunds, 0)         as refunds,
    coalesce(o.net, 0)             as net,
    coalesce(s.ad_spend, 0)        as ad_spend,
    coalesce(o.new_customers, 0)   as new_customers,
    round(coalesce(o.net, 0) / nullif(s.ad_spend, 0), 2)            as roas,
    round(s.ad_spend / nullif(o.new_customers, 0), 2)               as blended_cac
from days d
left join o using (day)
left join s using (day)
order by d.day
