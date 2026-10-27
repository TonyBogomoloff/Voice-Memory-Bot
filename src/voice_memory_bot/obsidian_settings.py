"""Owner defaults and optional per-folder audio preferences for Obsidian."""


class ObsidianSettings:
    def __init__(self, store):
        self.store = store

    def override(self, owner, scope='all'):
        if scope != 'all':
            self.store.folder(owner, scope)
        row = self.store.one('SELECT audio FROM obsidian_settings WHERE owner=? AND scope=?', (owner, scope))
        return bool(row['audio']) if row else None

    def audio(self, owner, scope='all'):
        selected = self.override(owner, scope)
        if selected is None and scope != 'all':
            selected = self.override(owner)
        return True if selected is None else selected

    def set_audio(self, owner, scope, enabled):
        if scope != 'all':
            self.store.folder(owner, scope)
        if enabled is not None and type(enabled) is not bool:
            raise ValueError('invalid_obsidian_audio')
        with self.store.transaction():
            if enabled is None:
                self.store.db.execute('DELETE FROM obsidian_settings WHERE owner=? AND scope=?', (owner, scope))
            else:
                self.store.db.execute('INSERT INTO obsidian_settings(owner,scope,audio) VALUES (?,?,?) '
                                      'ON CONFLICT(owner,scope) DO UPDATE SET audio=excluded.audio',
                                      (owner, scope, int(enabled)))
