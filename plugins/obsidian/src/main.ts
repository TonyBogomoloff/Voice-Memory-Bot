import { App, Notice, Platform, Plugin, PluginSettingTab, Setting, TFile, TFolder } from 'obsidian';
import { ID, Importer, MAX_BATCH, MAX_DOCUMENT, noteId, Report, safePath, State, VaultIO } from './engine';
import { APIError, NetworkSource, serverUrl } from './network';
import { loadZips } from './zip';

interface Settings { server: string; folder: string; auto: boolean; secret: string; state: State }
const defaults: Settings = { server: 'https://audio-notes.korobkoff.com', folder: 'Voice Memory', auto: true, secret: '', state: {} };

function vaultIO(app: App): VaultIO {
  const file = (path: string): TFile => {
    safePath(path);
    const entry = app.vault.getAbstractFileByPath(path);
    if (!(entry instanceof TFile)) throw new Error('Не удалось прочитать файл в папке экспорта.');
    if (entry.stat.size > MAX_BATCH) throw new Error('Локальный файл слишком большой.');
    return entry;
  };
  const parents = async (path: string) => {
    safePath(path);
    const parts = path.split('/').slice(0, -1);
    for (let i = 1; i <= parts.length; i++) {
      const dir = parts.slice(0, i).join('/');
      const entry = app.vault.getAbstractFileByPath(dir);
      if (entry && !(entry instanceof TFolder)) throw new Error('Папка назначения занята файлом.');
      if (!entry) {
        try { await app.vault.createFolder(dir); }
        catch (error) { if (!(app.vault.getAbstractFileByPath(dir) instanceof TFolder)) throw error; }
      }
    }
  };
  return {
    exists: async path => { safePath(path); return app.vault.getAbstractFileByPath(path) !== null; },
    readText: path => app.vault.read(file(path)),
    readBytes: async path => new Uint8Array(await app.vault.readBinary(file(path))),
    createText: async (path, text) => { await parents(path); await app.vault.create(path, text); },
    createBytes: async (path, bytes) => { await parents(path); await app.vault.createBinary(path, new Uint8Array(bytes).buffer); },
    replaceText: async (path, expected, text) => {
      let replaced = false;
      await app.vault.process(file(path), current => { if (current !== expected) return current; replaced = true; return text; });
      return replaced;
    },
    findNotes: async root => {
      safePath(root);
      const result = new Map<string, string[]>();
      for (const entry of app.vault.getMarkdownFiles()) {
        if (!entry.path.startsWith(root + '/') || entry.stat.size > MAX_DOCUMENT) continue;
        const cached = app.metadataCache.getFileCache(entry)?.frontmatter?.voice_memory_id;
        const id = typeof cached === 'string' && ID.test(cached) ? cached : noteId(await app.vault.read(entry));
        if (id) result.set(id, [...(result.get(id) ?? []), entry.path]);
      }
      return result;
    }
  };
}

export default class VoiceMemory extends Plugin {
  settings: Settings = { ...defaults, state: {} };
  busy = false;
  status = 'Подключи бота или загрузи ZIP.';
  private unloaded = false;
  private statusElements = new Set<HTMLElement>();
  private lastAutomatic = 0;

