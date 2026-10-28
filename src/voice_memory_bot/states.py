"""Typed runtime values with adapters for the existing SQLite and callback format."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum


class DeliveryState(StrEnum):
    PENDING = 'pending'
    SENDING = 'sending'
    SENT = 'sent'
    UNKNOWN = 'unknown'
    CANCELLED = 'cancelled'


class NoteState(StrEnum):
    QUEUED = 'queued'
    PROCESSING = 'processing'
    READY = 'ready'
    ASR_FAILED = 'asr_failed'


class AIState(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    READY = 'ready'
    FAILED = 'failed'
    DELETED = 'deleted'


class InputKind(StrEnum):
    CREATE = 'create'
    SEARCH = 'search'
    AI_NAMES = 'ai_names'
    RENAME = 'rename'
    ALIASES = 'aliases'
    AI_NOTE = 'ai_note'
    REPLY_MOVE = 'replymove'


@dataclass(frozen=True)
class Target:
    kind: str
    argument: str = ''

    @classmethod
    def parse(cls, value: str):
        if not isinstance(value, str) or not value:
            raise ValueError('invalid_navigation_target')
        kind, _, argument = value.partition(':')
        return cls(kind, argument)

    @property
    def wire(self):
        return self.kind + (':' + self.argument if self.argument else '')

    @property
    def can_reopen(self):
        # Back must never replay a mutation, deletion or confirmation.
        return self.kind in {'folders', 'foldermenu', 'settings', 'pagesettings', 'export', 'exportsettings', 'obsidian',
                             'aisettings', 'aimodels', 'aiaccessmenu', 'ailist', 'help'}


@dataclass(frozen=True)
class InputAction:
    kind: InputKind
    argument: str = ''

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        target = Target.parse(value)
        kind = InputKind(target.kind)
        if kind in {InputKind.RENAME, InputKind.ALIASES, InputKind.AI_NOTE, InputKind.REPLY_MOVE} and not target.argument:
            raise ValueError('missing_action_target')
        return cls(kind, target.argument)

    @property
    def wire(self):
        return self.kind.value + (':' + self.argument if self.argument else '')


@dataclass
class MenuState:
    screen: Target
    back: Target
    text: str
    rows: list[list[dict]]
    page: int = 0
    keep: bool = False
    actions: dict[str, str] = field(default_factory=dict)
    epoch: str | None = None
    expires: float = 0
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value):
        known = {'screen', 'back', 'text', 'rows', 'page', 'keep', 'actions', 'epoch', 'expires'}
        return cls(Target.parse(value['screen']), Target.parse(value.get('back', 'root')),
                   value.get('text', ''), value.get('rows', []), int(value.get('page', 0)),
                   bool(value.get('keep', False)), dict(value.get('actions', {})),
                   value.get('epoch'), value.get('expires', 0), {k: v for k, v in value.items() if k not in known})

    @classmethod
    def from_json(cls, value):
        return cls.from_mapping(json.loads(value))

    def to_mapping(self):
        value = {**self.extra, 'screen': self.screen.wire, 'back': self.back.wire, 'text': self.text,
                 'rows': self.rows, 'page': self.page, 'keep': self.keep, 'actions': self.actions}
        if self.epoch is not None:
            value.update(epoch=self.epoch, expires=self.expires)
        return value

    def to_json(self):
        return json.dumps(self.to_mapping(), ensure_ascii=False)

    def consume_confirmation(self, label, epoch, now):
        if self.epoch != epoch or now >= self.expires:
            raise ValueError('Подтверждение устарело. Открой действие заново.')
        return self.actions.pop(label)
