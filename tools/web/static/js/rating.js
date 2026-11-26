// Rating gate: the owner must score a finished job before artifacts can be downloaded.
import { $, show } from "./utils.js";
import { message as m, setText } from "./i18n.js";
import * as api from "./api.js";
import { autoPublish } from "./library.js";

const DOWNLOADS = ["dlNpz", "dlFbx", "menuNpz"];
const TAG_SPLIT = /[,，、;；\n]+/;

// Free-form tags for the reference library: split, trim, dedupe, cap at 20 × 32 chars.
function parseTags(value) {
  const seen = new Set();
  for (const raw of value.split(TAG_SPLIT)) {
    const tag = raw.trim().replace(/\s+/g, " ").slice(0, 32);
    if (tag) seen.add(tag);
  }
  return [...seen].slice(0, 20);
}

export function initRating({ onSaved, capturePreview, captureMotion } = {}) {
  const dialog = $("ratingDialog"), stars = $("ratingStars");
  const tags = $("ratingTags");
  const comment = $("ratingComment"), error = $("ratingError");
  let job = null, score = 0, dirty = false;

  function paintStars() {
    for (const node of stars.children) node.classList.toggle("on", Number(node.dataset.score) <= score);
  }

  function setError(msg) {
    setText(error, msg);
    show(error, !!msg);
  }

  // Auto backup: prefer the character FBX baked from the current viewport, then
  // fall back to the server-generated motion when no character is applied.
  async function backup(id, preview) {
    let motion = null;
    try { motion = await captureMotion?.(); } catch { motion = null; }
    if (motion?.blob) {
      try { await api.uploadLibraryMotion(id, motion.blob, motion.filename); } catch { /* fall back */ }
    }
    await autoPublish(id, preview);
  }

  function open() {
    if (!job || job.status !== "done") return;
    const rating = job.rating || {};
    score = rating.score || 0;
    tags.value = (rating.tags || []).join(", ");
    comment.value = rating.comment || "";
    dirty = false;
    paintStars();
    setError("");
    if (!dialog.open) dialog.showModal();
  }

  async function submit(event) {
    event.preventDefault();
    if (!job || !score) {
      setError(m("rating.score_required"));
      return;
    }
    const button = $("ratingSubmit");
    button.disabled = true;
    try {
      await api.saveRating(job.id, {
        score,
        tags: parseTags(tags.value),
        comment: comment.value.trim(),
      });
      dirty = true;
      const preview = capturePreview?.();
      dialog.close();
      void backup(job.id, preview);
      onSaved?.(job.id);
    } catch (err) {
      setError(err.message ? err : m("rating.save_failed"));
    } finally {
      button.disabled = false;
    }
  }

  stars.addEventListener("click", (event) => {
    const node = event.target.closest(".rating-star");
    if (!node) return;
    score = Number(node.dataset.score);
    paintStars();
    setError("");
  });
  $("ratingSubmit").addEventListener("click", submit);
  $("btnRate").addEventListener("click", open);
  dialog.addEventListener("close", () => { if (dirty) { dirty = false; } });

  // Anchor/export clicks are intercepted before the viewer reacts, so an unrated job
  // opens the rating dialog instead of a 409 download.
  for (const id of DOWNLOADS) {
    $(id)?.addEventListener("click", (event) => {
      if (!job || job.status !== "done" || job.rating) return;
      if (job.owner_id === "mcp") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      open();
    }, true);
  }

  function update(next) {
    job = next || null;
    const done = !!job && job.status === "done";
    show("btnRate", done);
    show("ratingPending", done && !job.rating);
    setText($("btnRate"), job?.rating?.score ? `★ ${job.rating.score}` : m("rating.rate"));
  }

  return { update, open };
}