  async onload(): Promise<void> {
    const data = await this.loadData();
    this.settings = { ...defaults, ...data, state: data?.state && typeof data.state === 'object' ? data.state : {} };
    // Tokens live in Obsidian's per-vault local SecretStorage, never in synced data.json.
    if (!/^voice-memory-[a-z0-9-]+$/.test(this.settings.secret)) {
      this.settings.secret = 'voice-memory-' + crypto.randomUUID();
      await this.save();
    }
    this.addSettingTab(new VoiceMemorySettings(this.app, this));
    this.addCommand({ id: 'receive-notes', name: 'Получить заметки', callback: () => void this.sync(true) });
    this.addCommand({ id: 'import-zip', name: 'Загрузить ZIP из бота', callback: () => this.chooseZip() });
    this.addRibbonIcon('download', 'Voice Memory: получить заметки', () => void this.sync(true));
    this.registerInterval(window.setInterval(() => void this.automatic(), 60000));
    this.registerDomEvent(document, 'visibilitychange', () => { if (!document.hidden) void this.automatic(); });
    this.app.workspace.onLayoutReady(() => void this.automatic());
  }
  onunload(): void { this.unloaded = true; }
  token(): string { return this.app.secretStorage.getSecret(this.settings.secret) ?? ''; }
  async save(): Promise<void> { await this.saveData(this.settings); }
  watchStatus(element: HTMLElement): void { this.statusElements.add(element); this.showStatus(this.status); }
  private showStatus(text: string): void {
    this.status = text;
    for (const el of this.statusElements) { if (el.isConnected) el.setText(text); else this.statusElements.delete(el); }
  }
  private async automatic(): Promise<void> {
    if (!this.settings.auto || !this.token() || document.hidden || this.busy || Date.now() - this.lastAutomatic < 30000) return;
    this.lastAutomatic = Date.now(); await this.sync(false);
  }
  async connect(code: string, device: string): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const server = serverUrl(this.settings.server);
      const token = await new NetworkSource(server, '').pair(code, device);
      this.app.secretStorage.setSecret(this.settings.secret, token);
      this.settings.server = server;
      await this.save(); this.showStatus('Подключено. Можно получать заметки.'); new Notice(this.status);
    } catch (error) { this.failure(error, true); }
    finally { this.busy = false; }
  }
  async disconnect(): Promise<void> {
    if (this.busy) return;
    this.app.secretStorage.setSecret(this.settings.secret, '');
    this.showStatus('Отключено на этом устройстве. Ключ можно отозвать в Telegram → Настройки → Obsidian.');
    await this.save();
  }
  private failure(error: unknown, notify: boolean): void {
    const text = error instanceof Error ? error.message : 'Не удалось получить заметки.';
    this.showStatus(text);
    if (notify) new Notice(text, 8000);
  }
  private finish(report: Report, notify: boolean): void {
    const text = `Добавлено: ${report.added}. Обновлено: ${report.updated}. Без изменений: ${report.unchanged}.` +
      (report.conflicts ? ` Твои правки сохранены; серверных копий: ${report.conflicts}.` : '') +
      (report.pending ? ` Ожидают обработки или недостающих ZIP-частей: ${report.pending}.` : '');
    this.showStatus(text); if (notify) new Notice(text, 8000);
  }
  async sync(notify: boolean): Promise<void> {
    if (this.busy || this.unloaded) return;
    if (!this.token()) { if (notify) new Notice('Введи код подключения в настройках Voice Memory или загрузи ZIP.'); return; }
    this.busy = true; this.showStatus('Получаю заметки…');
    try {
      const source = new NetworkSource(serverUrl(this.settings.server), this.token());
      const importer = new Importer(vaultIO(this.app), safePath(this.settings.folder), this.settings.state, () => this.save());
      await importer.init();
      let cursor = '';
      const seen = new Set<string>();
      do {
        if (this.unloaded || document.hidden) throw new Error('Загрузка приостановлена. Продолжится при открытии Obsidian.');
        if (seen.has(cursor) || seen.size >= 400) throw new Error('Некорректная пагинация сервера.');
        seen.add(cursor);
        const page = await source.list(cursor);
        importer.report.pending += page.pending;
        for (const row of page.notes) {
          if (this.unloaded) return;
          try { await importer.accept(row, source); }
          catch (error) {
            if (error instanceof APIError && (error.status === 404 || error.status === 409)) importer.report.pending++;
            else throw error;
          }
        }
        cursor = page.next ?? '';
      } while (cursor);
      this.finish(importer.report, notify);
    } catch (error) { this.failure(error, notify); }
    finally { this.busy = false; }
  }
  chooseZip(): void {
    if (this.busy) return;
    const input = document.createElement('input');
    input.type = 'file'; input.accept = '.zip,application/zip'; input.multiple = true;
    input.onchange = () => { void this.importZips([...input.files ?? []]); };
    input.click();
  }
  private async importZips(files: File[]): Promise<void> {
    if (this.busy || !files.length) return;
    this.busy = true; this.showStatus('Проверяю ZIP…');
    try {
      if (files.reduce((n, file) => n + file.size, 0) > MAX_BATCH) throw new Error('Выбери ZIP общим размером до 200 МБ.');
      const zip = await loadZips(await Promise.all(files.map(async file => new Uint8Array(await file.arrayBuffer()))));
      const importer = new Importer(vaultIO(this.app), safePath(this.settings.folder), this.settings.state, () => this.save());
      await importer.init();
      for (const row of zip.notes) { if (this.unloaded) return; await importer.accept(row, zip.source); }
      importer.report.pending += zip.pending;
      this.finish(importer.report, true);
    } catch (error) { this.failure(error, true); }
    finally { this.busy = false; }
  }
}

