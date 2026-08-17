/* One-screen mode: upload, wait, download. No settings anywhere. */

(() => {
  "use strict";

  const CHUNK = 8 * 1024 * 1024;
  const $ = (id) => document.getElementById(id);
  const show = (el, on = true) => el.classList.toggle("hidden", !on);

  let jobId = null;
  let poll = null;
  let lastPath = null;      // kept so "Compress anyway" needs no re-upload

  // The three strings the status line is allowed to show, keyed by the phase
  // names the existing /api/status endpoint already returns.
  const PHASE_TEXT = {
    queued: "Analyzing…",
    analyzing: "Analyzing…",
    searching: "Finding the best quality…",
    segmenting: "Finding the best quality…",
    encoding: "Compressing…",
    done: "Compressing…",
  };

  async function api(url, options) {
    const response = await fetch(url, options);
    let body = {};
    try { body = await response.json(); } catch { /* empty */ }
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  }

  function reset() {
    if (poll) { clearInterval(poll); poll = null; }
    jobId = null;
    lastPath = null;
    $("file-input").value = "";
    show($("state-working"), false);
    show($("state-idle"), true);
    show($("working"), true);
    show($("done"), false);
    show($("refused"), false);
    show($("failed"), false);
    $("simple-fill").style.width = "0%";
  }

  function fail(message) {
    if (poll) { clearInterval(poll); poll = null; }
    show($("working"), false);
    $("simple-error").textContent = message;
    show($("failed"), true);
  }

  async function start(file) {
    if (!file) return;
    $("simple-filename").textContent = file.name;
    show($("state-idle"), false);
    show($("state-working"), true);
    show($("working"), true);
    show($("done"), false);
    show($("refused"), false);
    show($("failed"), false);
    $("simple-status").textContent = "Uploading…";
    $("simple-fill").style.width = "0%";

    try {
      // Same resumable upload path the advanced UI uses.
      const created = await api("/api/upload/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, totalSize: file.size }),
      });

      let offset = created.offset || 0;
      while (offset < file.size) {
        const response = await fetch(`/api/upload/${created.upload_id}`, {
          method: "PATCH",
          headers: {
            "Upload-Offset": String(offset),
            "Content-Type": "application/octet-stream",
          },
          body: file.slice(offset, offset + CHUNK),
        });
        if (!response.ok) throw new Error("Upload failed. Please try again.");
        offset = (await response.json()).offset;
        // Upload occupies the first quarter of the bar.
        $("simple-fill").style.width = `${(offset / file.size) * 25}%`;
      }

      const job = await api("/api/simple-compress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: created.upload_id }),
      });
      jobId = job.job_id;
      $("simple-status").textContent = "Analyzing…";
      poll = setInterval(check, 800);
      check();
    } catch (error) {
      fail(error.message);
    }
  }

  async function check() {
    if (!jobId) return;
    let job;
    try {
      job = await api(`/api/status/${jobId}`);
    } catch (error) {
      fail(error.message);
      return;
    }

    // Remaining three quarters of the bar belong to the job itself.
    $("simple-fill").style.width = `${25 + job.percent * 0.75}%`;
    $("simple-status").textContent = PHASE_TEXT[job.phase] || "Compressing…";

    if (job.status === "done") {
      clearInterval(poll); poll = null;
      const result = job.result;
      show($("working"), false);
      $("simple-result").textContent =
        `Reduced by ${result.reduction_percent.toFixed(0)}%` +
        (result.vmaf ? ` (VMAF ${result.vmaf.toFixed(0)})` : "");

      // Disclosure, not settings: state what happened, offer no choice about it.
      const notes = [];
      if (job.used_hardware) {
        notes.push("Compressed quickly using hardware acceleration — a software "
                 + "pass would be smaller for the same quality.");
      }
      if (job.container_changed) {
        notes.push("Saved as .mp4 — your original format doesn't support this codec.");
      }
      $("simple-note").textContent = notes.join(" ");
      show($("simple-note"), notes.length > 0);
      show($("done"), true);
    } else if (job.status === "no_headroom") {
      // The silent retry at a lower floor already happened server-side, so this
      // is a real refusal, not an ambitious first guess.
      clearInterval(poll); poll = null;
      show($("working"), false);
      show($("refused"), true);
    } else if (job.status === "error" || job.status === "cancelled") {
      fail(job.error || "Something went wrong. Please try again.");
    }
  }

  async function forceCompress() {
    try {
      const job = await api(`/api/retry/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      });
      jobId = job.job_id;
      show($("refused"), false);
      show($("working"), true);
      $("simple-status").textContent = "Compressing…";
      poll = setInterval(check, 800);
      check();
    } catch (error) {
      fail(error.message);
    }
  }

  function init() {
    const zone = $("drop-zone");
    const input = $("file-input");

    zone.addEventListener("click", () => input.click());
    input.addEventListener("change", (e) => start(e.target.files[0]));

    ["dragenter", "dragover"].forEach((t) =>
      zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((t) =>
      zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
    zone.addEventListener("drop", (e) => start(e.dataTransfer.files[0]));
    ["dragover", "drop"].forEach((t) =>
      window.addEventListener(t, (e) => e.preventDefault()));

    $("simple-download").addEventListener("click", () => {
      if (jobId) window.location.href = `/api/download/${jobId}`;
    });
    $("simple-force").addEventListener("click", forceCompress);
    ["simple-again", "simple-again-2", "simple-again-3"].forEach((id) =>
      $(id).addEventListener("click", reset));
  }

  document.addEventListener("DOMContentLoaded", init);
})();
