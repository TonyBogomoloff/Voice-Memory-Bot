import json


def manifest(zipped):
    return json.loads(zipped.read(next(n for n in zipped.namelist() if n.startswith("manifest-"))))


class ExportTelegram:
    def __init__(self):
        self.sent, self.documents = [], []

    async def call(self, method, **values):
        self.sent.append((method, values))
        return {"message_id": len(self.sent)}

    async def text(self, owner, text, thread=None, keys=None):
        return await self.call("sendMessage", chat_id=owner, text=text, reply_markup=keys)

    async def document(self, owner, path, thread=None, caption=None):
        self.documents.append((owner, path.read_bytes(), caption))
