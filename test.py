import json
import os
import requests
from bs4 import BeautifulSoup
from collections import Counter
import re

# Load news site configuration from JSON (configurable path).
# If the JSON is missing or invalid, falls back to a built-in default.

def load_news_sites(config_path=None):
    default_sites = {
        'CNN': {'url': 'https://www.cnn.com', 'bias': 'left'},
        'MSNBC': {'url': 'https://www.msnbc.com', 'bias': 'left'},
        'NY Times': {'url': 'https://www.nytimes.com', 'bias': 'left'},
        'Washington Post': {'url': 'https://www.washingtonpost.com', 'bias': 'left'},
        'HuffPost': {'url': 'https://www.huffpost.com', 'bias': 'left'},
        'Vox': {'url': 'https://www.vox.com', 'bias': 'left'},
        'Slate': {'url': 'https://slate.com', 'bias': 'left'},
        'The Atlantic': {'url': 'https://www.theatlantic.com', 'bias': 'left'},
        'Politico': {'url': 'https://www.politico.com', 'bias': 'center'},
        'NPR': {'url': 'https://www.npr.org', 'bias': 'center'},
        'AP News': {'url': 'https://apnews.com', 'bias': 'center'},
        'USA Today': {'url': 'https://www.usatoday.com', 'bias': 'center'},
        'Reuters': {'url': 'https://www.reuters.com', 'bias': 'center'},
        'Bloomberg': {'url': 'https://www.bloomberg.com', 'bias': 'center'},
        'Wall Street Journal': {'url': 'https://www.wsj.com', 'bias': 'right'},
        'Fox News': {'url': 'https://www.foxnews.com', 'bias': 'right'},
        'The Blaze': {'url': 'https://www.theblaze.com', 'bias': 'right'},
        'Daily Caller': {'url': 'https://dailycaller.com', 'bias': 'right'},
        'Breitbart': {'url': 'https://www.breitbart.com', 'bias': 'right'},
        'National Review': {'url': 'https://www.nationalreview.com', 'bias': 'right'},
        'The Hill': {'url': 'https://thehill.com', 'bias': 'center'},
        'ABC News': {'url': 'https://abcnews.go.com', 'bias': 'center'},
        'NBC News': {'url': 'https://www.nbcnews.com', 'bias': 'center'},
        'CBS News': {'url': 'https://www.cbsnews.com', 'bias': 'center'},
        'Axios': {'url': 'https://www.axios.com', 'bias': 'center'},
        'BuzzFeed News': {'url': 'https://www.buzzfeednews.com', 'bias': 'left'},
        'The New York Post': {'url': 'https://nypost.com', 'bias': 'right'},
    }

    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'news_sites.json')

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            if isinstance(config, dict):
                return config
    except Exception:
        pass

    return default_sites


NEWS_SITES = load_news_sites()

