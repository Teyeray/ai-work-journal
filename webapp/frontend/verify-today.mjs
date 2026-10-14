// Today-page acceptance: real data, real browser. Run with the server up:
//   node verify-today.mjs
import puppeteer from "puppeteer-core";
import { mkdirSync } from "fs";

const BASE = "http://127.0.0.1:8799";
const OUT = "/home/yangrm/Projects/ai-work-journal/webapp/screenshots";
mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const check = (name, cond, extra = "") =>
  results.push(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);

const browser = await puppeteer.launch({
  executablePath: "/usr/bin/google-chrome",
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-device-scale-factor=1"],
  defaultViewport: { width: 1600, height: 1000 },
});
// the checks below read Chinese UI text; pin the language (a separate block checks English)
async function zhPage() {
  const p = await browser.newPage();
  await p.evaluateOnNewDocument(() => { try { localStorage.setItem("lang", "zh"); } catch { /* */ } });
  return p;
}

async function open(url) {
  const page = await zhPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); });
  await page.goto(BASE + url, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector(".tl2-body", { timeout: 15000 });
  await sleep(1500);
  return { page, errors };
}
const viewLabel = (page) => page.$eval(".tl2-bar .tabular-nums", (e) => e.textContent);
const spanMin = (label) => {
  const [a, b] = label.split("–").map((t) => +t.slice(0, 2) * 60 + +t.slice(3, 5));
  return b - a;
};
const centerOf = (page, sel) => page.evaluate((sel) => {
  const els = [...document.querySelectorAll(sel)].filter((e) => e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  const el = els.sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
  const r = el.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}, sel);

// ---------- 1. layout: insights first, overview lane, project lanes on expand ----------
{
  const { page, errors } = await open("/?t=today&day=2026-10-07");
  await page.evaluate(() => { try { localStorage.removeItem("tl.detail"); } catch {} });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".tl2-body");
  await sleep(1500);
  const ov = await page.evaluate(() => ({
    runs: document.querySelectorAll(".tl2-ovr").length,
    agentLines: document.querySelectorAll(".tl2-ova").length,
    lanesHidden: document.querySelectorAll(".tl2-lane:not(.tl2-ov)").length === 0,
    label: document.querySelector(".tl2-ov .tl2-lab")?.innerText ?? "",
    kpi: document.body.innerText,
  }));
  check("overview lane first: whole day on one track, agents inside it, lanes collapsed",
    ov.runs > 5 && ov.agentLines > 0 && ov.lanesHidden, JSON.stringify({ runs: ov.runs, agentLines: ov.agentLines }));
  const ovHuman = ov.label.match(/你 (\S+)/)?.[1];
  check("overview totals match the KPI header", !!ovHuman && ov.kpi.includes(`${ovHuman}\n你在电脑前活跃`), ov.label.replace(/\n/g, " "));
  await page.screenshot({ path: `${OUT}/00-today-1007-overview-lane.png` });
  await page.click(".tl2-toggle");
  await sleep(400);
  const g = await page.evaluate(() => {
    const labs = [...document.querySelectorAll(".tl2-lab b, .tl2-lab small")].map((e) => e.getBoundingClientRect())
      .filter((r) => r.height > 0);
    let overlaps = 0;
    for (let i = 0; i < labs.length; i++) for (let j = i + 1; j < labs.length; j++) {
      const a = labs[i], b = labs[j];
      if (a.left < b.right && b.left < a.right && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps++;
    }
    const lanes = [...document.querySelectorAll(".tl2-lane")];
    const clipped = lanes.filter((l) => {
      const small = l.querySelector(".tl2-lab small");
      return small && small.getBoundingClientRect().bottom > l.getBoundingClientRect().bottom + 1;
    }).length;
    const dots = [...document.querySelectorAll(".tl2-lab i")].slice(0, 3).map((i) => getComputedStyle(i).backgroundColor);
    const insight = document.querySelector(".card .line-clamp-2");
    const timeline = document.querySelector(".tl2");
    return {
      lanes: lanes.length, laneNames: lanes.map((l) => l.querySelector("b")?.textContent).slice(0, 6),
      overlaps, clipped, distinctTop3: new Set(dots).size,
      insightsAbove: !!insight && insight.getBoundingClientRect().top < timeline.getBoundingClientRect().top,
      kpiAgent: document.body.innerText.includes("峰值"),
      chapters: document.querySelectorAll(".tl2-ep").length,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      nan: /NaN|undefined/.test(document.body.innerText),
    };
  });
  check("one lane per project (+其他活动), tiny projects folded", g.lanes >= 3 && g.lanes <= 8, `${g.lanes}: ${g.laneNames.join("|")}`);
  check("lane labels never overlap or clip", g.overlaps === 0 && g.clipped === 0, `overlaps=${g.overlaps} clipped=${g.clipped}`);
  check("top project lanes have distinct colours", g.distinctTop3 === 3, `${g.distinctTop3}`);
  check("insights sit above the timeline (first screen)", g.insightsAbove);
  check("agent KPI is de-duplicated with peak parallel", g.kpiAgent);
  check("episodes shown as chapters", g.chapters >= 5, `chapters=${g.chapters}`);
  const v0 = await viewLabel(page);
  check("default view is the workday, not midnight-to-midnight", +v0.slice(0, 2) >= 7 && spanMin(v0) < 14 * 60, v0);
  check("no overflow / NaN / errors", !g.overflow && !g.nan && errors.length === 0, errors.slice(0, 2).join("; "));
  await page.screenshot({ path: `${OUT}/01-today-1007-overview.png` });

  // ---------- 2. hover readout ----------
  const st = await centerOf(page, ".tl2-st");
  await page.mouse.move(st.x, st.y);
  await sleep(250);
  const ro = await page.$eval(".tl2-readout", (e) => e.innerText);
  check("hover readout: second, window, stay, origin", /\d{2}:\d{2}:\d{2}/.test(ro) && ro.includes("已停留"), JSON.stringify(ro.slice(0, 90)));

  // ---------- 3. semantic zoom: click a merged stretch zooms in, singles get titles ----------
  const many = await centerOf(page, ".tl2-st.many");
  await page.mouse.click(many.x, many.y);
  await sleep(400);
  const v1 = await viewLabel(page);
  check("clicking a merged stretch zooms into it", spanMin(v1) < spanMin(v0), `${v0} → ${v1}`);
  await page.evaluate(() => [...document.querySelectorAll(".tl2-bar .seg button")].find((b) => b.textContent === "10 分").click());
  await sleep(400);
  const z = await page.evaluate(() => ({
    singles: document.querySelectorAll(".tl2-st.one").length,
    titled: [...document.querySelectorAll(".tl2-st .t")].filter((t) => t.textContent.length > 1).length,
  }));
  check("zoomed in: individual titled window blocks", z.singles > 0 && z.titled > 0, JSON.stringify(z));
  await page.screenshot({ path: `${OUT}/02-today-1007-zoomed.png` });

  // ---------- 4. keyboard + ctrl-wheel + drag ----------
  const k0 = await viewLabel(page);
  await page.focus(".tl2");
  await page.keyboard.press("0");
  await sleep(250);
  const k1 = await viewLabel(page);
  check("key 0 returns to the workday", k1 === v0, `${k0} → ${k1}`);
  const tr = await centerOf(page, ".tl2-hbg");
  await page.mouse.move(tr.x, tr.y);
  await page.keyboard.down("Control");
  await page.mouse.wheel({ deltaY: -400 });
  await page.keyboard.up("Control");
  await sleep(300);
  const k2 = await viewLabel(page);
  check("ctrl+wheel zooms at the cursor", spanMin(k2) < spanMin(k1), `${k1} → ${k2}`);
  await page.mouse.move(tr.x, tr.y + 2);
  await page.mouse.down();
  await page.mouse.move(tr.x - 250, tr.y + 2, { steps: 6 });
  await page.mouse.up();
  await sleep(300);
  const k3 = await viewLabel(page);
  check("drag pans in time", k3 !== k2 && spanMin(k3) === spanMin(k2), `${k2} → ${k3}`);

  // ---------- 5. minimap click recentres ----------
  const mini = await page.$eval(".tl2-mini", (e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
  await page.mouse.click(mini.x + mini.w * 0.95, mini.y + mini.h / 2);
  await sleep(300);
  const k4 = await viewLabel(page);
  check("minimap click moves the view", k4 !== k3, `${k3} → ${k4}`);

  // ---------- 5b. minimap frame edges resize the view ----------
  await page.focus(".tl2");
  await page.keyboard.press("0");
  await sleep(200);
  const r0 = await viewLabel(page);
  const hr = await page.$eval(".tl2-mini-h.r", (e) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; });
  await page.mouse.move(hr.x, hr.y);
  await page.mouse.down();
  await page.mouse.move(hr.x - 200, hr.y, { steps: 8 });
  await page.mouse.up();
  await sleep(300);
  const r1 = await viewLabel(page);
  check("dragging the frame's right edge shortens the view (start fixed)",
    r1.slice(0, 5) === r0.slice(0, 5) && spanMin(r1) < spanMin(r0), `${r0} → ${r1}`);
  const hl = await page.$eval(".tl2-mini-h.l", (e) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; });
  await page.mouse.move(hl.x, hl.y);
  await page.mouse.down();
  await page.mouse.move(hl.x + 120, hl.y, { steps: 8 });
  await page.mouse.up();
  await sleep(300);
  const r2 = await viewLabel(page);
  check("dragging the left edge moves the start (end fixed)",
    r2.slice(-5) === r1.slice(-5) && spanMin(r2) < spanMin(r1), `${r1} → ${r2}`);
  const hl2 = await page.$eval(".tl2-mini-h.l", (e) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; });
  await page.mouse.move(hl2.x, hl2.y);
  await page.mouse.down();
  await page.mouse.move(hl2.x + 2000, hl2.y, { steps: 8 });
  await page.mouse.up();
  await sleep(300);
  const r3 = await viewLabel(page);
  check("edges cannot cross (minimum span kept)", spanMin(r3) >= 3, `${r2} → ${r3}`);

  // ---------- 6. episode chapter → detail + zoom ----------
  await page.focus(".tl2");
  await page.keyboard.press("0");
  await sleep(200);
  const ep = await centerOf(page, ".tl2-ep");
  await page.mouse.click(ep.x, ep.y);
  await sleep(400);
  const d = await page.evaluate(() => ({
    tab: document.querySelector(".rail-tabs .on")?.textContent,
    mix: document.querySelector("aside").innerText.includes("时间构成"),
    sel: !!document.querySelector(".tl2-ep.sel"),
  }));
  const v2 = await viewLabel(page);
  check("chapter click: detail tab + view zooms to the episode", d.tab === "详情" && d.mix && d.sel && spanMin(v2) < spanMin(v0), `${JSON.stringify(d)} ${v2}`);
  await page.screenshot({ path: `${OUT}/03-today-1007-episode.png` });

  // ---------- 7. flow follows view + hover sync ----------
  await page.evaluate(() => [...document.querySelectorAll(".rail-tabs button")].find((b) => b.textContent === "活动流").click());
  await sleep(300);
  const blk = await centerOf(page, ".tl2-st");
  await page.mouse.move(blk.x, blk.y);
  await sleep(250);
  const f = await page.evaluate(() => ({
    rows: document.querySelectorAll(".flow-row").length,
    hot: !!document.querySelector(".flow-row.hot"),
    follows: document.querySelector("aside").innerText.includes("跟随时间轴视野"),
  }));
  check("activity flow follows the view and syncs hover", f.rows > 0 && f.hot && f.follows, JSON.stringify(f));

  // ---------- 8. expand a lane to sessions ----------
  await page.click(".tl2-lane:not(.tl2-ov) .tl2-lab");
  await sleep(300);
  const srows = await page.$$eval(".tl2-srow", (r) => r.length);
  check("lane label expands to per-session rows", srows > 0, `rows=${srows}`);
  check("no errors during interactions", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.close();
}

// ---------- 9. other days ----------
for (const [day, name] of [["2026-10-05", "heavy"], ["2026-09-30", "no-observation"], ["2026-10-08", "partial"]]) {
  const { page, errors } = await open(`/?t=today&day=${day}`);
  const info = await page.evaluate(() => ({
    eps: document.querySelectorAll(".tl2-ep").length, ex: document.querySelectorAll(".tl2-ex").length,
    st: document.querySelectorAll(".tl2-ovr").length,
    nan: /NaN/.test(document.body.innerText), banner: document.body.innerText.includes("没有前台窗口观察"),
  }));
  const ok = name === "no-observation" ? info.banner && info.st === 0 && info.ex > 0 : info.eps > 0 && info.st > 0 && info.ex > 0;
  check(`${day} (${name}) renders`, ok && !info.nan, JSON.stringify(info));
  check(`${day} no errors`, errors.length === 0, errors.slice(0, 2).join("; "));
  await page.screenshot({ path: `${OUT}/day-${day}.png` });
  await page.close();
}

// ---------- 10. projects & rules page (read-only checks: previews, never saves) ----------
{
  const page = await zhPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(BASE + "/?t=projects", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("aside .card", { timeout: 15000 });
  await sleep(1500);
  const g = await page.evaluate(() => ({
    cards: document.querySelectorAll(".chip-x").length,
    names: [...document.querySelectorAll("h3")].map((h) => h.textContent),
    uncl: document.querySelectorAll("aside button.grid").length,
    junk: [...document.querySelectorAll("h3")].some((h) => /^(org\.|apport|snap-|code_code|Unassigned$)/.test(h.textContent)),
  }));
  check("projects page lists real projects with rule chips, no app-id junk", g.cards > 3 && !g.junk, g.names.slice(0, 8).join("|"));
  check("unclassified windows listed", g.uncl > 3, `rows=${g.uncl}`);
  // tester
  await page.type("aside input[placeholder*='窗口标题']", "CASMI26 | v4n Fusion + PubChem");
  await sleep(700);
  const t = await page.$eval("aside .card", (e) => e.innerText);
  check("tester explains a title (numeric-suffix keyword)", t.includes("CASMI") && t.includes("因为"), JSON.stringify(t.slice(-40)));
  // preview a rule for the first unclassified row
  await page.evaluate(() => document.querySelectorAll("aside button.grid")[0].click());
  await sleep(200);
  const save0 = await page.evaluate(() => [...document.querySelectorAll("aside button")].find((b) => b.textContent === "保存规则").disabled);
  await page.evaluate(() => {
    const sel = [...document.querySelectorAll("aside select")].find((s) => [...s.options].some((o) => o.textContent === "选择项目…"));
    sel.value = sel.options[1].value;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await sleep(200);
  await page.evaluate(() => [...document.querySelectorAll("aside button")].find((b) => b.textContent === "预览影响").click());
  await sleep(1200);
  const pv = await page.$eval("aside", (e) => e.innerText);
  check("rule preview reports impact before saving (save disabled until previewed)",
    save0 && (pv.includes("将把") || pv.includes("没有窗口")), JSON.stringify(pv.match(/将把[^\n]*|近 14 天没有[^\n]*/)?.[0] ?? ""));
  check("projects page has no JS errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.screenshot({ path: `${OUT}/04-projects-rules.png` });
  await page.close();
}

// ---------- 11. analytics page ----------
{
  const page = await zhPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); });
  await page.goto(BASE + "/?t=analytics", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".card svg", { timeout: 15000 });
  await sleep(1500);
  const a = await page.evaluate(() => ({
    charts: document.querySelectorAll(".card svg").length,
    tiles: [...document.querySelectorAll(".card")].filter((c) => c.querySelector(".text-\\[24px\\]")).length,
    insights: document.querySelectorAll(".card li").length,
    text: document.body.innerText,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  }));
  check("analytics: 6 KPI tiles, charts and computed insights", a.tiles === 6 && a.charts >= 6 && a.insights >= 3,
    `tiles=${a.tiles} charts=${a.charts} insights=${a.insights}`);
  check("analytics: unobserved days are labelled, not drawn as zero", a.text.includes("无窗口数据") || !a.text.includes("天有窗口观察"));
  check("analytics: confidence shown on insights", /可信度 [高中低]/.test(a.text));
  check("analytics: no NaN / overflow", !/NaN|undefined/.test(a.text) && !a.overflow);
  const sub0 = await page.$eval("h1 + div", (e) => e.textContent);
  await page.evaluate(() => [...document.querySelectorAll(".seg button")].find((b) => b.textContent === "近 7 天").click());
  await sleep(1200);
  const sub1 = await page.$eval("h1 + div", (e) => e.textContent);
  check("analytics: range preset reloads data", sub0 !== sub1 && sub1.startsWith("7 天"), `${sub0} → ${sub1}`);
  check("analytics: no errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.screenshot({ path: `${OUT}/05-analytics.png`, fullPage: true });
  await page.close();
}

// ---------- 12. reports / sessions / projects pages (read-only checks) ----------
{
  const page = await zhPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); });

  await page.goto(BASE + "/?t=reports", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("article.md h1, article.md h2", { timeout: 15000 });
  await sleep(800);
  const rp = await page.evaluate(() => ({
    rows: [...document.querySelectorAll("button")].filter((b) => /^\d\d\/\d\d 周/.test(b.innerText)).length,
    feishu: document.querySelectorAll('a[href*="feishu.cn"]').length,
    days: [...document.querySelectorAll("button")].filter((b) => /^[一二三四五六日]$/.test(b.innerText.trim())).length,
    switches: document.querySelectorAll('[role="switch"]').length,
    heading: document.querySelector("article.md")?.innerText.slice(0, 20),
  }));
  check("reports: list with Feishu links and a rendered report", rp.rows >= 3 && rp.feishu >= 2 && !!rp.heading, JSON.stringify(rp));
  check("reports: weekday switches + weekly/Feishu toggles", rp.days === 7 && rp.switches === 2, `days=${rp.days} switches=${rp.switches}`);
  await page.screenshot({ path: `${OUT}/06-reports.png` });

  await page.goto(BASE + "/?t=sessions&day=2026-10-08", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".rail-tabs", { timeout: 15000 });
  await sleep(1200);
  const ss = await page.evaluate(() => ({
    rows: document.querySelectorAll(".xl\\:sticky button").length,
    titled: [...document.querySelectorAll(".xl\\:sticky .line-clamp-2")].filter((e) => !e.textContent.startsWith("<")).length,
    raw: [...document.querySelectorAll(".xl\\:sticky .line-clamp-2")].filter((e) => e.textContent.startsWith("<")).length,
    detail: !!document.querySelector("h2") && document.body.innerText.includes("条指令"),
    bubbles: document.querySelectorAll(".border-l-\\[3px\\]").length,
  }));
  check("sessions: titled rows (no raw injected tags) + detail transcript", ss.rows >= 5 && ss.raw === 0 && ss.detail && ss.bubbles > 0, JSON.stringify(ss));
  await page.evaluate(() => [...document.querySelectorAll("label")].find((l) => l.textContent.includes("只看我的指令")).querySelector("input").click());
  await sleep(300);
  const onlyMine = await page.evaluate(() => document.querySelectorAll(".rail-tabs ~ div .rounded-lg").length === document.querySelectorAll(".rail-tabs ~ div .border-l-\\[3px\\]").length);
  check("sessions: '只看我的指令' leaves only your instructions", onlyMine);
  await page.screenshot({ path: `${OUT}/07-sessions.png` });

  await page.goto(BASE + "/?t=projects", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("aside .card", { timeout: 15000 });
  await sleep(1500);
  const pj = await page.evaluate(() => ({
    statCards: [...document.querySelectorAll(".card")].filter((c) => c.innerText.includes("你的时间") && c.innerText.includes("Agent 执行")).length,
    rulesCollapsed: [...document.querySelectorAll("button")].filter((b) => b.textContent.includes("▸ 归类规则")).length,
  }));
  check("projects: per-project 30-day stats with rules collapsed", pj.statCards >= 3 && pj.rulesCollapsed >= 3, JSON.stringify(pj));
  await page.evaluate(() => [...document.querySelectorAll("button")].find((b) => b.textContent.includes("▸ 归类规则")).click());
  await sleep(300);
  const opened = await page.evaluate(() => document.querySelectorAll(".chip-x").length > 0);
  check("projects: expanding rules shows the rule chips", opened);
  check("reports/sessions/projects: no errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.screenshot({ path: `${OUT}/04-projects-rules.png` });
  await page.close();
}


// ---------------- day picker ----------------
{
  const { page, errors } = await open("/?t=today&day=2026-10-07");
  const trig = () => page.$eval(".dp-trigger", (e) => e.textContent);
  check("day picker: no long <select> of dates", !(await page.$$eval("select", (ss) => ss.some((s) => [...s.options].some((o) => /^\d{4}-\d{2}-\d{2}$/.test(o.textContent))))));
  check("day picker: trigger shows date + weekday", (await trig()).includes("10-07 周三"), await trig());
  await page.click(".dp-trigger");
  await page.waitForFunction(() => document.querySelectorAll(".dp-cell[style]").length > 0, { timeout: 5000 });
  const cal = await page.evaluate(() => {
    const pop = document.querySelector(".dp-pop"), r = pop.getBoundingClientRect();
    return { cells: pop.querySelectorAll(".dp-cell").length, shaded: pop.querySelectorAll(".dp-cell[style]").length,
      off: pop.querySelectorAll(".dp-cell.off").length, sel: pop.querySelector(".dp-cell.sel")?.textContent,
      title: pop.textContent.slice(0, 12), h: Math.round(r.height), w: Math.round(r.width), inView: r.right <= innerWidth && r.bottom <= innerHeight };
  });
  check("day picker: fixed-size month grid, shaded by work, selected day marked",
    cal.cells === 42 && cal.shaded >= 3 && cal.sel === "7" && cal.h < 360 && cal.inView, JSON.stringify(cal));
  const shaded = await page.$$(".dp-cell[style]:not(.sel)");
  await shaded[0].hover();
  await sleep(150);
  const readout = await page.$eval(".dp-pop", (e) => e.textContent);
  check("day picker: hover shows the day's time and projects", /你 \d/.test(readout), readout.match(/\d{2}-\d{2} 周.[^少]*/)?.[0]);
  await page.screenshot({ path: `${OUT}/11-day-picker.png` });
  await page.evaluate(() => [...document.querySelectorAll(".dp-pop button")].find((b) => b.title === "上个月").click());
  await sleep(600);
  const m = await page.$eval(".dp-pop", (e) => e.textContent.slice(0, 12));
  check("day picker: month navigation", m.includes("9月"), m);
  await page.keyboard.press("Escape");
  await sleep(100);
  check("day picker: Esc closes", !(await page.$(".dp-pop")));
  await page.click(".dp-trigger");
  await page.waitForSelector(".dp-pop");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await sleep(1200);
  check("day picker: keyboard pick in the calendar", (await trig()).includes("10-06"), await trig());
  await page.click("h1");
  await page.keyboard.press("ArrowRight");
  await sleep(1200);
  check("day picker: page-level → steps to the next recorded day", (await trig()).includes("10-07"), await trig());
  check("day picker: no errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.goto(BASE + "/?t=sessions", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".dp-trigger", { timeout: 8000 });
  check("day picker: also on the 会话 page", true);
  await page.close();
}


// ---------------- long-running sessions + screen-capture status ----------------
{
  const { page, errors } = await open("/?t=today&day=2026-10-12");
  const q = await page.evaluate(() => [...document.querySelectorAll("li")].find((l) => l.textContent.startsWith("屏幕截图"))?.textContent);
  check("quality card reports the day's screen frames", !!q, q);
  const alert = await page.$(".screen-alert");
  if (alert) {
    await alert.click();
    await page.waitForSelector(".screen-alert-pop");
    const t = await page.$eval(".screen-alert-pop", (e) => e.textContent);
    check("header warns that screen capture stopped, with cause and fix", t.includes("原因") && t.includes("处理"), t.slice(0, 80));
    await page.screenshot({ path: `${OUT}/12-screen-alert.png` });
  } else check("screen capture healthy: no header warning", true);
  await page.goto(BASE + "/?t=sessions", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".dp-trigger", { timeout: 8000 });
  await sleep(1500);
  // open the first carried-over session (the newest row may have started today)
  const opened = await page.evaluate(() => {
    const r = [...document.querySelectorAll(".card button")].find((b) => b.textContent.includes("自 ") && b.textContent.includes("起"));
    if (r) r.click();
    return !!r;
  });
  if (opened) await sleep(1200);
  const s = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".card button")].filter((b) => b.textContent.includes("自 ") && b.textContent.includes("起"));
    const pane = document.querySelectorAll(".card")[1]?.textContent ?? "";
    return { carried: rows.length, latest: rows.filter((r) => r.textContent.includes("最近:")).length,
      chip: pane.includes("所选日期:"), earlier: pane.includes("显示更早的") };
  });
  check("sessions: sessions carried from earlier days are described by the selected day", s.carried === 0 || (s.latest > 0 && s.chip), JSON.stringify(s));
  check("sessions: carried session opens at the selected day (earlier messages folded)", s.carried === 0 || s.earlier, JSON.stringify(s));
  await page.screenshot({ path: `${OUT}/13-sessions-carried.png` });
  check("sessions/status: no errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.close();
}

// ---------------- 问 AI ----------------
{
  const { page, errors } = await open("/?t=today&day=2026-10-07");
  await page.evaluate(() => { try { localStorage.removeItem("chat.thread"); } catch {} });
  check("chat: floating 问 AI button on every page", !!(await page.$(".chat-fab")));
  await page.keyboard.down("Control"); await page.keyboard.press("j"); await page.keyboard.up("Control");
  await page.waitForSelector(".chat-panel", { timeout: 3000 });
  await sleep(400); // padding animates (.18s)
  const pad = await page.$eval(".app-root", (e) => getComputedStyle(e).paddingRight);
  check("chat: Ctrl+J opens the panel and the page makes room for it", pad === "440px", pad);
  const sug = await page.$$eval(".chat-suggest", (b) => b.map((x) => x.textContent));
  check("chat: context-aware suggestions when empty", sug.length >= 3, sug[0]);
  await page.type(".chat-input textarea", "10 月 7 日下午我在 RSI 上做了什么?列出相关会话链接");
  await page.keyboard.press("Enter");
  const t0 = Date.now();
  await page.waitForSelector(".chat-step", { timeout: 30000 });
  await page.waitForFunction(() => !document.querySelector(".chat-send.stop"), { timeout: 150000 });
  const r = await page.evaluate(() => ({
    steps: document.querySelectorAll(".chat-step, .chat-steps").length,
    head: document.querySelector(".chat-steps-head")?.textContent,
    answer: document.querySelector(".chat-md")?.textContent?.length ?? 0,
    links: [...document.querySelectorAll('.chat-md a[href^="#session/"]')].map((a) => a.getAttribute("href")),
    err: document.querySelector(".chat-turn .text-red-600")?.textContent ?? null,
    ctx: document.querySelector(".chat-foot")?.textContent,
  }));
  check("chat: tool steps stream in, then a markdown answer", r.steps > 0 && r.answer > 80 && !r.err,
    `${Math.round((Date.now() - t0) / 1000)}s · ${r.head} · ${r.answer} chars${r.err ? " · " + r.err : ""}`);
  check("chat: answer links to sessions", r.links.length > 0, r.links.slice(0, 3).join(" "));
  check("chat: page context shown (tab · day)", /今日 · 2026-10-07/.test(r.ctx || ""), r.ctx);
  await page.screenshot({ path: `${OUT}/08-chat.png` });
  if (r.links.length) {
    const id = r.links[0].split("/")[1];
    await page.click(`.chat-md a[href="${r.links[0]}"]`);
    await page.waitForFunction((id) => location.search.includes("t=sessions") &&
      document.querySelector(".chat-foot")?.textContent?.includes(`#${id}`), { timeout: 10000 }, id).catch(() => {});
    const onSess = await page.evaluate(() => location.search);
    const foot = await page.$eval(".chat-foot", (e) => e.textContent);
    check("chat: clicking a session link opens it on the 会话 page", onSess.includes("t=sessions") && foot.includes(`#${id}`), `${onSess} · ${foot}`);
    await sleep(800);
    await page.screenshot({ path: `${OUT}/09-chat-session-link.png` });
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".chat-panel", { timeout: 8000 });
  await page.waitForSelector(".chat-q", { timeout: 8000 }).catch(() => {});
  const kept = await page.$$eval(".chat-q", (q) => q.length);
  check("chat: panel and conversation survive a reload", kept === 1, `turns=${kept}`);
  // OCR screenshots: the answer embeds a frame that really loads (jpg or extracted from screenpipe's mp4)
  await page.click(".chat-icon[title='新对话']");
  await page.type(".chat-input textarea", "10-05 屏幕上我看过哪些 GitHub 页面?嵌入一张截图");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".chat-step", { timeout: 30000 });
  await page.waitForFunction(() => !document.querySelector(".chat-send.stop"), { timeout: 150000 });
  await page.waitForFunction(() => [...document.querySelectorAll(".chat-md img")].some((i) => i.complete && i.naturalWidth > 0),
    { timeout: 20000 }).catch(() => {});
  const img = await page.evaluate(() => {
    const ims = [...document.querySelectorAll(".chat-md img")];
    return { n: ims.length, ok: ims.filter((i) => i.naturalWidth > 0).length, steps: document.querySelector(".chat-steps-head")?.textContent };
  });
  check("chat: OCR answer embeds screenshots that load", img.ok > 0, JSON.stringify(img));
  await page.screenshot({ path: `${OUT}/10-chat-ocr.png` });
  check("chat: no errors", errors.length === 0, errors.slice(0, 2).join("; "));
  await page.close();
}

