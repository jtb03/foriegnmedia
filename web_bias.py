from flask import Flask, render_template, jsonify
from test import NEWS_SITES, scrape_headlines, find_countries_in_text
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
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


def normalize_bias(bias):
    """Normalize bias labels and map unexpected values to unknown."""
    normalized = (bias or 'unknown').lower()
    return normalized if normalized in {'left', 'center', 'right', 'unknown'} else 'unknown'


def scrape_site_country_counts(site_name, site_info):
    """Scrape one site and return aggregated country counts."""
    url = site_info.get('url') if isinstance(site_info, dict) else site_info
    bias = site_info.get('bias') if isinstance(site_info, dict) else 'unknown'

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
        'unknown': Counter(),
    }
    bias_sites = {
        'left': {},
        'center': {},
        'right': {},
        'unknown': {},
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
                site_results[site_name] = ('unknown', Counter())

    # Keep output ordering stable using NEWS_SITES declaration order.
    for site_name in NEWS_SITES.keys():
        bias, site_countries = site_results.get(site_name, ('unknown', Counter()))
        all_countries.update(site_countries)
        site_data[site_name] = dict(site_countries.most_common(5))
        bias_totals[bias].update(site_countries)
        bias_sites[bias][site_name] = dict(site_countries.most_common(5))

    # Convert to lists of top items for templates
    top_countries = all_countries.most_common(10)
    bias_summary = {
        bias: {
            'top_countries': counter.most_common(5),
            'sites': sites,
        }
        for bias, (counter, sites) in zip(bias_totals.keys(), zip(bias_totals.values(), bias_sites.values()))
    }

    # Expose counts per bias for the top countries chart
    bias_country_counts = {
        bias: dict(counter)
        for bias, counter in bias_totals.items()
    }

    return top_countries, site_data, bias_summary, bias_country_counts


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
    top_countries, site_data, bias_summary, bias_country_counts = get_cached_bias_stats()
    return render_template(
        'bias.html',
        top_countries=top_countries,
        site_data=site_data,
        bias_summary=bias_summary,
        bias_country_counts=bias_country_counts,
    )


@app.route('/health')
def health():
    """Render health check endpoint; does not trigger scraping."""
    with _cache_lock:
        has_cache = _stats_cache['data'] is not None
        cache_age_seconds = int(time.time() - _stats_cache['timestamp']) if has_cache else None

    return jsonify({
        'status': 'ok',
        'cache_ready': has_cache,
        'cache_age_seconds': cache_age_seconds,
        'cache_ttl_seconds': CACHE_TTL_SECONDS,
    })


if __name__ == '__main__':
    app.run(debug=True)
