import re
import unicodedata
from flask import Flask, jsonify, request

import requests

app = Flask(__name__)

# ⚠️ Troque depois se subir pra repo público
TMDB_API_KEY = "15736b260562b6f8c8df048ce5258399"
ANIMEFIRE_BASE = "https://animefire.io"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

MANIFEST = {
    "id": "community.animefire.guilherme",
    "version": "1.0.0",
    "name": "AnimeFire",
    "description": "Addon pessoal para animefire.io (dublado/legendado)",
    "resources": ["stream"],
    "types": ["series", "movie"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}


# ------------------------------------------------------------
# Utils
# ------------------------------------------------------------

def normalize(text):
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def to_roman(num):
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    result = ""
    for value, symbol in vals:
        while num >= value:
            result += symbol
            num -= value
    return result


def similarity(a, b):
    wa = set(normalize(a).split())
    wb = set(normalize(b).split())
    if not wa or not wb:
        return 0
    return len(wa & wb) / max(len(wa), len(wb))


# ------------------------------------------------------------
# 1) IMDb -> TMDB -> título
# ------------------------------------------------------------

def imdb_to_tmdb_title(imdb_id, media_type):
    find_url = (
        f"https://api.themoviedb.org/3/find/{imdb_id}"
        f"?api_key={TMDB_API_KEY}&external_source=imdb_id&language=pt-BR"
    )
    res = requests.get(find_url, timeout=15)
    res.raise_for_status()
    data = res.json()

    key = "tv_results" if media_type == "series" else "movie_results"
    results = data.get(key, [])
    if not results:
        raise ValueError(f"IMDb {imdb_id} não achou correspondência no TMDB")

    item = results[0]
    title_pt = item.get("name") or item.get("title") or ""
    title_original = item.get("original_name") or item.get("original_title") or ""
    return title_pt, title_original


# ------------------------------------------------------------
# 2) Resolve slug no AnimeFire (URL direta primeiro, busca depois)
# ------------------------------------------------------------

def page_looks_valid(html):
    return bool(re.search(r"Epis[oó]dios", html, re.I)) and not re.search(
        r"p[aá]gina n[aã]o encontrada", html, re.I
    )


def try_direct_slug(base_slug):
    for slug in [f"{base_slug}-dublado", base_slug]:
        url = f"{ANIMEFIRE_BASE}/animes/{slug}-todos-os-episodios"
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            if res.status_code != 200:
                continue
            html = res.text
            if page_looks_valid(html):
                title_match = re.search(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", html, re.I)
                real_title = title_match.group(1).strip() if title_match else slug
                return {"slug": slug, "matched_title": real_title}
        except requests.RequestException:
            continue
    return None


def search_fallback(query):
    search_url = f"{ANIMEFIRE_BASE}/pesquisar/{normalize(query).replace(' ', '-')}"
    res = requests.get(search_url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    html = res.text

    candidates = re.findall(
        r'<a[^>]+href="([^"]*\/animes\/[^"]+)"[^>]*>[\s\S]*?<h3[^>]*>([^<]+)</h3>', html
    )
    if not candidates:
        return None

    best_href, best_title = max(candidates, key=lambda c: similarity(query, c[1]))
    best_score = similarity(query, best_title)
    if best_score < 0.3:
        return None

    full_url = best_href if best_href.startswith("http") else ANIMEFIRE_BASE + best_href
    full_url = full_url.rstrip("/")
    slug_match = re.search(r"/animes/([^/]+?)(-todos-os-episodios)?$", full_url)
    if not slug_match:
        return None

    return {"slug": slug_match.group(1), "matched_title": best_title.strip()}


def resolve_anime_slug(title):
    base_slug = normalize(title).replace(" ", "-")
    direct = try_direct_slug(base_slug)
    if direct:
        return direct
    return search_fallback(title)


# ------------------------------------------------------------
# 3) Página de download -> links .mp4
# ------------------------------------------------------------

def get_episode_streams(slug, episode_number):
    url = f"{ANIMEFIRE_BASE}/download/{slug}/{episode_number}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    html = res.text

    links = re.findall(
        r'<a[^>]+href="(https?://[^"]*lightspeedst\.net[^"]*\.mp4[^"]*)"[^>]*>\s*([^<]+?)\s*</a>',
        html,
    )

    quality_map = {"SD": "360p", "HD": "720p", "F-HD": "1080p", "FHD": "1080p"}

    streams = []
    seen = set()
    for video_url, label in links:
        video_url = video_url.replace("&amp;", "&")
        if video_url in seen:
            continue
        seen.add(video_url)
        label_up = label.strip().upper()
        streams.append(
            {"url": video_url, "quality": quality_map.get(label_up, label_up)}
        )

    return streams


# ------------------------------------------------------------
# Rotas Stremio
# ------------------------------------------------------------

@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.route("/stream/<media_type>/<video_id>.json")
def stream(media_type, video_id):
    # video_id vem tipo "tt1234567" (filme) ou "tt1234567:1:5" (série: temporada:episódio)
    parts = video_id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else 1
    episode = int(parts[2]) if len(parts) > 2 else 1

    try:
        title_pt, title_original = imdb_to_tmdb_title(imdb_id, media_type)
    except Exception as e:
        return jsonify({"streams": [], "error": str(e)})

    base_title = title_pt or title_original
    if not base_title:
        return jsonify({"streams": []})

    roman = to_roman(season) if season > 1 else None
    attempts = []
    if roman:
        attempts.append(f"{base_title} {roman}")
    attempts.append(base_title)
    if title_original and title_original != base_title:
        if roman:
            attempts.append(f"{title_original} {roman}")
        attempts.append(title_original)

    found = None
    for query in attempts:
        try:
            found = resolve_anime_slug(query)
        except requests.RequestException:
            found = None
        if found:
            break

    if not found:
        return jsonify({"streams": []})

    ep_num = episode if media_type == "series" else 1

    try:
        raw_streams = get_episode_streams(found["slug"], ep_num)
    except requests.RequestException as e:
        return jsonify({"streams": [], "error": str(e)})

    streams = [
        {
            "name": "AnimeFire",
            "title": f"{found['matched_title']} - Ep {ep_num} ({s['quality']})",
            "url": s["url"],
        }
        for s in raw_streams
    ]

    return jsonify({"streams": streams})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860)
