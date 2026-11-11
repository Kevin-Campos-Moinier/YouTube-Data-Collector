// The dashboard's journeys, by real clicks in Chrome, on the in-memory YouTube. Run through tests/test_e2e.py.
// Usage: node flows.mjs <port> <port where YouTube runs out of quota> <port with no key yet> <port where YouTube answers at once>
// Environment: CHROME, RESET_FLAG (created to end the quota), ENV_FILE (the no-key server's .env), SHOTS (optional folder).
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [FULL, LOW, NOKEY, QUICK] = process.argv.slice(2), SHOTS = process.env.SHOTS;
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
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error" && !/status of 40[04]/.test(m.params.entry.text)) jsErrors.push(m.params.entry.text);
  if (m.method === "Network.responseReceived" && (m.params.response.status >= 500 || (m.params.response.status === 404 && m.params.response.url.includes("/ui/")))) badStatus.push(`${m.params.response.status} ${m.params.response.url}`);
  if (m.method === "Page.javascriptDialogOpening") { dialogs++; send("Page.handleJavaScriptDialog", { accept: acceptDialog }); }
};
for (const d of ["Page", "Runtime", "Log", "Network"]) await send(d + ".enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false });
await send("Emulation.setFocusEmulationEnabled", { enabled: true });   // a page without a window gets no focus events otherwise
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
  const goodKey = "test-key-GOODgoodGOODgoodGOOD0000";
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
  check("a blank form: the videos box is empty", (await ev(`document.querySelector("#f-targets").value`)) === "", await ev(`document.querySelector("#f-targets").value`));
  await sleep(500);
  check("a blank form: no error before anything is typed", (await text("#f-targets-hint")) === "", await text("#f-targets-hint"));
  await val("#f-targets", "aaaaaaaaaaa\nchannel:@x\n");
  check("targets: counted as you type", await until(`document.querySelector("#f-targets-hint").innerText === "1 video, 1 channel"`, 3000), await text("#f-targets-hint"));
  await val("#f-targets", "aaaaaaaaaaa\n# a note\nnot a video !!");
  check("targets: the wrong line is named", await until(`document.querySelector("#f-targets-hint").innerText.includes("Line 3")`, 3000), await text("#f-targets-hint"));
  await val("#f-targets", "search:cats");
  check("targets: a search needs its box ticked", await until(`document.querySelector("#f-targets-hint").innerText.includes("allow search")`, 3000), await text("#f-targets-hint"));
  check("search: its options show with a search line", await until(`!document.querySelector("#f-search-opts").classList.contains("hidden")`, 3000));
  await val("#f-targets", "aaaaaaaaaaa");
  check("search: its options go away without one", await until(`document.querySelector("#f-search-opts").classList.contains("hidden")`, 3000));
  await val("#f-targets", "");

  // ---------------------------------------------------------------- a search: where each video comes from, its figures, the list as CSV
  await go(QUICK, "#new");
  await val("#f-name", "found"); await val("#f-targets", "bbbbbbbbbbb\nsearch:cats");
  await until(`!document.querySelector("#f-search-row").classList.contains("hidden")`, 3000);
  await click("#f-search"); await val("#f-s-lang", "fr"); await val("#f-s-region", "FR");
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`, 6000); await sleep(200);
  const heads = await ev(`[...document.querySelectorAll("#plan-videos th")].map((t) => t.innerText.trim()).join(",")`);
  check("check: the columns of before, and views", heads === "video,title,channel,views,comments,units", heads);
  check("search: language and country are lists of names", (await ev(`document.querySelector("#f-s-region").selectedOptions[0].text`)) === "France (FR)" && (await ev(`document.querySelector("#f-s-lang option[value=''").text`)).startsWith("Any"));
  const csv = await ev(`fetch("/download/check").then((r) => r.text())`);
  check("check: the CSV has the rest, with the search line, the rank and the options", csv.startsWith("target,rank,video_id") && /\nsearch:cats,\d/.test(csv) && csv.includes("language=fr; region=FR"), csv.slice(0, 300));
  await click('#plan-videos [data-sort="views"]'); await sleep(100);
  check("check: the list sorts by views", (await ev(`document.querySelector("#plan-videos th[aria-sort]")?.innerText || ""`)).startsWith("views"));
  // a draft saved by an older version, without the search options: they take their defaults
  await ev(`localStorage.setItem("smdc.draft", JSON.stringify({name: "old", targets: ["search:x"], collect: {max_items: 100, allow_search: true}, process: {languages: ["en"]}, privacy: {}}))`);
  await go(QUICK, "#new");
  const opts = await ev(`["#f-s-order", "#f-s-lang", "#f-s-region", "#f-s-length", "#f-s-after"].map((s) => document.querySelector(s).value).join("|")`);
  check("an older draft: no \"undefined\" in the search options", opts === "relevance|||any|", opts);
  await ev(`localStorage.setItem("smdc.draft", JSON.stringify({name: "odd", targets: ["search:x"], collect: {allow_search: true, search_language: "tlh"}, process: {languages: ["en"]}, privacy: {}}))`);
  await go(QUICK, "#new");
  check("a language the list lacks, from a run file: kept and shown", (await ev(`document.querySelector("#f-s-lang").value`)) === "tlh");
  await ev(`localStorage.removeItem("smdc.draft")`);

  // ---------------------------------------------------------------- a run so short that it ends under a click on Pause
  await go(QUICK, "#new");
  await val("#f-name", "brief"); await val("#f-targets", "bbbbbbbbbbb");
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`, 6000); await sleep(200);
  await click("#b-start"); await until(`location.hash === "#run=brief"`, 5000); await sleep(150);
  await click("#b-pause");
  check("short run: it stops", await until(`!state.job.busy && /paused|done/.test(document.querySelector("#d-status").innerText)`, 15000), await status());
  check("short run: the comments appear without a reload", await until(`document.querySelectorAll("#x-sample tr").length > 1 && !!document.querySelector("#x-timeline svg polyline, #x-timeline svg circle")`, 5000), await text("#x-sample p"));
  check("short run: Pause is off", await ev(`document.querySelector("#b-pause").disabled`));

  // ---------------------------------------------------------------- a run of many videos
  await go(QUICK, "#new");
  await val("#f-name", "many"); await val("#f-targets", Array.from({ length: 30 }, (_, n) => "v" + String(n).padStart(10, "0")).join("\n"));
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`, 6000); await sleep(200);
  await click("#b-start"); await until(`location.hash === "#run=many"`, 5000);
  check("many videos: the run ends", await until(`!state.job.busy && /done/.test(document.querySelector("#d-status").innerText)`, 30000), await status());
  await until(`document.querySelectorAll("#d-cov-videos tr").length === 31`, 5000);
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 800, deviceScaleFactor: 1, mobile: false }); await sleep(200);
  for (const [box, name] of [["#d-progress", "Progress"], ["#d-cov-videos", "Per video"]]) {
    const m = await ev(`(() => { const b = document.querySelector(${JSON.stringify(box)}); b.scrollIntoView(); b.scrollTop = 200; const r = b.getBoundingClientRect(), th = b.querySelector("th").getBoundingClientRect(); return {h: r.height, inner: b.scrollHeight, win: innerHeight, rows: b.querySelectorAll("tr").length, titleIn: th.top >= r.top - 1 && th.bottom <= r.bottom}; })()`);
    check(`${name}: 30 videos, in a list of its own`, m.rows === 31 && m.h <= m.win * .5 + 1 && m.inner > m.h && m.titleIn, JSON.stringify(m));
    await ev(`document.querySelector(${JSON.stringify(box)}).scrollTop = 200`);
    await ev(`loadRun()`); await sleep(400);
    check(`${name}: a refresh keeps the place in the list`, (await ev(`document.querySelector(${JSON.stringify(box)}).scrollTop`)) === 200, String(await ev(`document.querySelector(${JSON.stringify(box)}).scrollTop`)));
    await ev(`document.querySelector(${JSON.stringify(box)}).scrollTop = 0`);
  }
  check("wide window: no button to show the videos", !(await visible("#d-progress-all")) && !(await visible("#d-cov-all")));
  // sorting by a column's title
  const column = (box, n) => ev(`[...document.querySelectorAll(${JSON.stringify(box + " tr td:nth-child(" + n + ")")})].map(t => t.innerText.trim())`);
  const titled = (box, key) => `${box} [data-sort=${key}]`;
  const ownOrder = await column("#d-progress", 1);
  const counts = async () => (await column("#d-progress", 4)).map(Number);
  await click(titled("#d-progress", "comments")); await sleep(100);
  let got = await counts();
  check("sort: most comments first", got.every((v, n) => !n || got[n - 1] >= v) && got[0] === 7, got.join());
  check("sort: the title says which way", (await ev(`document.querySelector("#d-progress th[aria-sort]")?.getAttribute("aria-sort")`)) === "descending" && /↓/.test(await text(titled("#d-progress", "comments"))));
  await ev(`document.querySelector("#d-progress").scrollTop = 150`);
  await ev(`document.querySelector(${JSON.stringify(titled("#d-progress", "comments"))}).focus()`);
  await ev(`loadRun()`); await sleep(400);
  got = await counts();
  check("sort: a refresh keeps it", got[0] === 7 && got.every((v, n) => !n || got[n - 1] >= v), got.join());
  check("sort: a refresh keeps the keyboard on the title", (await ev(`document.activeElement.dataset.sort`)) === "comments", await ev(`document.activeElement.tagName`));
  await click(titled("#d-progress", "comments")); await sleep(100);
  got = await counts();
  check("sort: a second click, the other way", got[0] === 1 && got.every((v, n) => !n || got[n - 1] <= v), got.join());
  check("sort: the list goes back to its top", (await ev(`document.querySelector("#d-progress").scrollTop`)) === 0);
  await click(titled("#d-progress", "comments")); await sleep(100);
  check("sort: a third click, the run's own order again", JSON.stringify(await column("#d-progress", 1)) === JSON.stringify(ownOrder) && !(await ev(`!!document.querySelector("#d-progress th[aria-sort]")`)));
  const perVideoOwn = await column("#d-cov-videos", 1);
  await click(titled("#d-cov-videos", "collected")); await sleep(100);
  got = (await column("#d-cov-videos", 3)).map(t => parseInt(t, 10));
  check("sort: Per video too", got[0] === 7 && got.every((v, n) => !n || got[n - 1] >= v), got.join());
  check("sort: one table does not touch the other", JSON.stringify(await column("#d-progress", 1)) === JSON.stringify(ownOrder));
  await click(titled("#d-cov-videos", "collected")); await click(titled("#d-cov-videos", "collected")); await sleep(100);
  check("sort: Per video, back to its own order", JSON.stringify(await column("#d-cov-videos", 1)) === JSON.stringify(perVideoOwn));
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 800, deviceScaleFactor: 1, mobile: false }); await sleep(200);
  const rowsSeen = box => ev(`[...document.querySelectorAll(${JSON.stringify(box + " tr")})].filter(r => r.checkVisibility()).length`);
  for (const [box, button, name] of [["#d-progress", "#d-progress-all", "Progress"], ["#d-cov-videos", "#d-cov-all", "Per video"]]) {
    check(`${name} on a phone: the titles and the first 20`, (await rowsSeen(box)) === 21, String(await rowsSeen(box)));
    check(`${name} on a phone: no box inside the page`, await ev(`(() => { const b = document.querySelector(${JSON.stringify(box)}); return b.scrollHeight <= b.clientHeight + 1; })()`));
    check(`${name} on a phone: a button says how many there are`, await visible(button) && /^Show all 30 videos$/.test(await text(button)), await text(button));
  }
  await click("#d-progress-all"); await sleep(100);
  check("Show all: every video of that table", (await rowsSeen("#d-progress")) === 31 && (await rowsSeen("#d-cov-videos")) === 21, String(await rowsSeen("#d-progress")));
  check("Show all: the button goes, the keyboard stays in the list", !(await visible("#d-progress-all")) && (await ev(`document.activeElement.id`)) === "d-progress");
  await ev(`loadRun()`); await sleep(400);
  check("Show all: a refresh keeps it", (await rowsSeen("#d-progress")) === 31 && !(await visible("#d-progress-all")));
  check("many videos on a phone: the page does not scroll sideways", await ev(`document.documentElement.scrollWidth <= innerWidth`));
  await click(titled("#d-cov-videos", "collected")); await sleep(100);
  check("a sort on a phone: the first 20 are the first of that order", (await ev(`[...document.querySelectorAll("#d-cov-videos tr")].filter(r => r.checkVisibility()).slice(1).every(r => parseInt(r.children[2].innerText, 10) >= 3)`)));   // 7, 6, 5, 4 and 3 comments: four videos each
  await click(titled("#d-progress", "comments")); await sleep(100);
  await ev(`location.hash = "#run=brief"`); await until(`document.querySelectorAll("#d-progress tr").length === 2`, 5000); await sleep(200);
  check("another run: no order left from the last", !(await ev(`[...document.querySelectorAll("#d-cov-videos th[aria-sort], #d-progress th[aria-sort]")].some(t => t.checkVisibility())`)));
  check("a run of one video on a phone: no button to show them all", !(await visible("#d-progress-all")) && !(await visible("#d-cov-all")));
  await ev(`location.hash = "#run=many"`); await until(`document.querySelectorAll("#d-progress tr").length === 31`, 5000); await sleep(200);
  check("back on the long run, without a reload: the first 20 again", (await rowsSeen("#d-progress")) === 21, String(await rowsSeen("#d-progress")));
  // what needs a look comes first, when asked
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false }); await sleep(150);
  await go(QUICK, "#new");
  await val("#f-name", "mixed"); await val("#f-targets", "v0000000000\nvdisabled00");
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`, 6000); await sleep(200);
  await click("#b-start"); await until(`location.hash === "#run=mixed"`, 5000);
  await until(`!state.job.busy && /done|finished/.test(document.querySelector("#d-status").innerText)`, 15000);
  await until(`document.querySelectorAll("#d-progress tr").length === 3`, 5000); await sleep(300);
  const statuses = () => column("#d-progress", 2);
  const asRun = await statuses();
  check("mixed run: the run's order puts the finished video first", /^done/.test(asRun[0]) && /^unavailable/.test(asRun[1]), asRun.join(" | "));
  await click(titled("#d-progress", "status")); await sleep(100);
  check("sort by status: what needs a look first", /^unavailable/.test((await statuses())[0]), (await statuses()).join(" | "));
  await click(titled("#d-progress", "progress")); await sleep(100);
  got = await statuses();
  check("sort by progress: a video with no progress comes last", /^unavailable/.test(got[1]), got.join(" | "));
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false }); await sleep(150);

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
  const cost = () => text("#plan-out .ui-strip > div:nth-child(2) .v");
  const costBefore = await cost();
  check("the form: a run name appears with the estimate", await visible("#f-name-row"));
  await click('input[name=f-preset][value=all]');
  check("preset All: the sentence says so", /every comment/.test(await text("#f-summary")), await text("#f-summary"));
  check("a setting changed: the cost follows, for free", await until(`document.querySelector("#plan-out .ui-strip > div:nth-child(2) .v")?.innerText !== ${JSON.stringify(costBefore)}`, 4000), await cost());
  await click('input[name=f-preset][value=quick]');
  check("preset Quick: back to the first cost", await until(`document.querySelector("#plan-out .ui-strip > div:nth-child(2) .v")?.innerText === ${JSON.stringify(costBefore)}`, 4000), await cost());
  await val("#f-max", "60"); await sleep(100);
  check("an amount edited by hand: Custom", await ev(`document.querySelector("input[name=f-preset]:checked").value === "custom"`));
  check("the sentence follows", /up to 60 comments/.test(await text("#f-summary")), await text("#f-summary"));
  await val("#f-max", "100");
  check("back to the same amounts: Quick again", await ev(`document.querySelector("input[name=f-preset]:checked").value === "quick"`));
  check("advanced options start folded", !(await ev(`document.querySelector("#f-adv").open`)));
  const preset = () => ev(`document.querySelector("input[name=f-preset]:checked").value`);
  const folded = async () => !(await ev(`document.querySelector("#f-adv").open`));
  await click('input[name=f-preset][value=custom]'); await sleep(100);
  check("Custom clicked: it stays selected, on amounts that match Quick", (await preset()) === "custom", await preset());
  check("Custom clicked: the details open", !(await folded()));
  await click('input[name=f-preset][value=quick]'); await sleep(100);
  check("Quick clicked: selected, the details close", (await preset()) === "quick" && await folded(), await preset());
  await click('input[name=f-preset][value=custom]'); await sleep(100);
  await click('input[name=f-preset][value=all]'); await sleep(100);
  check("All clicked: selected, the details close", (await preset()) === "all" && await folded(), await preset());
  await click('input[name=f-preset][value=quick]'); await sleep(100);
  await ev(`(() => { const d = document.querySelector("#f-since"); d.value = "2025-01-02"; d.dispatchEvent(new Event("change", {bubbles: true})); })()`); await sleep(100);
  check("a date under the folded details: the sentence still says it", /from 2025-01-02/.test(await text("#f-summary")), await text("#f-summary"));
  await ev(`(() => { const d = document.querySelector("#f-since"); d.value = ""; d.dispatchEvent(new Event("change", {bubbles: true})); })()`); await sleep(100);
  check("the date gone: the sentence no longer says it", !/More options/.test(await text("#f-summary")), await text("#f-summary"));
  check("no game or brand among the examples", !(await ev(`/VLGy63pt9vA|W2Wnvvj33Wo|PlayStation|star wars/i.test(document.querySelector("#view-new").innerHTML)`)));
  const typed = await ev(`document.querySelector("#f-targets").value`);
  await val("#f-targets", "aaaaaaaaaaa");
  check("the accepted forms stay readable once something is typed", await visible("#f-targets-forms") && /channel:@name/.test(await text("#f-targets-forms")), await text("#f-targets-forms"));
  for (const width of [1300, 390]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: false }); await sleep(150);
    const box = () => ev(`(() => { const t = document.querySelector("#f-targets"); t.scrollIntoView({block: "start"}); const r = t.getBoundingClientRect(); return {x: r.right, y: r.bottom, w: r.width, h: r.height, wide: document.documentElement.scrollWidth, win: innerWidth, winh: innerHeight, shown: t.clientHeight, needs: (() => { const v = t.value; t.value = t.placeholder; const n = t.scrollHeight; t.value = v; return n; })()}; })()`);
    const a = await box();
    check(`at ${width} px: the whole example fits in the box`, a.needs <= a.shown, JSON.stringify(a));
    const mouse = (type, x, y, extra = {}) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1, ...extra });
    await mouse("mouseMoved", a.x - 4, a.y - 4, { button: "none", buttons: 0 });
    await mouse("mousePressed", a.x - 4, a.y - 4);
    for (let n = 1; n <= 4; n++) await mouse("mouseMoved", a.x - 4 + n * 150, a.y - 4 + n * 400);
    await mouse("mouseReleased", a.x + 596, a.y + 1596);
    await sleep(100);
    const b = await box();
    check(`at ${width} px: dragging the corner grows the box, up to a limit, and not sideways`, b.h > a.h && b.h <= b.winh * .6 + 1 && b.w === a.w && b.wide <= b.win, JSON.stringify([a, b]));
    await ev(`document.querySelector("#f-targets").style.height = ""`);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false });
  await val("#f-targets", typed); await sleep(150);
  check("fewer fields on screen than before", (await ev(`[...document.querySelectorAll("#view-new input, #view-new select, #view-new textarea")].filter(e => e.checkVisibility()).length`)) <= 12, String(await ev(`[...document.querySelectorAll("#view-new input, #view-new select, #view-new textarea")].filter(e => e.checkVisibility()).length`)));
  await val("#f-targets", "aaaaaaaaaaa"); await sleep(200);
  check("the videos changed: asks to check them again", /check them again/.test(await text("#plan-out")) && !(await ev(`!!document.querySelector("#b-start")`)), await text("#plan-out"));
  await val("#f-targets", "aaaaaaaaaaa\nbbbbbbbbbbb"); await sleep(200);
  await go(FULL, "#new");
  check("draft: the videos survive a reload", (await ev(`document.querySelector("#f-targets").value`)) === "aaaaaaaaaaa\nbbbbbbbbbbb", await ev(`document.querySelector("#f-targets").value`));
  check("draft: the name typed too", (await ev(`document.querySelector("#f-name").value`)) === "first", await ev(`document.querySelector("#f-name").value`));

  // ---------------------------------------------------------------- start, pause, resume
  await click("#b-plan"); await until(`!document.querySelector("#b-plan").disabled && !!document.querySelector("#b-start")`); await sleep(200);
  await click("#b-start");
  check("start: the run's page opens", await until(`location.hash === "#run=first" && !document.querySelector("#view-detail").classList.contains("hidden")`));
  check("start: shown as running", await until(`document.querySelector("#d-status").innerText.includes("running")`));
  check("running: Pause on, Resume and Refresh off", await ev(`!document.querySelector("#b-pause").disabled && document.querySelector("#b-resume").disabled && document.querySelector("#b-refresh").disabled`));
  check("running: the header says what is collecting (New run strip)", (await text("#new-strip")).includes("first"));
  check("running: overall progress and a word about the terminal", /% of this snapshot/.test(await text("#d-overall-text")) && /terminal/.test(await text("#d-overall-text")), await text("#d-overall-text"));
  check("running: says why the other buttons are off", /once this run is paused/.test(await text("#d-why")), await text("#d-why"));
  check("running: the tab's title shows the progress", /^\d+% · first$/.test(await ev(`document.title`)), await ev(`document.title`));
  await until(`/min left|minute left/.test(document.querySelector("#d-overall-text").innerText)`, 9000);
  console.log("     progress:", await text("#d-overall-text"));
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
  check("a finished run: Pause says why it is off", (await ev(`document.querySelector("#b-pause").title`)).includes("not collecting"), await ev(`document.querySelector("#b-pause").title`));
  check("a finished run: Resume says why it is off", (await ev(`document.querySelector("#b-resume").title`)).includes("finished"), await ev(`document.querySelector("#b-resume").title`));
  await sleep(2500);
  const after = await text("#d-strip");
  check("done: the comments are there without a reload", await until(`document.querySelectorAll("#x-sample tr").length > 1 && !!document.querySelector("#x-timeline svg polyline, #x-timeline svg circle") && document.querySelectorAll("#x-langs .b").length > 0`, 4000), await text("#x-sample"));
  check("done: the draft is gone", (await ev(`localStorage.getItem("smdc.draft")`)) === null);
  check("done: the title is back", (await ev(`document.title`)) === "Social Media Data Collector", await ev(`document.title`));
  check("done: figures filled", /Collected\s+[1-9]/.test(after), after);
  check("done: Refresh on, Pause and Resume off", await ev(`!document.querySelector("#b-refresh").disabled && document.querySelector("#b-pause").disabled && document.querySelector("#b-resume").disabled`));
  check("done: coverage strip", (await ev(`document.querySelectorAll("#d-coverage .ui-strip > div").length`)) === 3);
  check("done: progress at 100 %", await ev(`[...document.querySelectorAll("#d-progress .meter i")].every(i => i.style.width === "100%")`));

  // ---------------------------------------------------------------- refresh snapshot
  await click("#b-refresh"); await sleep(300);
  check("refresh: asks first, in the page", await visible("#b-refresh-yes") && !(await status()).includes("running"), await status());
  await click("#b-refresh-no"); await sleep(300);
  check("refresh cancelled: nothing starts", !(await visible("#b-refresh-yes")) && !(await status()).includes("running"), await status());
  await click("#b-refresh"); await click("#b-refresh-yes");
  check("refresh accepted: runs", await until(`document.querySelector("#d-status").innerText.includes("running")`, 5000), await status());
  check("refresh: finishes", await until(`document.querySelector("#d-status").innerText.includes("done")`, 90000), await status());
  await sleep(2500);
  check("two snapshots: strip says so", /snapshot 2 of the 2 stored/.test(await text("#d-strip")), await text("#d-strip"));
  check("two snapshots: selector shown", await ev(`!document.querySelector("#d-seq").classList.contains("hidden")`));
  check("two snapshots: diff with its 4 figures", await until(`document.querySelectorAll("#d-diff .ui-strip > div").length === 4`, 5000));
  await val("#d-seq", "1"); await sleep(1200);
  check("snapshot selector: goes back to the first", /snapshot 1 of the 2 stored/.test(await text("#d-strip")), await text("#d-strip"));
  await val("#d-seq", "2"); await sleep(1000);

  // ---------------------------------------------------------------- explore
  await until(`document.querySelector("#x-sample table")`, 5000);
  const all = await ev(`document.querySelectorAll("#x-sample tr").length`);
  check("explore: comments listed", all > 1, String(all));
  check("explore: timeline drawn", await ev(`!!document.querySelector("#x-timeline svg polyline, #x-timeline svg circle")`));
  check("explore: languages drawn", await ev(`document.querySelectorAll("#x-langs .b").length > 0`));
  check("explore: 50 comments a page", all === 51, String(all));
  const reach = () => ev(`(() => { const pos = document.querySelector("#x-pos").getBoundingClientRect(), ex = document.querySelector("#d-export").getBoundingClientRect(); return {toExport: ex.top - pos.top, win: innerHeight}; })()`);
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 800, deviceScaleFactor: 1, mobile: false }); await sleep(150);
  const far = await reach();
  check("explore: Export is less than a window below the list's start, however many comments", far.toExport < far.win, JSON.stringify(far));
  check("explore: position and pages come before the list", await ev(`!!(document.querySelector("#x-next").compareDocumentPosition(document.querySelector("#x-sample")) & Node.DOCUMENT_POSITION_FOLLOWING)`));
  await ev(`document.querySelector("#x-sample").scrollTop = 300`);
  await ev(`loadExplore()`); await sleep(500);
  check("explore: the 10-second refresh keeps the place in the list", (await ev(`document.querySelector("#x-sample").scrollTop`)) === 300, String(await ev(`document.querySelector("#x-sample").scrollTop`)));
  check("explore: column titles stay in view in the list", await ev(`(() => { const box = document.querySelector("#x-sample").getBoundingClientRect(), th = document.querySelector("#x-sample th").getBoundingClientRect(); return th.top >= box.top - 1 && th.bottom <= box.bottom; })()`));
  await click("#x-next"); await sleep(800);
  check("explore: the next page opens at its first comment", (await ev(`document.querySelector("#x-sample").scrollTop`)) === 0 && /^51–/.test(await text("#x-pos")), await text("#x-pos"));
  await click("#x-prev"); await sleep(600);
  for (const [to, name] of [["d-export", "Export"], ["d-logcard", "Log"], ["d-explore", "Explore"]]) {
    await click(`[data-jump=${to}]`); await sleep(100);
    const j = await ev(`(() => { const t = document.getElementById(${JSON.stringify(to)}).getBoundingClientRect(), b = document.querySelector(".jump").getBoundingClientRect(); return {top: t.top, barTop: b.top, barBottom: b.bottom, focus: document.activeElement.id, win: innerHeight}; })()`);
    check(`the link to ${name}: below the links, in view, with the keyboard's place`, j.top >= j.barBottom - 1 && j.top < j.win && j.barTop >= 0 && j.focus === to, JSON.stringify(j));
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 800, deviceScaleFactor: 1, mobile: false }); await sleep(200);
  check("explore on a phone: no scrolling box inside the page", await ev(`(() => { const b = document.querySelector("#x-sample"); return b.scrollHeight <= b.clientHeight + 1; })()`));
  check("explore on a phone: pages at the end of the list too", await visible("#x-next-end"));
  await ev(`document.querySelector("#x-next-end").scrollIntoView()`);
  await click("#x-next-end"); await sleep(800);
  check("explore on a phone: the next page opens at its start", await ev(`(() => { const t = document.querySelector("#x-pos").getBoundingClientRect().top; return t >= document.querySelector(".jump").getBoundingClientRect().bottom - 1 && t < innerHeight; })()`) && /^51–/.test(await text("#x-pos")), await text("#x-pos"));
  await click("#x-prev-end"); await sleep(600);
  check("explore on a phone: the page does not scroll sideways", await ev(`document.documentElement.scrollWidth <= innerWidth`));
  const firstRow = await ev(`(() => { const r = document.querySelector("#x-sample tr:nth-child(2)"), t = r.querySelector("td.txt").getBoundingClientRect(), d = r.querySelector("td:first-child").getBoundingClientRect(), box = document.querySelector("#x-sample").getBoundingClientRect(); return {textAbove: t.bottom <= d.top + 1, inside: t.left >= box.left - 1 && t.right <= box.right + 1, titles: document.querySelector("#x-sample tr:first-child").checkVisibility()}; })()`);
  check("explore on a phone: the text first, whole, in the width of the screen", firstRow.textAbove && firstRow.inside && !firstRow.titles, JSON.stringify(firstRow));
  await ev(`renderTimeline([{container_id: "aaaaaaaaaaa", day: "2024-03-04", n: 5}])`);
  check("a chart of one day shows that day", await ev(`(() => { const c = document.querySelector("#x-timeline circle"); if (!c) return false; const r = c.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()`));
  await ev(`renderTimeline(Array.from({length: 700}, (_, n) => ({container_id: "aaaaaaaaaaa", day: new Date(Date.UTC(2024, 0, 1 + n)).toISOString().slice(0, 10), n: 1 + n % 5})))`);
  check("a chart of several days draws a line", await ev(`!!document.querySelector("#x-timeline polyline")`));
  check("explore on a phone: the dates of a long chart do not overlap", await ev(`(() => { const t = [...document.querySelectorAll("#x-timeline .axis text[text-anchor]")].filter(e => /^\\d{4}-/.test(e.textContent)).map(e => e.getBoundingClientRect()).sort((a, b) => a.left - b.left); return t.length >= 2 && t.every((r, n) => !n || t[n - 1].right <= r.left); })()`));
  const millions = await ev(`(() => { const b = document.querySelector("#x-langs"); const keep = b.innerHTML; b.innerHTML = '<div class="b"><span>la</span><div class="t"><i style="width:50%"></i></div><span>1,000,000</span></div>'; const v = b.querySelector("span:last-child"); const out = v.scrollWidth <= v.clientWidth; b.innerHTML = keep; return out; })()`);
  check("explore on a phone: a count in the millions stays inside its bar's row", millions);
  await ev(`loadExplore()`); await sleep(400);
  await send("Emulation.setDeviceMetricsOverride", { width: 1300, height: 1600, deviceScaleFactor: 1, mobile: false }); await sleep(150);
  await val("#x-search", "zzzznothingzzzz"); await sleep(1000);
  check("explore: a search with no match says so", /^0 matches/.test(await text("#x-pos")), await text("#x-pos"));
  await val("#x-search", ""); await sleep(800);
  await val("#x-container", "bbbbbbbbbbb"); await sleep(800);
  check("explore: filter by video", await ev(`[...document.querySelectorAll("#x-sample tr td:nth-child(2)")].every(t => t.innerText === "bbbbbbbbbbb")`));
  await val("#x-container", ""); await sleep(800);
  const firstPage = await text("#x-pos");
  check("explore: says which comments are shown", /^1–\d+ of /.test(firstPage), firstPage);
  check("explore: pages when there are more", await visible("#x-next") && await ev(`document.querySelector("#x-prev").disabled`));
  const firstCell = await text("#x-sample tr:nth-child(2) td:last-child");
  await click("#x-next"); await sleep(800);
  check("explore: the next page", /^51–/.test(await text("#x-pos")) && await ev(`!document.querySelector("#x-prev").disabled`), await text("#x-pos"));
  await click("#x-prev"); await sleep(600);
  await val("#x-sort", "liked"); await sleep(800);
  const likes = await ev(`[...document.querySelectorAll("#x-sample tr td:nth-child(4)")].map(t => Number(t.innerText.replace(/,/g, "")))`);
  check("explore: most liked first", likes.every((v, n) => !n || likes[n - 1] >= v), likes.slice(0, 5).join());
  await val("#x-sort", "oldest"); await sleep(500);
  console.log("     first cell:", firstCell.slice(0, 40));
  await val("#x-reason", "other_language"); await click("#x-kept"); await sleep(1000);
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
  check("exports: listed", await until(`document.querySelectorAll("#e-list tr").length > 1`, 4000));
  const link = await ev(`document.querySelector("#e-list a[download]").getAttribute("href")`);
  const dl = await ev(`fetch(${JSON.stringify(link)}).then(async r => [r.status, r.headers.get("content-type"), (await r.arrayBuffer()).byteLength])`);
  check("exports: the download is a real .zip", dl[0] === 200 && dl[1] === "application/zip" && dl[2] > 100, dl.join());
  check("exports: a folder outside the list is refused", (await ev(`fetch("/download/export?name=first&folder=..").then(r => r.status)`)) === 404);
  await ev(`document.querySelector("#e-preview-box").open = true`);
  check("exports: a preview on request", await until(`document.querySelectorAll("#e-preview tr").length > 2`, 4000), await text("#e-preview"));
  check("exports: the preview shows no raw identifier", !/text_raw|author_name/.test(await text("#e-preview")));
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

  // ---------------------------------------------------------------- history, keyboard, explanations
  await go(FULL, "#runs");
  await click('nav a[data-view="quota"]'); await sleep(250);
  await click('nav a[data-view="new"]'); await sleep(250);
  await ev(`history.back()`); await sleep(350);
  check("Back: the previous page", await visible("#view-quota") && (await ev(`location.hash`)) === "#quota");
  await ev(`history.back()`); await sleep(350);
  check("Back again", await visible("#view-runs"));
  await ev(`history.forward()`); await sleep(350);
  check("Forward", await visible("#view-quota"));
  check("the current tab is marked for a screen reader", (await ev(`document.querySelector('nav a[aria-current="page"]')?.dataset.view`)) === "quota");
  check("after moving, the keyboard starts at the title", (await ev(`document.activeElement.tagName`)) === "H1", await ev(`document.activeElement.tagName`));
  await go(FULL, "#run=first"); await until(`document.querySelector("#d-name").innerText === "first"`, 4000); await sleep(600);
  check("every field has a name, on every page", (await ev(`[...document.querySelectorAll("input,select,textarea")].filter(i => !i.labels?.length && !i.getAttribute("aria-label") && !i.closest("label") && i.type !== "hidden").map(i => i.id || i.type).join()`)) === "", await ev(`[...document.querySelectorAll("input,select,textarea")].filter(i => !i.labels?.length && !i.getAttribute("aria-label") && !i.closest("label")).map(i => i.id || i.type).join()`));
  check("the quota gauge can be reached by keyboard", await ev(`document.querySelector(".quota").tabIndex >= 0 && document.querySelector(".quota").tagName === "A"`));
  await ev(`document.querySelector("#d-strip .term").focus()`); await sleep(100);
  check("a word is explained on focus", /allowance|collection of a run|passed your/.test(await text("#term-tip")) && await ev(`!document.querySelector("#term-tip").classList.contains("hidden")`), await text("#term-tip"));
  const held = await ev(`document.activeElement.dataset.tip`);
  await sleep(2500);
  check("refreshing the page does not lose the keyboard's place", (await ev(`document.activeElement.dataset.tip`)) === held);
  await ev(`document.activeElement.blur()`); await sleep(100);
  check("the explanation goes away", await ev(`document.querySelector("#term-tip").classList.contains("hidden")`));
  await go(FULL, "#runs");
  check("a run in the list is a real link", (await ev(`document.querySelector("#runs-table a")?.getAttribute("href")`)) === "#run=first");

  // ---------------------------------------------------------------- deadlines and deleting
  await go(FULL, "#run=first"); await until(`document.querySelector("#d-name").innerText === "first"`, 4000); await sleep(500);
  await click("#b-again"); await sleep(300);
  check("new run from these settings: the form is filled", (await ev(`document.querySelector("#f-targets").value`)).includes("aaaaaaaaaaa") && await visible("#view-new"));
  check("new run from these settings: another name", (await ev(`document.querySelector("#f-name").value`)) !== "first");
  await click("#b-plan"); await until(`!!document.querySelector("#b-start")`, 6000); await sleep(200);
  check("the name comes from the first video", /^video-aaaaaaaaaaa-\d{8}$/.test(await ev(`document.querySelector("#f-name").value`)), await ev(`document.querySelector("#f-name").value`));
  await ev(`localStorage.removeItem("smdc.draft")`);
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
  check("cancel: the panel closes, nothing is gone", !(await visible("#d-delete")) && /snapshot 2 of the 2 stored/.test(await text("#d-strip")));
  await val("#e-format", "csv"); await val("#d-seq", "1"); await sleep(900);
  await click("#b-export"); await until(`/Written to/.test(document.querySelector("#e-out").innerText)`, 8000);
  const firstExport = await ev(`document.querySelector("#e-out code").innerText`);
  await val("#d-seq", "2"); await sleep(900);
  await click("#b-export"); await until(`document.querySelector("#e-out code") && document.querySelector("#e-out code").innerText !== ${JSON.stringify(firstExport)}`, 8000);
  const secondExport = await ev(`document.querySelector("#e-out code").innerText`);
  check("two exports, one per snapshot", existsSync(firstExport) && existsSync(secondExport) && firstExport !== secondExport, `${firstExport} ${secondExport}`);
  await click("#b-delete"); await click("#b-del-older");
  check("delete older: one snapshot left", await until(`document.querySelector("#d-seq").classList.contains("hidden") && !/stored/.test(document.querySelector("#d-strip").innerText)`, 6000), await text("#d-strip"));
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
  check("quota: the notice says when it comes back", /ran out/.test(await text("#d-quota")) && /your time/.test(await text("#d-quota")), await text("#d-quota"));
  check("quota: no duplicate of that in the sentence", !/quota exhausted/.test(await text("#d-msg")), await text("#d-msg"));
  check("quota: not shown as done", !(await status()).includes("done"), await status());
  check("quota: what was collected is kept", /Collected\s+[1-9]/.test(await text("#d-strip")), await text("#d-strip"));
  check("quota: Resume offered, Refresh not", await ev(`!document.querySelector("#b-resume").disabled && document.querySelector("#b-refresh").disabled`));
  const backNotice = (mode) => ev(`(() => { const r = state.budget.refused, q = quotaStopped; state.budget.refused = false; quotaStopped = ${JSON.stringify(mode)}; renderQuotaNotice();
    const box = document.querySelector("#d-quota"), out = box.innerText + (box.querySelector("button") ? " [button]" : "");
    state.budget.refused = r; quotaStopped = q; renderQuotaNotice(); return out; })()`);
  check("the next day: a run stopped for quota says the quota is back", /quota is back: press Resume/.test(await backNotice("stopped")) && !/resets|button/.test(await backNotice("stopped")), await backNotice("stopped"));
  check("just after the reset: a waiting run says it starts again", /starts again/.test(await backNotice("waiting")) && !/resets/.test(await backNotice("waiting")), await backNotice("waiting"));
  const info = (b) => ev(`document.querySelector(${JSON.stringify(b)}).click()`);
  check("the (i) beside the 30-day rule starts closed", (await ev(`document.querySelector("#d-rules").checkVisibility()`)) === false);
  await info('[aria-controls="d-rules"]');
  check("the (i): opens, with the rules and their links", await ev(`document.querySelector("#d-rules").checkVisibility() && /pass on the comments/.test(document.querySelector("#d-rules").innerText) && document.querySelectorAll("#d-rules a[href^=https]").length === 3`));
  check("the (i): says it is open", (await ev(`document.querySelector('[aria-controls="d-rules"]').getAttribute("aria-expanded")`)) === "true");
  await sleep(2500);
  check("the (i): stays open through the page's refreshes", await ev(`document.querySelector("#d-rules").checkVisibility()`));
  await shot("rules-open");
  await info('[aria-controls="d-rules"]');
  check("the (i): closes", (await ev(`document.querySelector("#d-rules").checkVisibility()`)) === false);
  check("the (i) has a name for a screen reader", await ev(`!!document.querySelector('[aria-controls="e-rules"]').getAttribute("aria-label")`));
  await shot("quota-out");
  await click("#b-resume"); await sleep(600);
  check("resume with no quota left: stops again, no crash", await until(stopped, 20000), await status());
  await sleep(2200);
  await click("#b-wait");
  check("continue after the reset: says it is waiting", await until(`document.querySelector("#d-status").innerText.includes("waiting")`, 8000), await status());
  check("waiting: the notice says so", /Waiting for the quota/.test(await text("#d-quota")), await text("#d-quota"));
  await shot("waiting");
  await sleep(2500);
  check("waiting: can be paused", await ev(`!document.querySelector("#b-pause").disabled`));
  await click("#b-pause");
  check("waiting, then pause: stops", await until(stopped, 30000), await status());
  await sleep(2200);
  writeFileSync(process.env.RESET_FLAG, "");
  await click("#b-resume");
  check("after the reset: running again, the notice is gone", await until(`/running|done/.test(document.querySelector("#d-status").innerText) && document.querySelector("#d-quota").classList.contains("hidden")`, 15000), await status());
  check("after the reset: resume finishes the run", await until(`document.querySelector("#d-status").innerText.includes("done")`, 90000), await status());
  await sleep(2500);
  check("after the reset: every video finished", /2 finished/.test(await text("#d-strip")), await text("#d-strip"));
} catch (e) { ko++; console.log("CRASH", e.message); }

check("no script error on any page", jsErrors.length === 0, jsErrors.slice(0, 5).join(" || "));
check("the browser's own dialogs are never used", dialogs === 0, String(dialogs));
check("no server error (5xx)", badStatus.length === 0, badStatus.slice(0, 5).join(" || "));
console.log(`\n${ok} ok, ${ko} failed`);
ws.close(); chrome.kill(); await sleep(400); rmSync(profile, { recursive: true, force: true });
process.exit(ko ? 1 : 0);
