-- A charge can't be refunded for more than was charged.
select charge_id, paid, refunded from {{ ref('int_order_payments') }} where refunded > paid + 0.005
