#!/usr/bin/env python3
"""hermes-bridge.py — Translates Hermes Agent session JSONL into Claude Code JSONL
so that peon-pet can detect and react to Hermes events.

Reads:   ~/.hermes/sessions/*.jsonl  (Hermes format)
Writes:  ~/.claude/projects/hermes/<session-uuid>.jsonl  (Claude Code format)

The bridge tails active Hermes sessions and emits Claude Code-compatible records
that peon-pet's JsonlWatcher understands:
  - SessionStart on new file detection
  - type:assistant with content[] containing tool_use or text
  - type:user with content[] containing tool_result
  - type:system with subtype:turn_duration on Stop
"""

import json
import os
import sys
import time
import uuid
import signal
import logging
import argparse
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hermes-bridge] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HERMES_SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects" / "hermes"

# Hermes session filenames: 20260519_143052_a1b2c3d4.jsonl
# We map each to a UUID that peon-pet expects
_filename_to_uuid: dict[str, str] = {}


def session_uuid_for(hermes_filename: str) -> str:
    """Stable UUID per Hermes session file."""
    if hermes_filename not in _filename_to_uuid:
        # Generate a deterministic UUID v5 from the filename
        _filename_to_uuid[hermes_filename] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-session://{hermes_filename}")
        )
    return _filename_to_uuid[hermes_filename]


def hermes_session_cwd(session_path: Path) -> str | None:
    """Try to extract cwd from the first user message in a Hermes session."""
    try:
        with open(session_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Check for cwd markers in user messages
                content = rec.get("content", "")
                if isinstance(content, str) and content:
                    # Hermes CLI sessions often have working directory context
                    # in the system prompt or first user message
                    for marker in ["Current working directory:", "working directory"]:
                        idx = content.find(marker)
                        if idx != -1:
                            # Extract the path after the marker
                            rest = content[idx + len(marker):].strip()
                            path = rest.split("\n")[0].strip()
                            if path and os.path.isabs(path):
                                return path
    except Exception:
        pass
    return None


def translate_record(rec: dict, session_uuid: str) -> list[dict]:
    """Translate a single Hermes JSONL record into one or more Claude Code JSONL records.

    Returns a list of Claude Code compatible records.
    """
    role = rec.get("role", "")
    records = []

    if role == "user":
        content_str = rec.get("content", "")

        # Claude Code user messages: message.content can be a string or array
        # peon-pet's _handleUser checks for tool_result items in content array
        cc_rec = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content_str,
            },
        }
        # Add optional fields peon-pet may use
        if rec.get("timestamp"):
            cc_rec["timestamp"] = rec["timestamp"]
        records.append(cc_rec)

    elif role == "assistant":
        content_str = rec.get("content", "")
        tool_calls = rec.get("tool_calls", [])
        finish_reason = rec.get("finish_reason", "stop")

        # Build Claude Code content array
        content_array = []

        # Add text content if present
        if content_str and content_str.strip():
            content_array.append({"type": "text", "text": content_str})

        # Add tool_use entries from Hermes tool_calls
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "unknown")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}
            content_array.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tool_name,
                "input": tool_args,
            })

        if not content_array:
            content_array.append({"type": "text", "text": ""})

        cc_rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": content_array,
            },
        }
        if rec.get("timestamp"):
            cc_rec["timestamp"] = rec["timestamp"]
        records.append(cc_rec)

        # If this is a stop (no tool calls, finish_reason=stop), emit a system turn_duration
        # This triggers peon-pet's "celebrate" animation
        if finish_reason == "stop" and not tool_calls:
            records.append({
                "type": "system",
                "subtype": "turn_duration",
                "duration_ms": 0,
            })

    elif role == "tool":
        # Hermes tool results need to be wrapped in a user message with tool_result content
        tool_name = rec.get("tool_name", rec.get("name", "unknown"))
        tool_call_id = rec.get("tool_call_id", "")
        content_str = rec.get("content", "")
        is_error = False

        # Detect errors in tool output
        if content_str and isinstance(content_str, str):
            try:
                result = json.loads(content_str)
                if isinstance(result, dict) and result.get("success") is False:
                    is_error = True
            except json.JSONDecodeError:
                pass

        cc_rec = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "content": content_str,
                    "tool_use_id": tool_call_id,
                    "is_error": is_error,
                }],
            },
        }
        if rec.get("timestamp"):
            cc_rec["timestamp"] = rec["timestamp"]
        records.append(cc_rec)

    return records


