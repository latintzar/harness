"""Project a streamed display title from the first assistant commentary.

The title is display metadata, never an authorization or tool instruction. This
uses the current model turn; it does not make a second inference request.
"""
import unicodedata

OPEN = "<rocoto_title>"
CLOSE = "</rocoto_title>"
INSTRUCTION = ("Begin your first commentary with <rocoto_title> followed by a concise, "
    "specific 3–6 word chat title (at most 80 characters), then </rocoto_title>. "
    "Rocoto streams this display title separately from your message. Continue with "
    "your normal commentary after that tag. Use set_chat_title only to revise it later.")


class TitleStream:
    def __init__(self, previous):
        self.previous = previous
        self.buffer = ""
        self.item = None
        self.finished = False
        self.preview = None

    @staticmethod
    def valid(text):
        return bool(text.strip()) and len(text) <= 80 and all(
            not unicodedata.category(char).startswith("C") for char in text)

    def feed(self, event):
        if self.finished:
            return [event]
        if event.get("type") in {"done", "error", "title"}:
            self.finished = True
            rollback = ([{"type": "title", "title": self.previous, "partial": False}]
                        if self.preview and event.get("type") != "title" else [])
            return rollback + [event]
        if not (event.get("type") == "delta" or event.get("type") == "progress"
                and event.get("activity") == "commentary"):
            return [event]
        identity = event.get("item_id", "message")
        if self.item is None:
            self.item = identity
        if identity != self.item:
            return [event]
        self.buffer += event.get("text", "")
        value = self.buffer.lstrip()
        if OPEN.startswith(value):
            return []
        if not value.startswith(OPEN):
            self.finished = True
            return [{**event, "text": self.buffer}]
        value = value[len(OPEN):]
        complete = CLOSE in value
        title, _, remainder = value.partition(CLOSE)
        if not complete:
            # Hold any partial closing delimiter, including a single '<'.
            for length in range(min(len(CLOSE) - 1, len(title)), 0, -1):
                if title.endswith(CLOSE[:length]):
                    title = title[:-length]
                    break
        if len(title) > 80 or any(unicodedata.category(c).startswith("C") for c in title):
            self.finished = True
            rollback = ([{"type": "title", "title": self.previous, "partial": False}]
                        if self.preview else [])
            return rollback + [{**event, "text": self.buffer}]
        output = []
        if self.valid(title) and (title.strip() != self.preview or complete):
            self.preview = title.strip()
            output.append({"type": "title", "title": self.preview, "partial": not complete})
        if complete:
            self.finished = True
            if not self.valid(title):
                return [{**event, "text": self.buffer}]
            if remainder:
                output.append({**event, "text": remainder.lstrip()})
        return output
