import { $, esc, api, post, toast, fail } from "./util.js";

const ORIGIN = { missing: "не найден", user: "загружен", extension: "расширение", bundled: "встроенный" };

export function initSetup() {
  const panel = $("#setup");
  const body = $("#setupBody");
  const brief = $("#setupBrief");
  const banner = $("#firstrun");
  let state = null;
  let sig = "";
  let timer = 0;
  let working = "";
  let active = "";
  let uploadSeries = "";

  const current = () => (state ? state.series.find((item) => item.series === active) : null);
  const query = (key) => (key ? `?series=${encodeURIComponent(key)}` : "");

  const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(n / 1024))} КБ`);
  const fmtDate = (t) =>
    t ? new Date(t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "дата неизвестна";

  function fileHtml(cur) {
    const f = cur.prefs;
    if (!f) return `<p>Для версии ${esc(cur.series)} файл настроек не загружен.</p>`;
    if (!f.exists) {
      return `<p class="alert">Файл настроек для ${esc(cur.series)} записан в базе, но не найден на диске: ${esc(f.path)}. Загрузите его заново.</p>`;
    }
    return `<div class="filebox"><span><b>${esc(f.name)}</b> загружен, ${fmtSize(f.size)}, ${fmtDate(f.uploaded)}</span>
      <span class="dim">на сервере: ${esc(f.path)}</span>
      <span class="dim">найдено аддонов: ${cur.addons.length}${cur.scanned ? `, список прочитан ${fmtDate(cur.scanned)}` : ""}</span></div>`;
  }

  function briefText() {
    const loaded = state.series.filter((item) => item.prefs && item.prefs.exists);
    if (!loaded.length) return "Пока ни для одной версии настройки не загружены.";
    return "Загружено: " + loaded.map((item) => `${item.series} (${item.prefs.name}, ${fmtSize(item.prefs.size)})`).join(", ");
  }

  function pickActive() {
    if (!state || !state.series.length) {
      active = "";
      return;
    }
    if (state.series.some((item) => item.series === active)) return;
    const filled = state.series.find((item) => item.prefs || item.addons.length);
    const installed = state.series.find((item) => item.installed);
    active = (filled || installed || state.series[0]).series;
  }

  function rowHtml(item, cur) {
    const meta = [item.module !== item.name ? esc(item.module) : "", item.version ? esc(item.version) : ""].filter(Boolean).join(" ");
    if (item.origin === "missing") {
      const text =
        item.status === "installing"
          ? "Скачиваю с extensions.blender.org"
          : item.status === "failed"
            ? `Не удалось скачать: ${esc(item.message)}. Скачайте архив на extensions.blender.org и добавьте его здесь.`
            : !cur.extensions_supported && item.module.startsWith("bl_ext.")
              ? "Это расширение, для него нужен Blender 4.2 или новее."
              : `Нет на сервере для версии ${esc(cur.series)}. Добавьте архив аддона кнопкой «Добавить аддон».`;
      return `<div class="addon${item.status === "failed" ? " bad" : ""}">
        <span><b>${esc(item.name)}</b> <span class="dim">${meta}</span><br><span class="${item.status === "failed" ? "alert" : "dim"}">${text}</span></span>
        <button type="button" class="btn ghost" data-s="remove" data-m="${esc(item.module)}">Убрать из списка</button>
      </div>`;
    }
    const removable = item.origin !== "bundled";
    return `<div class="addon">
      <label class="check"><input type="checkbox" data-s="toggle" data-m="${esc(item.module)}"${item.enabled ? " checked" : ""}>
        <span><b>${esc(item.name)}</b> <span class="dim">${meta}${meta ? " · " : ""}${ORIGIN[item.origin]}</span></span></label>
      ${removable ? `<button type="button" class="btn ghost" data-s="remove" data-m="${esc(item.module)}" aria-label="Удалить ${esc(item.name)}">Удалить</button>` : ""}
    </div>`;
  }

  function group(title, items, cur, open = true) {
    if (!items.length) return "";
    const rows = `<div class="addons">${items.map((item) => rowHtml(item, cur)).join("")}</div>`;
    if (open) return `<div class="group"><p class="dim">${title}</p>${rows}</div>`;
    return `<details class="group"><summary class="dim">${title}</summary>${rows}</details>`;
  }

  function tabsHtml() {
    return `<div class="field"><span>версия Blender, для которой настройки и аддоны</span>
      <div class="seg" role="radiogroup">${state.series
        .map(
          (item) =>
            `<label><input type="radio" name="series-tab" value="${esc(item.series)}"${item.series === active ? " checked" : ""}><span>${esc(item.series)}${item.installed ? "" : " (нет)"}${item.prefs ? " ✓" : ""}</span></label>`
        )
        .join("")}</div></div>`;
  }

  function render() {
    if (!Array.isArray(state.series)) {
      body.innerHTML = `<p class="alert">Сервер ответил в старом формате: он работает на устаревшем коде, а страница новая. Перезапустите службу командой systemctl --user restart blender-render и обновите страницу (Ctrl+Shift+R). Если это не помогло, проверьте, что файлы проекта заменены в той папке, откуда запускается служба.</p>`;
      brief.textContent = "Сервер на старой версии кода";
      return;
    }
    banner.hidden = !state.needs_setup;
    pickActive();
    const next = JSON.stringify([state, working, active, uploadSeries]);
    if (next === sig) return;
    sig = next;
    brief.textContent = briefText();
    const cur = current();
    const off = working ? " disabled" : "";
    const busy = working || (state.busy ? "Сервер работает с Blender…" : "");
    const autoOptions = [["", "определить автоматически"], ...state.installed_series.map((key) => [key, key])]
      .map(([value, text]) => `<option value="${esc(value)}"${value === uploadSeries ? " selected" : ""}>${esc(text)}</option>`)
      .join("");
    let content = "";
    if (cur) {
      content = `
        ${fileHtml(cur)}
        <p class="dim">Папка версии на сервере: ${esc(cur.folder)}</p>
        ${cur.installed ? "" : `<p class="alert">Blender ${esc(cur.series)} не установлен на сервере. Скачайте его в разделе «Версии Blender», тогда эти настройки можно будет применять.</p>`}
        <div class="row">
          <button type="button" class="btn" data-s="rescan"${off}>Проверить заново</button>
          ${cur.prefs ? `<button type="button" class="btn ghost" data-s="reset"${off}>Сбросить настройки ${esc(cur.series)}</button>` : ""}
          <label class="btn solid"><span>Добавить аддон для ${esc(cur.series)}</span><input type="file" accept=".zip,.py" data-up="addon" hidden${off}></label>
        </div>
        ${group("Нужно добавить", cur.addons.filter((a) => a.origin === "missing"), cur)}
        ${group("Аддоны с вашего компьютера", cur.addons.filter((a) => a.origin === "user" || a.origin === "extension"), cur)}
        ${group(`Встроенные в Blender (${cur.addons.filter((a) => a.origin === "bundled").length})`, cur.addons.filter((a) => a.origin === "bundled"), cur, false)}`;
    }
    body.innerHTML = `
      <p class="dim">У каждой версии Blender свои настройки и свои аддоны: проект на 4.5 использует только то, что загружено для 4.5. Переносятся аддоны и их настройки из userpref.blend, тема и горячие клавиши на рендер не влияют. Отмеченные аддоны включаются при каждом рендере этой версии.</p>
      ${state.has_blender ? "" : `<p class="alert">Сначала установите хотя бы одну версию Blender в разделе «Версии Blender»: без неё сервер не может прочитать настройки.</p>`}
      <div class="row">
        <label class="btn solid"><span>Загрузить настройки</span><input type="file" accept=".blend,.zip" data-up="prefs" hidden${off}></label>
        <label class="field"><span>версия загружаемых настроек</span><select data-s="useries"${off}>${autoOptions}</select></label>
      </div>
      <p class="dim">Подойдёт userpref.blend или zip. Версию сервер берёт из выбранного списка, а при автоопределении из названия файла, папки вида 4.5 внутри архива или самого файла. Архив с папками нескольких версий загрузится сразу для всех.</p>
      ${state.series.length ? tabsHtml() : ""}
      ${busy ? `<p class="dim"><span class="tag t-run"><i></i><span>${esc(busy)}</span></span></p>` : ""}
      ${content}
      <div class="actions">
        <button type="button" class="btn solid" data-s="done">${state.configured ? "Настройка завершена" : "Готово"}</button>
      </div>`;
  }

  function schedule() {
    clearTimeout(timer);
    if (state && (state.busy || panel.open)) timer = setTimeout(load, state.busy ? 1200 : 4000);
  }

  async function load() {
    try {
      state = await api("/api/setup");
      render();
    } catch (error) {
      if (state) {
        body.innerHTML = `<p class="alert">Не удалось показать настройки: ${esc(error.message)}. Сделайте снимок консоли браузера (F12) и пришлите разработчику.</p>`;
        return;
      }
      if (!state) {
        body.innerHTML = `<p class="alert">Не удалось получить настройки с сервера: ${esc(error.message)}. Если файлы проекта только что обновили, перезапустите службу командой systemctl --user restart blender-render и обновите страницу (Ctrl+Shift+R).</p><div class="actions"><button type="button" class="btn" data-s="retry">Повторить</button></div>`;
        return;
      }
    }
    schedule();
  }

  async function act(label, action) {
    working = label;
    if (state) render();
    let result = null;
    try {
      result = await action();
      state = result;
    } catch (error) {
      fail(error);
    } finally {
      working = "";
      if (state) render();
      schedule();
    }
    return result;
  }

  async function sendPrefs(file, key) {
    const data = new FormData();
    data.append("file", file);
    const result = await act("Читаю настройки в Blender…", () => api(`/api/setup/prefs${query(key)}`, { method: "POST", body: data }));
    if (result && result.applied && result.applied.length) {
      active = result.applied[0];
      sig = "";
      render();
      toast(`Настройки загружены для Blender ${result.applied.join(", ")}`);
    }
  }

  async function sendAddon(file) {
    const data = new FormData();
    data.append("file", file);
    const result = await act("Устанавливаю аддон и проверяю список…", () => api(`/api/setup/addons${query(active)}`, { method: "POST", body: data }));
    if (result) toast(`Аддон добавлен для Blender ${active}`);
  }

  document.addEventListener("settings-file", async (event) => {
    panel.open = true;
    if (!state) await load();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    await sendPrefs(event.detail, "");
  });

  panel.addEventListener("toggle", () => {
    if (panel.open) load();
  });

  body.addEventListener("change", async (event) => {
    const target = event.target;
    if (target.matches("[data-up]")) {
      const file = target.files[0];
      const kind = target.dataset.up;
      target.value = "";
      if (!file) return;
      if (kind === "prefs") await sendPrefs(file, uploadSeries);
      else await sendAddon(file);
    } else if (target.matches("[data-s=useries]")) {
      uploadSeries = target.value;
      sig = "";
      render();
    } else if (target.matches("input[name=series-tab]")) {
      active = target.value;
      sig = "";
      render();
    } else if (target.matches("[data-s=toggle]")) {
      await act("", () => post(`/api/setup/addons/${encodeURIComponent(target.dataset.m)}/enabled${query(active)}`, { enabled: target.checked }));
    }
  });

  body.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-s]");
    if (!button) return;
    const kind = button.dataset.s;
    const module = button.dataset.m;
    if (kind === "retry") load();
    else if (kind === "rescan") await act("Проверяю список аддонов…", () => post(`/api/setup/rescan${query(active)}`));
    else if (kind === "done") {
      await act("", () => post("/api/setup/done", { skipped: false }));
      toast("Настройка сохранена");
    } else if (kind === "reset") {
      if (confirm(`Сбросить загруженные настройки для Blender ${active}? Аддоны, добавленные вручную, останутся.`)) {
        await act("Сбрасываю настройки…", () => api(`/api/setup/prefs${query(active)}`, { method: "DELETE" }));
      }
    } else if (kind === "remove") {
      const cur = current();
      const item = cur && cur.addons.find((a) => a.module === module);
      if (item && item.origin !== "missing" && !confirm(`Удалить аддон «${item.name}» для Blender ${active} с сервера?`)) return;
      await act("", () => api(`/api/setup/addons/${encodeURIComponent(module)}${query(active)}`, { method: "DELETE" }));
    }
  });

  banner.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-fr]");
    if (!button) return;
    if (button.dataset.fr === "open") {
      panel.open = true;
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      await act("", () => post("/api/setup/done", { skipped: true }));
      toast("Первую настройку можно пройти позже в разделе «Настройки Blender»");
    }
  });

  load();
}