import { $, esc, api, post, toast, fail } from "./util.js";

const ORIGIN = { missing: "не найден", user: "загружен", extension: "расширение", bundled: "встроенный" };

export function initSetup() {
  const panel = $("#setup");
  const body = $("#setupBody");
  const banner = $("#firstrun");
  let state = null;
  let sig = "";
  let timer = 0;
  let working = "";

  function rowHtml(item) {
    const meta = [item.module !== item.name ? esc(item.module) : "", item.version ? esc(item.version) : ""].filter(Boolean).join(" ");
    if (item.origin === "missing") {
      const text =
        item.status === "installing"
          ? "Скачиваю с extensions.blender.org"
          : item.status === "failed"
            ? `Не удалось скачать: ${esc(item.message)}. Скачайте архив на extensions.blender.org и загрузите его здесь.`
            : !state.extensions_supported && item.module.startsWith("bl_ext.")
              ? "Это расширение, для него нужен Blender 4.2 или новее."
              : "Нет на сервере. Загрузите архив аддона кнопкой «Добавить аддон».";
      return `<div class="addon${item.status === "failed" ? " bad" : ""}">
        <span><b>${esc(item.name)}</b> <span class="dim">${meta}</span><br><span class="${item.status === "failed" ? "alert" : "dim"}">${text}</span></span>
        <button type="button" class="btn ghost" data-s="remove" data-m="${esc(item.module)}">Убрать из списка</button>
      </div>`;
    }
    const removable = item.origin !== "bundled";
    return `<div class="addon">
      <label class="check"><input type="checkbox" data-s="toggle" data-m="${esc(item.module)}"${item.enabled ? " checked" : ""}>
        <span><b>${esc(item.name)}</b> <span class="dim">${meta} · ${ORIGIN[item.origin]}</span></span></label>
      ${removable ? `<button type="button" class="btn ghost" data-s="remove" data-m="${esc(item.module)}" aria-label="Удалить ${esc(item.name)}">Удалить</button>` : ""}
    </div>`;
  }

  function group(title, items, open = true) {
    if (!items.length) return "";
    const rows = `<div class="addons">${items.map(rowHtml).join("")}</div>`;
    if (open) return `<div class="group"><p class="dim">${title}</p>${rows}</div>`;
    return `<details class="group"><summary class="dim">${title}</summary>${rows}</details>`;
  }

  function render() {
    banner.hidden = !state.needs_setup;
    const next = JSON.stringify([state, working]);
    if (next === sig) return;
    sig = next;
    const addons = state.addons;
    const prefs = state.prefs
      ? `Настройки: <b>${esc(state.prefs.name)}</b>${state.blender ? `, прочитаны в Blender ${esc(state.blender)}` : ""}.`
      : "Настройки не загружены. Пока сервер не знает, какие аддоны вам нужны.";
    const busy = working || (state.busy ? "Сервер работает с Blender…" : "");
    const off = working ? " disabled" : "";
    body.innerHTML = `
      <p class="dim">Переносятся аддоны и их настройки из userpref.blend. Тема, раскладка и горячие клавиши на рендер не влияют, их сервер игнорирует. Аддоны, отмеченные здесь, включаются при каждом рендере.</p>
      ${state.has_blender ? "" : `<p class="alert">Сначала установите хотя бы одну версию Blender в разделе «Версии Blender»: без неё сервер не может прочитать настройки.</p>`}
      <p>${prefs}</p>
      <div class="row">
        <label class="btn solid"><span>Загрузить настройки</span><input type="file" accept=".blend,.zip" data-up="prefs" hidden${off}></label>
        <label class="btn"><span>Добавить аддон</span><input type="file" accept=".zip,.py" data-up="addon" hidden${off}></label>
        <button type="button" class="btn ghost" data-s="rescan"${off}>Проверить заново</button>
        ${state.prefs ? `<button type="button" class="btn ghost" data-s="reset"${off}>Сбросить настройки</button>` : ""}
      </div>
      ${busy ? `<p class="dim"><span class="tag t-run"><i></i><span>${esc(busy)}</span></span></p>` : ""}
      ${group("Нужно добавить", addons.filter((a) => a.origin === "missing"))}
      ${group("Аддоны с вашего компьютера", addons.filter((a) => a.origin === "user" || a.origin === "extension"))}
      ${group(`Встроенные в Blender (${addons.filter((a) => a.origin === "bundled").length})`, addons.filter((a) => a.origin === "bundled"), false)}
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
      if (!state) return;
    }
    schedule();
  }

  async function act(label, action) {
    working = label;
    if (state) render();
    try {
      state = await action();
    } catch (error) {
      fail(error);
    } finally {
      working = "";
      if (state) render();
      schedule();
    }
  }

  function sendFile(path, file, label) {
    const data = new FormData();
    data.append("file", file);
    return act(label, () => api(path, { method: "POST", body: data }));
  }

  panel.addEventListener("toggle", () => {
    if (panel.open) load();
  });

  body.addEventListener("change", async (event) => {
    const target = event.target;
    if (target.matches("[data-up]")) {
      const file = target.files[0];
      target.value = "";
      if (!file) return;
      if (target.dataset.up === "prefs") await sendFile("/api/setup/prefs", file, "Читаю настройки в Blender…");
      else await sendFile("/api/setup/addons", file, "Устанавливаю аддон и проверяю список…");
      toast(target.dataset.up === "prefs" ? "Настройки загружены" : "Аддон добавлен");
    } else if (target.matches("[data-s=toggle]")) {
      await act("", () => post(`/api/setup/addons/${encodeURIComponent(target.dataset.m)}/enabled`, { enabled: target.checked }));
    }
  });

  body.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-s]");
    if (!button) return;
    const kind = button.dataset.s;
    const module = button.dataset.m;
    if (kind === "rescan") await act("Проверяю список аддонов…", () => post("/api/setup/rescan"));
    else if (kind === "done") {
      await act("", () => post("/api/setup/done", { skipped: false }));
      toast("Настройка сохранена");
    } else if (kind === "reset") {
      if (confirm("Сбросить загруженные настройки? Аддоны, добавленные вручную, останутся.")) {
        await act("Сбрасываю настройки…", () => api("/api/setup/prefs", { method: "DELETE" }));
      }
    } else if (kind === "remove") {
      const item = state.addons.find((a) => a.module === module);
      if (item && item.origin !== "missing" && !confirm(`Удалить аддон «${item.name}» с сервера?`)) return;
      await act("", () => api(`/api/setup/addons/${encodeURIComponent(module)}`, { method: "DELETE" }));
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
