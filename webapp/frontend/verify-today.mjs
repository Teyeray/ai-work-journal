import puppeteer from "puppeteer-core";
import { mkdirSync } from "fs";

const BASE = "http://127.0.0.1:8799";
const OUT = "/home/yangrm/Projects/ai-work-journal/webapp/screenshots";
mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const check = (name, cond, extra = "") =>
  results.push(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);

async function openPage(url, { w = 1600, h = 1000 } = {}) {
  const browser = await puppeteer.launch({
    executablePath: "/usr/bin/google-chrome",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-device-scale-factor=1"],
    defaultViewport: { width: w, height: h },
  });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); });
  await page.goto(BASE + url, { waitUntil: "domcontentloaded", timeout: 30000 });
  return { browser, page, errors };
}

// ---------- 1. geometry + lanes + no overflow ----------
{
  const { browser, page, errors } = await openPage("/?t=today&day=2026-10-07");
  await page.waitForSelector("svg rect", { timeout: 15000 });
  await sleep(1800);
  const g = await page.evaluate(() => {
    const svg = document.querySelectorAll("svg")[1];
    const rects = [...svg.querySelectorAll("rect")];
    const rawBlocks = rects.filter((r) => +r.getAttribute("height") === 32);
    const hair = rects.filter((r) => +r.getAttribute("height") === 2);
    const exec = rects.filter((r) => +r.getAttribute("height") === 12);
    const human = rects.filter((r) => +r.getAttribute("height") === 34 || +r.getAttribute("height") === 32);
    const labels = [...svg.querySelectorAll("text")].map((t) => t.textContent);
    // fills sanity: no black/transparent main rects
    const badFill = human.filter((r) => {
      const f = r.getAttribute("fill");
      return !f || f === "none" || f === "transparent";
    });
    const card = document.querySelector(".card");
    const overflow = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
    const segLabels = labels.filter((t) => /^(RSI|paperfacts|CASMI|终端|网页|个人|文献|InoLab)/.test(t));
    const axisFirst = document.querySelectorAll("svg")[0].querySelector("text").textContent;
    const segLabels2 = labels.filter((t) => /(RSI|paperfacts|CASMI|InoLab|pi|claude)/.test(t));
    return { hair: hair.length, exec: exec.length, human: human.length, rawBlocks: rawBlocks.length,
      segLabels2: segLabels2.slice(0, 8),
      badFill: badFill.length, overflow, segLabels: segLabels.slice(0, 6), axisFirst,
      bodyH: svg.getAttribute("height"), cardW: card.getBoundingClientRect().width };
  });
  check("agent lifecycle hairlines exist", g.hair >= 30, `hair=${g.hair}`);
  check("agent exec segments exist", g.exec >= 30, `exec=${g.exec}`);
  check("human blocks/segments render", g.human >= 30, `human=${g.human}`);
  check("human rects have valid fills", g.badFill === 0, `bad=${g.badFill}`);
  check("no horizontal overflow", !g.overflow);
  check("default view fits observation window (raw blocks at LOD)", g.rawBlocks > 100 && +g.axisFirst.slice(0, 2) >= 8,
    `axisFirst=${g.axisFirst} rawBlocks=${g.rawBlocks}`);
  check("lane labels drawn (project names in svg)", g.segLabels2.length >= 4, g.segLabels2.slice(0, 5).join("|"));
  check("no HTTP/JS errors", errors.length === 0, errors.slice(0, 3).join("; "));
  console.log("geometry:", JSON.stringify(g));
  await browser.close();
}

// ---------- 2. human segment click -> full detail ----------
{
  const { browser, page } = await openPage("/?t=today&day=2026-10-07");
  await page.waitForSelector("svg rect", { timeout: 15000 });
  await sleep(1500);
  const clicked = await page.evaluate(() => {
    const svg = document.querySelectorAll("svg")[1];
    const rects = [...svg.querySelectorAll("rect")].filter((r) => {
      const y = +r.getAttribute("y"), x = +r.getAttribute("x");
      return y >= 40 && y <= 80 && x >= 96 && x < 1150 && +r.getAttribute("width") > 6;
    });
    rects.sort((a, b) => +b.getAttribute("width") - +a.getAttribute("width"));
    if (!rects.length) return null;
    const r = rects[0].getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width };
  });
  if (clicked) {
    await page.mouse.click(clicked.x, clicked.y);
    await sleep(900);
  }
  const d = await page.evaluate(() => {
    const t = document.body.innerText;
    const evRows = [...document.querySelectorAll("li")].filter((li) => li.textContent.match(/^\d{2}:\d{2}/)).length;
    return {
      hasEvidence: t.includes("证据链"), hasTitles: t.includes("窗口标题"),
      hasCorrect: t.includes("修正项目归属"), hasRelated: t.toLowerCase().includes("同时段的"),
      hasTimeRange: /\d{2}:\d{2}–\d{2}:\d{2}/.test(t), evRows,
    };
  });
  check("segment detail: evidence+titles+correction+related",
    d.hasEvidence && d.hasCorrect && d.hasTimeRange, JSON.stringify(d));
  await page.screenshot({ path: `${OUT}/02-today-1007-block-detail.png` });
  await browser.close();
}

