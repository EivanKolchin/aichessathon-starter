// Optional: NODE_PATH must resolve Playwright; start Chess Lab before running this file.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.goto(process.env.CHESSLAB_URL || "http://127.0.0.1:8765");
    await page.waitForSelector("#opponents input", { state: "attached" });
    assert.equal(await page.locator("#board .square").count(), 64);
    assert.equal(await page.locator('[data-square="a1"]').getAttribute("class").then(s => s.includes("dark")), true);
    assert.equal(await page.locator('[data-square="h1"]').getAttribute("class").then(s => s.includes("dark")), false);
    await page.waitForFunction(() => document.querySelector("#game-select").options[0].value.startsWith("g"));
    await page.getByRole("button", { name: "First position", exact: true }).click();
    assert.equal(await page.locator("#position-number").textContent(), "Starting position");
    await page.getByRole("button", { name: "Next move", exact: true }).click();
    assert.match(await page.locator("#position-number").textContent(), /Ply 1 /);
    await page.getByRole("button", { name: "Flip board", exact: true }).click();
    assert.equal(await page.locator("#board .square").first().getAttribute("data-square"), "h1");
    await page.getByRole("button", { name: "Flip board", exact: true }).click();
    await page.getByRole("button", { name: "Play replay", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#position-number").textContent.includes("Ply 3 "));
    await page.getByRole("button", { name: "Pause replay", exact: true }).click();
    const [download] = await Promise.all([page.waitForEvent("download"), page.locator("#download-pgn").click()]);
    assert.match(download.suggestedFilename(), /\.pgn$/);
    await page.getByRole("button", { name: "New experiment", exact: true }).click();
    await page.locator("#select-none").click();
    await page.locator('#opponents input[value="greedy"]').check();
    await page.locator('#opponents input[value="tactical"]').check();
    await page.locator("#opening-count").selectOption("2");
    await page.locator("#clock-preset").selectOption("smoke");
    await page.locator("#label").fill("Browser smoke · live board");
    assert.equal(await page.locator("#game-count").textContent(), "8 games");
    assert.match(await page.locator("#complete-test").textContent(), /Complete test . \d+ games/);
    await page.locator("#parallel").selectOption("4");
    assert.match(await page.locator("#launch-note").textContent(), /4 at once/);
    await page.locator("#parallel").selectOption("1");
    await page.locator("#start").click();
    await page.waitForFunction(() => document.querySelector("#run-status").textContent === "running");
    await page.waitForFunction(() => document.querySelector("#position-number").textContent.startsWith("Ply "));
    fs.mkdirSync(".chesslab", { recursive: true });
    await page.screenshot({ path: ".chesslab/live-desktop.png", fullPage: true });
    await page.waitForFunction(() => document.querySelector("#run-status").textContent === "completed", null, { timeout: 90000 });
    assert.equal(await page.locator("#game-map button").count(), 8);
    // A square on the map opens the game on its own; the arena keeps following the live one.
    await page.locator("#game-map button").first().click();
    await page.waitForSelector("#game-dialog[open]");
    assert.equal(await page.locator("#g-board .square").count(), 64);
    assert.match(await page.locator("#game-eyebrow").textContent(), /^Game 1 of 8$/);
    assert.ok(await page.locator("#g-facts div").count() >= 5);
    await page.getByRole("button", { name: "First position", exact: true }).nth(1).click();
    assert.equal(await page.locator("#g-position").textContent(), "Starting position");
    await page.getByRole("button", { name: "Next move", exact: true }).nth(1).click();
    assert.match(await page.locator("#g-position").textContent(), /^Ply 1 /);
    await page.getByRole("button", { name: "Close this game" }).click();
    assert.equal(await page.locator("#follow").evaluate((el) => el.classList.contains("active")), true);
    const [exported] = await Promise.all([page.waitForEvent("download"), page.locator("#export").click()]);
    assert.match(exported.suggestedFilename(), /\.zip$/);
    assert.equal(await page.locator("#resume").isVisible(), false);
    await page.locator("#data-view").click();
    assert.equal(await page.locator(".arena-grid").isVisible(), false);
    assert.equal(await page.locator(".results").isVisible(), true);
    await page.locator("#data-view").click();
    assert.equal(await page.locator(".arena-grid").isVisible(), true);
    await page.locator("#open-registry").click();
    assert.equal(await page.locator("#registry-dialog").isVisible(), true);
    assert.ok(await page.locator("#registry-engines .registry-engine").count() >= 10);
    const registered = await page.locator("#registry-engines .registry-engine").count();
    await page.locator(".path-form > summary").click();
    await page.locator("#engine-name").fill("Browser bench copy");
    assert.equal(await page.locator("#engine-id").inputValue(), "browser-bench-copy");
    await page.locator("#engine-family").fill("Bench");
    await page.locator("#engine-path").fill("does/not/exist");
    await page.locator("#engine-save").click();
    await page.waitForSelector("#engine-error:not([hidden])");
    await page.locator("#engine-path").fill("baselines/greedy");
    await page.locator("#engine-save").click();
    await page.waitForFunction((count) => document.querySelectorAll("#registry-engines .registry-engine").length === count + 1, registered);
    assert.equal(await page.locator('#opponents input[value="browser-bench-copy"]').count(), 1);
    await page.locator('.remove-engine[data-engine="browser-bench-copy"]').click();
    await page.waitForFunction((count) => document.querySelectorAll("#registry-engines .registry-engine").length === count, registered);
    // Dropping an agent in: the page zips what it was given and the lab plays a move out of it.
    const agent = [
      "import chess",
      "",
      "",
      "def get_move(fen, ms):",
      "    return next(iter(chess.Board(fen).legal_moves)).uci()",
      "",
    ].join("\n");
    await page.evaluate((source) => window.receive([
      { name: "dropped-bench/agent.py", blob: new Blob([source]) },
      { name: "dropped-bench/weights/table.txt", blob: new Blob(["0.5"]) },
    ]), agent);
    await page.waitForSelector("#drop-form:not([hidden])");
    assert.equal(await page.locator("#drop-name").inputValue(), "Dropped bench");
    assert.match(await page.locator("#drop-summary").textContent(), /^dropped-bench . 2 files/);
    await page.locator("#drop-family").fill("Bench");
    await page.locator("#drop-save").click();
    await page.waitForSelector("#drop-report:not([hidden])", { timeout: 60000 });
    assert.match(await page.locator("#drop-report").textContent(), /Started and played \w+ in/);
    assert.equal(await page.locator('#opponents input[value="dropped-bench"]').count(), 1);
    // A build that cannot play is refused, and says why.
    await page.evaluate(() => window.receive([
      { name: "agent.py", blob: new Blob(['raise RuntimeError("browser smoke failure")\n']) },
    ]));
    await page.locator("#drop-name").fill("Broken drop");
    await page.locator("#drop-save").click();
    await page.waitForSelector("#drop-error:not([hidden])", { timeout: 60000 });
    assert.match(await page.locator("#drop-error").textContent(), /browser smoke failure/);
    assert.equal(await page.locator('#opponents input[value="broken-drop"]').count(), 0);
    await page.locator('.remove-engine[data-engine="dropped-bench"]').click();
    await page.waitForFunction((count) => document.querySelectorAll("#registry-engines .registry-engine").length === count, registered);
    await page.getByRole("button", { name: "Close engine registry" }).click();
    await page.locator("#open-play-top").click();
    await page.locator("#play-engine").selectOption("greedy");
    await page.locator("#play-clock").selectOption("bullet");
    await page.locator("#play-start").click();
    await page.waitForFunction(() => document.querySelector("#outcome").textContent === "Your move", null, { timeout: 60000 });
    assert.ok(await page.locator("#board").evaluate(el => el.classList.contains("playable")));
    await page.locator('#board [data-square="e2"]').click();
    assert.equal(await page.locator("#board .square.target").count(), 2);
    await page.locator('#board [data-square="e4"]').click();
    await page.waitForFunction(() => document.querySelectorAll("#moves button").length >= 2, null, { timeout: 30000 });
    await page.screenshot({ path: ".chesslab/sparring.png" });
    await page.getByRole("button", { name: "Take back", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll("#moves button").length === 0);
    assert.match(await page.locator("#top-clock").textContent(), /^\d+:\d\d/);
    assert.match(await page.locator("#bottom-clock").textContent(), /^\d+:\d\d/);
    await page.getByRole("button", { name: "Resign", exact: true }).click();
    await page.waitForSelector("#play-result:not([hidden])");
    assert.equal(await page.locator("#result-headline").textContent(), "You lost");
    assert.equal(await page.locator("#result-detail").textContent(), "you resigned");
    await page.locator("#close-result").click();
    await page.getByRole("button", { name: "Leave", exact: true }).click();
    await page.waitForFunction(() => !document.body.classList.contains("play-mode"));
    assert.equal(await page.locator("#results-body tr").count() >= 1, true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.locator("#board").isVisible(), true);
    await page.screenshot({ path: ".chesslab/mobile.png", fullPage: true });
    await page.getByRole("button", { name: "New experiment", exact: true }).click();
    assert.equal(await page.locator("#setup-dialog").isVisible(), true);
    assert.equal(await page.locator("#setup-dialog").evaluate(el => el.scrollWidth > el.clientWidth), false);
    await page.screenshot({ path: ".chesslab/mobile-setup.png" });
    await page.getByRole("button", { name: "Close experiment setup" }).click();
    assert.deepEqual(errors, []);
    console.log("Browser checks passed: board coordinates, replay, flip, PGN, live batch, export, parallel controls, game window, registry edits, dropped agents, sparring, mobile, no JS errors.");
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
