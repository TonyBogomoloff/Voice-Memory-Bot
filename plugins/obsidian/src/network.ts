import { requestUrl, RequestUrlResponse } from 'obsidian';
import { Attachment, FORMAT, ID, Note, Source, validateRecord } from './engine';

export function serverUrl(raw: string): string {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Нужен HTTPS-адрес сервера без пути и пароля.');
  }
  return url.origin;
}
export class APIError extends Error {
  constructor(readonly status: number) {
    super(status === 401 ? 'Подключение недействительно. Получи новый код в боте.' :
      status === 429 ? 'Слишком много запросов. Повтори через минуту.' :
      status === 409 ? 'Заметка изменилась или ещё обрабатывается.' : 'Сервер недоступен. Повтори загрузку позже.');
  }
}
export class NetworkSource implements Source {
  constructor(readonly server: string, private token: string) { serverUrl(server); }
  private async request(path: string, body?: object): Promise<RequestUrlResponse> {
    const response = await requestUrl({ url: this.server + path, method: body ? 'POST' : 'GET',
      headers: this.token ? { Authorization: 'Bearer ' + this.token } : {},
      contentType: body ? 'application/json' : undefined, body: body ? JSON.stringify(body) : undefined, throw: false });
    if (response.status !== 200) throw new APIError(response.status);
    return response;
  }
  async pair(code: string, name: string): Promise<string> {
    const result = (await this.request('/v1/pair', { code: code.trim(), name: name.trim() })).json;
    if (result.schema_version !== 1 || !/^vm_[A-Za-z0-9_-]{43}$/.test(result.token)) throw new Error('Сервер вернул некорректный ключ.');
    return result.token;
  }
  async list(cursor: string): Promise<{ notes: Note[]; next: string | null; pending: number }> {
    const result = (await this.request('/v2/folders' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''))).json;
    if (result.format !== FORMAT || result.schema_version !== 2 || !Array.isArray(result.notes)
        || result.notes.length > 20 || (result.next_cursor !== null && !ID.test(result.next_cursor))) throw new Error('Формат сервера не поддерживается.');
    const notes = result.notes.map((value: unknown) => {
      const r = validateRecord(value);
      if (r.kind !== 'note' || r.layout !== 'folder') throw new Error('Некорректная запись.');
      return r;
    });
    return { notes, next: result.next_cursor, pending: Number.isSafeInteger(result.pending) ? result.pending : 0 };
  }
  async read(record: Note | Attachment): Promise<Uint8Array> {
    const path = record.kind === 'note' ? `/v2/folders/${record.note_id}/document` : `/v2/media/${record.note_id}`;
    const response = await this.request(`${path}?sha256=${record.sha256}`);
    return new Uint8Array(response.arrayBuffer);
  }
}