// ---- English UI: the toggle switches pages and backend-generated text
{
  const page = await browser.newPage();
  await page.goto(BASE + "/?t=today&lang=en", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".tab", { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 2500));
  const en = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll(".tab")].map((t) => t.textContent),
    html: document.documentElement.lang,
    cjk: (document.querySelector("header")?.textContent.match(/[\u4e00-\u9fff]/g) || []).join(""),
  }));
  check("i18n: ?lang=en renders English tabs", en.tabs.join(",") === "Today,Analytics,Sessions,Projects,Reports", en.tabs.join(","));
  check("i18n: <html lang> follows", en.html === "en", en.html);
  check("i18n: header has only the 中 toggle in CJK", en.cjk === "中", en.cjk);
  const kpi = await page.evaluate(() => fetch("/api/v1/days").then((r) => r.json()).then((d) => fetch("/api/v1/today?day=" + d.days.at(-1)))
    .then((r) => r.json()).then((t) => t.kpis[0].label));
  check("i18n: backend writes English under the lang cookie", kpi === "Human active", kpi);
  await page.click("header button[title='切换到中文']");
  await new Promise((r) => setTimeout(r, 1500));
  const zh = await page.$$eval(".tab", (t) => t.map((x) => x.textContent).join(","));
  check("i18n: toggle switches back to Chinese", zh === "今日,分析,会话,项目,日报", zh);
  await page.close();
}

await browser.close();
console.log("\n===== RESULTS =====");
for (const r of results) console.log(r);
if (results.some((r) => r.startsWith("FAIL"))) process.exit(1);
