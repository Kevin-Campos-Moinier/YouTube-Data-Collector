// The dashboard's journeys, by real clicks in Chrome, on the in-memory YouTube. Run through tests/test_e2e.py.
// Usage: node flows.mjs <port> <port where YouTube runs out of quota> <port with no key yet>
// Environment: CHROME, RESET_FLAG (created to end the quota), ENV_FILE (the no-key server's .env), SHOTS (optional folder).
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [FULL, LOW, NOKEY] = process.argv.slice(2), SHOTS = process.env.SHOTS;
const profile = mkdtempSync(join(tmpdir(), "cdp-")), port = 9400 + Math.floor(Math.random() * 200);
const chrome = spawn(process.env.CHROME || "google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let url; for (let i = 0; i < 50 && !url; i++) { try { url = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === "page").webSocketDebuggerUrl; } catch { /* not up */ } await sleep(200); }
const ws = new WebSocket(url); await new Promise(r => { ws.onopen = r; });
let id = 0, acceptDialog = true, dialogs = 0; const pending = new Map(), jsErrors = [], badStatus = [];
const send = (method, params = {}) => new Promise(r => { const n = ++id; pending.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? { error: m.error }); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") jsErrors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") jsErrors.push(m.params.args.map(a => a.value ?? a.description).join(" "));
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error" && !/status of 400/.test(m.params.entry.text)) jsErrors.push(m.params.entry.text);
  if (m.method === "Network.responseReceived" && m.params.response.status >= 500) badStatus.push(`${m.params.response.status} ${m.params.response.url}`);
  if (m.method === "Page.javascriptDialogOpening") { dialogs++; send("Page.handleJavaScriptDialog", { accept: acceptDialog }); }
};
for (const d of ["Page", "Runtime", "Log", "Network"]) await send(d + ".enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false });
const ev = async expr => { const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || expr); return r.result.value; };
const until = async (expr, ms = 20000) => { const t = Date.now(); while (Date.now() - t < ms) { if (await ev(expr)) return true; await sleep(150); } return false; };
const go = async (p, hash) => { await send("Page.navigate", { url: "about:blank" }); await send("Page.navigate", { url: `http://127.0.0.1:${p}/${hash}` }); await until(`!!window.state || document.readyState === "complete"`); await sleep(900); };
const click = sel => ev(`document.querySelector(${JSON.stringify(sel)}).click()`);
const text = sel => ev(`(document.querySelector(${JSON.stringify(sel)})?.innerText ?? "").trim()`);
const val = (sel, v) => ev(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); e.value = ${JSON.stringify(v)}; e.dispatchEvent(new Event("input", {bubbles:true})); e.dispatchEvent(new Event("change", {bubbles:true})); })()`);
const shot = async name => { if (!SHOTS) return; const s = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(join(SHOTS, name + ".png"), Buffer.from(s.data, "base64")); };
let ok = 0, ko = 0;
const check = (name, cond, extra = "") => { if (cond) { ok++; console.log("ok  ", name); } else { ko++; console.log("FAIL", name, extra); } };
const status = () => text("#d-status");

const visible = sel => ev(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); return !!e && !!e.offsetParent; })()`);

