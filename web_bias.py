from flask import Flask, render_template, jsonify, request
from test import NEWS_SITES, scrape_headlines, find_countries_in_text
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import date, timedelta
import json
import os
import requests
import time

# Bias is now stored alongside site definitions in test.py.
# Each entry in NEWS_SITES should be a dict with at least 'url' and 'bias'.
app = Flask(__name__)

# Keep recent scrape results in memory to avoid re-scraping on every request.
CACHE_TTL_SECONDS = 15 * 60
MAX_SCRAPE_WORKERS = 8
_stats_cache = {
    'timestamp': 0.0,
    'data': None,
}
_cache_lock = Lock()
_leaderboard_lock = Lock()
_sync_lock = Lock()
BIAS_ORDER = ('left', 'center', 'right')
LEADERBOARD_SIZE = 10
LEADERBOARD_STORE_PATH = os.getenv(
    'LEADERBOARD_STORE_PATH',
    os.path.join(os.path.dirname(__file__), 'leaderboard_points.json')
)
SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_ANON_KEY = os.getenv('SUPABASE_ANON_KEY', '')
SUPABASE_SERVICE_ROLE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
SUPABASE_DAILY_TABLE = os.getenv('SUPABASE_DAILY_TABLE', 'country_leaderboard_daily')
SUPABASE_COMPAT_MODE = os.getenv('SUPABASE_COMPAT_MODE', 'auto').strip().lower()
SUPABASE_PAGE_SIZE = 1000
_sync_state = {
    'last_attempt_unix': None,
    'last_success_unix': None,
    'last_storage': None,
    'last_error': None,
    'last_supabase_attempt_unix': None,
    'last_supabase_success_unix': None,
    'last_supabase_error': None,
}


def mark_sync_attempt(storage):
    """Record a leaderboard persistence attempt for debugging."""
    with _sync_lock:
        _sync_state['last_attempt_unix'] = int(time.time())
        _sync_state['last_storage'] = storage
        if storage == 'supabase':
            _sync_state['last_supabase_attempt_unix'] = int(time.time())


def mark_sync_success(storage):
    """Record a successful leaderboard persistence."""
    with _sync_lock:
        _sync_state['last_success_unix'] = int(time.time())
        _sync_state['last_storage'] = storage
        # Only clear the generic error on success in the same storage path.
        if storage == 'supabase':
            _sync_state['last_error'] = None
            _sync_state['last_supabase_success_unix'] = int(time.time())
            _sync_state['last_supabase_error'] = None
        elif _sync_state.get('last_storage') == 'local':
            _sync_state['last_error'] = None


def mark_sync_error(storage, error):
    """Record an error during leaderboard persistence."""
    with _sync_lock:
        _sync_state['last_storage'] = storage
        _sync_state['last_error'] = str(error)
        if storage == 'supabase':
            _sync_state['last_supabase_error'] = str(error)


def get_sync_state_snapshot():
    """Return an immutable copy of sync diagnostics."""
    with _sync_lock:
        return dict(_sync_state)


def load_leaderboard_store():
    """Load leaderboard history from disk."""
    if not os.path.exists(LEADERBOARD_STORE_PATH):
        return {'daily_points': {}}

    try:
        with open(LEADERBOARD_STORE_PATH, 'r', encoding='utf-8') as file_handle:
            raw = json.load(file_handle)
            if isinstance(raw, dict) and isinstance(raw.get('daily_points'), dict):
                return raw
    except Exception as error:
        print(f"Error loading leaderboard store: {error}")

    return {'daily_points': {}}


def save_leaderboard_store(store):
    """Persist leaderboard history to disk atomically."""
    temp_path = f"{LEADERBOARD_STORE_PATH}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as file_handle:
        json.dump(store, file_handle, ensure_ascii=True, indent=2, sort_keys=True)
    os.replace(temp_path, LEADERBOARD_STORE_PATH)


def get_supabase_read_key():
    """Return key used for SELECTs, preferring anon/public key."""
    return SUPABASE_ANON_KEY or SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY


def get_supabase_write_key():
    """Return key used for writes, preferring service role key."""
    return SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY or SUPABASE_KEY


def get_supabase_write_key_source():
    """Return which env var currently provides the write key."""
    if SUPABASE_SERVICE_ROLE_KEY:
        return 'SUPABASE_SERVICE_ROLE_KEY'
    if SUPABASE_ANON_KEY:
        return 'SUPABASE_ANON_KEY'
    if SUPABASE_KEY:
        return 'SUPABASE_KEY'
    return None


def use_supabase_leaderboard_storage():
    """Return True when Supabase leaderboard storage is configured."""
    return bool(SUPABASE_URL and get_supabase_read_key() and get_supabase_write_key())


def supabase_headers(api_key, extra_headers=None):
    """Build Supabase REST headers for authenticated requests."""
    headers = {
        'apikey': api_key,
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def use_legacy_leaderboard_schema():
    """Return True when using leaderboard(user, points, updated_at) compatibility mode."""
    if SUPABASE_COMPAT_MODE == 'legacy':
        return True
    if SUPABASE_COMPAT_MODE == 'daily':
        return False
    return SUPABASE_DAILY_TABLE.lower() == 'leaderboard'


def get_utc_day_bounds_iso(leaderboard_date):
    """Return [day_start, next_day_start) ISO timestamps for a YYYY-MM-DD date."""
    day = date.fromisoformat(leaderboard_date)
    next_day = day + timedelta(days=1)
    return f"{day.isoformat()}T00:00:00+00:00", f"{next_day.isoformat()}T00:00:00+00:00"


def upsert_daily_points_supabase_legacy(leaderboard_date, leaderboard):
    """Upsert one day's points using legacy schema: leaderboard(user, points, updated_at)."""
    if not leaderboard:
        return True

    write_key = get_supabase_write_key()
    read_key = get_supabase_read_key()
    if not write_key or not read_key:
        raise RuntimeError('Supabase read/write key is not configured')

    day_start, _ = get_utc_day_bounds_iso(leaderboard_date)
    endpoint = f"{SUPABASE_URL}/rest/v1/{SUPABASE_DAILY_TABLE}"

    for row in leaderboard:
        country = row['country']
        points = int(row['points'])

        # Read-first upsert keeps writes idempotent even without a unique constraint.
        lookup = requests.get(
            endpoint,
            headers=supabase_headers(read_key),
            params={
                'select': 'id,updated_at',
                'user': f'eq.{country}',
                'updated_at': f'gte.{day_start}',
                'order': 'updated_at.desc',
                'limit': 1,
            },
            timeout=12,
        )
        if not lookup.ok:
            raise RuntimeError(
                f"Supabase legacy lookup failed ({lookup.status_code}): {lookup.text[:300]}"
            )
        lookup_payload = lookup.json()
        existing_rows = lookup_payload if isinstance(lookup_payload, list) else []

        if existing_rows and str(existing_rows[0].get('updated_at', '')).startswith(leaderboard_date):
            row_id = existing_rows[0].get('id')
            if not row_id:
                continue
            update_endpoint = f"{endpoint}?id=eq.{row_id}"
            update_resp = requests.patch(
                update_endpoint,
                headers=supabase_headers(write_key, {'Prefer': 'return=minimal'}),
                json={'points': points, 'updated_at': day_start},
                timeout=12,
            )
            if not update_resp.ok:
                raise RuntimeError(
                    f"Supabase legacy update failed ({update_resp.status_code}): {update_resp.text[:300]}"
                )
            continue

        insert_resp = requests.post(
            endpoint,
            headers=supabase_headers(write_key, {'Prefer': 'return=minimal'}),
            json={
                'user': country,
                'points': points,
                'updated_at': day_start,
            },
            timeout=12,
        )
        if not insert_resp.ok:
            raise RuntimeError(
                f"Supabase legacy insert failed ({insert_resp.status_code}): {insert_resp.text[:300]}"
            )

    return True


def upsert_daily_points_supabase(leaderboard_date, leaderboard):
    """Upsert one day's leaderboard points into Supabase."""
    if not use_supabase_leaderboard_storage():
        return False

    if not leaderboard:
        return True

    if use_legacy_leaderboard_schema():
        return upsert_daily_points_supabase_legacy(leaderboard_date, leaderboard)

    rows = [
        {
            'leaderboard_date': leaderboard_date,
            'country': row['country'],
            'points': int(row['points']),
        }
        for row in leaderboard
    ]

    write_key = get_supabase_write_key()
    if not write_key:
        raise RuntimeError('Supabase write key is not configured')

    endpoint = f"{SUPABASE_URL}/rest/v1/{SUPABASE_DAILY_TABLE}?on_conflict=leaderboard_date,country"
    response = requests.post(
        endpoint,
        headers=supabase_headers(write_key, {'Prefer': 'resolution=merge-duplicates,return=minimal'}),
        json=rows,
        timeout=12,
    )
    if not response.ok:
        raise RuntimeError(
            f"Supabase upsert failed ({response.status_code}): {response.text[:300]}"
        )
    response.raise_for_status()
    return True


def fetch_supabase_daily_rows():
    """Fetch all daily leaderboard rows from Supabase with paging."""
    endpoint = f"{SUPABASE_URL}/rest/v1/{SUPABASE_DAILY_TABLE}"
    all_rows = []
    start = 0

    read_key = get_supabase_read_key()
    if not read_key:
        raise RuntimeError('Supabase read key is not configured')

    if use_legacy_leaderboard_schema():
        while True:
            end = start + SUPABASE_PAGE_SIZE - 1
            response = requests.get(
                endpoint,
                headers=supabase_headers(
                    read_key,
                    {
                        'Range-Unit': 'items',
                        'Range': f'{start}-{end}',
                    }
                ),
                params={'select': 'updated_at,user,points'},
                timeout=12,
            )
            if not response.ok:
                raise RuntimeError(
                    f"Supabase fetch failed ({response.status_code}): {response.text[:300]}"
                )
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else []
            all_rows.extend(rows)

            if len(rows) < SUPABASE_PAGE_SIZE:
                break
            start += SUPABASE_PAGE_SIZE

        return all_rows

    while True:
        end = start + SUPABASE_PAGE_SIZE - 1
        response = requests.get(
            endpoint,
            headers=supabase_headers(
                read_key,
                {
                    'Range-Unit': 'items',
                    'Range': f'{start}-{end}',
                }
            ),
            params={'select': 'leaderboard_date,country,points'},
            timeout=12,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase fetch failed ({response.status_code}): {response.text[:300]}"
            )
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else []
        all_rows.extend(rows)

        if len(rows) < SUPABASE_PAGE_SIZE:
            break
        start += SUPABASE_PAGE_SIZE

    return all_rows


def get_cumulative_leaderboard_from_supabase():
    """Build cumulative leaderboard from Supabase rows."""
    rows = fetch_supabase_daily_rows()
    daily_points = {}

    for row in rows:
        if use_legacy_leaderboard_schema():
            updated_at = row.get('updated_at')
            leaderboard_date = str(updated_at)[:10] if updated_at else None
            country = row.get('user')
        else:
            leaderboard_date = row.get('leaderboard_date')
            country = row.get('country')

        points = row.get('points')

        if not leaderboard_date or not country:
            continue

        try:
            points_int = int(points)
        except (TypeError, ValueError):
            continue

        country_points = daily_points.setdefault(leaderboard_date, {})
        # If duplicates exist in legacy table, keep one value per country/day.
        previous = country_points.get(country)
        country_points[country] = points_int if previous is None else max(previous, points_int)

    cumulative = build_cumulative_leaderboard(daily_points)
    tracked_days = len(daily_points)
    return cumulative, tracked_days


def build_cumulative_leaderboard(daily_points):
    """Aggregate cumulative points and days-scored from daily leaderboard history."""
    total_points = Counter()
    days_scored = Counter()

    for country_points in daily_points.values():
        if not isinstance(country_points, dict):
            continue
        for country, points in country_points.items():
            try:
                points_int = int(points)
            except (TypeError, ValueError):
                continue
            total_points[country] += points_int
            days_scored[country] += 1

    sorted_rows = sorted(total_points.items(), key=lambda item: (-item[1], item[0]))
    cumulative = []
    for rank, (country, points) in enumerate(sorted_rows, start=1):
        cumulative.append({
            'rank': rank,
            'country': country,
            'points': points,
            'days_scored': days_scored[country],
        })

    return cumulative


def update_and_get_cumulative_leaderboard(leaderboard_date, leaderboard):
    """Store today's leaderboard points and return updated cumulative standings."""
    if use_supabase_leaderboard_storage():
        with _leaderboard_lock:
            try:
                mark_sync_attempt('supabase')
                upsert_daily_points_supabase(leaderboard_date, leaderboard)
                cumulative_data = get_cumulative_leaderboard_from_supabase()
                mark_sync_success('supabase')
                return cumulative_data
            except Exception as error:
                mark_sync_error('supabase', error)
                print(f"Supabase leaderboard fallback to local storage due to error: {error}")

    today_points = {row['country']: row['points'] for row in leaderboard}

    with _leaderboard_lock:
        mark_sync_attempt('local')
        store = load_leaderboard_store()
        daily_points = store.get('daily_points', {})
        daily_points[leaderboard_date] = today_points
        store['daily_points'] = daily_points
        save_leaderboard_store(store)

        cumulative = build_cumulative_leaderboard(daily_points)
        tracked_days = len(daily_points)
        mark_sync_success('local')

    return cumulative, tracked_days


def normalize_bias(bias):
    """Normalize bias labels and map unexpected values to center."""
    normalized = (bias or 'center').lower()
    return normalized if normalized in BIAS_ORDER else 'center'


def scrape_site_country_counts(site_name, site_info):
    """Scrape one site and return aggregated country counts."""
    url = site_info.get('url') if isinstance(site_info, dict) else site_info
    bias = site_info.get('bias') if isinstance(site_info, dict) else 'center'

    headlines = scrape_headlines(url)
    site_countries = Counter()

    for headline in headlines:
        countries = find_countries_in_text(headline)
        site_countries.update(countries)

    return site_name, normalize_bias(bias), site_countries


def get_bias_stats():
    """Collect country mention statistics by bias category."""
    # Aggregate overall counts
    all_countries = Counter()

    # Track per-site counts
    site_data = {}

    # Track per-bias counts and per-bias site breakdowns
    bias_totals = {
        'left': Counter(),
        'center': Counter(),
        'right': Counter(),
    }
    bias_sites = {
        'left': {},
        'center': {},
        'right': {},
    }

    site_results = {}
    site_items = list(NEWS_SITES.items())
    worker_count = min(MAX_SCRAPE_WORKERS, max(1, len(site_items)))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(scrape_site_country_counts, site_name, site_info): site_name
            for site_name, site_info in site_items
        }

        for future in as_completed(futures):
            site_name = futures[future]
            try:
                result_site_name, bias, site_countries = future.result()
                site_results[result_site_name] = (bias, site_countries)
            except Exception as error:
                print(f"Error processing {site_name}: {error}")
                site_results[site_name] = ('center', Counter())

    # Keep output ordering stable using NEWS_SITES declaration order.
    for site_name in NEWS_SITES.keys():
        bias, site_countries = site_results.get(site_name, ('center', Counter()))
        all_countries.update(site_countries)
        site_data[site_name] = dict(site_countries.most_common(5))
        bias_totals[bias].update(site_countries)
        bias_sites[bias][site_name] = dict(site_countries.most_common(5))

    # Convert to lists of top items for templates
    top_countries = all_countries.most_common(10)
    leaderboard_raw = all_countries.most_common(LEADERBOARD_SIZE)
    leaderboard = []
    max_points = len(leaderboard_raw)
    for rank, (country, mentions) in enumerate(leaderboard_raw, start=1):
        leaderboard.append({
            'rank': rank,
            'country': country,
            'mentions': mentions,
            'points': max_points - rank + 1,
        })

    bias_summary = {
        bias: {
            'top_countries': bias_totals[bias].most_common(5),
            'sites': bias_sites[bias],
        }
        for bias in BIAS_ORDER
    }

    # Expose counts per bias for the top countries chart
    bias_country_counts = {
        bias: dict(bias_totals[bias])
        for bias in BIAS_ORDER
    }

    leaderboard_date = date.today().isoformat()
    cumulative_leaderboard, tracked_days = update_and_get_cumulative_leaderboard(leaderboard_date, leaderboard)

    return top_countries, site_data, bias_summary, bias_country_counts, leaderboard, leaderboard_date, cumulative_leaderboard, tracked_days


