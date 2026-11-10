-- The daily mart adds up to the order-level mart (catches join fan-outs and dropped days).
select (select sum(net) from {{ ref('fct_daily') }}) as daily_net, (select sum(net) from {{ ref('fct_orders') }}) as orders_net
where abs((select sum(net) from {{ ref('fct_daily') }}) - (select sum(net) from {{ ref('fct_orders') }})) > 0.005
