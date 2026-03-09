from flask import Flask, render_template
from test import NEWS_SITES, scrape_headlines, find_countries_in_text
from collections import Counter

# filepath: c:\Users\jtb10\Coding\web.py

app = Flask(__name__)

def get_country_stats():
    """Collect country mention statistics."""
    all_countries = Counter()
    results = {}
    
    for site_name, url in NEWS_SITES.items():
        headlines = scrape_headlines(url)
        site_countries = Counter()
        
        for headline in headlines:
            countries = find_countries_in_text(headline)
            site_countries.update(countries)
            all_countries.update(countries)
        
        results[site_name] = dict(site_countries.most_common(5))
    
    return all_countries.most_common(10), results

@app.route('/')
def index():
    """Display country statistics."""
    top_countries, site_data = get_country_stats()
    return render_template('custom.html', top_countries=top_countries, site_data=site_data)

if __name__ == '__main__':
    app.run(debug=True)