def get_cached_bias_stats():
    """Return cached stats when fresh; otherwise recompute and refresh cache."""
    now = time.time()

    with _cache_lock:
        if _stats_cache['data'] is not None and (now - _stats_cache['timestamp']) < CACHE_TTL_SECONDS:
            return _stats_cache['data']

    data = get_bias_stats()

    with _cache_lock:
        _stats_cache['data'] = data
        _stats_cache['timestamp'] = time.time()

    return data


@app.route('/')
def index():
    """Display country statistics (with bias grouping)."""
    force_refresh = request.args.get('refresh', '').strip().lower() in {'1', 'true', 'yes'}
    (
        top_countries,
        site_data,
        bias_summary,
        bias_country_counts,
        leaderboard,
        leaderboard_date,
        cumulative_leaderboard,
        tracked_days,
    ) = get_bias_stats() if force_refresh else get_cached_bias_stats()
    return render_template(
        'bias.html',
        top_countries=top_countries,
        site_data=site_data,
        bias_summary=bias_summary,
        bias_country_counts=bias_country_counts,
        leaderboard=leaderboard,
        leaderboard_date=leaderboard_date,
        cumulative_leaderboard=cumulative_leaderboard,
        cumulative_days=tracked_days,
        bias_order=BIAS_ORDER,
    )


