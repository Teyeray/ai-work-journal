import puppeteer from "puppeteer-core";
const b = await puppeteer.launch({ executablePath: "/usr/bin/google-chrome", headless: "new", args: ["--no-sandbox"] });
const pg = await b.newPage();
const errs = [];
pg.on("pageerror", (e) => errs.push(String(e).slice(0, 150)));
await pg.goto("http://127.0.0.1:8799/?t=sessions", { waitUntil: "domcontentloaded", timeout: 20000 });
await pg.waitForSelector("table tbody tr", { timeout: 8000 });
await new Promise((r) => setTimeout(r, 1500));
// click first session row
await pg.click("table tbody tr");
await new Promise((r) => setTimeout(r, 2000));
const hasDetail = await pg.evaluate(() => document.body.innerText.includes("质量评审"));
console.log("detail loaded:", hasDetail);
// click 开始评审 (cached → instant)
await pg.evaluate(() => {
  const btn = [...document.querySelectorAll("button")].find((x) => x.textContent.includes("开始评审") || x.textContent.includes("重新评审"));
  btn?.click();
});
await new Promise((r) => setTimeout(r, 3000));
const txt = await pg.evaluate(() => document.body.innerText);
console.log("review rendered:", txt.includes("/25") && txt.includes("改进建议"));
console.log("scores visible:", (txt.match(/\/\d+/g) || []).length, "score cells");
console.log("summary sample:", txt.split("会话内容")[1]?.slice(0, 80) || "");
console.log("JS errors:", errs.length ? errs : "none");
await b.close();
