"""Flask API routes — the REST layer of the second brain.
Clean API designed for easy integration with other projects.
"""
import json
import io
import zipfile
import re
import os
import sqlite3
import urllib.request
from datetime import datetime
from flask import Blueprint, request, jsonify, Response
from app.database import (
    insert_item, update_item, delete_item, get_item,
    list_items, search_text, get_stats,
    generate_api_key, list_api_keys, revoke_api_key, has_api_keys
)
from app.embeddings import embed_and_store, semantic_search, reindex_all

api = Blueprint("api", __name__)


def _parse_item(row):
    """Convert a DB row dict to a JSON-safe dict."""
    return {
        "id": row["id"],
        "item_type": row["item_type"],
        "title": row["title"],
        "content": row["content"],
        "tags": json.loads(row["tags"]),
        "metadata": json.loads(row["metadata"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _strip_quotes(s):
    """Strip surrounding quotes from a YAML value."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s.strip()


# ─── CRUD ───────────────────────────────────────────────────────────────────

@api.route("/items", methods=["POST"])
def create_item():
    """Create a new item (note, code, bookmark, or task)."""
    body = request.get_json(force=True)

    if not body.get("title"):
        return jsonify(error="title is required"), 400
    item_type = body.get("item_type", "note")
    if item_type not in ("note", "code", "bookmark", "task"):
        return jsonify(error="item_type must be note, code, bookmark, or task"), 400

    item_id = insert_item(
        item_type=item_type,
        title=body["title"],
        content=body.get("content", ""),
        tags=body.get("tags", []),
        metadata=body.get("metadata", {}),
    )

    try:
        embed_and_store(item_id)
    except Exception:
        pass

    item = get_item(item_id)
    parsed = _parse_item(item)

    # Emit WebSocket event for real-time collaboration
    try:
        from flask_socketio import emit
        emit("item_created", parsed, namespace="/")
    except Exception:
        pass

    return jsonify(parsed), 201


@api.route("/items", methods=["GET"])
def list_all_items():
    """List all items, optionally filtered by type."""
    item_type = request.args.get("item_type")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)

    items = list_items(item_type=item_type, limit=limit, offset=offset)
    return jsonify([_parse_item(i) for i in items])


@api.route("/items/<int:item_id>", methods=["GET"])
def get_single_item(item_id):
    """Get a single item by ID."""
    item = get_item(item_id)
    if not item:
        return jsonify(error="Item not found"), 404
    return jsonify(_parse_item(item))


@api.route("/items/<int:item_id>", methods=["PUT"])
def update_single_item(item_id):
    """Update an item. Only provided fields are changed."""
    existing = get_item(item_id)
    if not existing:
        return jsonify(error="Item not found"), 404

    body = request.get_json(force=True)
    updates = {}
    for key in ("title", "content", "tags", "metadata", "item_type"):
        if key in body:
            updates[key] = body[key]

    if updates:
        update_item(item_id, **updates)
        if "title" in updates or "content" in updates:
            try:
                embed_and_store(item_id)
            except Exception:
                pass

    item = get_item(item_id)
    parsed = _parse_item(item)

    # Emit WebSocket event for real-time collaboration
    try:
        from flask_socketio import emit
        emit("item_updated", parsed, namespace="/")
    except Exception:
        pass

    return jsonify(parsed)


@api.route("/items/<int:item_id>", methods=["DELETE"])
def delete_single_item(item_id):
    """Delete an item."""
    if not delete_item(item_id):
        return jsonify(error="Item not found"), 404

    # Emit WebSocket event for real-time collaboration
    try:
        from flask_socketio import emit
        emit("item_deleted", {"id": item_id}, namespace="/")
    except Exception:
        pass

    return jsonify(ok=True, deleted_id=item_id)


# ─── SEARCH ─────────────────────────────────────────────────────────────────

@api.route("/search", methods=["POST"])
def search():
    """
    Search the brain.
    - mode='text': full-text search using FTS5
    - mode='semantic': vector similarity search
    - mode='hybrid': combines both
    """
    body = request.get_json(force=True)
    query = body.get("query", "").strip()
    mode = body.get("mode", "text")
    item_type = body.get("item_type")
    limit = min(int(body.get("limit", 20)), 100)

    if not query:
        return jsonify(error="query is required"), 400

    results = []

    if mode == "text":
        rows = search_text(query, limit=limit)
        for i, row in enumerate(rows):
            if item_type and row["item_type"] != item_type:
                continue
            score = 1.0 - (i / max(len(rows), 1))
            results.append({"item": _parse_item(row), "score": round(score, 4), "match_type": "text"})

    elif mode == "semantic":
        sem_results = semantic_search(query, limit=limit)
        for r in sem_results:
            if item_type and r["item"]["item_type"] != item_type:
                continue
            r["item"] = _parse_item(r["item"])
            r["score"] = round(r["score"], 4)
            results.append(r)

    elif mode == "hybrid":
        text_rows = search_text(query, limit=limit)
        sem_results = semantic_search(query, limit=limit)

        seen = {}
        for i, row in enumerate(text_rows):
            if item_type and row["item_type"] != item_type:
                continue
            score = 1.0 - (i / max(len(text_rows), 1))
            seen[row["id"]] = {"item": _parse_item(row), "score": score * 0.5, "match_type": "text"}

        for r in sem_results:
            if item_type and r["item"]["item_type"] != item_type:
                continue
            item_id = r["item"]["id"]
            parsed = _parse_item(r["item"])
            if item_id in seen:
                seen[item_id]["score"] += r["score"] * 0.5
                seen[item_id]["match_type"] = "hybrid"
            else:
                seen[item_id] = {"item": parsed, "score": r["score"] * 0.5, "match_type": "semantic"}

        results = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    return jsonify(results[:limit])


# ─── IMPORT / EXPORT ────────────────────────────────────────────────────────

@api.route("/export", methods=["GET"])
def export_json():
    """
    Export all items as JSON.
    Query params:
      - item_type: filter by type (optional)
      - include_embeddings: include vector data (default: false)
    """
    item_type = request.args.get("item_type")
    include_embeddings = request.args.get("include_embeddings", "").lower() == "true"

    items = list_items(item_type=item_type, limit=10000, offset=0)
    parsed = [_parse_item(i) for i in items]

    export = {
        "version": "1.0",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "count": len(parsed),
        "items": parsed,
    }

    if include_embeddings:
        from app.database import get_all_embeddings
        embeddings = get_all_embeddings()
        export["embeddings"] = {str(e["item_id"]): e["vector"] for e in embeddings}

    return Response(
        json.dumps(export, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=brain-export-{datetime.utcnow().strftime('%Y%m%d')}.json"}
    )


@api.route("/export/markdown", methods=["GET"])
def export_markdown():
    """
    Export all items as a zip of markdown files.
    Each item becomes: {type}/{title}.md with YAML frontmatter.
    """
    item_type = request.args.get("item_type")
    items = list_items(item_type=item_type, limit=10000, offset=0)
    parsed = [_parse_item(i) for i in items]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write index file
        index_lines = ["# Second Brain Export", "", f"Exported: {datetime.utcnow().isoformat()}Z", f"Items: {len(parsed)}", ""]
        for item in parsed:
            index_lines.append(f"- [{item['item_type']}] {item['title']} (id:{item['id']})")
        zf.writestr("INDEX.md", "\n".join(index_lines))

        # Write each item as markdown
        for item in parsed:
            tags = item["tags"] if isinstance(item["tags"], list) else []
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", item["title"])[:100]
            filename = f"{item['item_type']}/{safe_title}.md"

            tag_str = ", ".join(tags)
            lines = [
                "---",
                f"id: {item['id']}",
                f"type: {item['item_type']}",
                f'title: "{item["title"]}"',
                f"tags: [{tag_str}]",
                f"created: {item['created_at']}",
                f"updated: {item['updated_at']}",
                "---",
                "",
                f"# {item['title']}",
                "",
                item["content"],
            ]
            zf.writestr(filename, "\n".join(lines))

    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=brain-export-{datetime.utcnow().strftime('%Y%m%d')}.zip"}
    )


@api.route("/import", methods=["POST"])
def import_json():
    """
    Bulk import items from JSON.
    Accepts:
      - A JSON array of items (direct)
      - A JSON export object with an "items" key (from /export)
    Query params:
      - reindex: re-embed all after import (default: true)
    """
    body = request.get_json(force=True)
    reindex = request.args.get("reindex", "true") != "false"

    if isinstance(body, list):
        items_to_import = body
    elif isinstance(body, dict) and "items" in body:
        items_to_import = body["items"]
    else:
        return jsonify(error="Expected a JSON array or {items: [...]}"), 400

    imported = 0
    skipped = 0
    errors = []

    for i, item in enumerate(items_to_import):
        try:
            title = item.get("title", "")
            if not title:
                skipped += 1
                continue

            item_type = item.get("item_type", "note")
            if item_type not in ("note", "code", "bookmark", "task"):
                item_type = "note"

            tags = item.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            content = item.get("content", "")
            metadata = item.get("metadata", {})

            insert_item(item_type, title, content, tags, metadata)
            imported += 1
        except Exception as e:
            errors.append({"index": i, "title": item.get("title", "?"), "error": str(e)})

    reindexed = 0
    if reindex and imported > 0:
        try:
            reindexed = reindex_all()
        except Exception:
            pass

    return jsonify(
        ok=True,
        imported=imported,
        skipped=skipped,
        errors=errors,
        reindexed=reindexed,
    )


@api.route("/import/markdown", methods=["POST"])
def import_markdown():
    """
    Import markdown files uploaded as a zip.
    Each .md file is parsed for YAML frontmatter + content.
    Files in subdirectories are categorized by folder name.
    """
    reindex = request.args.get("reindex", "true") != "false"

    if "file" not in request.files:
        return jsonify(error="Upload a zip file as 'file'"), 400

    f = request.files["file"]
    imported = 0
    skipped = 0
    errors = []

    with zipfile.ZipFile(f.stream, "r") as zf:
        for name in zf.namelist():
            basename = name.split("/")[-1]
            if not name.endswith(".md") or name.startswith("__MACOSX") or basename.lower() == "index.md":
                continue

            try:
                raw = zf.read(name).decode("utf-8")
                item_type = "note"

                # Detect type from folder name
                parts = name.split("/")
                if len(parts) > 1:
                    folder = parts[0].lower()
                    if folder in ("note", "code", "bookmark", "task"):
                        item_type = folder

                # Parse YAML frontmatter
                title = None
                tags = []
                content = raw

                fm_match = re.match(r'^---\n(.+?)\n---\n(.*)', raw, re.DOTALL)
                if fm_match:
                    fm_text, body_text = fm_match.groups()
                    content = body_text.strip()

                    for line in fm_text.split("\n"):
                        line = line.strip()
                        if line.startswith("title:"):
                            title = _strip_quotes(line.split(":", 1)[1])
                        elif line.startswith("tags:"):
                            tag_str = line.split(":", 1)[1].strip()
                            tags = [_strip_quotes(t) for t in tag_str.strip("[]").split(",") if t.strip()]
                        elif line.startswith("type:"):
                            t = line.split(":", 1)[1].strip()
                            if t in ("note", "code", "bookmark", "task"):
                                item_type = t

                # Fallback title from filename
                if not title:
                    basename = name.split("/")[-1].replace(".md", "")
                    title = basename.replace("_", " ").replace("-", " ").title()

                if not title:
                    skipped += 1
                    continue

                insert_item(item_type, title, content, tags, {})
                imported += 1

            except Exception as e:
                errors.append({"file": name, "error": str(e)})

    reindexed = 0
    if reindex and imported > 0:
        try:
            reindexed = reindex_all()
        except Exception:
            pass

    return jsonify(
        ok=True,
        imported=imported,
        skipped=skipped,
        errors=errors,
        reindexed=reindexed,
    )


# ─── PORTABLE BRAIN EXPORT/IMPORT ──────────────────────────────────────────

@api.route("/export/brain", methods=["GET"])
def export_brain():
    """
    Export the entire brain as a single portable .md file.
    Contains all items, tags, and metadata in a structured format.
    This is the "master transfer file" — import it on any Second Brain instance.
    """
    items = list_items(limit=100000, offset=0)
    parsed = [_parse_item(i) for i in items]

    # Group by type
    by_type = {}
    for item in parsed:
        t = item["item_type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(item)

    # Collect all unique tags
    all_tags = set()
    for item in parsed:
        for tag in item["tags"]:
            all_tags.add(tag)

    # Build the master markdown
    lines = [
        "---",
        f"title: Second Brain Export",
        f"version: 1.0",
        f"format: brain-master",
        f"exported: {datetime.utcnow().isoformat()}Z",
        f"total_items: {len(parsed)}",
        f"types: {', '.join(sorted(by_type.keys()))}",
        f"tags: {', '.join(sorted(all_tags))}",
        "---",
        "",
        "# 🧠 Second Brain — Master Export",
        "",
        "This file contains the complete state of a Second Brain instance.",
        "Import it into any Second Brain to restore all data.",
        "",
        f"**Exported:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"**Items:** {len(parsed)} total across {len(by_type)} types",
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
        for item in by_type[item_type]:
            safe_title = item["title"].replace("|", "-")
            lines.append(f"  - [{safe_title}](#{safe_title.lower().replace(' ', '-')})")
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

    # Also include items not in the 4 standard types
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

    return Response(
        md_content,
        mimetype="text/markdown",
        headers={
            "Content-Disposition": f"attachment; filename=second-brain-master-{datetime.utcnow().strftime('%Y%m%d')}.md"
        }
    )


@api.route("/import/brain", methods=["POST"])
def import_brain():
    """
    Import from a master brain .md file (produced by /export/brain).
    Upload the .md file as 'file' in the POST body.
    Supports both plain .md and encrypted .enc.md files.
    """
    reindex = request.args.get("reindex", "true") != "false"
    password = request.args.get("password", "")

    if "file" not in request.files:
        return jsonify(error="Upload a .md file as 'file'"), 400

    f = request.files["file"]
    raw_bytes = f.read()

    # Check if file is encrypted
    from app.encryption import is_encrypted, decrypt_data
    if is_encrypted(raw_bytes):
        if not password:
            return jsonify(
                error="File is encrypted. Provide ?password=... to decrypt.",
                encrypted=True
            ), 400
        try:
            raw = decrypt_data(raw_bytes, password)
        except Exception:
            return jsonify(error="Wrong password or corrupted file"), 400
    else:
        raw = raw_bytes.decode("utf-8")

    # Parse YAML frontmatter
    fm_match = re.match(r'^---\n(.+?)\n---\n(.*)', raw, re.DOTALL)
    content = raw
    if fm_match:
        content = fm_match.group(2)

    # Split by ### headings (each is an item)
    sections = re.split(r'\n### ', content)
    imported = 0
    skipped = 0
    errors = []

    for section in sections[1:]:  # Skip the preamble
        try:
            lines = section.strip().split("\n")
            title = lines[0].strip()

            if not title:
                skipped += 1
                continue

            # Parse metadata from the bullet points
            item_type = "note"
            tags = []
            body_lines = []
            in_body = False

            for line in lines[1:]:
                if line.startswith("- **Type:**"):
                    item_type = line.split(":", 1)[1].strip()
                elif line.startswith("- **Tags:**"):
                    tag_str = line.split(":", 1)[1].strip()
                    if tag_str != "none":
                        tags = [t.strip() for t in tag_str.split(",") if t.strip()]
                elif line.startswith("- **Created:**") or line.startswith("- **Updated:**"):
                    pass  # Skip metadata
                elif line == "---":
                    in_body = False
                elif line.strip() == "" and not in_body:
                    continue
                else:
                    in_body = True
                    body_lines.append(line)

            content = "\n".join(body_lines).strip()

            if not title:
                skipped += 1
                continue

            insert_item(item_type, title, content, tags, {})
            imported += 1

        except Exception as e:
            errors.append({"section": title[:50] if 'title' in dir() else "?", "error": str(e)})

    reindexed = 0
    if reindex and imported > 0:
        try:
            reindexed = reindex_all()
        except Exception:
            pass

    return jsonify(
        ok=True,
        imported=imported,
        skipped=skipped,
        errors=errors,
        reindexed=reindexed,
    )


@api.route("/export/backup", methods=["GET"])
def export_backup():
    """
    Export the entire SQLite database as a binary backup.
    This is a complete clone — includes items, embeddings, FTS indexes, API keys.
    Import on another system to get an exact copy.
    """
    from app.database import DB_PATH
    import tempfile

    if not os.path.exists(DB_PATH):
        return jsonify(error="Database file not found"), 404

    # Create backup using a temp file (sqlite3.backup works with file paths)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.db')
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        dst.close()
        src.close()

        with open(tmp_path, 'rb') as f:
            data = f.read()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return Response(
        data,
        mimetype="application/x-sqlite3",
        headers={
            "Content-Disposition": f"attachment; filename=second-brain-backup-{datetime.utcnow().strftime('%Y%m%d')}.db"
        }
    )


@api.route("/import/backup", methods=["POST"])
def import_backup():
    """
    Restore from a SQLite backup file.
    This REPLACES the current database entirely.
    """
    from app.database import DB_PATH, init_db
    import shutil

    if "file" not in request.files:
        return jsonify(error="Upload a .db file as 'file'"), 400

    f = request.files["file"]

    # Validate it's a SQLite file
    header = f.read(16)
    f.seek(0)
    if not header.startswith(b"SQLite format 3"):
        return jsonify(error="Not a valid SQLite database file"), 400

    # Save to a temp file, then swap
    tmp_path = DB_PATH + ".tmp"
    f.save(tmp_path)

    # Close current connections by reinitializing
    try:
        src = sqlite3.connect(tmp_path)
        dst = sqlite3.connect(DB_PATH)
        src.backup(dst)
        dst.close()
        src.close()
        os.remove(tmp_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return jsonify(error=f"Backup restore failed: {str(e)}"), 500

    return jsonify(
        ok=True,
        message="Database restored. Restart the server to apply changes.",
        warning="You may need to restart the server for changes to take full effect."
    )


# ─── AUTO-BACKUP ───────────────────────────────────────────────────────────

@api.route("/autobackup", methods=["GET"])
def get_autobackup():
    """Get auto-backup configuration and status."""
    from app.autobackup import get_config
    cfg = get_config()
    return jsonify(
        enabled=cfg.get("enabled", False),
        interval_hours=cfg.get("interval_hours", 6),
        backup_dir=cfg.get("backup_dir", ""),
        max_backups=cfg.get("max_backups", 10),
        encrypt=cfg.get("encrypt", False),
        has_password=cfg.get("password_hash") is not None,
        last_backup=cfg.get("last_backup"),
        last_error=cfg.get("last_error"),
        total_backups=cfg.get("total_backups", 0),
    )


@api.route("/autobackup", methods=["POST"])
def update_autobackup():
    """Update auto-backup configuration."""
    from app.autobackup import update_config
    from app.encryption import hash_password, verify_password
    body = request.get_json(force=True) or {}

    allowed = {"enabled", "interval_hours", "backup_dir", "max_backups", "encrypt"}
    updates = {k: v for k, v in body.items() if k in allowed}

    # Handle password setting
    password = body.get("password")
    if password is not None:
        if password == "":
            # Clear password
            updates["password_hash"] = None
            updates["password_salt"] = None
            updates["encrypt"] = False
        else:
            pw_hash, salt = hash_password(password)
            updates["password_hash"] = pw_hash
            updates["password_salt"] = salt

    cfg = update_config(updates)
    return jsonify(ok=True, config=cfg)


@api.route("/autobackup/verify-password", methods=["POST"])
def verify_autobackup_password():
    """Verify the encryption password."""
    from app.autobackup import get_config
    from app.encryption import verify_password
    body = request.get_json(force=True) or {}
    password = body.get("password", "")

    cfg = get_config()
    stored_hash = cfg.get("password_hash")
    stored_salt = cfg.get("password_salt")

    if not stored_hash or not stored_salt:
        return jsonify(error="No password configured"), 400

    if verify_password(password, stored_hash, stored_salt):
        return jsonify(ok=True, valid=True)
    else:
        return jsonify(ok=True, valid=False), 401


@api.route("/autobackup/run", methods=["POST"])
def run_autobackup():
    """Trigger an immediate backup."""
    from app.autobackup import _do_backup, get_config
    body = request.get_json(silent=True) or {}
    password = body.get("password", "")

    # If encryption is enabled, pass the password to _do_backup
    cfg = get_config()
    if cfg.get("encrypt") and password:
        from app.autobackup import _load_config, _save_config
        cfg["_password"] = password
        _save_config(cfg)

    success, msg = _do_backup()
    return jsonify(ok=success, message=msg if success else None, error=msg if not success else None)


# ─── GITHUB SYNC ───────────────────────────────────────────────────────────

@api.route("/github-sync", methods=["GET"])
def get_github_sync():
    """Get GitHub sync configuration and status."""
    from app.github_sync import get_config
    cfg = get_config()
    return jsonify(
        enabled=cfg.get("enabled", False),
        repo=cfg.get("repo", ""),
        branch=cfg.get("branch", "main"),
        path=cfg.get("path", "brain.md"),
        auto_sync=cfg.get("auto_sync", False),
        token_masked=cfg.get("token_masked", ""),
        last_sync=cfg.get("last_sync"),
        last_error=cfg.get("last_error"),
        total_syncs=cfg.get("total_syncs", 0),
    )


@api.route("/github-sync", methods=["POST"])
def update_github_sync():
    """Update GitHub sync configuration."""
    from app.github_sync import update_config
    body = request.get_json(force=True) or {}

    allowed = {"enabled", "repo", "token", "branch", "path", "auto_sync"}
    updates = {k: v for k, v in body.items() if k in allowed}

    cfg = update_config(updates)
    return jsonify(ok=True, config=cfg)


@api.route("/github-sync/test", methods=["POST"])
def test_github_sync():
    """Test GitHub connection and repo access."""
    from app.github_sync import test_connection
    body = request.get_json(force=True) or {}

    token = body.get("token")
    repo = body.get("repo")

    if not token or not repo:
        return jsonify(error="Provide 'token' and 'repo'"), 400

    ok, info = test_connection(token, repo)
    if ok:
        return jsonify(ok=True, info=info)
    else:
        return jsonify(ok=False, error=info), 400


@api.route("/github-sync/run", methods=["POST"])
def run_github_sync():
    """Trigger a manual GitHub sync."""
    from app.github_sync import sync_to_github
    body = request.get_json(silent=True) or {}

    # Get the brain content
    from app.autobackup import _load_config, _do_backup
    from app.routes import _parse_item
    from app.database import list_items

    # Generate fresh brain content
    items = list_items(limit=100000, offset=0)
    parsed = [_parse_item(i) for i in items]

    # Group by type
    by_type = {}
    for item in parsed:
        t = item["item_type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(item)

    all_tags = set()
    for item in parsed:
        for tag in item["tags"]:
            all_tags.add(tag)

    # Build markdown
    lines = [
        "---",
        f"title: Second Brain Export",
        f"version: 1.0",
        f"format: brain-master",
        f"exported: {datetime.utcnow().isoformat()}Z",
        f"total_items: {len(parsed)}",
        f"types: {', '.join(sorted(by_type.keys()))}",
        f"tags: {', '.join(sorted(all_tags))}",
        "---",
        "",
        "# 🧠 Second Brain — GitHub Sync",
        "",
        f"**Synced:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"**Items:** {len(parsed)} total across {len(by_type)} types",
        "",
        "---",
        "",
    ]

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

    for item_type in by_type:
        if item_type not in ("note", "code", "bookmark", "task"):
            lines.append(f"## {item_type.title()}s")
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

    success, info = sync_to_github(md_content)
    if success:
        return jsonify(ok=True, message=info["message"], commit_url=info.get("commit_url"), sha=info.get("sha"))
    else:
        return jsonify(ok=False, error=info), 500


# ─── API KEY MANAGEMENT ────────────────────────────────────────────────────

@api.route("/setup", methods=["POST"])
def setup():
    """
    First-time setup: generate an API key.
    Only works when no keys exist yet. After that, use /auth/keys to manage.
    """
    if has_api_keys():
        return jsonify(
            error="API keys already exist. Use /auth/keys to manage.",
            docs="/api/v1/auth/keys"
        ), 403

    body = request.get_json(silent=True) or {}
    name = body.get("name", "barq")

    key = generate_api_key(name)
    return jsonify(
        ok=True,
        message="First API key generated. Store it securely — it won't be shown again.",
        api_key=key,
        name=name,
        usage={
            "header": "X-API-Key: " + key,
            "query": f"?api_key={key}",
            "curl": f'curl -H "X-API-Key: {key}" http://localhost:8000/api/v1/stats'
        }
    ), 201


@api.route("/auth/keys", methods=["GET"])
def list_keys():
    """List all API keys (with masked previews)."""
    keys = list_api_keys()
    return jsonify(keys=keys, count=len(keys))


@api.route("/auth/keys", methods=["POST"])
def create_key():
    """Create a new API key."""
    body = request.get_json(force=True)
    name = body.get("name", "unnamed")
    key = generate_api_key(name)
    return jsonify(
        ok=True,
        api_key=key,
        name=name,
        message="Store this key securely — it won't be shown again."
    ), 201


@api.route("/auth/keys/<int:key_id>", methods=["DELETE"])
def revoke_key(key_id):
    """Revoke an API key."""
    if not revoke_api_key(key_id):
        return jsonify(error="Key not found"), 404
    return jsonify(ok=True, revoked_id=key_id)


@api.route("/auth/status", methods=["GET"])
def auth_status():
    """Check auth status — are keys set up?"""
    return jsonify(
        auth_enabled=has_api_keys(),
        key_count=len(list_api_keys())
    )


# ─── UTILITIES ──────────────────────────────────────────────────────────────

@api.route("/stats", methods=["GET"])
def get_statistics():
    """Get brain stats — item counts, embedding coverage."""
    return jsonify(get_stats())


@api.route("/reindex", methods=["POST"])
def trigger_reindex():
    """Re-embed all items. Use after bulk imports or model changes."""
    count = reindex_all()
    return jsonify(ok=True, reindexed=count)


@api.route("/system", methods=["GET"])
def system_health():
    """
    System health endpoint — server status, DB size, embedding model, memory.
    """
    import os
    import sqlite3
    import time
    import platform

    # DB info
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'brain.db')
    db_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    db_size_mb = round(db_size_bytes / (1024 * 1024), 2)
    db_last_modified = time.ctime(os.path.getmtime(db_path)) if os.path.exists(db_path) else 'unknown'

    # Table stats
    table_stats = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for table in ['items', 'embeddings', 'api_keys']:
                try:
                    count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    table_stats[table] = count
                except Exception:
                    table_stats[table] = 0
            # DB page count and page size
            page_count = conn.execute('PRAGMA page_count').fetchone()[0]
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            table_stats['db_pages'] = page_count
            table_stats['db_page_size'] = page_size
            table_stats['db_internal_size_mb'] = round((page_count * page_size) / (1024 * 1024), 2)
    except Exception:
        pass

    # Embedding model info
    embedding_info = {}
    try:
        import sentence_transformers
        embedding_info['model_library'] = 'sentence-transformers'
        embedding_info['model_version'] = getattr(sentence_transformers, '__version__', 'unknown')
    except ImportError:
        embedding_info['model_library'] = 'sqlite-vec (fallback)'
        embedding_info['model_version'] = 'N/A'

    # Try to detect model name from embeddings
    try:
        with sqlite3.connect(db_path) as conn:
            # Check embeddings table schema
            schema = conn.execute("PRAGMA table_info(embeddings)").fetchall()
            embedding_info['vector_dimensions'] = 384  # default for all-MiniLM-L6-v2
            embedding_info['model_name'] = 'all-MiniLM-L6-v2'
            embedding_info['similarity'] = 'cosine'
    except Exception:
        pass

    # Server info
    server_info = {
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'architecture': platform.machine(),
        'hostname': platform.node(),
    }

    # Memory usage
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS ru_maxrss is in bytes, Linux is in KB
        import sys
        if sys.platform == 'darwin':
            server_info['memory_rss_mb'] = round(usage.ru_maxrss / (1024 * 1024), 1)
        else:
            server_info['memory_rss_mb'] = round(usage.ru_maxrss / 1024, 1)
    except Exception:
        server_info['memory_rss_mb'] = 'unknown'

    # Process uptime
    try:
        pid = os.getpid()
        proc_stat = f'/proc/{pid}/stat' if os.path.exists(f'/proc/{pid}/stat') else None
        if proc_stat:
            with open(proc_stat) as f:
                st = f.read().split()
                start_ticks = int(st[21])
                import sysconf
                clocks = sysconf.sysconf('SC_CLK_TCK')
                uptime_sec = time.time() - (start_ticks / clocks)
                server_info['uptime_seconds'] = int(uptime_sec)
        else:
            server_info['uptime_seconds'] = 'unknown'
    except Exception:
        server_info['uptime_seconds'] = 'unknown'

    # FTS status
    fts_enabled = True
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('SELECT * FROM items_fts LIMIT 1')
    except Exception:
        fts_enabled = False

    # API keys count
    api_key_count = 0
    try:
        api_key_count = table_stats.get('api_keys', 0)
    except Exception:
        pass

    # Calculate embedding coverage
    items_count = table_stats.get('items', 0)
    embeddings_count = table_stats.get('embeddings', 0)
    embedding_coverage = round((embeddings_count / max(items_count, 1)) * 100, 1)

    return jsonify(
        status='ok',
        server=server_info,
        database={
            'size_mb': db_size_mb,
            'internal_size_mb': table_stats.get('db_internal_size_mb', 0),
            'last_modified': db_last_modified,
            'tables': table_stats,
            'fts_enabled': fts_enabled,
        },
        embeddings={
            **embedding_info,
            'count': embeddings_count,
            'coverage_percent': embedding_coverage,
        },
        api={
            'key_count': api_key_count,
            'total_items': items_count,
        },
    )


# ─── REPOS API (for coding agents) ──────────────────────────────────────────

@api.route("/repos", methods=["GET"])
def list_repos():
    """
    List all repositories with skills, install commands, and GitHub URLs.
    Designed for coding agents to discover and use repos.
    Query params: ?skill=python&search=crawl
    """
    items = list_items(limit=5000)
    skill_filter = request.args.get("skill", "").lower()
    search = request.args.get("search", "").lower()

    repos = []
    for item in items:
        tags = json.loads(item["tags"]) if isinstance(item["tags"], str) else item["tags"]
        if "github" not in tags:
            continue
        content = item["content"]
        # Parse structured content
        repo_data = {
            "id": item["id"],
            "name": item["title"],
            "github_url": f"https://github.com/{item['title']}",
            "tags": tags,
            "content_preview": content[:300],
        }
        # Extract skills from content
        if "Skills & Capabilities:" in content:
            skills_line = content.split("Skills & Capabilities:")[1].split("\n")[0].strip()
            repo_data["skills"] = [s.strip() for s in skills_line.split(", ") if s.strip()]
        else:
            repo_data["skills"] = [t for t in tags if t not in ("github", "graphify", "open-source", "small", "medium", "large")]
        # Extract install command
        if "Install / Clone:" in content:
            install_lines = content.split("Install / Clone:")[1].split("\n\n")[0].strip().split("\n")
            repo_data["install"] = install_lines[0].strip() if install_lines else ""
        else:
            repo_data["install"] = f"git clone https://github.com/{item['title']}.git"
        # Extract use case
        if "Description:" in content:
            repo_data["use_case"] = content.split("Description:")[1].split("\n")[0].strip()
        else:
            repo_data["use_case"] = ""

        # Apply filters
        if skill_filter and not any(skill_filter in s.lower() for s in repo_data.get("skills", [])):
            continue
        if search and search not in item["title"].lower() and search not in repo_data.get("use_case", "").lower():
            continue

        repos.append(repo_data)

    return jsonify(repos=repos, total=len(repos))


@api.route("/repos/<int:repo_id>", methods=["GET"])
def get_repo(repo_id):
    """Get full details of a specific repo for agent use."""
    item = get_item(repo_id)
    if not item:
        return jsonify(error="Repo not found"), 404
    tags = json.loads(item["tags"]) if isinstance(item["tags"], str) else item["tags"]
    if "github" not in tags:
        return jsonify(error="Not a repository item"), 400
    return jsonify({
        "id": item["id"],
        "name": item["title"],
        "github_url": f"https://github.com/{item['title']}",
        "content": item["content"],
        "tags": tags,
        "metadata": json.loads(item["metadata"]) if isinstance(item["metadata"], str) else item["metadata"],
    })


# ─── CHAT ───────────────────────────────────────────────────────────────────

@api.route("/chat", methods=["POST"])
def chat():
    """
    Chat with Gemini, grounded in your Second Brain memory.
    Searches the brain for relevant context, then sends to Gemini.
    """
    body = request.get_json(force=True)
    message = body.get("message", "").strip()
    history = body.get("history", [])  # optional: [{"role": "user"|"model", "parts": ["..."]}]

    if not message:
        return jsonify(error="message is required"), 400

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return jsonify(
            error="GEMINI_API_KEY not set. Export it: export GEMINI_API_KEY=your_key",
            setup_hint="Get a free key at https://aistudio.google.com/apikey"
        ), 500

    # 1. Search brain for relevant context
    context_parts = []
    try:
        results = semantic_search(message, limit=5)
        for r in results:
            item = r["item"]
            score = r["score"]
            if score > 0.15:
                context_parts.append(
                    f"[{item['item_type'].upper()}] {item['title']}\n"
                    f"Tags: {', '.join(json.loads(item['tags']) if isinstance(item['tags'], str) else item['tags'])}\n"
                    f"{item['content'][:800]}"
                )
    except Exception:
        pass

    # 2. Build the prompt
    system_prompt = """You are the user's Second Brain AI assistant. You have access to their stored
memories, notes, code snippets, bookmarks, and tasks. Use the provided context
to give accurate, helpful answers. If the context is relevant, reference it.
If not relevant, answer generally. Be concise and actionable.

When discussing BARQ project details, be specific and reference stored knowledge."""

    if context_parts:
        brain_context = "\n\n--- RELEVANT MEMORIES FROM YOUR SECOND BRAIN ---\n\n" + "\n\n".join(context_parts) + "\n\n--- END MEMORIES ---\n"
    else:
        brain_context = "\n\n(No relevant memories found in your Second Brain for this query.)\n"

    # 3. Build Gemini API request
    contents = []

    # Add conversation history
    for msg in (history or [])[-10:]:  # last 10 messages
        role = msg.get("role", "user")
        parts = msg.get("parts", [msg.get("text", "")])
        if isinstance(parts, str):
            parts = [parts]
        contents.append({"role": role, "parts": [{"text": p} for p in parts]})

    # Add current message with brain context
    user_text = brain_context + "\n\nUSER QUESTION: " + message
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    payload = json.dumps({
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
        }
    }).encode("utf-8")

    # 4. Call Gemini API
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={gemini_key}"

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return jsonify(error=f"Gemini API error: {e.code}", details=error_body), 502
    except Exception as e:
        return jsonify(error=f"Failed to reach Gemini: {str(e)}"), 502

    # 5. Extract response
    try:
        candidate = result["candidates"][0]
        reply = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return jsonify(error="Empty response from Gemini", raw=result), 502

    # 6. Return with context info
    sources = []
    for r in (results if 'results' in dir() else []):
        if r["score"] > 0.15:
            sources.append({
                "id": r["item"]["id"],
                "title": r["item"]["title"],
                "type": r["item"]["item_type"],
                "score": round(r["score"], 4)
            })

    return jsonify({
        "reply": reply,
        "sources": sources,
        "context_used": len(context_parts)
    })


# ─── GRAPHIFY GRAPH ─────────────────────────────────────────────────────────

@api.route("/graph", methods=["GET"])
def graphify_graph():
    """
    Generate an interactive vis.js knowledge graph.
    Optimized for 100+ items: filtered edges, tag-based communities.
    """
    import networkx as nx
    from collections import defaultdict

    items = list_items(limit=5000)
    if not items:
        return jsonify(nodes=[], edges=[], communities={}, community_labels={}, stats={"total_nodes":0,"total_edges":0,"total_communities":0})

    G = nx.Graph()
    tag_to_items = defaultdict(list)
    item_tags = {}

    # 1. Add item nodes
    for item in items:
        iid = f"item_{item['id']}"
        tags = json.loads(item["tags"]) if isinstance(item["tags"], str) else item["tags"]
        item_tags[iid] = set(tags)
        for t in tags:
            tag_to_items[t].append(iid)
        G.add_node(iid, id=iid, label=item["title"][:40], title=item["title"],
                   node_type="item", item_type=item["item_type"],
                   content_preview=item["content"][:200].replace("\n", " "),
                   tags=tags, size=max(8, min(25, len(item["content"]) / 200 + 6)))

    # 2. Item-item edges: require 2+ shared tags, limit per node
    edge_candidates = []
    for i, a in enumerate(items):
        a_id = f"item_{a['id']}"
        for b in items[i+1:]:
            b_id = f"item_{b['id']}"
            shared = item_tags[a_id] & item_tags[b_id]
            if len(shared) >= 2:
                edge_candidates.append((a_id, b_id, len(shared), list(shared)))
    edge_candidates.sort(key=lambda x: -x[2])
    node_edge_count = defaultdict(int)
    MAX_EDGES_PER_NODE = 8
    for a_id, b_id, weight, shared in edge_candidates:
        if node_edge_count[a_id] < MAX_EDGES_PER_NODE and node_edge_count[b_id] < MAX_EDGES_PER_NODE:
            G.add_edge(a_id, b_id, weight=weight * 0.5, shared_tags=shared, confidence="EXTRACTED")
            node_edge_count[a_id] += 1
            node_edge_count[b_id] += 1

    # 3. Tag nodes: only tags with 2+ items, connect to their items
    TAG_THRESHOLD = 2
    for tag, iids in tag_to_items.items():
        if len(iids) < TAG_THRESHOLD:
            continue
        tid = f"tag_{tag}"
        G.add_node(tid, id=tid, label=f"#{tag}", title=f"Tag: {tag} ({len(iids)} items)",
                   node_type="tag", item_type="tag", size=max(5, min(18, len(iids) * 1.5 + 3)))
        for iid in iids:
            G.add_edge(tid, iid, weight=0.2, confidence="INFERRED")

    # 4. Community detection on item-item subgraph (exclude tag nodes)
    item_subgraph = G.subgraph([n for n in G.nodes() if n.startswith("item_")]).copy()
    try:
        from networkx.algorithms.community import greedy_modularity_communities
        communities_list = list(greedy_modularity_communities(item_subgraph))
        communities = {i: list(c) for i, c in enumerate(communities_list)}
    except Exception:
        communities = {0: list(item_subgraph.nodes())}

    # Assign tag nodes to the community of their most-connected item
    for tid in [n for n in G.nodes() if n.startswith("tag_")]:
        neighbors = list(G.neighbors(tid))
        best_comm = 0
        best_count = 0
        for comm_id, members in communities.items():
            count = sum(1 for n in neighbors if n in members)
            if count > best_count:
                best_count = count
                best_comm = comm_id
        communities.setdefault(best_comm, []).append(tid)

    # 5. Compute node sizes by degree
    degrees = dict(G.degree())
    for nid, data in G.nodes(data=True):
        deg = degrees.get(nid, 1)
        base = data.get("size", 8)
        data["size"] = max(base, min(30, 4 + deg * 1.5))

    # 6. Convert to frontend format
    frontend_nodes = []
    for nid, data in G.nodes(data=True):
        frontend_nodes.append({
            "id": nid, "label": data.get("label", nid), "title": data.get("title", ""),
            "node_type": data.get("node_type", "item"), "item_type": data.get("item_type", ""),
            "content_preview": data.get("content_preview", ""), "tags": data.get("tags", []),
            "size": data.get("size", 10),
            "group": next((k for k, v in communities.items() if nid in v), 0),
        })

    frontend_edges = [{"from": u, "to": v, "weight": d.get("weight", 1),
                       "confidence": d.get("confidence", "EXTRACTED"),
                       "shared_tags": d.get("shared_tags", [])}
                      for u, v, d in G.edges(data=True)]

    # 7. Community labels by dominant tag
    community_labels = {}
    for cid, nids in communities.items():
        tag_counts = defaultdict(int)
        type_counts = defaultdict(int)
        for nid in nids:
            nd = G.nodes.get(nid, {})
            if nd.get("node_type") == "tag":
                tag_counts[nd.get("label", "").lstrip("#")] += 3
            else:
                type_counts[nd.get("item_type", "item")] += 1
        if tag_counts:
            top_tag = max(tag_counts, key=tag_counts.get)
            community_labels[cid] = f"#{top_tag}"
        elif type_counts:
            top_type = max(type_counts, key=type_counts.get)
            community_labels[cid] = f"{top_type.title()}s"
        else:
            community_labels[cid] = f"Cluster {cid}"

    # 8. Build sections with headings + subheadings
    SECTION_RULES = [
        {"heading": "Your Projects", "sub": "Personal projects & portfolio", "match_tags": ["projects", "portfolio", "barq", "ludo", "ai-resume", "whiteboard"], "color": "#7b93c4"},
        {"heading": "Career & Job Search", "sub": "CV, job tools, career strategy", "match_tags": ["cv", "career-ops", "ai-job-search", "resume", "manifesto", "ethics", "job-providers", "evaluators", "career"], "color": "#6ba88a"},
        {"heading": "BARQ AI Assistant", "sub": "Voice-controlled desktop AI", "match_tags": ["barq", "voice", "desktop", "electron"], "color": "#fb923c"},
        {"heading": "Python & AI Repos", "sub": "Python, ML, AI tools from Graphify", "match_tags": ["python"], "color": "#a78bfa"},
        {"heading": "TypeScript & Web Repos", "sub": "TypeScript, JS, web frameworks", "match_tags": ["typescript", "javascript"], "color": "#c49a6b"},
        {"heading": "Rust & Systems Repos", "sub": "Rust, C++, systems programming", "match_tags": ["rust", "c++", "c", "zig"], "color": "#b87a7a"},
        {"heading": "Go & Cloud Repos", "sub": "Go, cloud-native, infrastructure", "match_tags": ["go", "lua", "ruby"], "color": "#86efac"},
        {"heading": "Kotlin & Mobile Repos", "sub": "Kotlin, Android, Swift, Dart", "match_tags": ["kotlin", "swift", "dart", "java"], "color": "#f0abfc"},
        {"heading": "Dev Tools & Editors", "sub": "CLI, Neovim, utilities", "match_tags": ["neovim", "fzf", "shell", "cli", "magisk", "developer-tools", "container", "vpn", "root", "android"], "color": "#67e8f9"},
        {"heading": "Media & Content", "sub": "Music, video, reading, IPTV", "match_tags": ["media", "music", "video", "ebook", "iptv", "youtube", "blog"], "color": "#fcd34d"},
    ]

    sections = []
    assigned = set()
    for rule in SECTION_RULES:
        section_nodes = []
        rule_tags = set(rule["match_tags"])
        for nid, data in G.nodes(data=True):
            if nid in assigned or data.get("node_type") == "tag":
                continue
            tags = set(data.get("tags", []))
            # Require primary tag (first tag) OR 2+ matching tags
            tag_list = data.get("tags", [])
            primary_match = len(tag_list) > 0 and tag_list[0] in rule_tags
            multi_match = len(tags & rule_tags) >= 2
            title_match = any(t in data.get("title", "").lower() for t in rule["match_tags"])
            if primary_match or multi_match or title_match:
                section_nodes.append(nid)
                assigned.add(nid)
        if section_nodes:
            sections.append({
                "heading": rule["heading"],
                "subheading": rule["sub"],
                "color": rule["color"],
                "node_ids": section_nodes,
                "count": len(section_nodes),
            })

    # Add unassigned items to "Other"
    other_nodes = [nid for nid, d in G.nodes(data=True) if nid not in assigned and d.get("node_type") != "tag"]
    if other_nodes:
        sections.append({
            "heading": "Other", "subheading": "Uncategorized items",
            "color": "#94a3b8", "node_ids": other_nodes, "count": len(other_nodes),
        })

    # Tag all nodes with their section index
    section_map = {}
    for si, sec in enumerate(sections):
        for nid in sec["node_ids"]:
            section_map[nid] = si
    for node in frontend_nodes:
        node["section"] = section_map.get(node["id"], len(sections) - 1)

    return jsonify(nodes=frontend_nodes, edges=frontend_edges, communities=communities,
                   community_labels=community_labels, sections=sections,
                   stats={"total_nodes": len(frontend_nodes), "total_edges": len(frontend_edges),
                          "total_communities": len(communities), "total_sections": len(sections)})
