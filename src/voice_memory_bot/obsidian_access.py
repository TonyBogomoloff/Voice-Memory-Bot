"""Expiring pairing codes and individually revocable, read-only device access."""
import hashlib
import re
import secrets
import time

from .store import new_id


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AccessError(ValueError):
    pass


class ObsidianAccess:
    def __init__(self, store):
        self.store = store

    def issue(self, owner, folder=None):
        if folder:
            self.store.folder(owner, folder)
        code = secrets.token_urlsafe(18)
        now = time.time()
        with self.store.transaction():
            self.store.db.execute('DELETE FROM obsidian_pairing WHERE owner=? OR expires_at<=?', (owner, now))
            self.store.db.execute('INSERT INTO obsidian_pairing VALUES (?,?,?,?)',
                                  (digest(code), owner, folder, now + 600))
        return code

    def pair(self, code, name, allowed_owners):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24}', code):
            raise AccessError('invalid_pairing_code')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60
                or any(ord(c) < 32 for c in name)):
            raise ValueError('invalid_device_name')
        now = time.time()
        with self.store.transaction():
            pairing = self.store.one('SELECT * FROM obsidian_pairing WHERE code_hash=? AND expires_at>?',
                                     (digest(code), now))
            if not pairing or pairing['owner'] not in allowed_owners:
                raise AccessError('invalid_pairing_code')
            if pairing['folder_id']:
                self.store.folder(pairing['owner'], pairing['folder_id'])
            count = self.store.one('SELECT count(*) AS n FROM obsidian_devices WHERE owner=?', (pairing['owner'],))
            if count['n'] >= 10:
                raise ValueError('device_limit')
            device_id, token = new_id(), 'vm_' + secrets.token_urlsafe(32)
            self.store.db.execute('INSERT INTO obsidian_devices VALUES (?,?,?,?,?,?,?)',
                                  (device_id, pairing['owner'], pairing['folder_id'], digest(token),
                                   name.strip(), now, now))
            self.store.db.execute('DELETE FROM obsidian_pairing WHERE code_hash=?', (digest(code),))
        return {'device_id': device_id, 'token': token, 'scope': pairing['folder_id'] or 'all'}

    def authenticate(self, token, allowed_owners):
        if not isinstance(token, str) or not re.fullmatch(r'vm_[A-Za-z0-9_-]{43}', token):
            raise AccessError('unauthorized')
        device = self.store.one('SELECT * FROM obsidian_devices WHERE token_hash=?', (digest(token),))
        if not device or device['owner'] not in allowed_owners:
            raise AccessError('unauthorized')
        if device['folder_id']:
            try:
                self.store.folder(device['owner'], device['folder_id'])
            except ValueError:
                raise AccessError('scope_unavailable') from None
        if time.time() - device['last_used'] > 60:
            with self.store.transaction():
                self.store.db.execute('UPDATE obsidian_devices SET last_used=? WHERE id=?',
                                      (time.time(), device['id']))
        return device

    def devices(self, owner):
        return self.store.all('SELECT id,name,folder_id,created_at,last_used FROM obsidian_devices '
                              'WHERE owner=? ORDER BY created_at', (owner,))

    def revoke(self, owner, device_id):
        with self.store.transaction():
            self.store.db.execute('DELETE FROM obsidian_devices WHERE owner=? AND id=?', (owner, device_id))