try {
  // ---------------------------------------------------------------- first launch, no key
  await go(NOKEY, "#new");
  check("no key: the page asks for one on arrival", await visible("#view-key") && !(await visible("#view-new")));
  check("no key: it says where the key goes", (await text("#k-file")).endsWith(".env"), await text("#k-file"));
  await click('nav a[data-view="runs"]'); await sleep(200); await click('nav a[data-view="new"]'); await sleep(200);
  check("no key: New run still leads to the key", await visible("#view-key") && !(await visible("#view-new")));
  await val("#k-key", "short"); await click("#b-key");
  check("key in a bad format: refused with a reason", await until(`document.querySelector("#k-err").innerText.includes("does not look like")`, 4000), await text("#k-err"));
  await val("#k-key", "badAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"); await click("#b-key");
  check("key refused by YouTube: says so", await until(`document.querySelector("#k-err").innerText.includes("not valid")`, 4000), await text("#k-err"));
  check("key refused: nothing written", !existsSync(process.env.ENV_FILE));
  await shot("key-refused");
  const goodKey = "AIzaGOODgoodGOODgoodGOODgoodGOOD0000000";
  await val("#k-key", goodKey); await click("#b-key");
  check("good key: on to New run", await until(`!!document.querySelector("#view-new").offsetParent`, 4000), await text("#k-err"));
  check("good key: saved", readFileSync(process.env.ENV_FILE, "utf-8").includes("YOUTUBE_API_KEY=" + goodKey));
  if (process.platform !== "win32") check("good key: readable by its owner only", (statSync(process.env.ENV_FILE).mode & 0o777) === 0o600);
  check("good key: no longer in the page", !(await ev(`document.documentElement.outerHTML.includes(${JSON.stringify(goodKey)}) || document.querySelector("#k-key").value !== ""`)));
  await go(NOKEY, "#new");
  check("reload: the key is known, New run shows", await visible("#view-new"));
  await go(NOKEY, "#key");
  check("change the key: same page, another title", (await text("#k-title")).startsWith("Change") && await visible("#view-key"));
  await go(NOKEY, "#quota");
  check("the Quota page links to it", await visible('#view-quota a[data-view="key"]'));
  await click('#view-quota a[data-view="key"]'); await sleep(200);
  check("the link opens it", await visible("#view-key"));

  // ---------------------------------------------------------------- what the videos box understands
  await go(FULL, "#new");
  await val("#f-targets", "aaaaaaaaaaa\nchannel:@x\n");
  check("targets: counted as you type", await until(`document.querySelector("#f-targets-hint").innerText === "1 video, 1 channel"`, 3000), await text("#f-targets-hint"));
  await val("#f-targets", "aaaaaaaaaaa\n# a note\nnot a video !!");
  check("targets: the wrong line is named", await until(`document.querySelector("#f-targets-hint").innerText.includes("Line 3")`, 3000), await text("#f-targets-hint"));
  await val("#f-targets", "search:cats");
  check("targets: a search needs its box ticked", await until(`document.querySelector("#f-targets-hint").innerText.includes("allow search")`, 3000), await text("#f-targets-hint"));
  await val("#f-targets", "");

  // ---------------------------------------------------------------- planning errors
  await go(FULL, "#new");
  await click("#b-plan"); await until(`!document.querySelector("#b-plan").disabled`);
  check("plan with no video: the reason is shown", (await text("#plan-err")).length > 0, await text("#plan-err"));
  await val("#f-name", "bad name!"); await val("#f-targets", "aaaaaaaaaaa\nbbbbbbbbbbb");
  await click("#b-plan"); await until(`document.querySelector("#plan-err").innerText || document.querySelector("#b-start")`);
  check("an invalid run name is refused", (await text("#plan-err")).length > 0, await text("#plan-err"));
  await val("#f-name", "first");
  await click("#b-plan"); check("plan: estimate and Start appear", await until(`!!document.querySelector("#b-start")`));
  check("plan: strip of 3 figures", (await ev(`document.querySelectorAll("#plan-out .ui-strip > div").length`)) === 3);
  await val("#f-max", "60"); await click("#b-start"); await sleep(300);
  check("settings changed after the estimate: refused", /changed/i.test(await text("#plan-err")), await text("#plan-err"));
  await val("#f-max", "100");

  // ---------------------------------------------------------------- start, pause, resume
  await click("#b-plan"); await until(`!document.querySelector("#b-plan").disabled && !!document.querySelector("#b-start")`); await sleep(200);
  await click("#b-start");
  check("start: the run's page opens", await until(`location.hash === "#run=first" && !document.querySelector("#view-detail").classList.contains("hidden")`));
  check("start: shown as running", await until(`document.querySelector("#d-status").innerText.includes("running")`));
  check("running: Pause on, Resume and Refresh off", await ev(`!document.querySelector("#b-pause").disabled && document.querySelector("#b-resume").disabled && document.querySelector("#b-refresh").disabled`));
  check("running: the header says what is collecting (New run strip)", (await text("#new-strip")).includes("first"));
  await click("#b-pause");
  check("pause: the run stops", await until(`!state.job.busy && !document.querySelector("#d-status").innerText.includes("running") && document.querySelector("#d-status").innerText.trim() !== ""`), await status());
  const paused = await status();
  check("paused: Resume on, Pause off", await until(`!document.querySelector("#b-resume").disabled && document.querySelector("#b-pause").disabled`), paused);
  const before = await text("#d-strip");
  console.log("     paused as:", paused, "|", before.replace(/\n+/g, " "));
  await shot("paused");
  await click("#b-resume");
  check("resume: running again", await until(`document.querySelector("#d-status").innerText.includes("running")`, 5000), await status());
  check("resume: finishes", await until(`document.querySelector("#d-status").innerText.includes("done")`, 90000), await status());
  await sleep(2500);
  const after = await text("#d-strip");
  check("done: figures filled", /Collected\s+[1-9]/.test(after), after);
  check("done: Refresh on, Pause and Resume off", await ev(`!document.querySelector("#b-refresh").disabled && document.querySelector("#b-pause").disabled && document.querySelector("#b-resume").disabled`));
  check("done: coverage strip", (await ev(`document.querySelectorAll("#d-coverage .ui-strip > div").length`)) === 3);
  check("done: progress at 100 %", await ev(`[...document.querySelectorAll("#d-progress .meter i")].every(i => i.style.width === "100%")`));

  // ---------------------------------------------------------------- refresh snapshot
  acceptDialog = false; await click("#b-refresh"); await sleep(800);
  check("refresh refused in the dialog: nothing starts", dialogs === 1 && !(await status()).includes("running"), `${dialogs} ${await status()}`);
  acceptDialog = true; await click("#b-refresh");
  check("refresh accepted: runs", await until(`document.querySelector("#d-status").innerText.includes("running")`, 5000), await status());
  check("refresh: finishes", await until(`document.querySelector("#d-status").innerText.includes("done")`, 90000), await status());
  await sleep(2500);
  check("two snapshots: strip says so", /snapshot 2, 2 kept/.test(await text("#d-strip")), await text("#d-strip"));
  check("two snapshots: selector shown", await ev(`!document.querySelector("#d-seq").classList.contains("hidden")`));
  check("two snapshots: diff with its 4 figures", await until(`document.querySelectorAll("#d-diff .ui-strip > div").length === 4`, 5000));
  await val("#d-seq", "1"); await sleep(1200);
  check("snapshot selector: goes back to the first", /snapshot 1, 2 kept/.test(await text("#d-strip")), await text("#d-strip"));
  await val("#d-seq", "2"); await sleep(1000);

  // ---------------------------------------------------------------- explore
  await until(`document.querySelector("#x-sample table")`, 5000);
  const all = await ev(`document.querySelectorAll("#x-sample tr").length`);
  check("explore: comments listed", all > 1, String(all));
  check("explore: timeline drawn", await ev(`!!document.querySelector("#x-timeline svg polyline")`));
  check("explore: languages drawn", await ev(`document.querySelectorAll("#x-langs .b").length > 0`));
  await val("#x-search", "zzzznothingzzzz"); await sleep(1000);
  check("explore: a search with no match says so", /^0 matches/.test(await text("#x-sample")), await text("#x-sample"));
  await val("#x-search", ""); await sleep(800);
  await val("#x-container", "bbbbbbbbbbb"); await sleep(800);
  check("explore: filter by video", await ev(`[...document.querySelectorAll("#x-sample tr td:nth-child(2)")].every(t => t.innerText === "bbbbbbbbbbb")`));
  await val("#x-container", ""); await val("#x-reason", "other_language"); await click("#x-kept"); await sleep(1000);
  const excluded = await ev(`document.querySelectorAll("#x-sample tr").length`);
  check("explore: filter by status", await ev(`[...document.querySelectorAll("#x-sample tr td:nth-child(5)")].every(t => t.innerText.includes("other language"))`), String(excluded));
  await val("#x-reason", ""); await click("#x-kept"); await sleep(600);

  // ---------------------------------------------------------------- re-run processing
  const keptBefore = await text("#d-strip");
  await ev(`document.querySelector("details").open = true`);
  await ev(`document.querySelectorAll("#p-langs input").forEach(i => { i.checked = false; })`);
  await click("#b-process"); await sleep(2000);
  const keptAfter = await text("#d-strip");
  check("re-run processing: no error", (await text("#d-err")) === "", await text("#d-err"));
  check("re-run processing, all languages: more comments kept", keptAfter !== keptBefore, keptAfter.replace(/\n+/g, " "));
  console.log("     kept:", keptBefore.replace(/\n+/g, " "), "→", keptAfter.replace(/\n+/g, " "));

  // ---------------------------------------------------------------- exports
  for (const [fmt, name] of [["csv", "items.csv"], ["jsonl", "items.jsonl"], ["parquet", "items.parquet"]]) {
    await val("#e-format", fmt); await click("#b-export");
    await until(`!/Exporting/.test(document.querySelector("#e-out").innerText)`, 15000);
    const path = await ev(`document.querySelector("#e-out code")?.innerText ?? ""`);
    check(`export ${fmt}: written, file exists`, !!path && existsSync(join(path, name)), `${await text("#e-out")} ${path && existsSync(path) ? readdirSync(path) : ""}`);
  }
  await val("#e-format", "csv"); await click("#e-kept"); await click("#e-excel"); await click("#b-export");
  await until(`!/Exporting/.test(document.querySelector("#e-out").innerText)`, 15000);
  check("export csv, kept only, Excel: written", /Written to/.test(await text("#e-out")), await text("#e-out"));
  await shot("run-done");

  // ---------------------------------------------------------------- errors, navigation, addresses, theme
  await click('nav a[data-view="new"]'); await sleep(300);
  await val("#f-name", "first"); await click("#b-plan"); await until(`!!document.querySelector("#b-start") && !document.querySelector("#b-plan").disabled`, 8000); await sleep(300);
  if (await ev(`!!document.querySelector("#b-start")`)) { await click("#b-start"); await sleep(800); }
  check("a name already used is refused", (await text("#plan-err")).length > 0, await text("#plan-err"));
  await click('nav a[data-view="runs"]'); await sleep(400);
  check("nav: Runs, address #runs", (await ev(`location.hash`)) === "#runs");
  check("runs: the list and its figures", /Runs\s+1/.test(await text("#runs-strip")) && (await ev(`document.querySelectorAll("#runs-table tr.click").length`)) === 1, await text("#runs-strip"));
  await click("#runs-table tr.click"); await sleep(800);
  check("runs: a click opens the run", (await ev(`location.hash`)) === "#run=first");
  await click('nav a[data-view="runs"]'); await sleep(200); await click("#nav-detail"); await sleep(400);
  check("the run's tab comes back to it, with its address", (await ev(`location.hash`)) === "#run=first" && await ev(`!document.querySelector("#view-detail").classList.contains("hidden")`));
  await click(".quota"); await sleep(600);
  check("header quota opens Quota", (await ev(`location.hash`)) === "#quota" && /Units left today/.test(await text("#qv-strip")));
  check("quota: bars and chart", await ev(`document.querySelectorAll("#qv-endpoints .b").length > 0 && !!document.querySelector("#qv-days svg")`));
  await go(FULL, "#runs"); check("reload on #runs stays there", await ev(`!document.querySelector("#view-runs").classList.contains("hidden")`));
  await go(FULL, "#quota"); check("reload on #quota stays there", await ev(`!document.querySelector("#view-quota").classList.contains("hidden")`));
  await go(FULL, "#run=first"); check("reload on a run opens it", await until(`document.querySelector("#d-name").innerText === "first"`, 4000));
  await go(FULL, "#run=nothing"); check("an unknown run: back on New run, no error", await ev(`!document.querySelector("#view-new").classList.contains("hidden")`));
  const theme = () => ev(`document.documentElement.getAttribute("data-theme") || "system"`);
  const seen = []; for (let i = 0; i < 3; i++) { await click("#theme"); seen.push(await theme()); }
  check("theme: light, dark, system", seen.join() === "light,dark,system", seen.join());
  await click("#theme"); await click("#theme"); await go(FULL, "#new");
  check("theme: kept after a reload", (await theme()) === "dark", await theme());
  check("dark: the page really changes", await ev(`getComputedStyle(document.body).backgroundColor`) !== "rgb(244, 243, 238)");
  await click("#theme");

  // ---------------------------------------------------------------- deadlines and deleting
  await go(FULL, "#runs");
  check("runs: a date to refresh or delete by", /\d{4}-\d{2}-\d{2}/.test(await text("#runs-table tr.click td:nth-child(6)")), await text("#runs-table tr.click"));
  await go(FULL, "#run=first"); await until(`document.querySelector("#d-name").innerText === "first"`, 4000); await sleep(800);
  check("delete: nothing happens before the panel", !(await visible("#d-delete")));
  await click("#b-delete");
  check("delete: the panel says it cannot be undone", /cannot be undone/.test(await text("#d-delete")));
  check("delete: exports ticked by default", await ev(`document.querySelector("#del-exports").checked`));
  check("delete: older snapshots offered, there are two", await visible("#b-del-older"));
  await shot("delete-panel");
  await click("#b-del-cancel");
  check("cancel: the panel closes, nothing is gone", !(await visible("#d-delete")) && /snapshot 2, 2 kept/.test(await text("#d-strip")));
  await val("#e-format", "csv"); await val("#d-seq", "1"); await sleep(900);
  await click("#b-export"); await until(`/Written to/.test(document.querySelector("#e-out").innerText)`, 8000);
  const firstExport = await ev(`document.querySelector("#e-out code").innerText`);
  await val("#d-seq", "2"); await sleep(900);
  await click("#b-export"); await until(`document.querySelector("#e-out code") && document.querySelector("#e-out code").innerText !== ${JSON.stringify(firstExport)}`, 8000);
  const secondExport = await ev(`document.querySelector("#e-out code").innerText`);
  check("two exports, one per snapshot", existsSync(firstExport) && existsSync(secondExport) && firstExport !== secondExport, `${firstExport} ${secondExport}`);
  await click("#b-delete"); await click("#b-del-older");
  check("delete older: one snapshot left", await until(`document.querySelector("#d-seq").classList.contains("hidden") && !document.querySelector("#d-strip").innerText.includes("kept\\n") && !/, \\d kept/.test(document.querySelector("#d-strip").innerText)`, 6000), await text("#d-strip"));
  check("delete older: says what went", /Deleted 1 older snapshot/.test(await text("#d-msg")), await text("#d-msg"));
  check("delete older: its export went, the latest's stayed", !existsSync(firstExport) && existsSync(secondExport));
  check("delete older: the selector is gone", await ev(`document.querySelector("#d-seq").classList.contains("hidden")`));
  check("delete older: comments still explore", await ev(`document.querySelectorAll("#x-sample tr").length > 1`));
  check("no script error so far", jsErrors.length === 0, jsErrors.join(" || "));
  await click("#b-delete");
  check("one snapshot: nothing older to offer", !(await visible("#b-del-older")));
  await click("#b-del-run");
  check("delete: back on Runs, with what went", await until(`/Deleted first/.test(document.querySelector("#runs-msg").innerText)`, 6000), await text("#runs-msg"));
  check("delete: the run is off the list", /No runs yet/.test(await text("#runs-table")), await text("#runs-table"));
  check("delete: its tab is gone", !(await visible("#nav-detail")));
  check("delete: its export is gone", !existsSync(secondExport));
  await go(FULL, "#run=first");
  check("delete: its address no longer opens it", await visible("#view-new"));

  // ---------------------------------------------------------------- YouTube answers quotaExceeded
  await go(LOW, "#new");
  await val("#f-name", "short"); await val("#f-targets", "aaaaaaaaaaa\nbbbbbbbbbbb");
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`); await sleep(200);
  await click("#b-start");
  check("quota: starts", await until(`location.hash === "#run=short"`));
  const stopped = `document.querySelector("#d-status").innerText.trim() !== "" && !document.querySelector("#d-status").innerText.includes("running") && !state.job.busy`;
  check("quota: stops by itself", await until(stopped, 40000), await status());
  await sleep(2500);
  console.log("     stopped as:", await status(), "|", (await text("#d-msg")).slice(0, 120));
  check("quota: not shown as done", !(await status()).includes("done"), await status());
  check("quota: what was collected is kept", /Collected\s+[1-9]/.test(await text("#d-strip")), await text("#d-strip"));
  check("quota: Resume offered, Refresh not", await ev(`!document.querySelector("#b-resume").disabled && document.querySelector("#b-refresh").disabled`));
  await shot("quota-out");
  await click("#b-resume"); await sleep(600);
  check("resume with no quota left: stops again, no crash", await until(stopped, 20000), await status());
  await sleep(2200);
  await click("#d-wait"); await click("#b-resume");
  check("resume and wait for the reset: stays on, waiting", await until(`document.querySelector("#d-status").innerText.includes("running")`, 6000), await status());
  await sleep(2500);
  check("waiting: can be paused", await ev(`!document.querySelector("#b-pause").disabled`));
  await click("#b-pause");
  check("waiting, then pause: stops", await until(stopped, 30000), await status());
  await sleep(2200);
  await click("#d-wait");
  writeFileSync(process.env.RESET_FLAG, "");
  await click("#b-resume");
  check("after the reset: resume finishes the run", await until(`document.querySelector("#d-status").innerText.includes("done")`, 90000), await status());
  await sleep(2500);
  check("after the reset: every video finished", /2 finished/.test(await text("#d-strip")), await text("#d-strip"));
} catch (e) { ko++; console.log("CRASH", e.message); }

check("no script error on any page", jsErrors.length === 0, jsErrors.slice(0, 5).join(" || "));
check("no server error (5xx)", badStatus.length === 0, badStatus.slice(0, 5).join(" || "));
console.log(`\n${ok} ok, ${ko} failed`);
ws.close(); chrome.kill(); await sleep(400); rmSync(profile, { recursive: true, force: true });
process.exit(ko ? 1 : 0);
