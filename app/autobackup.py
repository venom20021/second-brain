"""
🧠 Auto-Backup — Periodically exports brain to a local directory.

The directory can be a cloud-synced folder (Dropbox, iCloud, Google Drive,
OneDrive, etc.) so backups appear on all your devices automatically.

Usage:
    from app.autobackup import start_autobackup, stop_autobackup, get_config, update_config
"""

import os
import json
import time
import threading
import re
import shutil
from datetime import datetime


# ─── Config ─────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "autobackup_config.json"
)

DEFAULT_CONFIG = {
    "enabled": False,
    "interval_hours": 6,           # How often to backup (hours)
    "backup_dir": os.path.expanduser("~/SecondBrain_Backups"),  # Default backup location
    "max_backups": 10,             # Keep last N backups (0 = unlimited)
    "include_embeddings": False,   # Include vector data (large files)
    "encrypt": False,              # Encrypt backup files
    "password_hash": None,         # PBKDF2 hash for password verification
    "password_salt": None,         # Salt for password hash
    "last_backup": None,           # Timestamp of last successful backup
    "last_error": None,            # Last error message
    "total_backups": 0,            # Total backups created
}


def _load_config():
    """Load config from disk, or create default."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            # Merge with defaults (in case new keys were added)
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def _save_config(cfg):
    """Save config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_config():
    """Get current auto-backup config."""
    return _load_config()


def update_config(updates):
    """Update auto-backup config and apply changes."""
    cfg = _load_config()
    cfg.update(updates)
    _save_config(cfg)

    # Restart scheduler if enabled/disabled changed
    if "enabled" in updates:
        if updates["enabled"]:
            start_autobackup()
        else:
            stop_autobackup()

    return cfg


# ─── Export Logic ────────────────────────────────────────────────────────────

