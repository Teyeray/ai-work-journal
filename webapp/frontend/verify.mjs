import puppeteer from "puppeteer-core";

async function check(url, sel, label, extra) {
  const b = await puppeteer.launch({ executablePath: "/usr/bin/google-chrome", headless: "new", args: ["--no-sandbox", "--disable-gpu"] });
  const pg = await b.newPage();
  const errors = [];
  pg.on("pageerror", (e) => errors.push(String(e).slice(0, 100)));
  pg.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url().slice(0, 60)}`); });
  try {
    await pg.goto(url, { waitUntil: "domcontentloaded", timeout: 20000 });
    await pg.waitForSelector(sel, { timeout: 10000 });
    await new Promise((r) => setTimeout(r, 2000));
    const html = await pg.content();
    const rects = (html.match(/<rect/g) || []).length;
    const bad = />undefined|>NaN</.test(html);
    console.log(`${label}: len=${html.length} rects=${rects} bad=${bad}${extra ? " " + await extra(pg) : ""}`);
    if (errors.length) console.log("   http/js errors:", errors.slice(0, 3));
  } catch (e) {
    console.log(`${label}: FAILED ${String(e).slice(0, 100)}`);
    if (errors.length) console.log("   errors:", errors.slice(0, 3));
  }
  await b.close();
}

await check("http://127.0.0.1:8799/?t=today&day=2026-10-07", "svg", "TODAY");

await check("http://127.0.0.1:8799/?t=sessions", "table", "SESSIONS");
await check("http://127.0.0.1:8799/?t=projects", "table", "PROJECTS");
