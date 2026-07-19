#!/usr/bin/env python3
"""Update the terminal-style SVGs with GitHub statistics."""

from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profile.json"
STATS_PATH = ROOT / "assets" / "stats.json"
CACHE_PATH = ROOT / "cache" / "repositories.json"
TEMPLATE_PATH = ROOT / "assets" / "templates" / "profile.svg.tmpl"

THEMES = {
    "dark": {
        "BACKGROUND": "#161b22",
        "TEXT": "#c9d1d9",
        "KEY": "#ffa657",
        "VALUE": "#a5d6ff",
        "ADD": "#3fb950",
        "DELETE": "#f85149",
        "DOT": "#616e7f",
    },
    "light": {
        "BACKGROUND": "#f6f8fa",
        "TEXT": "#24292f",
        "KEY": "#953800",
        "VALUE": "#0a3069",
        "ADD": "#1a7f37",
        "DELETE": "#cf222e",
        "DOT": "#c2cfde",
    },
}


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_json(url: str, token: str = "", payload: dict[str, Any] | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "FotieMConstant-profile-readme",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    result = request_json(
        "https://api.github.com/graphql", token, {"query": query, "variables": variables}
    )
    if result.get("errors"):
        raise RuntimeError(result["errors"][0]["message"])
    return result["data"]


def owned_repositories(username: str, token: str) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    for page in range(1, 20):
        query = urllib.parse.urlencode(
            {"type": "owner", "sort": "full_name", "per_page": 100, "page": page}
        )
        batch = request_json(f"https://api.github.com/users/{username}/repos?{query}", token)
        repositories.extend(batch)
        if len(batch) < 100:
            break
    return repositories


def affiliated_repositories(username: str, token: str) -> list[dict[str, Any]]:
    query = """
      query($login: String!, $cursor: String) {
        user(login: $login) {
          repositories(
            first: 100,
            after: $cursor,
            ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]
          ) {
            nodes {
              nameWithOwner
              defaultBranchRef { target { ... on Commit { history { totalCount } } } }
            }
            pageInfo { endCursor hasNextPage }
          }
        }
      }
    """
    repositories: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = graphql(query, {"login": username, "cursor": cursor}, token)
        page = data["user"]["repositories"]
        repositories.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return repositories
        cursor = page["pageInfo"]["endCursor"]


def repository_code_stats(name_with_owner: str, username: str, token: str) -> dict[str, int]:
    owner, name = name_with_owner.split("/", 1)
    query = """
      query($owner: String!, $name: String!, $cursor: String) {
        repository(owner: $owner, name: $name) {
          defaultBranchRef {
            target {
              ... on Commit {
                history(first: 100, after: $cursor) {
                  nodes { additions deletions author { user { login } } }
                  pageInfo { endCursor hasNextPage }
                }
              }
            }
          }
        }
      }
    """
    totals = {"commits": 0, "additions": 0, "deletions": 0}
    cursor: str | None = None
    while True:
        data = graphql(
            query, {"owner": owner, "name": name, "cursor": cursor}, token
        )
        branch = data["repository"]["defaultBranchRef"]
        if not branch:
            return totals
        history = branch["target"]["history"]
        for commit in history["nodes"]:
            author = commit.get("author") or {}
            user = author.get("user") or {}
            if user.get("login", "").casefold() == username.casefold():
                totals["commits"] += 1
                totals["additions"] += commit["additions"]
                totals["deletions"] += commit["deletions"]
        if not history["pageInfo"]["hasNextPage"]:
            return totals
        cursor = history["pageInfo"]["endCursor"]


def collect_code_stats(username: str, token: str) -> dict[str, int]:
    repositories = affiliated_repositories(username, token)
    cache: dict[str, dict[str, int]] = load_json(CACHE_PATH, {})
    active_names: set[str] = set()

    for index, repository in enumerate(repositories, start=1):
        name = repository["nameWithOwner"]
        active_names.add(name)
        branch = repository.get("defaultBranchRef")
        commit_count = branch["target"]["history"]["totalCount"] if branch else 0
        cached = cache.get(name, {})
        if cached.get("branch_commits") == commit_count:
            continue
        print(f"[{index}/{len(repositories)}] scanning {name}")
        totals = repository_code_stats(name, username, token)
        cache[name] = {"branch_commits": commit_count, **totals}
        save_json(CACHE_PATH, cache)

    cache = {name: data for name, data in cache.items() if name in active_names}
    save_json(CACHE_PATH, cache)
    return {
        "contributed_repos": len(repositories),
        "commits": sum(repo["commits"] for repo in cache.values()),
        "loc_add": sum(repo["additions"] for repo in cache.values()),
        "loc_del": sum(repo["deletions"] for repo in cache.values()),
    }


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def elapsed_date(start: dt.date, end: dt.date) -> str:
    months = (end.year - start.year) * 12 + end.month - start.month
    if add_months(start, months) > end:
        months -= 1
    anchor = add_months(start, months)
    years, remaining_months = divmod(months, 12)
    days = (end - anchor).days

    def unit(number: int, singular: str) -> str:
        return f"{number} {singular}{'' if number == 1 else 's'}"

    return f"{unit(years, 'year')}, {unit(remaining_months, 'month')}, {unit(days, 'day')}"


def collect_stats(profile: dict[str, Any]) -> dict[str, Any]:
    username = profile["username"]
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    defaults = {
        "repos": "—",
        "stars": "—",
        "followers": "—",
        "contributed_repos": "—",
        "commits": "—",
        "loc_add": "—",
        "loc_del": "—",
        "loc_net": "—",
        "uptime": "—",
    }
    cached = defaults | load_json(STATS_PATH, {})
    if os.environ.get("OFFLINE") == "1":
        return cached
    try:
        user = request_json(f"https://api.github.com/users/{username}", token)
        repositories = owned_repositories(username, token)
        stats = cached | {
            "repos": user["public_repos"],
            "stars": sum(repo["stargazers_count"] for repo in repositories),
            "followers": user["followers"],
        }
        birth_value = profile.get("birthday")
        start = (
            dt.date.fromisoformat(birth_value)
            if birth_value
            else dt.datetime.fromisoformat(user["created_at"].replace("Z", "+00:00")).date()
        )
        stats["uptime"] = elapsed_date(start, dt.datetime.now(dt.timezone.utc).date())
        if token:
            stats.update(collect_code_stats(username, token))
        if isinstance(stats.get("loc_add"), int) and isinstance(stats.get("loc_del"), int):
            stats["loc_net"] = stats["loc_add"] - stats["loc_del"]
        stats = {key: stats[key] for key in defaults}
        save_json(STATS_PATH, stats)
        return stats
    except (OSError, KeyError, RuntimeError, TypeError, ValueError, urllib.error.HTTPError) as error:
        print(f"GitHub stats unavailable ({error}); rendering cached values.")
        return cached


def dotted(value: Any, width: int) -> str:
    count = max(1, width - len(f"{value:,}" if isinstance(value, int) else str(value)))
    return " " + "." * count + " "


def render(template: str, values: dict[str, Any]) -> str:
    rendered = template
    for key, value in values.items():
        display = f"{value:,}" if isinstance(value, int) else str(value)
        rendered = rendered.replace("{{" + key + "}}", html.escape(display))
    missing = sorted(set(re.findall(r"{{([A-Z0-9_]+)}}", rendered)))
    if missing:
        raise ValueError(f"Missing template values: {', '.join(missing)}")
    return rendered


def main() -> None:
    profile = load_json(PROFILE_PATH, {})
    stats = collect_stats(profile)
    values = {key.upper(): value for key, value in (profile | stats).items()}
    for key, width in {
        "REPOS": 6,
        "STARS": 14,
        "COMMITS": 22,
        "FOLLOWERS": 10,
        "LOC_NET": 9,
        "LOC_DEL": 7,
    }.items():
        values[f"{key}_DOTS"] = dotted(values[key], width)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for theme_name, colors in THEMES.items():
        output = render(template, values | colors)
        path = ROOT / "assets" / f"profile-{theme_name}.svg"
        path.write_text(output, encoding="utf-8")
        print(f"Rendered {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
