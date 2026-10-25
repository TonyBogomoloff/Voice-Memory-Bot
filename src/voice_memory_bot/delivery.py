"""Persist delivery intent before I/O and retain ambiguous outcomes on failure."""
from enum import StrEnum
from functools import partial

from .providers import ProviderError
from .states import DeliveryState


class DeletionOutcome(StrEnum):
    REMOVED = 'removed'
    MISSING = 'missing'
    FORBIDDEN = 'forbidden'
    RETRY = 'retry'

    @property
    def complete(self):
        return self in {self.REMOVED, self.MISSING}


def deletion_error(error):
    return {'delete_target_missing': DeletionOutcome.MISSING,
            'delete_forbidden': DeletionOutcome.FORBIDDEN}.get(
                getattr(error, 'reason', None), DeletionOutcome.RETRY)


async def delete_message(telegram, owner, message_id):
    try:
        await telegram.call('deleteMessage', chat_id=owner, message_id=message_id)
    except ProviderError as error:
        return deletion_error(error)
    return DeletionOutcome.REMOVED


class DeliveryAttempt:
    def __init__(self, update, fields, intent):
        self.update, self.fields, self.intent = update, fields, intent
        self.finished = False

    def __enter__(self):
        self.update(**dict.fromkeys(self.fields, DeliveryState.SENDING), **self.intent)
        return self

    def finish(self, *, state=DeliveryState.SENT, **receipt):
        self.update(**dict.fromkeys(self.fields, DeliveryState(state)), **receipt)
        self.finished = True

    def __exit__(self, kind, error, traceback):
        if not self.finished:
            # Includes cancellation. Confirmed chunk IDs remain in MessageStore.
            self.update(**dict.fromkeys(self.fields, DeliveryState.UNKNOWN))
            if kind is None:
                raise RuntimeError('delivery_result_not_recorded')
        return False


class DeliveryService:
    def __init__(self, store, ai_state):
        self.store, self.ai_state = store, ai_state

    def note(self, note, *fields, **intent):
        return DeliveryAttempt(partial(self.store.update_note, note['owner'], note['id']),
                               fields or ('delivery',), intent)

    def ai(self, owner, identity):
        return DeliveryAttempt(partial(self.ai_state.update_request, owner, identity), ('delivery',), {})
