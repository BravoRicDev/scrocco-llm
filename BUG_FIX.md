# scrocco-llm Bug Fix Report

## Summary of Issues Found

During a thorough codebase analysis, the following bugs were identified and categorized by severity.

---

## 🔴 **CRITICAL BUG #1: `app/health.py` - NameError on `_time`**

**File**: `app/health.py`  
**Line 60**: `router.last_health = {"last_cycle_at": int(_time.time()),`  

**Problem**: The variable `_time` is only imported inside the `health_loop` function at line 69 (`import time as _time`), but `run_health_cycle()` at module scope uses `_time.time()` without it being defined at module level.

**Impact**: The health monitoring task crashes with `NameError: name '_time' is not defined` every cycle. The `except Exception` at line 79-80 catches it silently, but `last_health` never gets set, disabling proactive health checks.

**Fix**: Move `import time as _time` to module level, or use `time.time()` directly with a top-level `import time`.

---

## 🔴 **CRITICAL BUG #2: `app/policy.py` - Duplicate method definitions**

**File**: `app/policy.py`

**Problem**: Two sets of duplicate methods exist:

1. **`routing_active()`**:
   - Lines 305-307: First definition (effectively dead code)
   - Lines 341-344: Second definition (overwrites first, this is the active one)

2. **`caps_for()`**:
   - Lines 309-327: First definition (effectively dead code)
   - Lines 346-364: Second definition (overwrites first, this is the active one)

**Impact**: The first definitions are dead code. If someone modifies the first `routing_active()` or `caps_for()` thinking it's the active one, changes won't take effect. This is confusing and maintenance-breaking.

**Fix**: Remove the duplicate definitions, keeping only one set of each method.

---

## 🟠 **HIGH BUG #3: `app/forwarder.py` - `_retry_after_from` regex issue**

**File**: `app/forwarder.py`  
**Lines 266-271**: `_RETRY_BODY_RE` regex for extracting `retryDelay` from 429 response body

**Problem**: The regex has three alternation groups for different `retryDelay` formats:
- `"retryDelay": "58s"` (string)
- `"retryDelay": {"seconds": 58}` (object)
- `"retry in 58.9s"` (text)

And `next((g for g in m.groups() if g), None)` takes only the first non-None group, ignoring potentially more relevant values.

**Impact**: Could result in incorrectly short or long cooldown periods for rate-limited keys, affecting both budget guard and normal cooldown behavior.

**Fix**: Improve the logic to prefer the most specific/reliable value, or test with various 429 body formats from different providers.

---

## 🟡 **MEDIUM BUG #4: `app/config.py` - `_classify` category logic complexity**

**File**: `app/config.py`  
**Lines 131-152**: The `_classify` function determines deployment category (free/priority/go/fallback/zen)

**Problem**: The category determination has a chain of if/elif/else conditions (lines 143-152) that check provider name, model name, and other heuristics. The special case at line 143-144 (`"opencode-zen" in provider`) appears to be a hack for a specific provider that may not be well-documented or tested. Multiple fallback conditions could lead to unexpected category assignments.

**Impact**: Deployments could be classified into wrong buckets, affecting routing priority, cooldown behavior, and budget guard scoring.

**Fix**: Add comprehensive tests for all category scenarios and refactor the logic for clarity and correctness.

---

## 🟢 **LOW BUG #5: `app/main.py` - `_peek_stream` buffer size threshold**

**File**: `app/main.py`  
**Lines 997-1039**: `MAX_PEEK_BUFFER_BYTES = 2 * 1024 * 1024` (2MB)

**Problem**: When the peeking buffer exceeds 2MB without finding sufficient content (answer_chars or tool_calls), it returns "timeout" if none were found. This could prematurely timeout on legitimate large responses (e.g., long reasoning chains, large code generation outputs).

**Impact**: Could cause unnecessary fallback/rotation when the upstream is actually producing valid but large responses.

**Fix**: Increase the threshold or make it configurable via policy YAML.

---

## 🟢 **LOW BUG #6: `app/csv_store.py` - `row_id` function stability**

**File**: `app/csv_store.py`  
**Lines 96-111**: The `row_id` function computes a stable MD5-based ID for CSV rows

**Problem**: The function includes "extras" (extra columns not in the known set) in the hash basis. If the CSV format changes (new columns added), the row_id could change, breaking the idempotency guarantee of PUT/DELETE operations by row ID.

**Impact**: Row IDs might not be stable across CSV format changes, causing duplicate or missed operations in admin CRUD.

**Fix**: Ensure the row_id calculation is more robust, or document the stability guarantees clearly.

---

## 🟢 **INFO BUG #7: `app/admin.py` - Late `Policy` import**

**File**: `app/admin.py`  
**Line 815**: `from .policy import Policy` appears after extensive use of the class

**Problem**: The `Policy` class is imported at the bottom of the file, after all the functions that use it. While Python handles this fine (late binding), it's unusual and could mask import-order issues.

**Impact**: Minimal, but makes the code harder to understand and debug.

**Fix**: Move the import to the top of the file.

---

## Prioritized Fix Order

1. **Fix #1** (`health.py` `_time` NameError): Critical - health monitoring will crash
2. **Fix #2** (`policy.py` duplicate methods): Critical - dead code confusion, maintenance risk
3. **Fix #3** (`forwarder.py` retry-after regex): High - affects cooldown behavior
4. **Fix #4** (`config.py` `_classify` logic): Medium - could misroute deployments
5. **Fix #5** (`main.py` buffer threshold): Low - edge case with large responses
6. **Fix #6** (`csv_store.py` row_id stability): Low - edge case on CSV format changes
7. **Fix #7** (`admin.py` import order): Info - minor cleanup

---

## Fix Commands

After switching to build mode, apply fixes in this order:

```bash
# 1. Fix health.py - add module-level time import
# 2. Fix policy.py - remove duplicate routing_active() and caps_for() definitions  
# 3. Fix forwarder.py - improve retry-after body parsing logic
# 4. Fix config.py - refactor _classify category logic with better tests
# 5. Fix main.py - increase or make MAX_PEEK_BUFFER_BYTES configurable
# 6. Fix csv_store.py - ensure row_id stability across format changes
# 7. Fix admin.py - move Policy import to top
# 8. Fix router.py - add escalation indication to cooldown log for better observability
# 9. Fix router.py - respect failed_unique in ULTIMA SPIAGGIA step (last resort)
# 10. Fix test_provider_resilience.py - explicitly set exponential cooldown mode in test
# 11. Fix test_logging_detail.py - explicitly set exponential cooldown mode in test
# 12. Fix test_tui_observability.py - guard _TestApp class definition when textual not available
```

Each fix should be tested with the existing test suite (`python3 -m pytest tests/ -q`) to ensure no regressions.