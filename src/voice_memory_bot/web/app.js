"use strict";
const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const state = {token: "", folders: [], folder: null, notes: [], more: false, loading: false,
  maxBytes: 20000000, maxSeconds: 1800, upload: false, failedUpload: null, recorder: null, starting: false, drafts: {}, saving: false};
const types = {voice: ["≋", "Голосовая запись"], audio: ["♫", "Аудио"], photo: ["▧", "Фото"],
  video: ["▷", "Видео"], document: ["▤", "Файл"], text: ["≡", "Заметка"]};
let toastTimer, pollTimer;

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function action(text, className, handler) {
  const el = element("button", className, text);
  el.type = "button";
  el.addEventListener("click", handler);
  return el;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5500);
}
function fail(error) { toast(error.message || "Не удалось выполнить действие."); }
function closingGuard() {
  if (!tg?.isVersionAtLeast?.("6.2")) return;
  if (state.upload || state.recorder || state.starting || state.failedUpload || state.saving || hasDraft()) tg.enableClosingConfirmation();
  else tg.disableClosingConfirmation();
}
async function api(path, options = {}) {
  const headers = {...(options.body ? {"Content-Type": "application/json"} : {}),
    ...(state.token ? {Authorization: `Bearer ${state.token}`} : {}), ...options.headers};
  const response = await fetch(path, {...options, headers, signal: AbortSignal.timeout(25000)});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      state.token = "";
      clearTimeout(pollTimer);
      $("gate-text").textContent = "Сессия закончилась. Закрой мини-эп и открой его заново из меню бота.";
      // Keep a recording or failed upload available until it has been saved or cancelled.
      if (!state.recorder && !state.failedUpload && !state.upload) {
        $("app").hidden = true;
        $("gate").hidden = false;
      }
    }
    throw new Error(data.error || "Не удалось выполнить действие.");
  }
  return data;
}
const post = (path, body = {}) => api(path, {method: "POST", body: JSON.stringify(body)});
function bytes(value) { return value < 1000000 ? `${Math.ceil(value / 1000)} КБ` : `${Number((value / 1000000).toFixed(1))} МБ`; }
function clock(value) { return `${Math.floor(value / 60).toString().padStart(2, "0")}:${(value % 60).toString().padStart(2, "0")}`; }
function folderButton(folder, callback) {
  const button = action("", "folder", callback);
  button.append(element("span", "folder-icon", folder.is_inbox ? "▱" : "▰"),
    element("span", "name", folder.name), element("span", "badge", folder.count ?? ""));
  return button;
}
function renderFolders() {
  const signature = JSON.stringify([state.folders, state.folder]);
  if ($("folders").dataset.signature === signature) return;
  $("folders").dataset.signature = signature;
  $("folders").replaceChildren(...state.folders.map(folder => {
    const button = folderButton(folder, () => openFolder(folder.id).catch(fail));
    button.classList.toggle("active", folder.id === state.folder);
    button.setAttribute("aria-current", folder.id === state.folder ? "page" : "false");
    button.addEventListener("dragover", e => {
      if (!e.dataTransfer.types.includes("application/x-audio-note")) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "move"; button.classList.add("drop-target");
    });
    button.addEventListener("dragleave", () => button.classList.remove("drop-target"));
    button.addEventListener("drop", e => {
      e.preventDefault(); button.classList.remove("drop-target");
      const id = e.dataTransfer.getData("application/x-audio-note");
      if (state.notes.some(note => note.id === id && note.can_move)) moveNote(id, folder.id).catch(fail);
    });
    return button;
  }));
  const current = state.folders.find(f => f.id === state.folder);
  $("folder-name").textContent = current?.name || "Папка";
  const count = current?.count || 0;
  const ending = count % 10 === 1 && count % 100 !== 11 ? "запись" :
    count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 12 || count % 100 > 14) ? "записи" : "записей";
  $("count").textContent = `${count} ${ending}`;
}
async function loadFolders() {
  const data = await api("/api/folders");
  state.folders = data.folders;
  if (!state.folders.some(f => f.id === state.folder)) state.folder = state.folders[0]?.id;
  renderFolders();
}
async function openFolder(id) {
  if (state.recorder || state.starting) { toast("Сначала заверши или отмени запись."); return; }
  state.folder = id; state.notes = []; state.more = false; renderDraft();
  $("app").classList.add("in-folder");
  $("notes").replaceChildren(element("p", "empty", "Загружаем записи…"));
  renderFolders();
  if (tg?.BackButton) tg.BackButton.show();
  await loadNotes();
}
async function loadNotes(append = false) {
  if (!state.folder) return;
  const folder = state.folder;
  const offset = append ? state.notes.length : 0;
  const data = await api(`/api/folders/${folder}/notes?offset=${offset}`);
  if (folder !== state.folder) return;
  if (append) {
    const seen = new Set(state.notes.map(n => n.id));
    state.notes.push(...data.notes.filter(n => !seen.has(n.id)));
  } else state.notes = data.notes;
  state.more = data.more;
  renderNotes();
}
function back() {
  if (state.recorder || state.starting) { toast("Сначала заверши или отмени запись."); return; }
  $("app").classList.remove("in-folder"); tg?.BackButton?.hide();
}
function renderNotes() {
  const parent = $("notes");
  if (!state.notes.length) {
    const empty = element("div", "empty");
    empty.append(element("div", "empty-icon", "▱"), element("h2", "", "Здесь начнётся твоя история"),
      element("p", "", "Запиши мысль голосом или прикрепи фото, видео, файл. Всё останется в этой папке."));
    parent.replaceChildren(empty);
  } else {
    parent.querySelectorAll(".empty").forEach(el => el.remove());
    const existing = new Map([...parent.children].map(el => [el.dataset.id, el]));
    const active = new Set();
    state.notes.forEach((note, index) => {
      active.add(note.id);
      const signature = JSON.stringify(note);
      let card = existing.get(note.id);
      if (!card || card.dataset.signature !== signature) {
        const fresh = noteCard(note); fresh.dataset.signature = signature;
        if (card) card.replaceWith(fresh);
        card = fresh;
      }
      if (parent.children[index] !== card) parent.insertBefore(card, parent.children[index] || null);
    });
    [...parent.children].forEach(el => { if (!active.has(el.dataset.id)) el.remove(); });
  }
  $("more").hidden = !state.more;
}
function closeMenus() { document.querySelectorAll(".note-menu").forEach(el => { el.hidden = true; el.parentElement.querySelector(".icon-button").setAttribute("aria-expanded", "false"); }); }
function noteCard(note) {
  const card = element("article", "note" + (note.ai_generated ? " ai-note" : "")); card.dataset.id = note.id;
  card.draggable = note.can_move;
  card.addEventListener("dragstart", e => {
    if (e.target.closest("button,audio,video,details,a,img")) { e.preventDefault(); return; }
    e.dataTransfer.setData("application/x-audio-note", note.id); e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  const type = types[note.kind] || types.document;
  const heading = note.ai_generated ? "Ответ ИИ" : ["voice", "audio", "text"].includes(note.kind) ? type[1] : note.original_name || type[1];
  const head = element("div", "note-head"), info = element("div", "note-info");
  info.append(element("div", "note-title", heading));
  const date = new Intl.DateTimeFormat("ru", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}).format(new Date(note.created_at * 1000));
  info.append(element("div", "note-date", [date, note.audio_bytes ? bytes(note.audio_bytes) : ""].filter(Boolean).join(" · ")));
  const actions = element("div", "note-actions"), menu = element("div", "note-menu"); menu.hidden = true;
  const more = action("⋯", "icon-button", e => {
    e.stopPropagation(); const open = menu.hidden; closeMenus(); menu.hidden = !open;
    more.setAttribute("aria-expanded", String(open));
  });
  more.setAttribute("aria-label", `Опции: ${heading}`); more.setAttribute("aria-expanded", "false");
  if (["voice", "audio", "text"].includes(note.kind)) {
    const execute = action(note.ai?.state === "failed" ? "Повторить выполнение" : "Выполнить", "", async e => {
      e.stopPropagation(); e.currentTarget.disabled = true;
      try { await post(`/api/notes/${note.id}/execute`); closeMenus(); toast("Выполняю. Ответ появится в этой папке."); await loadNotes(); }
      catch (err) { fail(err); e.target.disabled = !note.can_execute; }
    });
    execute.disabled = !note.can_execute; menu.append(execute);
  }
  menu.append(action("Использовать в команде", "", () => {
    draft().context = {id: note.id, title: heading}; closeMenus(); renderDraft(); $("note-input").focus(); closingGuard();
  }));
  const move = action("Переместить", "", () => showMove(note)); move.disabled = !note.can_move;
  menu.append(move); actions.append(more, menu);
  head.append(element("div", "type-icon", note.ai_generated ? "✦" : type[0]), info, actions); card.append(head);
  if (note.has_file) {
    const media = element("div", "inline-media"); card.append(media);
    if (["voice", "audio", "video", "photo"].includes(note.kind)) inlineMedia(note, media).catch(error => {
      media.replaceChildren(action("Загрузить файл", "media-button", () => inlineMedia(note, media).catch(fail)));
      media.append(element("p", "error", error.message));
    });
    else media.append(action("↓ Скачать файл", "media-button", () => downloadNote(note).catch(fail)));
  }
  if (note.text) card.append(element("div", "note-text", note.text));
  if (["queued", "processing", "asr_failed"].includes(note.state)) {
    const error = note.state === "asr_failed";
    const status = element("div", "note-status" + (error ? " error" : ""), error ? "Не удалось обработать. Оригинал сохранён." :
      ["voice", "audio"].includes(note.kind) ? "Расшифровываем голосовое…" : "Сохраняем файл…");
    if (note.can_retry) status.append(action("Повторить", "text-button", async e => {
      e.currentTarget.disabled = true;
      try { await post(`/api/notes/${note.id}/retry`); await loadNotes(); } catch (err) { fail(err); e.target.disabled = false; }
    }));
    card.append(status);
  }
  if (note.ai) {
    const running = ["queued", "running"].includes(note.ai.state), failed = note.ai.state === "failed";
    card.append(element("div", "ai-status" + (running ? " running" : failed ? " error" : ""),
      running ? "✦ ИИ выполняет команду…" : failed ? note.ai.error || "Не удалось выполнить. Можно повторить через ⋯." : "✓ Выполнено · ответ добавлен"));
  }
  return card;
}
async function inlineMedia(note, host) {
  let media;
  if (note.kind === "photo") {
    media = element("img", "note-photo"); media.alt = note.original_name || "Фото"; media.loading = "lazy";
  } else {
    media = element(note.kind === "video" ? "video" : "audio", "player");
    media.controls = true; media.preload = "none"; media.playsInline = true;
    media.setAttribute("aria-label", note.kind === "video" ? "Видео" : "Прослушать голосовое");
  }
  host.replaceChildren(media);
  const {url} = await api(`/api/notes/${note.id}/media`);
  media.src = url;
  if (note.kind === "photo") {
    media.tabIndex = 0; media.setAttribute("role", "button");
    const preview = () => {
      $("preview-title").textContent = note.original_name || "Фото";
      const img = element("img", "photo-preview"); img.src = url; img.alt = media.alt;
      $("preview-content").replaceChildren(img, action("Скачать оригинал", "text-button", () => downloadNote(note).catch(fail)));
      $("preview-dialog").showModal();
    };
    media.onclick = preview;
    media.onkeydown = e => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); preview(); } };
  } else {
    media.addEventListener("error", () => toast("Этот формат не воспроизводится здесь. Можно скачать оригинал."));
    host.append(action("Скачать оригинал", "text-button", () => downloadNote(note).catch(fail)));
  }
}
function draft() {
  return state.drafts[state.folder] ||= {text: "", key: crypto.randomUUID().replaceAll("-", ""), context: null};
}
function hasDraft() { return Object.values(state.drafts).some(d => d.text.trim()); }
function renderDraft() {
  const current = draft(); $("note-input").value = current.text;
  $("context").hidden = !current.context; $("context-label").textContent = current.context ? "К записи: " + current.context.title : "";
  $("note-input").placeholder = current.context ? "Что сделать с этой записью?" : "Напиши заметку…";
  busyButtons();
}
async function downloadNote(note, url) {
  if (!url) ({url} = await api(`/api/notes/${note.id}/media`));
  const name = note.original_name || note.title || "Файл";
  if (tg?.isVersionAtLeast?.("8.0") && tg.downloadFile) {
    tg.downloadFile({url: new URL(url + "&download=1", location.origin).href, file_name: name});
    return;
  }
  // Older clients: download a Blob without navigating to an uploaded document.
  const response = await fetch(url + "&download=1", {signal: AbortSignal.timeout(120000)});
  if (!response.ok) throw new Error("Не удалось скачать файл.");
  const blobUrl = URL.createObjectURL(await response.blob());
  const link = element("a"); link.href = blobUrl; link.download = name;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
}
function showMove(note) {
  closeMenus(); $("move-title").textContent = note.title;
  $("move-folders").replaceChildren(...state.folders.filter(f => f.id !== note.folder_id).map(folder =>
    folderButton(folder, async e => {
      e.currentTarget.disabled = true;
      try { await moveNote(note.id, folder.id); $("move-dialog").close(); } catch (err) { fail(err); e.target.closest("button").disabled = false; }
    })));
  if (state.folders.length < 2) $("move-folders").append(element("p", "muted", "Сначала создай ещё одну папку кнопкой ＋ в списке папок."));
  $("move-dialog").showModal();
}
async function moveNote(id, folder) {
  await post(`/api/notes/${id}/move`, {folder_id: folder});
  toast("Запись перемещена"); await loadFolders(); await loadNotes();
}
function busyButtons() {
  $("note-input").disabled = state.saving;
  $("send-text").disabled = state.saving || !!state.recorder || state.starting || !draft().text.trim();
  $("attach").disabled = state.saving || state.upload || !!state.recorder || state.starting || !!state.failedUpload;
  $("record").disabled = state.saving || state.upload || state.starting || !!state.failedUpload;
  closingGuard();
}
function sendFile(job) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open("PUT", `/api/folders/${job.folder}/uploads/${job.key}`);
    xhr.timeout = 130000; xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    xhr.setRequestHeader("X-File-Name", encodeURIComponent(job.file.name));
    xhr.setRequestHeader("Content-Type", job.file.type || "application/octet-stream");
    if (job.context) xhr.setRequestHeader("X-Context-Note", job.context);
    if (job.voice) xhr.setRequestHeader("X-Media-Kind", "voice");
    xhr.upload.onprogress = e => { if (e.lengthComputable) $("progress").value = e.loaded / e.total * 100; };
    xhr.onload = () => {
      try { const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || "Не удалось загрузить файл."));
      } catch { reject(new Error("Не удалось подтвердить загрузку. Нажми «Повторить».")); }
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error("Связь прервалась. Файл сохранён здесь — нажми «Повторить»."));
    xhr.send(job.file);
  });
}
async function uploadJobs(jobs) {
  state.upload = true; state.failedUpload = null; busyButtons();
  $("upload-status").hidden = false; $("retry-upload").hidden = true; $("dismiss-upload").hidden = true;
  document.querySelectorAll(".saved-upload").forEach(el => { URL.revokeObjectURL(el.href); el.remove(); });
  for (let i = 0; i < jobs.length; i++) {
    const job = jobs[i]; $("upload-label").textContent = `Загружаем ${job.file.name}`; $("progress").value = 0;
    try {
      if (job.file.size > state.maxBytes) throw new Error(`Файл больше ${bytes(state.maxBytes)}. Выбери файл поменьше.`);
      await sendFile(job);
    } catch (error) {
      state.failedUpload = jobs.slice(i); $("upload-label").textContent = error.message;
      $("retry-upload").hidden = false; $("dismiss-upload").hidden = false;
      for (const pending of state.failedUpload) {
        const link = element("a", "text-button saved-upload", `Скачать копию: ${pending.file.name}`);
        link.href = URL.createObjectURL(pending.file); link.download = pending.file.name;
        $("upload-status").append(link);
      }
      state.upload = false; busyButtons(); return;
    }
  }
  state.upload = false; $("upload-status").hidden = true; busyButtons();
  toast("Сохранено в папке"); await loadFolders(); await loadNotes();
}
function job(file, voice = false, folder = state.folder) {
  return {file, voice, folder, context: voice ? state.drafts[folder]?.context?.id : null, key: crypto.randomUUID().replaceAll("-", "")};
}
async function record() {
  if (state.recorder) { state.recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    toast("Запись с микрофона недоступна в этом Telegram. Прикрепи готовое аудио или запиши голосовое боту."); return;
  }
  state.starting = true; busyButtons();
  let stream, timer;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(m => MediaRecorder.isTypeSupported(m));
    const recorder = new MediaRecorder(stream, {...(mime ? {mimeType: mime} : {}), audioBitsPerSecond: 64000});
    const chunks = [], folder = state.folder, started = Date.now(); let size = 0, cancelled = false;
    state.recorder = recorder; state.starting = false;
    timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000); $("record-time").textContent = clock(seconds);
      if (seconds >= state.maxSeconds - 2 && recorder.state === "recording") recorder.stop();
    }, 250);
    $("cancel-record").onclick = () => { cancelled = true; if (recorder.state !== "inactive") recorder.stop(); };
    recorder.ondataavailable = e => {
      if (e.data.size) { chunks.push(e.data); size += e.data.size; }
      if (size > state.maxBytes - 65536 && recorder.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      clearInterval(timer); stream.getTracks().forEach(track => track.stop()); state.recorder = null;
      $("record-status").hidden = true; $("record").textContent = "● Записать голос"; busyButtons();
      if (cancelled) return;
      const type = recorder.mimeType || mime || "audio/webm";
      const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      const blob = new Blob(chunks, {type});
      if (!blob.size) { toast("Запись пустая. Попробуй ещё раз."); return; }
      uploadJobs([job(new File([blob], `Голосовая ${new Date().toLocaleTimeString("ru").replaceAll(":", "-")}.${ext}`, {type}), true, folder)]).catch(fail);
    };
    recorder.onerror = () => { toast("Запись прервалась. Сохраняем доступную часть."); if (recorder.state !== "inactive") recorder.stop(); };
    recorder.start(1000); $("record-time").textContent = "00:00"; $("record-status").hidden = false;
    $("record").textContent = "■ Завершить запись"; busyButtons();
  } catch {
    clearInterval(timer);
    stream?.getTracks().forEach(track => track.stop()); state.starting = false; state.recorder = null; busyButtons();
    toast("Нет доступа к микрофону. Разреши его в настройках Telegram или прикрепи аудиофайл.");
  }
}
$("note-input").oninput = () => {
  const current = draft(); current.text = $("note-input").value; current.key = crypto.randomUUID().replaceAll("-", ""); busyButtons();
};
$("clear-context").onclick = () => { draft().context = null; renderDraft(); };
$("text-form").onsubmit = async e => {
  e.preventDefault(); if (state.saving || !draft().text.trim()) return;
  const folder = state.folder, current = draft(); state.saving = true; busyButtons();
  try {
    await api(`/api/folders/${folder}/text/${current.key}`, {method: "PUT", body: JSON.stringify({text: current.text, context_note_id: current.context?.id})});
    delete state.drafts[folder]; if (state.folder === folder) renderDraft();
    await loadFolders(); await loadNotes(); toast("Заметка сохранена. Чтобы обратиться к ИИ: ⋯ → Выполнить.");
  } catch (error) { fail(error); } finally { state.saving = false; busyButtons(); }
};
$("note-input").onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("text-form").requestSubmit(); } };
$("new-folder").onclick = () => { $("folder-error").textContent = ""; $("folder-input").value = ""; $("folder-dialog").showModal(); $("folder-input").focus(); };
$("folder-form").onsubmit = async e => {
  e.preventDefault(); const submit = e.submitter; submit.disabled = true;
  try { const folder = await post("/api/folders", {name: $("folder-input").value.trim()});
    $("folder-dialog").close(); await loadFolders(); await openFolder(folder.id);
  } catch (err) { $("folder-error").textContent = err.message; } finally { submit.disabled = false; }
};
document.querySelectorAll(".close-dialog").forEach(button => button.onclick = () => button.closest("dialog").close());
document.addEventListener("click", e => { if (!e.target.closest(".note-actions")) closeMenus(); });
$("back").onclick = back; tg?.BackButton?.onClick(back);
$("refresh").onclick = () => loadFolders().then(() => loadNotes()).catch(fail);
$("more").onclick = async () => { $("more").disabled = true; try { await loadNotes(true); } catch (err) { fail(err); } finally { $("more").disabled = false; } };
$("attach").onclick = () => $("file-input").click();
$("file-input").onchange = e => { const files = [...e.target.files]; e.target.value = ""; if (files.length) uploadJobs(files.map(file => job(file))).catch(fail); };
$("record").onclick = () => record().catch(fail);
$("retry-upload").onclick = () => { if (state.failedUpload) uploadJobs(state.failedUpload).catch(fail); };
$("dismiss-upload").onclick = () => { state.failedUpload = null; $("upload-status").hidden = true; busyButtons(); };
window.addEventListener("beforeunload", e => { if (state.recorder || state.upload || state.failedUpload || state.saving || hasDraft()) { e.preventDefault(); e.returnValue = ""; } });

