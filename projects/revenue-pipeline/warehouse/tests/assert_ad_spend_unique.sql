-- One row per platform, day and campaign: re-loads must replace, never add.
select channel, day, campaign, count(*) as n from {{ ref('stg_ad_spend') }} group by 1, 2, 3 having count(*) > 1
