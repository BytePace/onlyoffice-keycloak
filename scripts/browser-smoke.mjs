#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const moduleName = process.env.PLAYWRIGHT_MODULE || "@playwright/test";
const DEFAULT_SMOKE_PASSWORD = "SmokePass123!";
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

let chromium;
try {
  ({ chromium } = await import(moduleName));
} catch (error) {
  console.error(
    `[browser-smoke] Failed to import ${moduleName}. Install it with:\n` +
      `  npm install ${moduleName}\n` +
      `and install Chromium with:\n` +
      `  npx playwright install chromium`
  );
  process.exit(1);
}

const args = parseArgs(process.argv.slice(2));
const baseUrl = requiredArg(args, "base-url");
const headless = args.headless !== "false";
const timeoutMs = Number(args.timeout || 60000);
const screenshotPath = args.screenshot || "";
const fileName = args["file-name"] || `Smoke ${Date.now()}`;
const useManagedSmokeUser = args["manage-smoke-user"] === "true";
const keycloakUrl = args["keycloak-url"] || "https://auth.bytepace.com";
const realm = args.realm || "ssa";
const keycloakAdminPassword = args["keycloak-admin-password"] || "";

let username = args.username || "";
let password = args.password || "";
let createdSmokeUserEmail = "";

if (useManagedSmokeUser) {
  requiredArg(args, "keycloak-admin-password");
  username ||= `smoke-${Date.now()}@bytepace.test`;
  password ||= DEFAULT_SMOKE_PASSWORD;
  createManagedSmokeUser({
    email: username,
    password,
    keycloakUrl,
    realm,
    keycloakAdminPassword,
  });
  createdSmokeUserEmail = username;
} else {
  username = requiredArg(args, "username");
  password = requiredArg(args, "password");
}

const browser = await chromium.launch({ headless });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(timeoutMs);

try {
  await page.goto(normalizeBaseUrl(baseUrl) + "/login", { waitUntil: "domcontentloaded" });
  await page.waitForURL(/\/realms\/[^/]+\/protocol\/openid-connect\/auth/, { timeout: timeoutMs });

  await fillFirst(page, [
    'input[name="username"]',
    'input[id="username"]',
    'input[type="email"]',
  ], username);
  await fillFirst(page, [
    'input[name="password"]',
    'input[id="password"]',
    'input[type="password"]',
  ], password);

  await clickFirst(page, [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign In")',
    'button:has-text("Log in")',
  ]);

  await page.waitForURL(/\/apps\/files\/files/, { timeout: timeoutMs });
  await page.waitForLoadState("networkidle");

  const newButton = page.getByRole("button", { name: /^New$/i }).first();
  await newButton.click();

  const spreadsheetOption = page.locator(
    'button, a, li, div[role="menuitem"], span'
  ).filter({ hasText: /spreadsheet/i }).first();
  await spreadsheetOption.waitFor({ state: "visible", timeout: timeoutMs });
  await spreadsheetOption.click();

  const nameInput = page.locator('input[type="text"]').filter({ has: page.locator(":scope") }).first();
  await nameInput.waitFor({ state: "visible", timeout: timeoutMs });
  await nameInput.fill(fileName);
  await page.keyboard.press("Enter");

  await waitForEditor(page, timeoutMs);

  if (screenshotPath) {
    await page.screenshot({ path: screenshotPath, fullPage: true });
  }

  const result = {
    ok: true,
    baseUrl: normalizeBaseUrl(baseUrl),
    username,
    fileName,
    finalUrl: page.url(),
    screenshotPath: screenshotPath || null,
  };
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  if (screenshotPath) {
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
  }
  console.error("[browser-smoke] Smoke test failed:");
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
} finally {
  await browser.close();
  if (createdSmokeUserEmail) {
    try {
      deleteManagedSmokeUser({
        email: createdSmokeUserEmail,
        keycloakUrl,
        realm,
        keycloakAdminPassword,
      });
    } catch (error) {
      console.error(
        `[browser-smoke] Failed to delete managed smoke user ${createdSmokeUserEmail}: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
      process.exitCode = 1;
    }
  }
}

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) {
      continue;
    }
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      parsed[key] = "true";
    } else {
      parsed[key] = next;
      i += 1;
    }
  }
  return parsed;
}

function requiredArg(args, key) {
  if (!args[key]) {
    console.error(`[browser-smoke] Missing required option --${key}`);
    process.exit(1);
  }
  return args[key];
}

function normalizeBaseUrl(url) {
  return url.replace(/\/+$/, "");
}

async function fillFirst(page, selectors, value) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.count()) {
      await locator.fill(value);
      return;
    }
  }
  throw new Error(`No matching input found for selectors: ${selectors.join(", ")}`);
}

async function clickFirst(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.count()) {
      await locator.click();
      return;
    }
  }
  throw new Error(`No clickable element found for selectors: ${selectors.join(", ")}`);
}

async function waitForEditor(page, timeoutMs) {
  const editorSignals = [
    page.locator("iframe").first().waitFor({ state: "visible", timeout: timeoutMs }),
    page.locator("text=ONLYOFFICE").first().waitFor({ state: "visible", timeout: timeoutMs }),
    page.waitForURL(/editor|openfile=true|onlyoffice/i, { timeout: timeoutMs }),
  ];

  await Promise.any(editorSignals);
}

function createManagedSmokeUser({ email, password, keycloakUrl, realm, keycloakAdminPassword }) {
  const scriptPath = join(SCRIPT_DIR, "manage-smoke-user.sh");
  execFileSync(
    "bash",
    [
      scriptPath,
      "create",
      "--keycloak-url",
      keycloakUrl,
      "--realm",
      realm,
      "--keycloak-admin-password",
      keycloakAdminPassword,
      "--email",
      email,
      "--password",
      password,
    ],
    { stdio: "inherit" }
  );
}

function deleteManagedSmokeUser({ email, keycloakUrl, realm, keycloakAdminPassword }) {
  const scriptPath = join(SCRIPT_DIR, "manage-smoke-user.sh");
  execFileSync(
    "bash",
    [
      scriptPath,
      "delete",
      "--keycloak-url",
      keycloakUrl,
      "--realm",
      realm,
      "--keycloak-admin-password",
      keycloakAdminPassword,
      "--email",
      email,
    ],
    { stdio: "inherit" }
  );
}
