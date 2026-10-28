"""Host-scoped selection and captions for an answer's video presentation."""

SPEC = {
    "type": "function", "name": "present_evidence",
    "description": "Choose the videos to show with your answer. Use players for one to three selected clips, or gallery for a larger set. Give every clip a short caption explaining its relevance to this question, grounded in inspected source evidence or the actual Cosmos answer. Preserve uncertainty; identify a search candidate as a candidate. Captions are assistant summaries, not new Cosmos observations.",
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["players", "gallery"]},
            "items": {"type": "array", "minItems": 1, "maxItems": 24,
                      "items": {"type": "object", "additionalProperties": False,
                                "properties": {"clip_id": {"type": "string", "minLength": 1, "maxLength": 200},
                                               "caption": {"type": "string", "minLength": 1, "maxLength": 240}},
                                "required": ["clip_id", "caption"]}},
        }, "required": ["mode", "items"],
    },
}


def build_presentation(arguments, *, index, collection, public_clip):
    if not isinstance(arguments, dict) or set(arguments) != {"mode", "items"}:
        raise ValueError("Choose a presentation and captioned clips.")
    mode, items = arguments["mode"], arguments["items"]
    if not isinstance(mode, str) or mode not in {"players", "gallery"} or not isinstance(items, list) or not 1 <= len(items) <= (3 if mode == "players" else 24):
        raise ValueError("Choose up to three video players or up to 24 gallery examples.")
    selected, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"clip_id", "caption"}:
            raise ValueError("Provide a source clip and its caption.")
        key, caption = item["clip_id"], item["caption"]
        if (not isinstance(key, str) or not 1 <= len(key) <= 200 or key in seen
                or not isinstance(caption, str) or not 1 <= len(caption.strip()) <= 240
                or any(ord(char) < 32 for char in caption)):
            raise ValueError("Choose distinct clips with a short, single-line caption each.")
        clip = index.get_clip(key)
        if clip is None or clip.get("dataset") != collection:
            raise ValueError("Choose footage in this chat's business collection.")
        seen.add(key)
        selected.append({**public_clip(clip), "caption": caption.strip(), "caption_kind": "assistant_summary"})
    return {"mode": mode, "items": selected}
