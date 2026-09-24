import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
MAX_REPOS = 3   # repo links are identity metadata only; nothing is fetched from GitHub


def parse_repos(urls, limit=MAX_REPOS):
    """"owner/repo" slugs from CoinGecko's links.repos_url.github, for display."""
    out = []
    for u in urls or []:
        if not u or "github.com" not in u:
            continue
        parts = [p for p in u.split("github.com/")[-1].split("/") if p]
        if len(parts) >= 2:
            slug = f"{parts[0]}/{parts[1].removesuffix('.git')}"
            if slug not in out:
                out.append(slug)
    return out[:limit]


def needs_refresh(row, refresh_days):
    if row is None or not row["metadata_updated_at"]:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["metadata_updated_at"])
    return age > timedelta(days=refresh_days)


def fetch(http, coin_id):
    return http.get_json("coingecko", f"{CG}/coins/{coin_id}", {
        "localization": "false", "tickers": "false", "market_data": "false",
        "community_data": "false", "developer_data": "false", "sparkline": "false",
    }, allow_404=True)


def upsert(con, coin_id, market_row, detail, llama_slug):
    links = (detail or {}).get("links") or {}
    home = next((h for h in (links.get("homepage") or []) if h), None)
    repos = parse_repos((links.get("repos_url") or {}).get("github"))
    con.execute(
        """INSERT INTO coins(coin_id,symbol,name,image,categories,github_repos,contract_addresses,
                             defillama_slug,homepage,metadata_updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(coin_id) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
             image=excluded.image, categories=excluded.categories, github_repos=excluded.github_repos,
             contract_addresses=excluded.contract_addresses, defillama_slug=excluded.defillama_slug,
             homepage=excluded.homepage, metadata_updated_at=excluded.metadata_updated_at""",
        (coin_id, (market_row.get("symbol") or "").upper(), market_row.get("name"), market_row.get("image"),
         json.dumps([c for c in ((detail or {}).get("categories") or []) if c]),
         json.dumps(repos), json.dumps((detail or {}).get("platforms") or {}),
         llama_slug, home, datetime.now(timezone.utc).isoformat()))
    return repos


def touch(con, coin_id, market_row):
    """Keep price-independent identity fresh for coins whose full metadata we skipped."""
    con.execute("""INSERT INTO coins(coin_id,symbol,name,image) VALUES(?,?,?,?)
                   ON CONFLICT(coin_id) DO UPDATE SET symbol=excluded.symbol,
                     name=excluded.name, image=excluded.image""",
                (coin_id, (market_row.get("symbol") or "").upper(),
                 market_row.get("name"), market_row.get("image")))
