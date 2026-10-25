"""Durable Telegram intake. Failed payloads need inspection, never blind replay."""
from __future__ import annotations

import json


class UpdateQueue:
    CAPACITY = 1000

    def __init__(self, store):
        self.store, self.db = store, store.db

    def counts(self):
        counts = dict.fromkeys(('pending', 'processing', 'failed'), 0)
        counts.update({row['state']: row['count'] for row in self.store.all(
            'SELECT state,count(*) AS count FROM updates GROUP BY state')})
        return counts

    def available(self):
        counts = self.counts()
        return max(0, self.CAPACITY - counts['pending'] - counts['processing'])

    def ingest(self, updates, allowed):
        # Persist the payload and offset together before the next getUpdates acknowledges it.
        with self.db:
            for update in updates:
                message = (update.get('message') or update.get('edited_message')
                           or update.get('callback_query', {}).get('message', {}))
                sender = update.get('callback_query', {}).get('from') or message.get('from', {})
                if sender.get('id') in allowed and message.get('chat', {}).get('type') == 'private':
                    self.db.execute('INSERT OR IGNORE INTO updates(id,payload) VALUES (?,?)',
                                    (update['update_id'], json.dumps(update, ensure_ascii=False)))
                self.db.execute("INSERT INTO meta(key,value) VALUES ('offset',?) "
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                (str(update['update_id'] + 1),))

    def offset(self):
        row = self.store.one("SELECT value FROM meta WHERE key='offset'")
        return int(row['value']) if row else 0

    def claim(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.store.one("SELECT * FROM updates WHERE state='pending' ORDER BY id LIMIT 1")
            if row:
                self.db.execute("UPDATE updates SET state='processing' WHERE id=?", (row['id'],))
                return json.loads(row['payload'])

    def finish(self, identity):
        # Successful payloads should not become a second archive of private messages.
        with self.db:
            self.db.execute('DELETE FROM updates WHERE id=?', (identity,))

    def fail(self, identity):
        with self.db:
            self.db.execute("UPDATE updates SET state='failed' WHERE id=?", (identity,))