def _do_backup():
    """Execute a single backup. Returns (success, message)."""
    cfg = _load_config()
    backup_dir = cfg.get("backup_dir", os.path.expanduser("~/SecondBrain_Backups"))

    try:
        # Ensure backup directory exists
        os.makedirs(backup_dir, exist_ok=True)

        # Import here to avoid circular imports
        from app.database import get_db, list_items

        # Get all items
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, item_type, title, content, tags, metadata, created_at, updated_at "
                "FROM items ORDER BY id"
            ).fetchall()

        items = []
        for row in rows:
            items.append({
                "id": row[0],
                "item_type": row[1],
                "title": row[2],
                "content": row[3],
                "tags": json.loads(row[4]),
                "metadata": json.loads(row[5]),
                "created_at": row[6],
                "updated_at": row[7],
            })

        # Group by type
        by_type = {}
        for item in items:
            t = item["item_type"]
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(item)

        # Collect all unique tags
        all_tags = set()
        for item in items:
            for tag in item["tags"]:
                all_tags.add(tag)

        # Build the master markdown
        lines = [
            "---",
            f"title: Second Brain Auto-Backup",
            f"version: 1.0",
            f"format: brain-master",
            f"exported: {datetime.utcnow().isoformat()}Z",
            f"total_items: {len(items)}",
            f"types: {', '.join(sorted(by_type.keys()))}",
            f"tags: {', '.join(sorted(all_tags))}",
            "---",
            "",
            "# 🧠 Second Brain — Auto-Backup",
            "",
            f"**Backed up:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            f"**Items:** {len(items)} total across {len(by_type)} types",
            f"**Tags:** {len(all_tags)} unique tags",
            "",
            "---",
            "",
        ]

        # Table of contents
        lines.append("## Table of Contents")
        lines.append("")
        for item_type in sorted(by_type.keys()):
            icon = {"note": "📝", "code": "💻", "bookmark": "🔗", "task": "✅"}.get(item_type, "📄")
            lines.append(f"- {icon} **{item_type.title()}s** ({len(by_type[item_type])})")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Write each item section
        for item_type in ["note", "code", "bookmark", "task"]:
            if item_type not in by_type:
                continue
            icon = {"note": "📝", "code": "💻", "bookmark": "🔗", "task": "✅"}.get(item_type, "📄")
            lines.append(f"## {icon} {item_type.title()}s")
            lines.append("")

            for item in by_type[item_type]:
                tags_str = ", ".join(item["tags"]) if item["tags"] else "none"
                lines.append(f"### {item['title']}")
                lines.append("")
                lines.append(f"- **Type:** {item['item_type']}")
                lines.append(f"- **Tags:** {tags_str}")
                lines.append(f"- **Created:** {item['created_at']}")
                lines.append(f"- **Updated:** {item['updated_at']}")
                lines.append("")
                lines.append(item["content"])
                lines.append("")
                lines.append("---")
                lines.append("")

        # Items not in standard types
        for item_type in by_type:
            if item_type not in ("note", "code", "bookmark", "task"):
                icon = "📄"
                lines.append(f"## {icon} {item_type.title()}s")
                lines.append("")
                for item in by_type[item_type]:
                    tags_str = ", ".join(item["tags"]) if item["tags"] else "none"
                    lines.append(f"### {item['title']}")
                    lines.append("")
                    lines.append(f"- **Type:** {item['item_type']}")
                    lines.append(f"- **Tags:** {tags_str}")
                    lines.append(f"- **Created:** {item['created_at']}")
                    lines.append(f"- **Updated:** {item['updated_at']}")
                    lines.append("")
                    lines.append(item["content"])
                    lines.append("")
                    lines.append("---")
                    lines.append("")

        md_content = "\n".join(lines)

        # Write to file
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        encrypt = cfg.get("encrypt", False)

        if encrypt:
            from app.encryption import encrypt_data
            password = cfg.get("_password", "")  # Temp, set during run
            if not password:
                return False, "Encryption enabled but no password provided"
            filename = f"brain-backup-{timestamp}.enc.md"
            filepath = os.path.join(backup_dir, filename)
            encrypted = encrypt_data(md_content, password)
            with open(filepath, "wb") as f:
                f.write(encrypted)
        else:
            filename = f"brain-backup-{timestamp}.md"
            filepath = os.path.join(backup_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

        # Rotate old backups
        max_backups = cfg.get("max_backups", 10)
        if max_backups > 0:
            _rotate_backups(backup_dir, max_backups)

        # Update config
        cfg = _load_config()
        cfg["last_backup"] = datetime.utcnow().isoformat() + "Z"
        cfg["last_error"] = None
        cfg["total_backups"] = cfg.get("total_backups", 0) + 1
        _save_config(cfg)

        # Auto-sync to GitHub if enabled
        github_synced = False
        if cfg.get("auto_sync"):
            try:
                from app.github_sync import sync_to_github
                ok, _ = sync_to_github(md_content)
                github_synced = ok
            except Exception:
                pass

        msg = f"Backed up {len(items)} items to {filename}"
        if github_synced:
            msg += " + synced to GitHub"
        return True, msg

    except Exception as e:
        cfg = _load_config()
        cfg["last_error"] = str(e)
        _save_config(cfg)
        return False, str(e)


def _rotate_backups(backup_dir, max_keep):
    """Keep only the last N backup files."""
    try:
        files = sorted([
            f for f in os.listdir(backup_dir)
            if f.startswith("brain-backup-") and f.endswith(".md")
        ])

        while len(files) > max_keep:
            oldest = files.pop(0)
            os.remove(os.path.join(backup_dir, oldest))
    except Exception:
        pass


# ─── Background Scheduler ───────────────────────────────────────────────────

_scheduler_thread = None
_stop_event = threading.Event()


def _scheduler_loop():
    """Background loop that runs backups on schedule."""
    while not _stop_event.is_set():
        cfg = _load_config()
        if cfg.get("enabled"):
            interval_seconds = cfg.get("interval_hours", 6) * 3600
            # Sleep in small chunks so we can stop quickly
            for _ in range(int(interval_seconds / 5)):
                if _stop_event.is_set():
                    return
                time.sleep(5)

            if _stop_event.is_set():
                return

            # Run backup
            success, msg = _do_backup()
            if success:
                print(f"✅ Auto-backup: {msg}")
            else:
                print(f"❌ Auto-backup failed: {msg}")
        else:
            # Check every 30 seconds if enabled changed
            for _ in range(6):
                if _stop_event.is_set():
                    return
                time.sleep(5)


def start_autobackup():
    """Start the auto-backup scheduler."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return  # Already running

    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
    print("🔄 Auto-backup scheduler started")


def stop_autobackup():
    """Stop the auto-backup scheduler."""
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=10)
    print("⏹️ Auto-backup scheduler stopped")
