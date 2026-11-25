// Rating gate: the owner must score a finished job before artifacts can be downloaded.
import { $, el, show } from "./utils.js";
import { message as m, setText } from "./i18n.js";
import * as api from "./api.js";

const TAGS = ["quality", "hands", "physics", "camera", "clothing", "other"];
const DOWNLOADS = ["dlNpz", "dlFbx", "menuNpz"];

export function initRating({ onSaved } = {}) {
  const dialog = $("ratingDialog"), stars = $("ratingStars"), tags = $("ratingTags");
  const comment = $("ratingComment"), error = $("ratingError");
  let job = null, score = 0, dirty = false;

  for (const tag of TAGS) {
    const label = el("label"), box = el("input"), text = el("span");
    box.type = "checkbox";
    box.value = tag;
    setText(text, m(`rating.tag_${tag}`));
    label.append(box, text);
    tags.append(label);
  }

  function paintStars() {
    for (const node of stars.children) node.classList.toggle("on", Number(node.dataset.score) <= score);
  }

  function setError(msg) {
    setText(error, msg);
    show(error, !!msg);
  }

  function open() {
    if (!job || job.status !== "done") return;
    const rating = job.rating || {};
    score = rating.score || 0;
    comment.value = rating.comment || "";
    for (const box of tags.querySelectorAll("input")) {
      box.checked = (rating.tags || []).includes(box.value);
    }
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
    const selected = [...tags.querySelectorAll("input:checked")].map((box) => box.value);
    const button = $("ratingSubmit");
    button.disabled = true;
    try {
      await api.saveRating(job.id, {
        score,
        tags: selected,
        comment: comment.value.trim(),
        allow_library: true,
      });
      dirty = true;
      dialog.close();
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