# Common country names to search for (excluding the United States)
COUNTRIES = [
    'Afghanistan', 'Albania', 'Algeria', 'Andorra', 'Angola',
    'Antigua and Barbuda', 'Argentina', 'Armenia', 'Australia', 'Austria',
    'Azerbaijan', 'Bahamas', 'Bahrain', 'Bangladesh', 'Barbados',
    'Belarus', 'Belgium', 'Belize', 'Benin', 'Bhutan',
    'Bolivia', 'Bosnia and Herzegovina', 'Botswana', 'Brazil', 'Brunei',
    'Bulgaria', 'Burkina Faso', 'Burundi', 'Cabo Verde', 'Cambodia',
    'Cameroon', 'Canada', 'Central African Republic', 'Chad', 'Chile',
    'China', 'Colombia', 'Comoros', 'Congo', 'Costa Rica',
    'Croatia', 'Cuba', 'Cyprus', 'Czechia', 'Democratic Republic of the Congo',
    'Denmark', 'Djibouti', 'Dominica', 'Dominican Republic', 'Ecuador',
    'Egypt', 'El Salvador', 'Equatorial Guinea', 'Eritrea', 'Estonia',
    'Eswatini', 'Ethiopia', 'Fiji', 'Finland', 'France',
    'Gabon', 'Gambia', 'Germany', 'Ghana',
    'Greece', 'Grenada', 'Guatemala', 'Guinea', 'Guinea-Bissau',
    'Guyana', 'Haiti', 'Honduras', 'Hungary', 'Iceland',
    'India', 'Indonesia', 'Iran', 'Iraq', 'Ireland',
    'Israel', 'Italy', 'Jamaica', 'Japan', 'Jordan',
    'Kazakhstan', 'Kenya', 'Kiribati', 'Kosovo', 'Kuwait',
    'Kyrgyzstan', 'Laos', 'Latvia', 'Lebanon', 'Lesotho',
    'Liberia', 'Libya', 'Liechtenstein', 'Lithuania', 'Luxembourg',
    'Madagascar', 'Malawi', 'Malaysia', 'Maldives', 'Mali',
    'Malta', 'Marshall Islands', 'Mauritania', 'Mauritius', 'Mexico',
    'Micronesia', 'Moldova', 'Monaco', 'Mongolia', 'Montenegro',
    'Morocco', 'Mozambique', 'Myanmar', 'Namibia', 'Nauru',
    'Nepal', 'Netherlands', 'New Zealand', 'Nicaragua', 'Niger',
    'Nigeria', 'North Korea', 'North Macedonia', 'Norway', 'Oman',
    'Pakistan', 'Palau', 'Panama', 'Papua New Guinea', 'Paraguay',
    'Peru', 'Philippines', 'Poland', 'Portugal', 'Qatar',
    'Romania', 'Russia', 'Rwanda', 'Saint Kitts and Nevis', 'Saint Lucia',
    'Saint Vincent and the Grenadines', 'Samoa', 'San Marino', 'Sao Tome and Principe',
    'Saudi Arabia', 'Senegal', 'Serbia', 'Seychelles', 'Sierra Leone',
    'Singapore', 'Slovakia', 'Slovenia', 'Solomon Islands', 'Somalia',
    'South Africa', 'South Korea', 'South Sudan', 'Spain', 'Sri Lanka',
    'Sudan', 'Suriname', 'Sweden', 'Switzerland', 'Syria',
    'Taiwan', 'Tajikistan', 'Tanzania', 'Thailand', 'Timor-Leste',
    'Togo', 'Tonga', 'Trinidad and Tobago', 'Tunisia', 'Turkey',
    'Turkmenistan', 'Tuvalu', 'Uganda', 'Ukraine', 'United Arab Emirates',
    'Uruguay', 'Uzbekistan', 'Vanuatu', 'Vatican City', 'Venezuela',
    'Vietnam', 'Yemen', 'Zambia', 'Zimbabwe'
]

def scrape_headlines(url):
    """Scrape headlines from a news site."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        headlines = [h.get_text() for h in soup.find_all(['h1', 'h2', 'h3'])]
        return headlines
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []

def find_countries_in_text(text):
    """Find country mentions in text."""
    found = []
    for country in COUNTRIES:
        if re.search(r'\b' + country + r'\b', text, re.IGNORECASE):
            ## print(f"{text} - {country}")  # Print the headline containing the country
            found.append(country)
    return found

def main():
    all_countries = Counter()
    
    print("Scraping headlines...\n")
    for site_name, site_info in NEWS_SITES.items():
        url = site_info.get('url') if isinstance(site_info, dict) else site_info
        print(f"Scraping {site_name} ({url})...")
        headlines = scrape_headlines(url)
        
        for headline in headlines:
            countries = find_countries_in_text(headline)
            all_countries.update(countries)
    
    print("\n" + "="*50)
    print("Top Countries Mentioned in Headlines:")
    print("="*50)
    
    for country, count in all_countries.most_common(10):
        print(f"{country}: {count} mentions")

if __name__ == '__main__':
    main()

    