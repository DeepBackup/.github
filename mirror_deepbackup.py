import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Optional

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

def get_starlist_repositories(token: str, list_name: str) -> List[Dict[str, str]]:
    """
    Fetch repositories from a specific GitHub Star List using GraphQL API
    Returns list of dicts with 'owner', 'name', 'url', 'is_private'
    """
    graphql_url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    repositories = []
    cursor = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""

        # Query to get lists and their items in one go
        query = f"""
        query {{
          viewer {{
            lists(first: 100) {{
              nodes {{
                name
                items(first: 100{after_clause}) {{
                  pageInfo {{
                    hasNextPage
                    endCursor
                  }}
                  nodes {{
                    ... on Repository {{
                      nameWithOwner
                      url
                      isPrivate
                      owner {{
                        login
                      }}
                      name
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        response = requests.post(
            graphql_url,
            json={"query": query},
            headers=headers,
            timeout=30
        )

        if response.status_code != 200:
            raise GraphQLError(f"GraphQL request failed: {response.status_code} {response.text}")

        data = response.json()
        if "errors" in data:
            raise GraphQLError(f"GraphQL errors: {data['errors']}")

        lists = data.get("data", {}).get("viewer", {}).get("lists", {}).get("nodes", [])

        # Find the target list
        target_list = None
        for lst in lists:
            if lst.get("name") == list_name:
                target_list = lst
                break

        if not target_list:
            available_lists = [l['name'] for l in lists]
            raise GraphQLError(
                f"Star List '{list_name}' not found. "
                f"Available lists: {available_lists if available_lists else 'None'}"
            )

        # Extract repositories from the target list
        items = target_list.get("items", {})
        nodes = items.get("nodes", [])

        for node in nodes:
            if node:  # Filter out null nodes
                repositories.append({
                    "owner": node["owner"]["login"],
                    "name": node["name"],
                    "url": node["url"],
                    "is_private": node["isPrivate"]
                })

        # Check pagination
        page_info = items.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break

        cursor = page_info.get("endCursor")

    print(f"Found {len(repositories)} repositories in Star List '{list_name}'")
    return repositories


def parse_github_url(url: str) -> Dict[str, str]:
    """
    Parse GitHub repository URL to extract owner and name
    Supports HTTPS URLs
    """
    url = url.strip().rstrip("/")

    # HTTPS format: https://github.com/owner/repo or https://github.com/owner/repo.git
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)

    if not match:
        raise ValueError(f"Invalid GitHub URL format: {url}")

    owner, name = match.groups()
    return {"owner": owner, "name": name, "url": url}


def load_private_repos_from_vars() -> List[Dict[str, str]]:
    """
    Load private repositories from GitHub Variables
    Reads PRIVATE_REPOS environment variable (newline or comma-separated URLs)
    Returns list of dicts with 'owner', 'name', 'url'
    """
    repos_var = os.getenv("PRIVATE_REPOS", "").strip()

    if not repos_var:
        print("No PRIVATE_REPOS variable set, skipping private repositories")
        return []

    # Support both newline and comma-separated formats
    if '\n' in repos_var:
        urls = [url.strip() for url in repos_var.split('\n') if url.strip()]
    else:
        urls = [url.strip() for url in repos_var.split(',') if url.strip()]

    repos = []
    for url in urls:
        try:
            repo = parse_github_url(url)
            repos.append(repo)
        except ValueError as e:
            print(f"Warning: Skipping invalid URL in PRIVATE_REPOS: {e}")

    print(f"Loaded {len(repos)} private repositories from PRIVATE_REPOS variable")
    return repos


def load_private_repos(filepath: Path) -> List[Dict[str, str]]:
    """
    Load private repositories from JSON file (deprecated - kept for backwards compatibility)
    Returns list of dicts with 'owner', 'name', 'url'
    """
    if not filepath.exists():
        print(f"Note: {filepath} does not exist, skipping file-based repos")
        return []

    try:
        with open(filepath, "r") as f:
            urls = json.load(f)

        if not isinstance(urls, list):
            raise ValueError("private_repos.json must contain a JSON array")

        if not urls:
            print(f"Note: {filepath} is empty")
            return []

        repos = []
        for url in urls:
            try:
                repo = parse_github_url(url)
                repos.append(repo)
            except ValueError as e:
                print(f"Warning: Skipping invalid URL: {e}")

        print(f"Loaded {len(repos)} repositories from {filepath}")
        return repos

    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {filepath}: {e}")


def merge_repositories(starlist_repos: List[Dict], private_repos: List[Dict]) -> List[Dict]:
    """
    Merge two repository lists, removing duplicates
    Returns unique repositories based on owner/name
    """
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
    Get all branches and tags with their commit SHAs using git ls-remote
    Returns dict of {ref_name: commit_sha} or None if failed

    This is more efficient than multiple API calls and works for both public and private repos
    """
    url = f"https://github.com/{owner}/{name}.git"
    auth_url = url.replace("https://", f"https://x-access-token:{token}@")

    try:
        result = subprocess.run(
            ["git", "ls-remote", auth_url],
            capture_output=True,
            text=True,
            timeout=30,
            check=False
        )

        if result.returncode != 0:
            return None

        refs = {}
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('\t')
                if len(parts) == 2:
                    commit_sha, ref = parts
                    # Only include branches and tags (exclude PRs and other special refs)
                    if ref.startswith('refs/heads/') or ref.startswith('refs/tags/'):
                        refs[ref] = commit_sha

        return refs

    except Exception as e:
        print(f"    Warning: Could not fetch refs for {owner}/{name}: {e}")
        return None


