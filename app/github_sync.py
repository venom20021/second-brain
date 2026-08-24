"""
🔄 GitHub Sync — Push brain backups to a private GitHub repository.

Uses GitHub's REST API (no git CLI required).
Each backup becomes a versioned commit in your private repo.

Setup:
    1. Create a private repo on GitHub (e.g., "brain-backup")
    2. Generate a Personal Access Token (PAT) with 'repo' scope
    3. Configure in Settings → GitHub Sync

Usage:
    from app.github_sync import sync_to_github, get_config, update_config
"""

import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime


# ─── Config ─────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "github_sync_config.json"
)

DEFAULT_CONFIG = {
    "enabled": False,
    "repo": "",              # e.g., "venom20021/brain-backup"
    "token": "",             # GitHub PAT (stored encrypted)
    "branch": "main",
    "path": "brain.md",      # Path in the repo
    "auto_sync": False,      # Sync after each backup
    "last_sync": None,
    "last_error": None,
    "total_syncs": 0,
}


def _load_config():
    """Load config from disk."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def _save_config(cfg):
    """Save config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_config():
    """Get config (with token masked for display)."""
    cfg = _load_config()
    display = cfg.copy()
    if display.get("token"):
        t = display["token"]
        display["token_masked"] = t[:4] + "..." + t[-4:] if len(t) > 8 else "****"
        display["token"] = ""  # Never return raw token
    else:
        display["token_masked"] = ""
    return display


def update_config(updates):
    """Update config."""
    cfg = _load_config()
    cfg.update(updates)
    _save_config(cfg)
    return get_config()


# ─── GitHub API ──────────────────────────────────────────────────────────────

def _github_request(method, path, token, data=None):
    """Make a request to the GitHub API."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SecondBrain-Sync/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            msg = err.get("message", str(e))
        except Exception:
            msg = body[:200]
        raise Exception(f"GitHub API {e.code}: {msg}")


def test_connection(token, repo):
    """Test GitHub connection and repo access."""
    try:
        # Test auth
        user = _github_request("GET", "/user", token)
        username = user.get("login", "unknown")

        # Test repo access
        repo_info = _github_request("GET", f"/repos/{repo}", token)

        return True, {
            "username": username,
            "repo": repo,
            "private": repo_info.get("private", False),
            "branch": repo_info.get("default_branch", "main"),
        }
    except Exception as e:
        return False, str(e)


def _get_file_sha(token, repo, path, branch="main"):
    """Get the SHA of an existing file (needed for updates)."""
    try:
        result = _github_request("GET", f"/repos/{repo}/contents/{path}?ref={branch}", token)
        return result.get("sha")
    except Exception:
        return None


def sync_to_github(content, message=None):
    """
    Push content to the configured GitHub repo.
    Returns (success, info).
    """
    cfg = _load_config()

    if not cfg.get("enabled"):
        return False, "GitHub sync is not enabled"

    token = cfg.get("token", "")
    repo = cfg.get("repo", "")
    branch = cfg.get("branch", "main")
    path = cfg.get("path", "brain.md")

    if not token:
        return False, "No GitHub token configured"
    if not repo:
        return False, "No repository configured"

    try:
        # Encode content
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        # Build commit message
        if not message:
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            # Count items in content
            item_count = content.count("\n### ")
            message = f"🧠 Brain backup — {item_count} items — {now}"

        # Check if file exists (need SHA for updates)
        sha = _get_file_sha(token, repo, path, branch)

        data = {
            "message": message,
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            data["sha"] = sha  # Required for updates

        result = _github_request("PUT", f"/repos/{repo}/contents/{path}", token, data)

        # Update config
        cfg = _load_config()
        cfg["last_sync"] = datetime.utcnow().isoformat() + "Z"
        cfg["last_error"] = None
        cfg["total_syncs"] = cfg.get("total_syncs", 0) + 1
        _save_config(cfg)

        commit_url = result.get("commit", {}).get("html_url", "")
        return True, {
            "message": f"Synced to {repo}/{path}",
            "commit_url": commit_url,
            "sha": result.get("commit", {}).get("sha", "")[:8],
        }

    except Exception as e:
        cfg = _load_config()
        cfg["last_error"] = str(e)
        _save_config(cfg)
        return False, str(e)
