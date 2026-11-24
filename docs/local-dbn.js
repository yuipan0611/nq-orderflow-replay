"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const panel = $("local-importer");
  const form = $("dbn-form");
  const fileInput = $("dbn-file");
  const date = $("dbn-date");
  const contract = $("dbn-symbol");
  const start = $("dbn-start");
  const end = $("dbn-end");
  const load = $("load-local");
  const find = $("find-window");
  const clear = $("clear-file");
  const example = $("load-example");
  const classroom = typeof NQ_CLASSROOM_SAMPLE === "undefined" ? null : NQ_CLASSROOM_SAMPLE;
  const status = $("import-status");
  let worker;
  let workerReady = false;
  let file = null;
  let usingExample = false;
  let metadata = null;
  let pending = null;
  let jobId = 0;
  const messages = { inspect: "Reading file metadata", suggest: "Finding a window with trades", replay: "Reading the selected window" };

  function showStatus(message = "", error = false) {
    status.textContent = message;
    status.toggleAttribute("data-error", error);
  }

  function enableControls() {
    fileInput.disabled = !workerReady;
    date.disabled = !metadata;
    contract.disabled = !metadata || contract.options.length === 0;
    start.disabled = end.disabled = !contract.value;
    find.disabled = !workerReady || !contract.value || Boolean(pending);
    load.disabled = find.disabled || !start.value || !end.value;
    clear.hidden = !file;
    example.disabled = !workerReady || !classroom;
  }

  function displayContracts() {
    const selected = contract.value;
    const day = Number(date.value.replaceAll("-", ""));
    const symbols = Object.keys(metadata?.contracts ?? {}).filter(symbol =>
      metadata.contracts[symbol].some(({ from, until }) => from <= day && day < until)).sort();
    contract.replaceChildren(...symbols.map(symbol => new Option(symbol, symbol)));
    if (symbols.includes(selected)) contract.value = selected;
    if (symbols.length > 1 && !symbols.includes(selected)) {
      contract.prepend(new Option("Choose a contract", ""));
      contract.value = "";
    }
  }

  function cancelJob() {
    if (pending) worker?.postMessage({ kind: "cancel" });
    pending = null;
  }

  function run(kind) {
    cancelJob();
    if (kind === "replay") loadNqReplay(null);
    pending = { id: ++jobId, kind };
    worker.postMessage({ ...pending, file, date: date.value, symbol: contract.value,
      start: kind === "replay" ? start.value : undefined,
      end: kind === "replay" ? end.value : undefined });
    showStatus(messages[kind] + "…");
    enableControls();
  }

  function exampleHint() {
    return usingExample ? "Synthetic example loaded. Available window " + classroom.start + "–" + classroom.end + " UTC." : "";
  }

  function chooseFile(next, synthetic = false) {
    cancelJob();
    file = next;
    usingExample = synthetic;
    metadata = null;
    date.value = start.value = end.value = "";
    displayContracts();
    loadNqReplay(null);
    showStatus();
    enableControls();
    if (file && workerReady) run("inspect");
  }

  panel.hidden = false;
  try {
    // A blob worker inherits this page's connect-src policy. Only static code URLs are included.
    const scripts = ["vendor/fzstd-0.1.1.js", "local-dbn-worker.js"]
      .map(path => JSON.stringify(new URL(path, location.href).href)).join(", ");
    const workerUrl = URL.createObjectURL(new Blob([`importScripts(${scripts});`], { type: "text/javascript" }));
    try {
      worker = new Worker(workerUrl);
    } finally {
      URL.revokeObjectURL(workerUrl);
    }
  } catch {
    showStatus("This browser cannot start the local file reader.", true);
    fileInput.disabled = true;
    return;
  }
  enableControls();
  worker.addEventListener("error", () => {
    worker.terminate();
    worker = null;
    workerReady = false;
    pending = null;
    enableControls();
    showStatus("The local file reader could not start. Reload this page to try again.", true);
  });
  worker.addEventListener("message", ({ data }) => {
    if (data.kind === "ready") {
      workerReady = true;
      enableControls();
      return;
    }
    if (!pending || pending.id !== data.id) return;
    const { kind } = pending;
    if (data.kind === "progress") {
      showStatus(messages[kind] + "… " + data.percent + "%");
      return;
    }
    pending = null;
    if (data.kind === "error") {
      showStatus(data.message, true);
      enableControls();
      return;
    }
    const result = data.result;
    if (kind === "inspect") {
      metadata = result;
      date.min = result.firstDate;
      date.max = result.lastDate ?? "";
      date.value = result.firstDate;
      displayContracts();
      if (usingExample) {
        start.value = classroom.start;
        end.value = classroom.defaultEnd;
      }
      showStatus(exampleHint() || (contract.options.length ? "Choose a contract and UTC time range." : "Choose a date with an NQ contract."));
    } else if (kind === "suggest") {
      start.value = result.start;
      end.value = result.end;
      showStatus("Suggested window with trades. Select Load replay to open it.");
    } else {
      try {
        loadNqReplay({ ...result, synthetic: usingExample });
        const count = result.trades.length;
        showStatus(count + (count === 1 ? " trade in this window." : " trades in this window."));
        $("play").focus({ preventScroll: true });
        $("replay-panel").scrollIntoView({ block: "start" });
      } catch {
        loadNqReplay(null);
        showStatus("Could not draw the replay on this browser.", true);
      }
    }
    enableControls();
  });
  fileInput.addEventListener("change", () => chooseFile(fileInput.files[0] ?? null));
  example.addEventListener("click", () => {
    const bytes = Uint8Array.from(atob(classroom.compressed), character => character.charCodeAt(0));
    fileInput.value = "";
    chooseFile(new File([bytes], classroom.filename, { type: "application/zstd" }), true);
  });
  clear.addEventListener("click", () => {
    fileInput.value = "";
    chooseFile(null);
  });
  date.addEventListener("change", () => {
    cancelJob();
    displayContracts();
    loadNqReplay(null);
    showStatus(contract.options.length ? "" : "No NQ contracts are mapped to this UTC date.", !contract.options.length);
    enableControls();
  });
  contract.addEventListener("change", () => {
    cancelJob();
    loadNqReplay(null);
    showStatus(exampleHint());
    enableControls();
  });
  for (const input of [start, end]) input.addEventListener("input", () => {
    if (pending) {
      cancelJob();
      showStatus();
    }
    enableControls();
  });
  find.addEventListener("click", () => {
    if (!find.disabled && date.reportValidity()) run("suggest");
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!load.disabled) run("replay");
  });
})();