async function poll() {
  try {
    if (!document.hidden && state.token && !state.loading) {
      state.loading = true; await loadFolders();
      // Do not collapse a manually expanded list or interrupt active playback while polling.
      if (state.notes.length <= 40) await loadNotes();
    }
  } catch {} finally { state.loading = false; pollTimer = setTimeout(poll, 5000); }
}
async function init() {
  tg?.ready(); tg?.expand();
  if (tg?.isVersionAtLeast?.("6.1")) { tg.setHeaderColor("#181a20"); tg.setBackgroundColor("#181a20"); }
  if (!tg?.initData) { $("gate-text").textContent = "Открой Audio Notes через пункт /app в меню своего бота Telegram."; return; }
  try {
    const result = await post("/api/session", {init_data: tg.initData});
    state.token = result.token; state.maxBytes = result.max_file_bytes; state.maxSeconds = result.max_audio_seconds;
    $("limit").textContent = `Файл до ${bytes(state.maxBytes)} · голос до ${Math.floor(state.maxSeconds / 60)} мин · автоматическая расшифровка`;
    await loadFolders(); await loadNotes(); renderDraft(); $("gate").hidden = true; $("app").hidden = false;
    pollTimer = setTimeout(poll, 5000);
  } catch (error) { $("gate-text").textContent = error.message; }
}
init();
