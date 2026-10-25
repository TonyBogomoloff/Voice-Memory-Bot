"""Bounded owner concurrency over the durable database queues."""
import asyncio


async def dispatch_by_owner(claim, handle, owner_of, limit):
    active = {}
    try:
        while True:
            for owner, task in list(active.items()):
                if task.done():
                    del active[owner]
                    task.result()
            while len(active) < limit:
                item = claim(excluded_owners=set(active))
                if item is None:
                    break
                active[owner_of(item)] = asyncio.create_task(handle(item))
            if active:
                await asyncio.wait(active.values(), timeout=.2, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(.2)
    finally:
        for task in active.values():
            task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)


async def run_for_owners(owners, worker):
    # The configured allow-list bounds the number of idle loops; I/O uses shared slots.
    async with asyncio.TaskGroup() as group:
        for owner in sorted(owners):
            group.create_task(worker(owner))
