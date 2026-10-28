"""Resolve complete branches from durable links, never from folder or time proximity."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class NoteBranch:
    notes: list[dict]
    conversations: set[str]

    def detach(self, store, owner):
        for conversation in self.conversations:
            store.db.execute("DELETE FROM ai_active WHERE owner=? AND conversation_id=?", (owner, conversation))

    def relocate(self, store, owner, folder_id):
        # Do not let the old folder's next ordinary chat message join a moved branch.
        for conversation in self.conversations:
            store.db.execute("UPDATE ai_conversations SET folder_id=? WHERE owner=? AND id=?",
                             (folder_id, owner, conversation))
            store.db.execute("DELETE FROM ai_active WHERE owner=? AND conversation_id=? AND folder_id<>?",
                             (owner, conversation, folder_id))


def is_branch_source(store, note):
    """AI requests and outputs have their own deletion scope, separate from the source branch."""
    owner, identity = note['owner'], note['id']
    if note['origin'] == 'ai' or note['ai_generated_from']:
        return False
    if note['reply_message_id'] and store.one(
            "SELECT 1 FROM notes n WHERE n.owner=? AND n.id<>? AND "
            "(n.message_id=? OR n.transcript_message_id=? OR n.result_message_id=? OR EXISTS "
            "(SELECT 1 FROM note_messages m WHERE m.owner=n.owner AND m.note_id=n.id AND m.message_id=?))",
            (owner, identity, *([note['reply_message_id']] * 4))):
        return False
    return not store.one("SELECT 1 FROM ai_requests WHERE owner=? AND note_id=?", (owner, identity))


def deletion_branch(store, owner, identities, *, exclude=None):
    selected = [store.note(owner, identity) for identity in dict.fromkeys(identities)]
    roots = [note['id'] for note in selected if is_branch_source(store, note)]
    branch = note_branch(store, owner, roots, exclude=exclude) if roots else NoteBranch([], set())
    members = {note['id']: note for note in [*branch.notes, *selected] if note['id'] != exclude}
    # A request owns only its direct results, including generated images and legacy
    # results linked through result_note_id. Conversation peers are not its children.
    for note in selected:
        if note['id'] == exclude:
            continue
        for result in store.all(
                "SELECT n.* FROM notes n JOIN ai_requests r ON r.owner=n.owner "
                "WHERE r.owner=? AND r.note_id=? AND n.deleted=0 "
                "AND (n.id=r.result_note_id OR n.ai_generated_from=r.note_id)", (owner, note['id'])):
            if result['id'] != exclude:
                members[result['id']] = result
    return NoteBranch(sorted(members.values(), key=lambda n: (n['created_at'], n['id'])), branch.conversations)


def note_branch(store, owner, identities, *, exclude=None):
    for identity in identities:
        store.note(owner, identity)
    rows = store.all("SELECT id,deleted,message_id,transcript_message_id,result_message_id,"
                     "reply_message_id,ai_generated_from FROM notes WHERE owner=?", (owner,))
    notes = {n['id']: n for n in rows}
    graph, messages = defaultdict(set), defaultdict(set)

    def link(left, right):
        graph[left].add(right)
        graph[right].add(left)

    def link_notes(left, right):
        if left in notes and right in notes:
            link(('note', left), ('note', right))

    for note in rows:
        for field in ('message_id', 'transcript_message_id', 'result_message_id'):
            if note[field] and note[field] > 0:
                messages[note[field]].add(note['id'])
        link_notes(note['id'], note['ai_generated_from'])
    # Deleted Telegram receipts still identify replies after navigation or an earlier move.
    for receipt in store.all("SELECT note_id,message_id FROM note_messages WHERE owner=?", (owner,)):
        if receipt['note_id'] in notes:
            messages[receipt['message_id']].add(receipt['note_id'])
    for note in rows:
        for parent in messages.get(note['reply_message_id'], ()):
            link_notes(note['id'], parent)
    conversations = {r['id'] for r in store.all("SELECT id FROM ai_conversations WHERE owner=?", (owner,))}
    for request in store.all("SELECT note_id,result_note_id,conversation_id FROM ai_requests WHERE owner=?", (owner,)):
        if request['conversation_id'] in conversations:
            for identity in (request['note_id'], request['result_note_id']):
                if identity in notes:
                    link(('note', identity), ('conversation', request['conversation_id']))
    for context in store.all("SELECT note_id,conversation_id FROM ai_note_context WHERE owner=?", (owner,)):
        if context['note_id'] in notes and context['conversation_id'] in conversations:
            link(('note', context['note_id']), ('conversation', context['conversation_id']))
    visited, pending = set(), [('note', identity) for identity in identities]
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(graph[node] - visited)
    # Deleted notes can bridge surviving replies, but must never be restored by a move.
    members = [store.note(owner, identity) for kind, identity in visited
               if kind == 'note' and not notes[identity]['deleted'] and identity != exclude]
    return NoteBranch(sorted(members, key=lambda n: (n['created_at'], n['id'])),
                      {identity for kind, identity in visited if kind == 'conversation'})