class HermesSessionState:
    """Tracks translation state for a single Hermes session file."""

    def __init__(self, hermes_path: Path):
        self.hermes_path = hermes_path
        self.session_uuid = session_uuid_for(hermes_path.name)
        self.claude_path = CLAUDE_PROJECTS_DIR / f"{self.session_uuid}.jsonl"
        self.is_wrapped_json = hermes_path.name.startswith("session_") and hermes_path.suffix == ".json"
        self.offset = 0  # bytes read from hermes file (for JSONL)
        self.messages_translated = 0  # count of messages already translated (for wrapped JSON)
        self.wrote_session_start = False
        self.cwd_written = False

    def sync_from_claude(self):
        """If the Claude file already exists, figure out how far we've translated."""
        if self.claude_path.exists():
            if self.is_wrapped_json:
                # Count how many messages we've already translated
                try:
                    with open(self.claude_path) as f:
                        self.messages_translated = sum(1 for _ in f)
                    # Also need to count messages in the hermes file to know offset
                    try:
                        with open(self.hermes_path) as f:
                            data = json.load(f)
                        total = len(data.get("messages", []))
                        # We've translated at least what's in the claude file
                        self.messages_translated = min(self.messages_translated, total)
                        self.wrote_session_start = True
                        self.cwd_written = True
                    except (json.JSONDecodeError, OSError):
                        pass
                except OSError:
                    pass
            else:
                # JSONL: seek to end of hermes file
                try:
                    self.offset = self.hermes_path.stat().st_size
                except OSError:
                    self.offset = 0
                self.wrote_session_start = True
                self.cwd_written = True

    def _parse_messages(self, raw: str) -> list[dict]:
        """Parse Hermes session data — handles both JSONL and wrapped JSON formats."""
        messages = []
        raw = raw.strip()
        if not raw:
            return messages

        # Try JSONL first (one JSON object per line)
        first_char = raw[0] if raw else ''
        if first_char == '{':
            # Could be single-line JSONL or multi-line wrapped JSON
            # Check if the FIRST LINE parses as a complete JSON object with a "role" key
            first_line = raw.split('\n', 1)[0].strip()
            try:
                obj = json.loads(first_line)
                if 'role' in obj:
                    # JSONL format — parse line by line
                    for line in raw.split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if isinstance(rec, dict) and 'role' in rec:
                                messages.append(rec)
                        except json.JSONDecodeError:
                            continue
                    return messages
            except json.JSONDecodeError:
                pass

            # Try parsing as wrapped JSON with "messages" array
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and 'messages' in data:
                    for msg in data['messages']:
                        if isinstance(msg, dict) and 'role' in msg:
                            messages.append(msg)
                    return messages
            except json.JSONDecodeError:
                pass

        return messages

    def translate_new(self):
        """Read new bytes from Hermes file and append translated records to Claude file."""
        if not self.hermes_path.exists():
            return False

        try:
            file_size = self.hermes_path.stat().st_size
        except OSError:
            return False

        if file_size <= self.offset:
            return False

        # Read new content
        try:
            with open(self.hermes_path, "r") as f:
                f.seek(self.offset)
                new_bytes = f.read()
                self.offset = file_size
        except OSError:
            return False

        # Ensure output directory exists
        CLAUDE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

        # Write session start on first activity
        lines_to_write = []
        if not self.wrote_session_start:
            lines_to_write.append(json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Session started"}],
                },
            }))
            self.wrote_session_start = True

        # Extract and write cwd once
        if not self.cwd_written:
            cwd = hermes_session_cwd(self.hermes_path)
            if cwd:
                lines_to_write.append(json.dumps({"cwd": cwd}))
                self.cwd_written = True

        # Get messages based on format
        if self.is_wrapped_json:
            # Wrapped JSON: re-read entire file, only translate new messages
            try:
                with open(self.hermes_path, "r") as f:
                    data = json.load(f)
                all_messages = data.get("messages", [])
                new_messages = all_messages[self.messages_translated:]
                self.messages_translated = len(all_messages)
            except (json.JSONDecodeError, OSError):
                new_messages = []
        else:
            # JSONL: parse from new bytes
            new_messages = self._parse_messages(new_bytes)

        # Translate each message
        for rec in new_messages:
            cc_records = translate_record(rec, self.session_uuid)
            for cc_rec in cc_records:
                lines_to_write.append(json.dumps(cc_rec))

        # Write to Claude file
        if lines_to_write:
            try:
                with open(self.claude_path, "a") as f:
                    for line in lines_to_write:
                        f.write(line + "\n")
            except OSError as e:
                log.error(f"Failed to write {self.claude_path}: {e}")
                return False

        return bool(lines_to_write)


