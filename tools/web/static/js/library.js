// Public reference library: browse published resources; finished jobs are backed up automatically.
import { $, el, show } from "./utils.js";
import { message as m, t, setText } from "./i18n.js";
import * as api from "./api.js";

const PAGE_SIZE = 24;
const ITEM_KEYS = ["items", "list", "resources", "rows"];

const items = (data) => {
  if (Array.isArray(data)) return data;
  for (const key of ITEM_KEYS) if (Array.isArray(data?.[key])) return data[key];
  return [];
};

const resourceId = (item) => item?.resource_id ?? item?.id ?? "";

// Best-effort backup: the backend re-checks every publish gate and records the outcome.
export async function autoPublish(jobId, preview) {
  try {
    await api.publishLibraryJob({ job_id: jobId, consent: true, ...(preview ? { preview } : {}) });
  } catch { /* a failed backup must never block the rating flow */ }
}

export function initLibrary() {
  const dialog = $("libraryDialog");
  if (!dialog) return { reset() {} };
  const grid = $("libraryGrid"), empty = $("libraryEmpty"), status = $("libraryStatus");
  const keyword = $("libraryKeyword"), media = $("libraryMedia");
  const detail = $("libraryDetail");
  let page = 1, total = 0, search = "", mediaType = "";
  let browseLoaded = false, busy = false;

  function setStatus(node, value, isError = false) {
    setText(node, value || "");
    show(node, !!value);
    node?.classList.toggle("error", isError);
  }

  async function loadConfig() {
    try {
      return await api.getLibraryConfig();
    } catch {
      return null;
    }
  }

  function renderItems(rows) {
    grid.replaceChildren();
    show(empty, rows.length === 0);
    for (const item of rows) {
      const id = resourceId(item);
      const card = el("button");
      card.type = "button";
      card.className = "library-card";
      card.dataset.resourceId = String(id);
      const thumb = el("img");
      thumb.loading = "lazy";
      thumb.alt = item.name || `#${id}`;
      thumb.src = api.libraryFileUrl(id, "image");
      thumb.onerror = () => { thumb.removeAttribute("src"); card.classList.add("broken"); };
      const caption = el("span", "library-card-name", item.name || `#${id}`);
      const meta = el("span", "library-card-meta", item.media_type || "");
      card.append(thumb, caption, meta);
      card.addEventListener("click", () => openDetail(id));
      grid.append(card);
    }
  }

  function paintPager() {
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    setText($("libraryPage"), t("library.page", { page, pages }));
    $("libraryPrev").disabled = page <= 1;
    $("libraryNext").disabled = page >= pages;
  }

  async function loadPage(target = 1) {
    if (busy) return;
    busy = true;
    setStatus(status, m("library.loading"));
    const params = { page: target, page_size: PAGE_SIZE };
    if (search) params.keyword = search;
    if (mediaType) params.media_type = mediaType;
    try {
      const data = await api.getLibraryResources(params);
      const rows = items(data);
      page = target;
      total = Number(data?.total ?? data?.count ?? data?.data?.total ?? rows.length) || rows.length;
      renderItems(rows);
      paintPager();
      setStatus(status, rows.length ? "" : m("library.empty"));
    } catch (error) {
      setStatus(status, error?.i18n || error?.message || m("library.load_failed"), true);
    } finally {
      busy = false;
    }
  }

  function openBrowse() {
    if (!dialog.open) dialog.showModal();
    if (browseLoaded) return;
    browseLoaded = true;
    void loadConfig().then((data) => {
      if (data && !data.enabled) {
        setStatus(status, m("library.disabled_hint"), true);
        return;
      }
      void loadPage(1);
    });
  }

  async function openDetail(id) {
    detail.dataset.resourceId = String(id);
    const preview = $("libraryDetailPreview");
    const title = $("libraryDetailTitle");
    const meta = $("libraryDetailMeta");
    const download = $("libraryDetailDownload");
    preview.replaceChildren();
    setText(title, `#${id}`);
    setText(meta, m("library.loading"));
    download.href = api.libraryFileUrl(id, "video");
    show(detail, true);
    try {
      const data = await api.getLibraryResource(id);
      const item = data && !Array.isArray(data) ? data : {};
      setText(title, item.name || `#${id}`);
      const counts = item.child_ids && typeof item.child_ids === "object"
        ? Object.entries(item.child_ids).filter(([, value]) => Array.isArray(value) && value.length)
        : [];
      const lines = [
        `${m("library.field.id")}: ${resourceId(item)}`,
        `${m("library.field.type")}: ${item.media_type || "-"}`,
      ];
      if (counts.length) {
        lines.push(`${m("library.field.children")}: ` + counts.map(([key, value]) => `${key} ${value.length}`).join(", "));
      }
      if (item.rating) lines.push(`${m("library.field_rating")}: ${item.rating} ★`);
      if (Array.isArray(item.tags) && item.tags.length) {
        lines.push(`${m("library.field_tags")}: ${item.tags.join(", ")}`);
      }
      const task = item.external_ref?.task_id;
      if (task) lines.push(`${m("library.field.task")}: ${task}`);
      if (item.description) lines.push(item.description);
      setText(meta, lines.join("\n"));
      const kind = item.media_type === "video" ? "video" : "image";
      download.href = api.libraryFileUrl(id, kind);
      if (kind === "video") {
        const video = el("video");
        video.controls = true;
        video.autoplay = true;
        video.muted = true;
        video.loop = true;
        video.src = api.libraryFileUrl(id, "video");
        preview.append(video);
      } else {
        const image = el("img");
        image.alt = item.name || `#${id}`;
        image.src = api.libraryFileUrl(id, "image");
        preview.append(image);
      }
    } catch (error) {
      setText(meta, error?.i18n || error?.message || m("library.load_failed"));
    }
  }

  $("menuLibrary")?.addEventListener("click", openBrowse);
  $("librarySearch")?.addEventListener("click", () => {
    search = keyword.value.trim();
    mediaType = media.value;
    void loadPage(1);
  });
  keyword?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    search = keyword.value.trim();
    mediaType = media.value;
    void loadPage(1);
  });
  media?.addEventListener("change", () => {
    mediaType = media.value;
    void loadPage(1);
  });
  $("libraryPrev")?.addEventListener("click", () => void loadPage(Math.max(1, page - 1)));
  $("libraryNext")?.addEventListener("click", () => void loadPage(page + 1));
  $("libraryClose")?.addEventListener("click", () => dialog.close());
  $("libraryDetailClose")?.addEventListener("click", () => {
    $("libraryDetailPreview").replaceChildren();
    show(detail, false);
  });

  function reset() {
    grid.replaceChildren();
    show(empty, false);
    setStatus(status, "");
    show(detail, false);
    $("libraryDetailPreview").replaceChildren();
    browseLoaded = false;
    page = 1;
    total = 0;
    search = "";
    mediaType = "";
    if (keyword) keyword.value = "";
    if (media) media.value = "";
    if (dialog.open) dialog.close();
  }

  return { reset, openBrowse };
}