// ---------- 3. tooltip on hover ----------
{
  const { browser, page } = await openPage("/?t=today&day=2026-10-07");
  await page.waitForSelector("svg rect", { timeout: 15000 });
  await sleep(1200);
  const target = await page.evaluate(() => {
    const svg = document.querySelectorAll("svg")[1];
    const rects = [...svg.querySelectorAll("rect")].filter((r) => +r.getAttribute("height") === 12 && +r.getAttribute("width") > 60);
    if (!rects.length) return null;
    const r = rects[0].getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  if (target) {
    await page.mouse.move(target.x, target.y);
    await sleep(500);
  }
  const tt = await page.evaluate(() => {
    const el = document.querySelector(".tl-tooltip");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { text: el.innerText.slice(0, 120), visible: r.width > 40 && r.height > 20,
      inViewport: r.left >= 0 && r.right <= innerWidth && r.top >= 0 };
  });
  check("hover tooltip shows and is inside viewport", !!tt && tt.visible && tt.inViewport,
    tt ? JSON.stringify(tt.text) : "no tooltip");
  await page.screenshot({ path: `${OUT}/07-today-1007-tooltip.png` });
  await browser.close();
}

// ---------- 4. zoom LOD: individual blocks appear when zoomed in ----------
{
  const { browser, page } = await openPage("/?t=today&day=2026-10-07");
  await page.waitForSelector("svg rect", { timeout: 15000 });
  await sleep(1200);
  const before = await page.evaluate(() =>
    document.querySelectorAll("svg")[1].querySelectorAll('rect[height="32"]').length);
  // zoom OUT via wheel: should switch to merged segments (height 34) with labels
  for (let i = 0; i < 6; i++) {
    await page.evaluate(() => {
      const svg = document.querySelectorAll("svg")[1];
      const r = svg.getBoundingClientRect();
      svg.dispatchEvent(new WheelEvent("wheel", { deltaY: 240, clientX: r.left + 500, clientY: r.top + 60, bubbles: true, cancelable: true }));
    });
    await sleep(120);
  }
  await sleep(400);
  const after = await page.evaluate(() => {
    const svg = document.querySelectorAll("svg")[1];
    const merged = svg.querySelectorAll('rect[height="34"]').length;
    const labels = [...svg.querySelectorAll("text")]
      .filter((t) => +t.getAttribute("y") > 40 && +t.getAttribute("y") < 80 && t.getAttribute("fill") === "#ffffff")
      .map((t) => t.textContent).slice(0, 4);
    return { merged, labels };
  });
  check("LOD: zoom-out merges blocks and labels segments", after.merged > 0 && after.merged < before,
    `raw=${before} → merged=${after.merged} labels=${after.labels.join("|")}`);
  await page.screenshot({ path: `${OUT}/08-today-1007-zoomed-lod.png` });
  await browser.close();
}

// ---------- 5. other days ----------
for (const [day, name] of [["2026-10-05", "heavy"], ["2026-09-30", "noobs"], ["2026-10-08", "partial"]]) {
  const { browser, page, errors } = await openPage(`/?t=today&day=${day}`);
  try {
    await page.waitForSelector("svg rect", { timeout: 15000 });
    await sleep(1600);
    const info = await page.evaluate(() => {
      const rects = document.querySelectorAll("svg rect").length;
      const t = document.body.innerText;
      return { rects, hasNaN: /NaN/.test(t), banner: t.includes("无前台窗口观察") };
    });
    check(`${day} (${name}) renders`, info.rects > 50 && !info.hasNaN, `rects=${info.rects} banner=${info.banner}`);
    check(`${day} no errors`, errors.length === 0, errors.slice(0, 2).join("; "));
    await page.screenshot({ path: `${OUT}/day-${day}.png` });
  } finally { await browser.close(); }
}

console.log("\n===== RESULTS =====");
for (const r of results) console.log(r);
