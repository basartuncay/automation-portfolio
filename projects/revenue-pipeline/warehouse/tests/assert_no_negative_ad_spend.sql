-- Negative spend means a broken export or a platform glitch; it would silently inflate ROAS.
select channel, day, campaign, spend from {{ ref('stg_ad_spend') }} where spend < 0