class VoiceMemorySettings extends PluginSettingTab {
  constructor(app: App, private plugin: VoiceMemory) { super(app, plugin); }
  display(): void {
    const { containerEl: el } = this; const plugin = this.plugin;
    el.empty(); el.createEl('h2', { text: 'Voice Memory' });
    el.createEl('p', { text: 'Одна папка бота — одна заметка в Obsidian: записи по времени, текст, фото и аудио. Экспорт аудио можно выключить в боте для всех или для одной папки. Обратной отправки и удаления файлов нет. При закрытом приложении загрузка ждёт следующего открытия.' });
    el.createEl('p', { text: 'Пиши свои мысли в разделе «Мои дополнения» — они сохранятся при обновлении. Правки внутри истории из бота сохраняются отдельной копией. Старые отдельные заметки версии 0.1 остаются на месте.' });
    plugin.watchStatus(el.createEl('p', { cls: 'voice-memory-status' }));
    new Setting(el).setName('Папка для заметок').setDesc('Только эта папка участвует в получении заметок.').addText(input => input
      .setValue(plugin.settings.folder).setDisabled(plugin.busy).onChange(async value => {
        try { safePath(value.trim()); plugin.settings.folder = value.trim(); await plugin.save(); } catch { /* Wait for valid input. */ }
      }));
    new Setting(el).setName('Сервер').addText(input => input.setValue(plugin.settings.server).setDisabled(plugin.busy || !!plugin.token())
      .onChange(async value => { try { plugin.settings.server = serverUrl(value.trim()); await plugin.save(); } catch { /* Incomplete input. */ } }));
    let code = ''; let device = Platform.isIosApp ? 'iPhone / iPad' : Platform.isAndroidApp ? 'Android' : 'Компьютер';
    new Setting(el).setName('Имя устройства').addText(input => input.setValue(device).onChange(value => device = value));
    new Setting(el).setName('Код из Telegram').setDesc('Бот → Настройки → Obsidian → Подключить устройство. Код действует 10 минут.')
      .addText(input => input.setPlaceholder('Одноразовый код').onChange(value => code = value))
      .addButton(button => button.setButtonText('Подключить').setDisabled(plugin.busy).onClick(async () => { await plugin.connect(code, device); this.display(); }));
    new Setting(el).setName('Получать автоматически').setDesc('При открытии и раз в минуту, пока Obsidian активен.').addToggle(toggle => toggle
      .setValue(plugin.settings.auto).onChange(async value => { plugin.settings.auto = value; await plugin.save(); }));
    new Setting(el).addButton(button => button.setButtonText('Получить заметки').setCta().onClick(() => void plugin.sync(true)))
      .addButton(button => button.setButtonText('Загрузить ZIP').onClick(() => plugin.chooseZip()));
    if (plugin.token()) new Setting(el).addButton(button => button.setButtonText('Отключить устройство').onClick(async () => { await plugin.disconnect(); this.display(); }));
  }
}
