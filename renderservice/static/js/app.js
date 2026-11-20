import { GlyphField, ProgressStrip, TrackOverlay, scramble } from "./glyphs.js";
import { $, esc, fmtBytes, api, post, toast, fail } from "./util.js";
import { initSetup } from "./setup.js";

function fmtTime(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}ч ${String(m).padStart(2, "0")}м`;
  if (m) return `${m}м ${String(r).padStart(2, "0")}с`;
  return `${r}с`;
}

const STATUS = {
  needs_selection: ["выберите файл", "idle"],
  preflight: ["проверка", "run"],
  ready: ["готов", "idle"],
  queued: ["в очереди", "run"],
  rendering: ["рендер", "run"],
  done: ["готово", "done"],
  failed: ["ошибка", "failed"],
  cancelled: ["отменено", "idle"],
};

const EXPECTED_BUILD = "2026-11-20-series";
let installed = [];
let installedAt = 0;
const cards = new Map();
const listEl = $("#jobs");

async function loadInstalled() {
  try {
    installed = (await api("/api/blenders")).map((item) => item.version);
    installedAt = Date.now();
  } catch {}
}

function factsHtml(job) {
  const info = job.info;
  if (!info) return `<span>${esc(job.filename)}</span>`;
  const parts = [];
  parts.push(`<span>движок <b>${esc(info.engine)}</b></span>`);
  if (info.resolution) {
    const pct = info.resolution_percentage;
    parts.push(`<span>кадр <b>${info.resolution[0]}×${info.resolution[1]}${pct && pct !== 100 ? ` @ ${pct}%` : ""}</b></span>`);
  }
  if (info.samples != null) parts.push(`<span>сэмплы <b>${info.samples}</b></span>`);
  if (info.frame_end != null) {
    const step = info.frame_step || 1;
    const count = Math.floor((info.frame_end - info.frame_start) / step) + 1;
    parts.push(`<span>кадры <b>${info.frame_start}–${info.frame_end}</b> (${count})</span>`);
  }
  if (info.format) parts.push(`<span>формат <b>${esc(info.format)}</b></span>`);
  if (job.blender) parts.push(`<span>Blender <b>${esc(job.blender.version)}</b></span>`);
  if (job.addons && job.addons.length) parts.push(`<span>аддоны <b>${job.addons.length}</b></span>`);
  return parts.join("");
}

function notesHtml(job) {
  const lines = (job.warnings || []).map((text) => `<p>${esc(text)}</p>`);
  const missing = (job.info && job.info.missing_files) || [];
  if (missing.length) {
    const names = missing.slice(0, 5).map((item) => item.path || item.name).join(", ");
    const more = missing.length > 5 ? ` и ещё ${missing.length - 5}` : "";
    lines.push(`<p class="bad">Не найдено файлов: ${missing.length} (${esc(names)}${more})</p>`);
  }
  return lines.join("");
}

function actionsHtml(job, extra = "") {
  const parts = [extra];
  parts.push(`<button type="button" class="btn ghost" data-act="log">Лог</button>`);
  if (!["queued", "rendering", "preflight"].includes(job.status)) {
    parts.push(`<button type="button" class="btn ghost" data-act="delete">Удалить</button>`);
  }
  return `<div class="actions">${parts.join("")}</div>`;
}

function statHtml(label, bind) {
  return `<div class="stat"><dt>${label}</dt><dd data-${bind}>—</dd></div>`;
}

function selectOptions(items, current) {
  return items
    .map(([value, text]) => `<option value="${esc(value)}"${value === (current || "") ? " selected" : ""}>${esc(text)}</option>`)
    .join("");
}

function formHtml(job, rerender) {
  const id = job.id;
  const info = job.info || {};
  const preset = rerender ? job.options || {} : {};
  const missing = (info.missing_files || []).length;
  const defaultRange = `${info.frame_start ?? 1}:${info.frame_end ?? 1}`;
  const range = preset.frames ? `${preset.frames[0]}:${preset.frames[1]}` : defaultRange;
  const still = preset.mode ? preset.mode === "still" : job.suggested_mode === "still";
  const frame = preset.frame ?? info.frame_current ?? info.frame_start ?? 1;
  const allow = !!preset.allow_missing;
  return `<div class="ready">
    ${rerender ? `<p class="dim">Прежние результаты этой задачи будут удалены. Файлы проекта берутся с сервера, загружать их заново не нужно.</p>` : ""}
    <div class="row">
      <div class="field"><span>режим</span>
        <div class="seg">
          <label><input type="radio" name="mode-${id}" value="still"${still ? " checked" : ""}><span>Кадр</span></label>
          <label><input type="radio" name="mode-${id}" value="animation"${still ? "" : " checked"}><span>Анимация</span></label>
        </div>
      </div>
      <div class="field"><span>устройство</span>
        <select data-device>${selectOptions([["OPTIX", "OptiX"], ["CUDA", "CUDA"], ["CPU", "CPU"]], preset.device || "OPTIX")}</select>
      </div>
      <div class="field"><span>версия Blender</span>
        <select data-blender>${selectOptions([["", "как в проекте"], ...installed.map((v) => [v, v])], preset.blender)}</select>
      </div>
      <div class="field" data-f-still><span>номер кадра</span><input type="number" data-frame value="${frame}"></div>
      <div class="field" data-f-anim><span>диапазон кадров</span><input type="text" data-range value="${range}" data-default="${defaultRange}" spellcheck="false" autocomplete="off"></div>
    </div>
    ${missing ? `<label class="check"><input type="checkbox" data-missing${allow ? " checked" : ""}><span class="alert">Запустить без недостающих файлов (${missing})</span></label>` : ""}
    <div class="actions">
      <button type="button" class="btn solid" data-act="start"${missing && !allow ? " disabled" : ""}>${rerender ? "Рендерить заново" : "Запустить рендер"}</button>
      ${rerender ? `<button type="button" class="btn ghost" data-act="rerender-cancel">Отмена</button>` : `<button type="button" class="btn ghost" data-act="delete">Удалить</button>`}
    </div>
  </div>`;
}

function bodyHtml(card) {
  const job = card.job;
  const id = job.id;
  if (card.rerender && ["done", "failed", "cancelled"].includes(job.status)) return formHtml(job, true);
  const rerunButton = !job.project_deleted && job.info ? `<button type="button" class="btn" data-act="rerender">Рендерить заново</button>` : "";
  switch (job.status) {
    case "needs_selection":
      return `<p>В архиве несколько файлов .blend. Выберите, какой рендерить.</p>
        <div class="pick">${(job.candidates || [])
          .map((path) => `<button type="button" class="btn" data-act="select" data-path="${esc(path)}">${esc(path)}</button>`)
          .join("")}</div>
        <div class="actions"><button type="button" class="btn ghost" data-act="delete">Удалить</button></div>`;
    case "preflight":
      return `<p>Проверяю проект. Это занимает от нескольких секунд до минуты.</p>
        <div data-install hidden><p data-install-text></p><div class="dl"><i data-install-bar></i></div></div>
        ${actionsHtml(job)}`;
    case "ready":
      return formHtml(job, false);
    case "queued":
      return `<p>Позиция в очереди: <b data-pos>—</b>. Рендер начнётся, когда освободится сервер.</p>
        ${actionsHtml(job, `<button type="button" class="btn danger" data-act="cancel">Отменить</button>`)}`;
    case "rendering":
      return `<canvas class="strip" data-strip></canvas>
        <dl class="stats">${statHtml("готово", "pct")}${statHtml("кадры", "frames")}${statHtml("сэмплы", "sample")}${statHtml("осталось", "eta")}${statHtml("прошло", "elapsed")}</dl>
        <div class="shot"><img data-img alt="Последний готовый кадр" hidden><canvas data-track></canvas><span class="cap dim" data-cap></span></div>
        ${actionsHtml(job, `<button type="button" class="btn danger" data-act="cancel">Отменить</button>`)}`;
    case "done":
      return `<canvas class="strip" data-strip></canvas>
        <dl class="stats">${statHtml("кадры", "frames")}${statHtml("размер", "size")}${statHtml("время", "elapsed")}${statHtml("на кадр", "avg")}</dl>
        <div class="thumbs" data-thumbs></div>
        ${job.project_deleted ? `<p class="dim">Файлы проекта удалены с сервера. Чтобы рендерить заново, загрузите проект ещё раз.</p>` : ""}
        ${actionsHtml(job, `<a class="btn solid" href="/api/jobs/${id}/download">Скачать результат</a>${rerunButton}`)}`;
    case "failed":
    case "cancelled": {
      const canResume = job.info && job.options && job.options.mode && !job.project_deleted;
      const lines = Array.isArray(job.error_log) ? job.error_log.join("\n") : "";
      const text =
        job.status === "cancelled"
          ? "Рендер отменён. Уже готовые кадры сохранены."
          : job.error || "Рендер завершился с ошибкой.";
      return `<p class="${job.status === "failed" ? "alert" : "dim"}">${esc(text)}</p>
        ${job.info ? `<canvas class="strip" data-strip></canvas>` : ""}
        ${lines ? `<pre class="log">${esc(lines)}</pre>` : ""}
        ${job.project_deleted ? `<p class="dim">Файлы проекта уже удалены, продолжить нельзя. Загрузите проект заново.</p>` : ""}
        ${actionsHtml(job, (canResume ? `<button type="button" class="btn solid" data-act="resume">Продолжить</button>` : "") + rerunButton)}`;
    }
  }
  return "";
}

function releaseBody(card) {
  if (card.strip) card.strip.destroy();
  if (card.overlay) card.overlay.destroy();
  card.strip = null;
  card.overlay = null;
  card.imgKey = null;
}

function buildBody(card) {
  releaseBody(card);
  const body = $(".body", card.root);
  body.innerHTML = bodyHtml(card);
  const strip = $("[data-strip]", body);
  if (strip) card.strip = new ProgressStrip(strip, { cell: 14 });
  const track = $("[data-track]", body);
  if (track) {
    card.overlay = new TrackOverlay(track);
    card.overlay.setActive(true);
  }
  syncMode(card);
}

function syncMode(card) {
  const checked = $("input[type=radio]:checked", card.root);
  if (!checked) return;
  const still = checked.value === "still";
  $("[data-f-still]", card.root).hidden = !still;
  $("[data-f-anim]", card.root).hidden = still;
}

function setText(root, selector, value) {
  const node = $(selector, root);
  if (node && node.textContent !== value) node.textContent = value;
}

function updateBody(card) {
  const job = card.job;
  const body = $(".body", card.root);
  const p = job.progress || {};
  const modes = { rendering: "running", queued: "queued", done: "done", failed: "failed", cancelled: "cancelled" };
  if (card.strip) card.strip.set(job.status === "done" ? 1 : p.percent || 0, modes[job.status] || "idle");
  if (job.status === "preflight") {
    const install = job.install;
    const box = $("[data-install]", body);
    if (box) {
      box.hidden = !install;
      if (install) {
        const label =
          install.state === "extracting"
            ? `Распаковываю Blender ${install.version}`
            : install.state === "queued"
              ? `Blender ${install.version} ждёт очереди на скачивание`
              : `Скачиваю Blender ${install.version}: ${fmtBytes(install.downloaded)}${install.total ? ` из ${fmtBytes(install.total)}` : ""}`;
        setText(body, "[data-install-text]", label);
        const bar = $("[data-install-bar]", body);
        const share = install.total ? Math.min(100, (install.downloaded / install.total) * 100) : install.state === "extracting" ? 100 : 0;
        bar.style.width = `${share}%`;
      }
    }
  } else if (job.status === "queued") {
    setText(body, "[data-pos]", job.queue_position != null ? String(job.queue_position) : "—");
    const cancel = $("[data-act=cancel]", body);
    if (cancel) cancel.disabled = !!job.cancelling;
  } else if (job.status === "rendering") {
    setText(body, "[data-pct]", `${Math.floor((p.percent || 0) * 100)}%`);
    setText(body, "[data-frames]", `${p.frames_done ?? 0}/${p.total_frames ?? "?"}`);
    setText(body, "[data-sample]", p.sample != null ? `${p.sample}/${p.samples_total}` : "—");
    setText(body, "[data-eta]", fmtTime(p.eta_seconds));
    setText(body, "[data-elapsed]", fmtTime(p.elapsed_seconds));
    const cancel = $("[data-act=cancel]", body);
    if (cancel) {
      cancel.disabled = !!job.cancelling;
      cancel.textContent = job.cancelling ? "Останавливаю…" : "Отменить";
    }
    if (card.overlay && job.info && job.info.resolution) card.overlay.resolution = job.info.resolution;
    const img = $("[data-img]", body);
    const key = job.last_preview ? `${job.last_preview}:${job.previews_count}` : "";
    if (img && key !== card.imgKey) {
      card.imgKey = key;
      if (job.last_preview) {
        img.onload = () => (img.hidden = false);
        img.src = `/api/jobs/${job.id}/previews/${encodeURIComponent(job.last_preview)}?n=${job.previews_count}`;
      } else {
        img.hidden = true;
      }
    }
    const cap = p.current_frame != null ? `кадр ${p.current_frame}` : "ждём первый кадр";
    setText(body, "[data-cap]", job.last_preview ? `последний готовый кадр, сейчас рендерится ${cap}` : cap);
  } else if (job.status === "done") {
    const out = job.outputs || {};
    setText(body, "[data-frames]", String(out.count ?? p.total_frames ?? "—"));
    setText(body, "[data-size]", fmtBytes(out.bytes));
    setText(body, "[data-elapsed]", fmtTime(p.elapsed_seconds));
    setText(body, "[data-avg]", p.avg_frame_seconds != null ? fmtTime(p.avg_frame_seconds) : "—");
    if (job.previews_count !== card.detailCount) {
      card.detailCount = job.previews_count;
      api(`/api/jobs/${job.id}`)
        .then((detail) => {
          const box = $("[data-thumbs]", card.root);
          if (!box) return;
          box.innerHTML = (detail.previews || [])
            .map((name) => {
              const url = `/api/jobs/${job.id}/previews/${encodeURIComponent(name)}`;
              return `<a href="${url}" target="_blank" rel="noopener"><img loading="lazy" src="${url}" alt="${esc(name)}"></a>`;
            })
            .join("");
        })
        .catch(() => {});
    }
  }
}

async function refreshLog(card) {
  const pre = $("pre.log[data-live]", card.root);
  if (!pre) return;
  try {
    const res = await fetch(`/api/jobs/${card.id}/log?lines=200`);
    if (!res.ok) return;
    const text = await res.text();
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = text || "Лог пока пуст";
    if (stick) pre.scrollTop = pre.scrollHeight;
  } catch {}
}

function createCard(job) {
  const root = document.createElement("article");
  root.className = "job";
  root.dataset.id = job.id;
  root.innerHTML = `<i class="c1">+</i><i class="c2">+</i>
    <div class="job-top"><h3></h3><span class="tag"></span></div>
    <div class="facts"></div><div class="notes"></div><div class="body"></div>
    <pre class="log" data-live hidden></pre>`;
  const card = {
    id: job.id,
    root,
    job,
    sig: null,
    strip: null,
    overlay: null,
    logOpen: false,
    detailCount: -1,
    tag: null,
    facts: null,
    notes: null,
    imgKey: null,
    rerender: false,
  };
  const title = $("h3", root);
  title.textContent = job.name;
  scramble(title, job.name, 600);
  root.addEventListener("click", (event) => onClick(card, event));
  root.addEventListener("change", (event) => onChange(card, event));
  return card;
}

function updateCard(card, job) {
  card.job = job;
  const root = card.root;
  root.classList.toggle("is-failed", job.status === "failed");
  const [label, kind] = STATUS[job.status] || [job.status, "idle"];
  if (card.tag !== job.status) {
    card.tag = job.status;
    card.rerender = false;
    const tag = $(".tag", root);
    tag.className = `tag t-${kind}`;
    tag.innerHTML = `<i></i><span></span>`;
    scramble($("span", tag), label, 420);
  }
  const facts = factsHtml(job);
  if (facts !== card.facts) {
    card.facts = facts;
    $(".facts", root).innerHTML = facts;
  }
  const notes = notesHtml(job);
  if (notes !== card.notes) {
    card.notes = notes;
    $(".notes", root).innerHTML = notes;
  }
  const sig = job.status + "|" + (job.status === "needs_selection" ? (job.candidates || []).join(",") : "") + "|" + (job.status === "failed" ? job.error || "" : "") + "|" + (job.project_deleted ? 1 : 0) + "|" + (card.rerender ? 1 : 0);
  if (sig !== card.sig) {
    card.sig = sig;
    card.detailCount = -1;
    buildBody(card);
  }
  updateBody(card);
  if (card.logOpen && ["preflight", "queued", "rendering"].includes(job.status)) refreshLog(card);
}

function removeCard(card) {
  releaseBody(card);
  card.root.remove();
  cards.delete(card.id);
}

function sync(jobs) {
  const seen = new Set();
  jobs.forEach((job, index) => {
    seen.add(job.id);
    let card = cards.get(job.id);
    if (!card) {
      card = createCard(job);
      cards.set(job.id, card);
    }
    updateCard(card, job);
    const at = listEl.children[index];
    if (at !== card.root) listEl.insertBefore(card.root, at || null);
  });
  for (const card of [...cards.values()]) if (!seen.has(card.id)) removeCard(card);
  let empty = $(".empty", listEl);
  if (!jobs.length && !empty) {
    empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Задач пока нет. Загрузите .blend или .zip выше.";
    listEl.append(empty);
  } else if (jobs.length && empty) {
    empty.remove();
  }
  $("#count").textContent = jobs.length ? `${jobs.length}` : "";
}

function parseRange(text) {
  const match = /^\s*(\d+)\s*[:\-–]\s*(\d+)\s*$/.exec(text);
  if (!match) return null;
  const a = Number(match[1]);
  const b = Number(match[2]);
  return a <= b ? [a, b] : null;
}

async function startJob(card) {
  const root = card.root;
  const mode = $("input[type=radio]:checked", root).value;
  const body = {
    mode,
    device: $("[data-device]", root).value,
    blender: $("[data-blender]", root).value || null,
    allow_missing: !!($("[data-missing]", root) && $("[data-missing]", root).checked),
  };
  if (mode === "still") {
    const frame = parseInt($("[data-frame]", root).value, 10);
    if (!Number.isNaN(frame)) body.frame = frame;
  } else {
    const input = $("[data-range]", root);
    if (input.value.trim() !== input.dataset.default) {
      const range = parseRange(input.value);
      if (!range) {
        toast("Диапазон кадров записывается как 1:100", true);
        return;
      }
      body.frames = range;
    }
  }
  await post(`/api/jobs/${card.id}/start`, body);
}

async function onClick(card, event) {
  const button = event.target.closest("[data-act]");
  if (!button || button.tagName === "A") return;
  const act = button.dataset.act;
  if (act === "rerender" || act === "rerender-cancel") {
    card.rerender = act === "rerender";
    updateCard(card, card.job);
    return;
  }
  try {
    if (act === "log") {
      card.logOpen = !card.logOpen;
      const pre = $("pre.log[data-live]", card.root);
      pre.hidden = !card.logOpen;
      button.textContent = card.logOpen ? "Скрыть лог" : "Лог";
      if (card.logOpen) refreshLog(card);
      return;
    }
    button.disabled = true;
    if (act === "select") await post(`/api/jobs/${card.id}/select`, { path: button.dataset.path });
    else if (act === "start") await startJob(card);
    else if (act === "cancel") await post(`/api/jobs/${card.id}/cancel`);
    else if (act === "resume") await post(`/api/jobs/${card.id}/start`, { ...card.job.options, resume: true });
    else if (act === "delete") {
      if (!confirm(`Удалить задачу «${card.job.name}» вместе с результатами?`)) {
        button.disabled = false;
        return;
      }
      await api(`/api/jobs/${card.id}`, { method: "DELETE" });
      removeCard(card);
      toast("Задача удалена");
      return;
    }
    await tick(true);
  } catch (error) {
    fail(error);
    button.disabled = false;
  }
}

function onChange(card, event) {
  const target = event.target;
  if (target.matches("input[type=radio]")) syncMode(card);
  if (target.matches("[data-missing]")) $("[data-act=start]", card.root).disabled = !target.checked;
}

const bar = $("#bar");
let barSig = "";

function renderBar(status) {
  let html;
  if (!status) {
    html = `<span class="alert"><i class="live"></i>Нет связи с сервером</span>`;
  } else {
    html = status.rendering
      ? `<span><i class="live on"></i><b>Рендерится:</b> ${esc(status.rendering)}</span>`
      : `<span><i class="live"></i><b>Сервер свободен</b></span>`;
    html += `<span class="dim">в очереди: <b>${status.queued}</b></span>`;
    html += `<span class="dim">свободно на диске: <b>${fmtBytes(status.disk_free)}</b></span>`;
  }
  if (html !== barSig) {
    barSig = html;
    bar.innerHTML = html;
  }
}

function versionNotice(status) {
  let notice = $("#buildNotice");
  const stale = status.build !== EXPECTED_BUILD;
  if (stale && !notice) {
    notice = document.createElement("p");
    notice.id = "buildNotice";
    notice.className = "alert";
    notice.setAttribute("role", "alert");
    $(".bar .wrap").append(notice);
  }
  if (notice) {
    notice.hidden = !stale;
    notice.textContent = `Сервер работает на старой версии кода (${status.build || "неизвестно"}, страница ждёт ${EXPECTED_BUILD}). Перезапустите службу: systemctl --user restart blender-render`;
  }
}

let timer = 0;
let polling = false;

async function tick(now = false) {
  if (polling) return;
  polling = true;
  clearTimeout(timer);
  let active = false;
  try {
    if (Date.now() - installedAt > 15000) await loadInstalled();
    const [jobs, status] = await Promise.all([api("/api/jobs"), api("/api/status")]);
    sync(jobs);
    renderBar(status);
    versionNotice(status);
    if (status.auth && !$("#logout")) $(".foot .wrap").insertAdjacentHTML("beforeend", ` <a id="logout" href="/logout">Выйти</a>`);
    active = jobs.some((job) => ["preflight", "queued", "rendering"].includes(job.status));
  } catch {
    renderBar(null);
  } finally {
    polling = false;
    timer = setTimeout(tick, document.hidden ? 6000 : active ? 1200 : 3000);
  }
}

const drop = $("#drop");
const dropText = $("#dropText");
const upbar = $("#upbar");
const defaultDropText = dropText.textContent;
let uploading = false;
let currentUpload = null;
const upcancel = $("#upcancel");

function resetDrop() {
  uploading = false;
  currentUpload = null;
  upcancel.hidden = true;
  drop.classList.remove("busy", "over");
  dropText.textContent = defaultDropText;
  upbar.style.width = "0";
}

function upload(file) {
  if (uploading) {
    toast("Дождитесь окончания текущей загрузки", true);
    return;
  }
  if (!/\.(blend|zip)$/i.test(file.name)) {
    toast("Нужен файл .blend или .zip", true);
    return;
  }
  uploading = true;
  drop.classList.add("busy");
  dropText.textContent = `Загружаю ${file.name}`;
  const data = new FormData();
  data.append("file", file);
  const xhr = new XMLHttpRequest();
  currentUpload = xhr;
  upcancel.hidden = false;
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    upbar.style.width = `${(event.loaded / event.total) * 100}%`;
    dropText.textContent = `Загружаю ${file.name}: ${fmtBytes(event.loaded)} из ${fmtBytes(event.total)}`;
  };
  xhr.onload = () => {
    if (xhr.status === 201) {
      toast("Проект загружен");
      tick(true);
    } else {
      let detail = xhr.statusText;
      try {
        detail = JSON.parse(xhr.responseText).detail || detail;
      } catch {}
      toast(`Не удалось загрузить: ${detail}`, true);
    }
    resetDrop();
  };
  xhr.onabort = () => {
    toast("Загрузка отменена");
    resetDrop();
  };
  xhr.onerror = () => {
    toast("Не удалось загрузить: нет связи с сервером", true);
    resetDrop();
  };
  xhr.send(data);
}

$("#file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (file) upload(file);
});

upcancel.addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  if (currentUpload) currentUpload.abort();
});

$("#settingsFile").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (file) document.dispatchEvent(new CustomEvent("settings-file", { detail: file }));
});

for (const name of ["dragover", "drop"]) window.addEventListener(name, (event) => event.preventDefault());
drop.addEventListener("dragover", () => drop.classList.add("over"));
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (event) => {
  drop.classList.remove("over");
  const file = event.dataTransfer.files[0];
  if (file) upload(file);
});

const vers = $("#vers");
const versBody = $("#versBody");
let versTimer = 0;
let versSig = "";

function downloadLabel(state) {
  if (state.state === "extracting") return "распаковка";
  if (state.state === "queued") return "в очереди";
  return `${fmtBytes(state.downloaded)}${state.total ? ` из ${fmtBytes(state.total)}` : ""}`;
}

function renderVers(data) {
  const active = data.downloads.filter((d) => d.state !== "failed" && d.state !== "done");
  const failed = data.downloads.filter((d) => d.state === "failed");
  const have = new Set(data.installed.map((item) => item.version));
  const sig = JSON.stringify([data.installed, data.catalog.map((e) => e.latest), active.map((d) => [d.version, d.state]), failed.map((d) => [d.version, d.error]), data.error, data.root]);
  if (sig !== versSig) {
    versSig = sig;
    const chips = data.installed.length
      ? data.installed
          .map(
            (item) =>
              `<span class="chip">${esc(item.version)}<button type="button" data-v="remove" data-ver="${esc(item.version)}" aria-label="Удалить Blender ${esc(item.version)}">×</button></span>`
          )
          .join("")
      : `<span class="dim">Пока ничего не установлено.</span>`;
    const cat = data.catalog
      .map((entry) => {
        const download = active.find((d) => d.version === entry.latest);
        const error = failed.find((d) => d.version === entry.latest);
        let action;
        if (have.has(entry.latest)) action = `<span class="dim">установлена</span>`;
        else if (download)
          action = `<div class="dl"><i data-dlbar="${esc(entry.latest)}"></i></div><span class="dim" data-dltext="${esc(entry.latest)}"></span>`;
        else
          action =
            (error ? `<span class="alert">${esc(error.error || "не удалось скачать")}</span>` : "") +
            `<button type="button" class="btn" data-v="get" data-ver="${esc(entry.latest)}">Скачать ${esc(entry.latest)}</button>`;
        return `<div class="cat"><b>Blender ${esc(entry.series)}</b>${action}</div>`;
      })
      .join("");
    versBody.innerHTML = `<p class="dim">Папка: ${esc(data.root)}. Автоскачивание нужной версии при загрузке проекта: ${data.auto_install ? "включено" : "выключено"}.</p>
      <div class="chips">${chips}</div>
      <div class="row"><button type="button" class="btn ghost" data-v="refresh">Обновить список</button></div>
      ${data.error ? `<p class="alert">${esc(data.error)}</p>` : ""}
      <div class="catalog">${cat}</div>`;
  }
  for (const d of active) {
    const barNode = $(`[data-dlbar="${CSS.escape(d.version)}"]`, versBody);
    if (!barNode) continue;
    const share = d.total ? Math.min(100, (d.downloaded / d.total) * 100) : d.state === "extracting" ? 100 : 0;
    barNode.style.width = `${share}%`;
    setText(versBody, `[data-dltext="${CSS.escape(d.version)}"]`, downloadLabel(d));
  }
  installed = data.installed.map((item) => item.version);
  installedAt = Date.now();
}

async function loadVers(refresh = false) {
  clearTimeout(versTimer);
  try {
    renderVers(await api(`/api/blenders/catalog${refresh ? "?refresh=true" : ""}`));
  } catch (error) {
    versBody.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
    versSig = "";
  }
  if (vers.open) versTimer = setTimeout(loadVers, 2500);
}

vers.addEventListener("toggle", () => {
  if (vers.open) {
    if (!versBody.innerHTML) versBody.innerHTML = `<p class="dim">Загружаю список версий…</p>`;
    loadVers();
  } else {
    clearTimeout(versTimer);
  }
});

versBody.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-v]");
  if (!button) return;
  const kind = button.dataset.v;
  try {
    if (kind === "refresh") {
      button.disabled = true;
      versBody.insertAdjacentHTML("afterbegin", `<p class="dim">Обновляю список…</p>`);
      versSig = "";
      await loadVers(true);
      return;
    }
    if (kind === "get") {
      button.disabled = true;
      await post("/api/blenders/download", { version: button.dataset.ver });
      toast(`Скачиваю Blender ${button.dataset.ver}`);
    } else if (kind === "remove") {
      if (!confirm(`Удалить Blender ${button.dataset.ver} с диска?`)) return;
      await api(`/api/blenders/${encodeURIComponent(button.dataset.ver)}`, { method: "DELETE" });
      toast(`Blender ${button.dataset.ver} удалён`);
    }
    versSig = "";
    await loadVers();
  } catch (error) {
    fail(error);
    versSig = "";
    loadVers();
  }
});

new GlyphField($("#field"), { cell: 14, seed: 3 });
const title = $(".hero h1");
scramble(title, title.textContent, 800);
loadInstalled().then(() => tick());
initSetup();