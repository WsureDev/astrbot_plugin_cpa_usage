#!/usr/bin/env node

import { createRequire } from "node:module";
import { homedir } from "node:os";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";
import fs from "node:fs";


function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) throw new Error(`unexpected argument: ${item}`);
    const key = item.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`missing value for --${key}`);
    values[key] = value;
    index += 1;
  }
  return values;
}


function loadChromiumDriver() {
  const require = createRequire(import.meta.url);
  const candidates = [
    process.env.CPA_PLAYWRIGHT_MODULE,
    "playwright",
    "playwright-core",
    path.join(homedir(), ".npm-global/lib/node_modules/@playwright/mcp/node_modules/playwright"),
    "/usr/local/lib/node_modules/playwright",
    "/usr/local/lib/node_modules/playwright-core",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const loaded = require(candidate);
      if (loaded.chromium) return loaded.chromium;
    } catch {
      // Try the next standard installation location.
    }
  }
  throw new Error("Playwright 未安装；请在项目目录运行 npm install，或设置 CPA_PLAYWRIGHT_MODULE");
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  for (const required of ["html", "output", "chromium"]) {
    if (!args[required]) throw new Error(`--${required} is required`);
  }
  const htmlPath = path.resolve(args.html);
  const outputPath = path.resolve(args.output);
  const chromiumPath = path.resolve(args.chromium);
  if (!fs.statSync(htmlPath).isFile()) throw new Error(`HTML 不存在：${htmlPath}`);
  if (!fs.statSync(chromiumPath).isFile()) throw new Error(`Chromium 不存在：${chromiumPath}`);

  const width = Math.max(320, Number.parseInt(args.width || "500", 10));
  const timeout = Math.max(1000, Number.parseInt(args.timeout || "30000", 10));
  const chromium = loadChromiumDriver();
  const browser = await chromium.launch({ headless: true, executablePath: chromiumPath });
  try {
    const page = await browser.newPage({
      viewport: { width, height: 420 },
      deviceScaleFactor: 1,
      locale: "zh-CN",
      colorScheme: "light",
    });
    page.setDefaultTimeout(timeout);
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load", timeout });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => document.documentElement.dataset.ready === "true");
    const height = await page.evaluate(() => {
      const report = document.getElementById("report");
      return Math.ceil(Math.max(
        report?.getBoundingClientRect().height || 0,
        document.documentElement.scrollHeight,
        document.body.scrollHeight,
      ));
    });
    if (!Number.isFinite(height) || height < 1 || height > 16000) {
      throw new Error(`卡片高度异常：${height}`);
    }
    await page.setViewportSize({ width, height });
    await page.screenshot({ path: outputPath, type: "png", fullPage: true, animations: "disabled" });
    process.stdout.write(JSON.stringify({ width, height, output: outputPath }));
  } finally {
    await browser.close();
  }
}


main().catch((error) => {
  process.stderr.write(`${error?.message || error}\n`);
  process.exitCode = 1;
});
