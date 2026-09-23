import logging
from datetime import datetime, timedelta, timezone

from ..http import ApiError

log = logging.getLogger(__name__)
GH = "https://api.github.com"
ZFMT = "%Y-%m-%dT%H:%M:%SZ"  # match GitHub's own format so stored timestamps sort lexically


def parse_repos(urls, limit):
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


def repo_info(http, repo):
    return http.get_json("github", f"{GH}/repos/{repo}", allow_404=True)


def fetch_commits(http, repo, since, max_pages=5):
    """Incremental: `since` is the stored watermark, so later runs pull only new commits."""
    out, page = [], 1
    while page <= max_pages:
        rows = http.get_json("github", f"{GH}/repos/{repo}/commits",
                             {"since": since, "per_page": 100, "page": page}, allow_404=True)
        if not rows:
            break
        for c in rows:
            when = ((c.get("commit") or {}).get("committer") or {}).get("date")
            if not when:
                continue
            author = (c.get("author") or {}).get("login") or \
                     ((c.get("commit") or {}).get("author") or {}).get("email")
            out.append((repo, c["sha"], author, when))
        if len(rows) < 100:
            break
        page += 1
    return out


def sync_repo(con, http, repo, coin_id, lookback_days, min_stars, refresh_meta):
    """Store new commits for one repo; returns (ok, note). Never raises."""
    row = con.execute("SELECT * FROM github_repos WHERE repo=?", (repo,)).fetchone()
    now = datetime.now(timezone.utc)
    stars, archived = (row["stars"], row["archived"]) if row else (None, 0)

    if row is None or refresh_meta or stars is None:
        try:
            info = repo_info(http, repo)
        except ApiError as e:
            return False, f"repo_info: {e}"
        if not info:
            con.execute("INSERT OR REPLACE INTO github_repos(repo,coin_id,stars,archived,checked_at,error)"
                        " VALUES(?,?,?,?,?,?)", (repo, coin_id, None, 0, now.isoformat(), "not_found"))
            return False, "repo not found"
        stars, archived = info.get("stargazers_count") or 0, int(bool(info.get("archived")))
    if stars is not None and stars < min_stars:
        con.execute("INSERT OR REPLACE INTO github_repos(repo,coin_id,stars,archived,last_commit_at,checked_at,error)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (repo, coin_id, stars, archived, row["last_commit_at"] if row else None,
                     now.isoformat(), "below_min_stars"))
        return False, f"{stars} stars < {min_stars}"

    watermark = row["last_commit_at"] if row and row["last_commit_at"] else \
        (now - timedelta(days=lookback_days)).strftime(ZFMT)
    try:
        commits = fetch_commits(http, repo, watermark)
    except ApiError as e:
        con.execute("INSERT OR REPLACE INTO github_repos(repo,coin_id,stars,archived,last_commit_at,checked_at,error)"
                    " VALUES(?,?,?,?,?,?,?)", (repo, coin_id, stars, archived, watermark, now.isoformat(), str(e)[:200]))
        return False, f"commits: {e}"
    if commits:
        con.executemany("INSERT OR IGNORE INTO repo_commits(repo,sha,author,committed_at) VALUES(?,?,?,?)", commits)
        watermark = max(watermark, max(c[3] for c in commits))
    con.execute("INSERT OR REPLACE INTO github_repos(repo,coin_id,stars,archived,last_commit_at,checked_at,error)"
                " VALUES(?,?,?,?,?,?,NULL)", (repo, coin_id, stars, archived, watermark, now.isoformat()))
    return True, f"+{len(commits)} commits"


def window_stats(con, repos, as_of, window_days):
    """(commits, contributors) for the trailing and prior windows, read from local storage."""
    if not repos:
        return (None, None), (None, None)
    marks = [(as_of - timedelta(days=n * window_days)).strftime(ZFMT) for n in (0, 1, 2)]
    q = ("SELECT COUNT(*) c, COUNT(DISTINCT author) a FROM repo_commits "
         f"WHERE repo IN ({','.join('?' * len(repos))}) AND committed_at > ? AND committed_at <= ?")
    cur = con.execute(q, (*repos, marks[1], marks[0])).fetchone()
    pri = con.execute(q, (*repos, marks[2], marks[1])).fetchone()
    return (cur["c"], pri["c"]), (cur["a"], pri["a"])


PERMANENT_ERRORS = ("not_found", "below_min_stars")


def usable_repos(con, coin_id):
    """Repos we can compute from: cleanly synced, or transiently failed but holding stored commits.
    A rate-limited run must not throw away history we already have."""
    rows = con.execute(
        "SELECT g.repo, g.error, EXISTS(SELECT 1 FROM repo_commits c WHERE c.repo=g.repo) has_commits "
        "FROM github_repos g WHERE g.coin_id=?", (coin_id,))
    return [r["repo"] for r in rows
            if r["error"] is None or (r["error"] not in PERMANENT_ERRORS and r["has_commits"])]