class HermesSessionHandler(FileSystemEventHandler):
    """Watches ~/.hermes/sessions/ for changes and translates them."""

    def __init__(self):
        super().__init__()
        self.sessions: dict[str, HermesSessionState] = {}
        self._lock = __import__("threading").Lock()

    def _get_or_create(self, hermes_path: Path) -> HermesSessionState:
        name = hermes_path.name
        if name not in self.sessions:
            state = HermesSessionState(hermes_path)
            state.sync_from_claude()
            self.sessions[name] = state
        return self.sessions[name]

    def _is_session_file(self, path: str) -> bool:
        return path.endswith(".jsonl") or (
            os.path.basename(path).startswith("session_") and path.endswith(".json")
        )

    def on_modified(self, event):
        if event.is_directory or not self._is_session_file(event.src_path):
            return
        hermes_path = Path(event.src_path)
        with self._lock:
            state = self._get_or_create(hermes_path)
            state.translate_new()

    def on_created(self, event):
        if event.is_directory or not self._is_session_file(event.src_path):
            return
        hermes_path = Path(event.src_path)
        with self._lock:
            state = self._get_or_create(hermes_path)
            state.translate_new()

    def scan_existing(self):
        """Process any existing session files that have new data."""
        if not HERMES_SESSIONS_DIR.exists():
            return
        with self._lock:
            # Hermes uses both .jsonl and session_*.json filenames
            files = sorted(HERMES_SESSIONS_DIR.glob("*.jsonl"))
            files.extend(sorted(HERMES_SESSIONS_DIR.glob("session_*.json")))
            # Deduplicate
            seen = set()
            for f in files:
                if f.name in seen:
                    continue
                seen.add(f.name)
                # Skip files older than 10 minutes on startup (like peon-pet does)
                try:
                    mtime = f.stat().st_mtime
                    if time.time() - mtime > 600:
                        continue
                except OSError:
                    continue
                state = self._get_or_create(f)
                state.translate_new()


def main():
    parser = argparse.ArgumentParser(description="Hermes-to-Claude Code JSONL bridge for peon-pet")
    parser.add_argument("--once", action="store_true", help="Translate existing sessions and exit")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not HERMES_SESSIONS_DIR.exists():
        log.error(f"Hermes sessions directory not found: {HERMES_SESSIONS_DIR}")
        sys.exit(1)

    CLAUDE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Bridge: {HERMES_SESSIONS_DIR} -> {CLAUDE_PROJECTS_DIR}")

    handler = HermesSessionHandler()
    handler.scan_existing()

    if args.once:
        log.info("One-shot mode, done")
        return

    observer = Observer()
    observer.schedule(handler, str(HERMES_SESSIONS_DIR), recursive=False)
    observer.start()
    log.info("Watching for new Hermes session data...")

    # Also poll every 2s as a fallback (watchdog can miss events on some FS)
    def poll_loop():
        while True:
            time.sleep(2)
            handler.scan_existing()

    import threading
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
