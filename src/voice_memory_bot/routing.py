from __future__ import annotations

import json
from dataclasses import dataclass

from .store import clean_name, normalize


@dataclass(frozen=True)
class Decision:
    folder_id: str
    title: str
    status: str
    requested_folder: str | None = None


def fallback_title(transcript):
    return " ".join(transcript.split())[:80] or "Голосовая заметка"


def decide(transcript, folders, current_folder, inbox, result) -> Decision:
    title = fallback_title(transcript)
    if result is None:
        # Unavailable classifier cannot establish whether a voice command overrides the manual choice.
        return Decision(inbox, title, "unavailable")
    try:
        title = clean_name(result["title"])
        if type(result["explicit"]) is not bool or type(result["ambiguous"]) is not bool:
            raise ValueError
        if not result["explicit"] and not result["ambiguous"]:
            return Decision(current_folder, title, "manual")
        evidence = result.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 250 or evidence not in transcript:
            raise ValueError
        requested = result.get("requested_folder")
        requested = clean_name(requested) if isinstance(requested, str) else None
        if requested and normalize(requested) not in normalize(evidence):
            raise ValueError
        if result["ambiguous"]:
            return Decision(inbox, title, "review")
        folder_id = result.get("folder_id")
        candidates = [f for f in folders if f["id"] == folder_id]
        if not candidates:
            return Decision(inbox, title, "review", requested)
        # The returned ID must match a supplied folder AND its spoken name/alias.
        folder = candidates[0]
        names = [folder["name"], *json.loads(folder["aliases"])]
        if not requested or normalize(requested) not in {normalize(n) for n in names}:
            return Decision(inbox, title, "review", requested)
        return Decision(folder_id, title, "explicit")
    except (KeyError, ValueError, TypeError):
        return Decision(inbox, title, "review")
