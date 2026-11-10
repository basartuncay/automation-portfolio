-- Google and Meta daily spend in one shape. The loader replaces each restatement window, so rows are already unique;
-- a test checks it anyway.
select platform as channel, day, campaign, spend, clicks, impressions
from {{ source('raw', 'ad_spend') }}
