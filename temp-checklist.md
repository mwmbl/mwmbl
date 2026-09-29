# Checklist: Move All Stats from Redis → Postgres/DB

## ✅ Already Done (Postgres-backed with Redis fallback)
- [x] `users_crawled_daily` → `DailyCrawlerStats.users_crawled`
- [x] `results_indexed_daily` → `DailyCrawlerStats.results_indexed`
- [x] `dataset_queries_daily` → `DailyCrawlerStats.dataset_queries`
- [x] `dataset_results_daily` → `DailyCrawlerStats.dataset_results`
- [x] `blacklisted_results_removed_daily` → `DailyCrawlerStats.blacklisted_results_removed`
- [x] `urls_in_index_daily` → `DailyIndexStats.urls_in_index`
- [x] `domains_in_index_daily` → `DailyIndexStats.domains_in_index`
- [x] `results_in_index_daily` → `DailyIndexStats.results_in_index`

## 🔴 Still Reading from Redis (Need to Fix)

### 1. `top_user_results` in `StatsManager.get_stats()` 
- **Current**: Reads from `USER_RESULTS_COUNT_KEY` sorted set in Redis
- **Fix**: Use `get_leaderboard_for_date(today)` which queries `UserStats` table ✅ **DONE**

### 2. `get_domain_result_count()` in `count_urls.py`
- **Current**: Reads from `INDEX_DOMAIN_RESULT_COUNT_KEY` sorted set in Redis
- **Fix**: Add `domain_result_count` field to `DailyIndexStats` or create new model, persist during `count_urls()` ✅ **DONE** - Created `DailyDomainResultCount` model

### 3. `get_user_stats()` in `StatsManager`
- **Current**: Queries `UserStats` table directly ✅ **Already uses Postgres!** 
- **Verify**: No Redis fallback here

## 🟡 Still Writing to Redis (Should Remove After Verifying Reads Work)

### In `StatsManager.record_results()`:
- [x] `RESULTS_COUNT_KEY` (redis incrby) - **REMOVED**
- [x] `USER_RESULTS_COUNT_KEY` (redis zincrby) - **REMOVED** 
- [x] `USERS_KEY` (redis sadd) - **REMOVED**

### In `StatsManager.record_blacklisted_removed()`:
- [x] `BLACKLISTED_REMOVED_COUNT_KEY` (redis incrby) - **REMOVED**

### In `StatsManager.record_dataset()`:
- [x] `DATASET_QUERIES_COUNT_KEY` (redis incrby) - **REMOVED**
- [x] `DATASET_RESULTS_COUNT_KEY` (redis incrby) - **REMOVED**

### In `count_urls.py`:
- [x] `INDEX_URL_COUNT_KEY`, `INDEX_DOMAIN_COUNT_KEY`, `INDEX_RESULT_COUNT_KEY` (redis set) - **REMOVED**
- [x] `INDEX_DOMAIN_RESULT_COUNT_KEY` (redis zscore) - **REMOVED**

## 📊 Progress Repo (`progress/collect-metrics.js`)
- [x] Uses `/api/v1/crawler/stats` → `results_in_index_daily` ✅ Already Postgres-backed via `get_counts()`

## 🎯 Frontend Pages
- [x] `/crawler-stats` → uses `/api/v1/crawler/stats` ✅
- [x] `/stats` → uses `/api/v1/crawler/stats` + `/api/v1/leaderboard/*` ✅
- [x] `/account/stats` → uses `/api/v1/platform/user/stats` → `StatsManager.get_user_stats()` ✅ Uses Postgres

## 🔍 Need to Verify
- [ ] `sync_search_counts` background task - syncs Redis ↔ Postgres for `UsageBucket` (quota, not stats)
- [ ] `traffic.py` - counts search traffic in Redis (separate from crawler stats, may be OK to keep)
- [ ] Blacklist snapshot / purge queue - Redis-only by design (OK to keep)

## Next Steps Priority Order
1. **Complete `top_user_results` fix** - verify `get_leaderboard_for_date()` works for today ✅ DONE
2. **Fix `get_domain_result_count()`** - add domain-level stats to `DailyIndexStats` or new model ✅ DONE
3. **Remove Redis writes** in `record_results()`, `record_blacklisted_removed()`, `record_dataset()`, `count_urls()` ✅ DONE
4. **Run tests** - `make test` to ensure nothing breaks ✅ DONE (910 passed)
5. **Verify frontend pages** still work ✅ DONE