def check_if_update_needed(
        token: str,
        org: str,
        source_owner: str,
        source_name: str
) -> tuple[bool, str]:
    """
    Check if the backup repository needs updating by comparing ALL branches and tags
    Returns (needs_update: bool, reason: str)
    """
    dest_name = f"{source_owner}__{source_name}"

    print(f"  Checking for updates...")

    # Get all refs from source
    source_refs = get_all_refs(token, source_owner, source_name)
    if source_refs is None:
        return True, "Could not fetch source refs"

    if not source_refs:
        return True, "Source has no refs"

    # Get all refs from destination
    dest_refs = get_all_refs(token, org, dest_name)
    if dest_refs is None:
        return True, "Destination doesn't exist or is empty"

    # Compare refs
    # Check if any ref is different or missing
    differences = []

    # Check for new or updated refs in source
    for ref, source_sha in source_refs.items():
        if ref not in dest_refs:
            differences.append(f"new ref: {ref}")
        elif dest_refs[ref] != source_sha:
            ref_name = ref.replace('refs/heads/', '').replace('refs/tags/', '')
            differences.append(f"{ref_name}: {dest_refs[ref][:7]} → {source_sha[:7]}")

    # Check for deleted refs (exist in dest but not in source)
    for ref in dest_refs:
        if ref not in source_refs:
            differences.append(f"deleted ref: {ref}")

    if differences:
        # Show first few differences
        if len(differences) <= 3:
            reason = ", ".join(differences)
        else:
            reason = f"{differences[0]}, {differences[1]}, and {len(differences) - 2} more changes"

        print(f"  ⚡ Update needed: {reason}")
        return True, reason
    else:
        print(f"  ✓ Already up-to-date ({len(source_refs)} refs checked)")
        return False, "All refs match"


def ensure_destination_repo(
        token: str,
        org: str,
        source_owner: str,
        source_name: str,
        visibility: str
) -> str:
    """
    Ensure destination repository exists in target organization
    Returns clone URL for the destination repository
    Uses collision-safe naming: owner__repo
    """
    dest_name = f"{source_owner}__{source_name}"

    # Check if repository exists
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    check_url = f"https://api.github.com/repos/{org}/{dest_name}"
    response = requests.get(check_url, headers=headers, timeout=30)

    if response.status_code == 200:
        print(f"  Repository {org}/{dest_name} already exists")
        clone_url = response.json()["clone_url"]
        return clone_url.replace("https://", f"https://x-access-token:{token}@")

    elif response.status_code == 404:
        # Create repository
        print(f"  Creating repository {org}/{dest_name}")
        create_url = f"https://api.github.com/orgs/{org}/repos"

        payload = {
            "name": dest_name,
            "private": visibility.lower() == "private",
            "description": f"Mirror of https://github.com/{source_owner}/{source_name}",
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False
        }

        response = requests.post(create_url, json=payload, headers=headers, timeout=30)

        if response.status_code not in (201, 200):
            raise RepositoryError(
                f"Failed to create repository {org}/{dest_name}: "
                f"{response.status_code} {response.text}"
            )

        clone_url = response.json()["clone_url"]
        print(f"  Successfully created {org}/{dest_name}")
        return clone_url.replace("https://", f"https://x-access-token:{token}@")

    else:
        raise RepositoryError(
            f"Failed to check repository {org}/{dest_name}: "
            f"{response.status_code} {response.text}"
        )


