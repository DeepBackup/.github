import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

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
    
    # First, get the list ID
    query_get_lists = """
    query {
      viewer {
        lists(first: 100) {
          nodes {
            id
            name
          }
        }
      }
    }
    """
    
    response = requests.post(
        graphql_url,
        json={"query": query_get_lists},
        headers=headers,
        timeout=30
    )
    
    if response.status_code != 200:
        raise GraphQLError(f"GraphQL request failed: {response.status_code} {response.text}")
    
    data = response.json()
    if "errors" in data:
        raise GraphQLError(f"GraphQL errors: {data['errors']}")
    
    lists = data.get("data", {}).get("viewer", {}).get("lists", {}).get("nodes", [])
    
    target_list = None
    for lst in lists:
        if lst.get("name") == list_name:
            target_list = lst
            break
    
    if not target_list:
        raise GraphQLError(f"Star List '{list_name}' not found. Available lists: {[l['name'] for l in lists]}")
    
    list_id = target_list["id"]
    print(f"Found Star List '{list_name}' with ID: {list_id}")
    
    # Now fetch repositories from this list
    repositories = []
    cursor = None
    
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query_get_repos = f"""
        query {{
          node(id: "{list_id}") {{
            ... on List {{
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
        """
        
        response = requests.post(
            graphql_url,
            json={"query": query_get_repos},
            headers=headers,
            timeout=30
        )
        
        if response.status_code != 200:
            raise GraphQLError(f"GraphQL request failed: {response.status_code} {response.text}")
        
        data = response.json()
        if "errors" in data:
            raise GraphQLError(f"GraphQL errors: {data['errors']}")
        
        items = data.get("data", {}).get("node", {}).get("items", {})
        nodes = items.get("nodes", [])
        
        for node in nodes:
            if node:  # Filter out null nodes
                repositories.append({
                    "owner": node["owner"]["login"],
                    "name": node["name"],
                    "url": node["url"],
                    "is_private": node["isPrivate"]
                })
        
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
        print(f"Repository {org}/{dest_name} already exists")
        clone_url = response.json()["clone_url"]
        return clone_url.replace("https://", f"https://x-access-token:{token}@")
    
    elif response.status_code == 404:
        # Create repository
        print(f"Creating repository {org}/{dest_name}")
        create_url = f"https://api.github.com/orgs/{org}/repos"
        
        payload = {
            "name": dest_name,
            "private": visibility.lower() == "private",
            "description": f"Mirror of {source_owner}/{source_name}",
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
        print(f"Successfully created {org}/{dest_name}")
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
    Mirror a repository using git clone --mirror and git push --mirror
    Idempotent operation
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
        
        print(f"Cloning {source_owner}/{source_name} as mirror...")
        
        # Clone with mirror
        subprocess.run(
            ["git", "clone", "--mirror", auth_source_url, str(mirror_dir)],
            check=True,
            capture_output=True,
            text=True
        )
        
        print(f"Pushing mirror to destination...")
        
        # Push mirror to destination
        subprocess.run(
            ["git", "-C", str(mirror_dir), "push", "--mirror", dest_url],
            check=True,
            capture_output=True,
            text=True
        )
        
        print(f"Successfully mirrored {source_owner}/{source_name}")
    
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
    
    if not token:
        print("Error: MIRROR_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)
    
    if not org:
        print("Error: MIRROR_ORG environment variable not set", file=sys.stderr)
        sys.exit(1)
    
    print(f"Starting mirror operation for Star List '{list_name}' to org '{org}'")
    print(f"Destination visibility: {visibility}")
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
        
        for idx, repo in enumerate(all_repos, 1):
            print(f"\n[{idx}/{len(all_repos)}] Processing {repo['owner']}/{repo['name']}")
            print("-" * 80)
            
            try:
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
            
            except (RepositoryError, subprocess.CalledProcessError) as e:
                print(f"Error: {e}", file=sys.stderr)
                failed_repos.append(f"{repo['owner']}/{repo['name']}")
                continue
        
        print("\n" + "=" * 80)
        print(f"Mirror operation complete")
        print(f"Successful: {len(all_repos) - len(failed_repos)}/{len(all_repos)}")
        
        if failed_repos:
            print(f"\nFailed repositories:")
            for repo in failed_repos:
                print(f"  - {repo}")
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
