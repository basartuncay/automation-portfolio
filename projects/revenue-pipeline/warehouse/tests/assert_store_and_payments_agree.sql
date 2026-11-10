-- Reconciliation: every order has exactly the charge the store says it does. Returns the orders that disagree.
select o.order_id, o.total, c.amount as charged
from {{ ref('stg_orders') }} o
left join {{ ref('stg_charges') }} c on c.order_id = o.order_id
where c.charge_id is null or abs(o.total - c.amount) > 0.005