def mirror_repository(
        source_url: str,
        dest_url: str,
        source_owner: str,
        source_name: str,
        token: str
):
    """
    Mirror a repository using git clone --mirror and selective push
    Idempotent operation - excludes pull request refs which GitHub doesn't allow
    """
    mirror_dir = Path(f"/tmp/mirror_{source_owner}__{source_name}")

    try:
        # Clean up any existing mirror directory
        if mirror_dir.exists():
            subprocess.run(
                ["rm", "-rf", str(mirror_dir)],
                check=True,
                capture_output=True
            )

        # Prepare authenticated source URL
        if source_url.startswith("https://"):
            auth_source_url = source_url.replace("https://", f"https://x-access-token:{token}@")
        else:
            auth_source_url = source_url

        print(f"  Cloning {source_owner}/{source_name} as mirror...")

        # Clone with mirror
        subprocess.run(
            ["git", "clone", "--mirror", auth_source_url, str(mirror_dir)],
            check=True,
            capture_output=True,
            text=True
        )

        print(f"  Pushing mirror to destination (excluding pull request refs)...")

        # Push all refs except pull request refs
        # GitHub doesn't allow pushing refs/pull/* as they are read-only
        subprocess.run(
            ["git", "-C", str(mirror_dir), "push", "--mirror", "--prune", dest_url],
            check=False,  # Don't fail on PR ref rejections
            capture_output=True,
            text=True
        )

        # Now push only the refs we want (branches and tags)
        # Get all refs
        result = subprocess.run(
            ["git", "-C", str(mirror_dir), "show-ref"],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0 and result.stdout:
            # Filter and push only branches, tags, and heads (exclude refs/pull/*)
            refs_to_push = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split()
                    if len(parts) >= 2:
                        ref = parts[1]
                        # Exclude pull request refs and other special refs
                        if not ref.startswith('refs/pull/'):
                            refs_to_push.append(ref)

            if refs_to_push:
                # Push all valid refs in one command
                push_cmd = ["git", "-C", str(mirror_dir), "push", "--force", dest_url] + refs_to_push
                subprocess.run(
                    push_cmd,
                    check=True,
                    capture_output=True,
                    text=True
                )

        print(f"  ✓ Successfully mirrored {source_owner}/{source_name}")

    except subprocess.CalledProcessError as e:
        raise RepositoryError(
            f"Git operation failed for {source_owner}/{source_name}: "
            f"{e.stderr if e.stderr else e.stdout}"
        )

    finally:
        # Clean up
        if mirror_dir.exists():
            subprocess.run(
                ["rm", "-rf", str(mirror_dir)],
                check=False,
                capture_output=True
            )


def main():
    """Main execution flow"""
    # Required environment variables
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
    if skip_check:
        print("⚠️  Update check DISABLED - will mirror all repos")
    else:
        print("✓ Update check ENABLED - checking ALL branches and tags")
    print("=" * 80)

    try:
        # Fetch repositories from Star List
        starlist_repos = get_starlist_repositories(token, list_name)

        # Load private repositories from GitHub Variables (preferred method)
        private_repos = load_private_repos_from_vars()

        # Fallback: Also check private_repos.json for backwards compatibility
        if not private_repos:
            private_repos_file = Path("private_repos.json")
            if private_repos_file.exists():
                private_repos = load_private_repos(private_repos_file)

        # Merge and deduplicate
        all_repos = merge_repositories(starlist_repos, private_repos)

        if not all_repos:
            print("No repositories to mirror")
            return

        print("=" * 80)

        # Mirror each repository
        failed_repos = []
        skipped_repos = []
        updated_repos = []

        for idx, repo in enumerate(all_repos, 1):
            print(f"\n[{idx}/{len(all_repos)}] Processing {repo['owner']}/{repo['name']}")
            print("-" * 80)

            try:
                # Check if update is needed (unless disabled)
                if not skip_check:
                    needs_update, reason = check_if_update_needed(
                        token=token,
                        org=org,
                        source_owner=repo["owner"],
                        source_name=repo["name"]
                    )

                    if not needs_update:
                        skipped_repos.append(f"{repo['owner']}/{repo['name']}")
                        continue

                # Ensure destination repository exists
                dest_url = ensure_destination_repo(
                    token=token,
                    org=org,
                    source_owner=repo["owner"],
                    source_name=repo["name"],
                    visibility=visibility
                )

                # Mirror the repository
                mirror_repository(
                    source_url=repo["url"],
                    dest_url=dest_url,
                    source_owner=repo["owner"],
                    source_name=repo["name"],
                    token=token
                )

                updated_repos.append(f"{repo['owner']}/{repo['name']}")

            except (RepositoryError, subprocess.CalledProcessError) as e:
                print(f"  ✗ Error: {e}", file=sys.stderr)
                failed_repos.append(f"{repo['owner']}/{repo['name']}")
                continue

        print("\n" + "=" * 80)
        print(f"Mirror operation complete")
        print(f"✓ Updated: {len(updated_repos)}")
        print(f"⊘ Skipped (up-to-date): {len(skipped_repos)}")
        print(f"✗ Failed: {len(failed_repos)}")
        print(f"Total: {len(all_repos)}")

        if skipped_repos and len(skipped_repos) <= 10:
            print(f"\nSkipped repositories (already up-to-date):")
            for repo in skipped_repos:
                print(f"  ⊘ {repo}")
        elif skipped_repos:
            print(f"\nSkipped {len(skipped_repos)} repositories (already up-to-date)")

        if updated_repos and len(updated_repos) <= 10:
            print(f"\nUpdated repositories:")
            for repo in updated_repos:
                print(f"  ✓ {repo}")
        elif updated_repos:
            print(f"\nUpdated {len(updated_repos)} repositories")

        if failed_repos:
            print(f"\nFailed repositories:")
            for repo in failed_repos:
                print(f"  ✗ {repo}")
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
