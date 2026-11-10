-- Monthly cohorts: of the customers whose first order was in a month, how many ordered again N months later.
with activity as (
    select distinct c.cohort_month, f.customer_id,
           datediff('month', c.cohort_month, date_trunc('month', f.order_date)) as months_since
    from {{ ref('fct_orders') }} f join {{ ref('dim_customers') }} c using (customer_id)
),
size as (select cohort_month, count(*) as customers from {{ ref('dim_customers') }} group by 1)
select a.cohort_month, a.months_since, count(*) as active_customers, s.customers as cohort_size,
       round(100.0 * count(*) / s.customers, 1) as retention_pct
from activity a join size s using (cohort_month)
group by 1, 2, 4
order by 1, 2