@app.route('/health')
def health():
    """Render health check endpoint; does not trigger scraping."""
    with _cache_lock:
        has_cache = _stats_cache['data'] is not None
        cache_age_seconds = int(time.time() - _stats_cache['timestamp']) if has_cache else None
    sync_state = get_sync_state_snapshot()

    return jsonify({
        'status': 'ok',
        'cache_ready': has_cache,
        'cache_age_seconds': cache_age_seconds,
        'cache_ttl_seconds': CACHE_TTL_SECONDS,
        'leaderboard_storage': 'supabase' if use_supabase_leaderboard_storage() else 'local',
        'supabase_url_configured': bool(SUPABASE_URL),
        'supabase_read_key_configured': bool(get_supabase_read_key()),
        'supabase_write_key_configured': bool(get_supabase_write_key()),
        'supabase_service_role_key_configured': bool(SUPABASE_SERVICE_ROLE_KEY),
        'supabase_anon_key_configured': bool(SUPABASE_ANON_KEY),
        'supabase_write_key_source': get_supabase_write_key_source(),
        'supabase_daily_table': SUPABASE_DAILY_TABLE,
        'supabase_compat_mode': SUPABASE_COMPAT_MODE,
        'supabase_legacy_schema_mode': use_legacy_leaderboard_schema(),
        'sync_last_attempt_unix': sync_state.get('last_attempt_unix'),
        'sync_last_success_unix': sync_state.get('last_success_unix'),
        'sync_last_storage': sync_state.get('last_storage'),
        'sync_last_error': sync_state.get('last_error'),
        'sync_last_supabase_attempt_unix': sync_state.get('last_supabase_attempt_unix'),
        'sync_last_supabase_success_unix': sync_state.get('last_supabase_success_unix'),
        'sync_last_supabase_error': sync_state.get('last_supabase_error'),
    })


if __name__ == '__main__':
    app.run(debug=True)
