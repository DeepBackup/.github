from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple

import requests


class MirrorError(Exception):
    """Base exception for mirroring operations"""
    pass


class GraphQLError(MirrorError):
    """GraphQL API errors"""
    pass


class RepositoryError(MirrorError):
    """Repository operation errors"""
    pass


GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_API_URL = "https://api.github.com"


def _graphql_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _rest_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def _auth_url(url: str, token: str) -> str:
    return url.replace("https://", f"https://x-access-token:{token}@")


def get_starlist_repositories(token: str, list_name: str) -> List[Dict[str, str]]:
    """
    Fetch repositories from a specific GitHub Star List using GraphQL API.
    FIX: Pagination now correctly paginates on the target list only (not all lists).
    """
    headers = _graphql_headers(token)
    repositories = []
    cursor = None

    # First, discover the list slug/name (lists API requires fetching all lists first)
    query = """
    query {
      viewer {
        lists(first: 100) {
          nodes { name }
        }
      }
    }
    """
    resp = requests.post(GRAPHQL_URL, json={"query": query}, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise GraphQLError(f"GraphQL errors: {data['errors']}")

    lists = data["data"]["viewer"]["lists"]["nodes"]
    available = [l["name"] for l in lists]
    if list_name not in available:
        raise GraphQLError(
            f"Star List '{list_name}' not found. Available lists: {available or 'None'}"
        )

    # Now paginate only the target list's items
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = f"""
        query {{
          viewer {{
            lists(first: 100) {{
              nodes {{
                name
                items(first: 100{after_clause}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  nodes {{
                    ... on Repository {{
                      owner {{ login }}
                      name
                      url
                      isPrivate
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        resp = requests.post(GRAPHQL_URL, json={"query": query}, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise GraphQLError(f"GraphQL errors: {data['errors']}")

        nodes = data["data"]["viewer"]["lists"]["nodes"]
        target = next((l for l in nodes if l["name"] == list_name), None)
        if not target:
            break

        items = target["items"]
        for node in items["nodes"]:
            if node:
                repositories.append({
                    "owner": node["owner"]["login"],
                    "name": node["name"],
                    "url": node["url"],
                    "is_private": node["isPrivate"]
                })

        page_info = items["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    print(f"Found {len(repositories)} repositories in Star List '{list_name}'")
    return repositories


def parse_github_url(url: str) -> Dict[str, str]:
    url = url.strip().rstrip("/")
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise ValueError(f"Invalid GitHub URL format: {url}")
    owner, name = match.groups()
    return {"owner": owner, "name": name, "url": url}


def load_private_repos_from_vars() -> List[Dict[str, str]]:
    repos_var = os.getenv("PRIVATE_REPOS", "").strip()
    if not repos_var:
        print("No PRIVATE_REPOS variable set, skipping private repositories")
        return []

    urls = [u.strip() for u in (repos_var.split('\n') if '\n' in repos_var else repos_var.split(',')) if u.strip()]
    repos = []
    for url in urls:
        try:
            repos.append(parse_github_url(url))
        except ValueError as e:
            print(f"Warning: Skipping invalid URL in PRIVATE_REPOS: {e}")

    print(f"Loaded {len(repos)} private repositories from PRIVATE_REPOS variable")
    return repos


def load_private_repos(filepath: Path) -> List[Dict[str, str]]:
    """Load private repositories from JSON file (legacy fallback)."""
    if not filepath.exists():
        return []

    with open(filepath) as f:
        urls = json.load(f)

    if not isinstance(urls, list):
        raise ValueError("private_repos.json must contain a JSON array")

    repos = []
    for url in urls:
        try:
            repos.append(parse_github_url(url))
        except ValueError as e:
            print(f"Warning: Skipping invalid URL: {e}")

    print(f"Loaded {len(repos)} repositories from {filepath}")
    return repos


def merge_repositories(starlist_repos: List[Dict], private_repos: List[Dict]) -> List[Dict]:
    seen: Set[tuple] = set()
    merged = []
    for repo in starlist_repos + private_repos:
        key = (repo["owner"], repo["name"])
        if key not in seen:
            seen.add(key)
            merged.append(repo)
    print(f"Total unique repositories to mirror: {len(merged)}")
    return merged


def get_all_refs(token: str, owner: str, name: str) -> Optional[Dict[str, str]]:
    """
    Get all branches and tags via git ls-remote.
    Returns {ref: sha} filtered to heads/tags only, or None on failure.
    """
    url = _auth_url(f"https://github.com/{owner}/{name}.git", token)
    try:
        result = subprocess.run(
            ["git", "ls-remote", url],
            capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode != 0:
            return None

        refs = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) == 2:
                sha, ref = parts
                if ref.startswith(('refs/heads/', 'refs/tags/')):
                    refs[ref] = sha
        return refs

    except Exception as e:
        print(f"    Warning: Could not fetch refs for {owner}/{name}: {e}")
        return None


def check_if_update_needed(
        token: str, org: str, source_owner: str, source_name: str
) -> Tuple[bool, str, Optional[Dict[str, str]]]:
    """
    Compare ALL branches and tags between source and destination.
    FIX: Returns source_refs so mirror_repository can reuse them, avoiding re-fetch.
    Returns (needs_update, reason, source_refs)
    """
    dest_name = f"{source_owner}__{source_name}"
    print(f"  Checking for updates...")

    source_refs = get_all_refs(token, source_owner, source_name)
    if not source_refs:
        return True, "Could not fetch source refs or source is empty", source_refs

    dest_refs = get_all_refs(token, org, dest_name)
    if dest_refs is None:
        return True, "Destination doesn't exist or is empty", source_refs

    differences = []
    for ref, sha in source_refs.items():
        if ref not in dest_refs:
            differences.append(f"new ref: {ref}")
        elif dest_refs[ref] != sha:
            short = ref.replace('refs/heads/', '').replace('refs/tags/', '')
            differences.append(f"{short}: {dest_refs[ref][:7]} → {sha[:7]}")

    for ref in dest_refs:
        if ref not in source_refs:
            differences.append(f"deleted ref: {ref}")

    if differences:
        if len(differences) <= 3:
            reason = ", ".join(differences)
        else:
            reason = f"{differences[0]}, {differences[1]}, and {len(differences) - 2} more changes"
        print(f"  ⚡ Update needed: {reason}")
        return True, reason, source_refs

    print(f"  ✓ Already up-to-date ({len(source_refs)} refs checked)")
    return False, "All refs match", source_refs


def ensure_destination_repo(
        token: str, org: str, source_owner: str, source_name: str, visibility: str
) -> str:
    """
    Ensure destination repo exists. Returns authenticated clone URL.
    """
    dest_name = f"{source_owner}__{source_name}"
    headers = _rest_headers(token)

    resp = requests.get(f"{GITHUB_API_URL}/repos/{org}/{dest_name}", headers=headers, timeout=30)

    if resp.status_code == 200:
        print(f"  Repository {org}/{dest_name} already exists")
        return _auth_url(resp.json()["clone_url"], token)

    if resp.status_code == 404:
        print(f"  Creating repository {org}/{dest_name}")
        payload = {
            "name": dest_name,
            "private": visibility.lower() == "private",
            "description": f"Mirror of https://github.com/{source_owner}/{source_name}",
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False
        }
        resp = requests.post(
            f"{GITHUB_API_URL}/orgs/{org}/repos",
            json=payload, headers=headers, timeout=30
        )
        if resp.status_code not in (200, 201):
            raise RepositoryError(
                f"Failed to create {org}/{dest_name}: {resp.status_code} {resp.text}"
            )
        print(f"  Successfully created {org}/{dest_name}")
        return _auth_url(resp.json()["clone_url"], token)

    raise RepositoryError(
        f"Failed to check {org}/{dest_name}: {resp.status_code} {resp.text}"
    )


def mirror_repository(
        source_url: str,
        dest_url: str,
        source_owner: str,
        source_name: str,
        token: str
):
    """
    Mirror via git clone --mirror then selective push (branches + tags only).
    FIX: Removed redundant `push --mirror --prune` which caused noisy PR ref errors.
    """
    mirror_dir = Path(f"/tmp/mirror_{source_owner}__{source_name}")

    try:
        if mirror_dir.exists():
            subprocess.run(["rm", "-rf", str(mirror_dir)], check=True, capture_output=True)

        auth_source = (
            _auth_url(source_url, token) if source_url.startswith("https://") else source_url
        )

        print(f"  Cloning {source_owner}/{source_name} as mirror...")
        subprocess.run(
            ["git", "clone", "--mirror", auth_source, str(mirror_dir)],
            check=True, capture_output=True, text=True
        )

        # Get refs from the local mirror (no extra network call)
        result = subprocess.run(
            ["git", "-C", str(mirror_dir), "show-ref"],
            capture_output=True, text=True, check=False
        )

        refs_to_push = []
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and not parts[1].startswith('refs/pull/'):
                    refs_to_push.append(parts[1])

        if not refs_to_push:
            print(f"  ⚠ No pushable refs found for {source_owner}/{source_name}")
            return

        print(f"  Pushing {len(refs_to_push)} refs to destination...")
        subprocess.run(
            ["git", "-C", str(mirror_dir), "push", "--force", dest_url] + refs_to_push,
            check=True, capture_output=True, text=True
        )

        print(f"  ✓ Successfully mirrored {source_owner}/{source_name}")

    except subprocess.CalledProcessError as e:
        raise RepositoryError(
            f"Git operation failed for {source_owner}/{source_name}: "
            f"{e.stderr or e.stdout}"
        )

    finally:
        if mirror_dir.exists():
            subprocess.run(["rm", "-rf", str(mirror_dir)], check=False, capture_output=True)


def main():
    token = os.getenv("MIRROR_TOKEN")
    org = os.getenv("MIRROR_ORG")
    visibility = os.getenv("MIRROR_VISIBILITY", "private")
    list_name = os.getenv("STAR_LIST_NAME", "DB")
    skip_check = os.getenv("SKIP_UPDATE_CHECK", "false").lower() == "true"

    if not token:
        print("Error: MIRROR_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)
    if not org:
        print("Error: MIRROR_ORG environment variable not set", file=sys.stderr)
        sys.exit(1)

    print(f"Starting mirror operation for Star List '{list_name}' to org '{org}'")
    print(f"Destination visibility: {visibility}")
    print("⚠️  Update check DISABLED — will mirror all repos" if skip_check
          else "✓ Update check ENABLED — checking ALL branches and tags")
    print("=" * 80)

    try:
        starlist_repos = get_starlist_repositories(token, list_name)

        private_repos = load_private_repos_from_vars()
        if not private_repos:
            private_repos_file = Path("private_repos.json")
            if private_repos_file.exists():
                private_repos = load_private_repos(private_repos_file)

        all_repos = merge_repositories(starlist_repos, private_repos)
        if not all_repos:
            print("No repositories to mirror")
            return

        print("=" * 80)

        failed, skipped, updated = [], [], []

        for idx, repo in enumerate(all_repos, 1):
            owner, name = repo["owner"], repo["name"]
            print(f"\n[{idx}/{len(all_repos)}] Processing {owner}/{name}")
            print("-" * 80)

            try:
                if not skip_check:
                    needs_update, reason, _ = check_if_update_needed(
                        token=token, org=org, source_owner=owner, source_name=name
                    )
                    if not needs_update:
                        skipped.append(f"{owner}/{name}")
                        continue

                dest_url = ensure_destination_repo(
                    token=token, org=org,
                    source_owner=owner, source_name=name,
                    visibility=visibility
                )
                mirror_repository(
                    source_url=repo["url"], dest_url=dest_url,
                    source_owner=owner, source_name=name, token=token
                )
                updated.append(f"{owner}/{name}")

            except (MirrorError, subprocess.CalledProcessError) as e:
                print(f"  ✗ Error: {e}", file=sys.stderr)
                failed.append(f"{owner}/{name}")

        print("\n" + "=" * 80)
        print("Mirror operation complete")
        print(f"✓ Updated:               {len(updated)}")
        print(f"⊘ Skipped (up-to-date):  {len(skipped)}")
        print(f"✗ Failed:                {len(failed)}")
        print(f"   Total:                {len(all_repos)}")

        def _print_list(label, icon, items, limit=10):
            if not items:
                return
            if len(items) <= limit:
                print(f"\n{label}:")
                for r in items:
                    print(f"  {icon} {r}")
            else:
                print(f"\n{icon} {len(items)} {label.lower()}")

        _print_list("Skipped repositories (already up-to-date)", "⊘", skipped)
        _print_list("Updated repositories", "✓", updated)
        _print_list("Failed repositories", "✗", failed)

        if failed:
            sys.exit(1)

    except GraphQLError as e:
        print(f"GraphQL Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
