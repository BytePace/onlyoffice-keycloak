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
const insecure = args.insecure === "true";
const keycloakUrl = args["keycloak-url"] || "https://auth.bytepace.com";
const realm = args.realm || "ssa";
const keycloakAdminPassword = args["keycloak-admin-password"] || "";
const nextcloudAdminUser = args["nextcloud-admin-user"] || "";
const nextcloudAdminPassword = args["nextcloud-admin-password"] || "";

if (insecure) {
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
}

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
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  ignoreHTTPSErrors: insecure,
});
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
  await dismissFirstRunWizard(page);

  const newButton = page.getByRole("button", { name: /^New$/i }).first();
  await newButton.click();

  const spreadsheetOption = page.locator(
    'button, a, li, div[role="menuitem"], span'
  ).filter({ hasText: /spreadsheet/i }).first();
  await spreadsheetOption.waitFor({ state: "visible", timeout: timeoutMs });
  await spreadsheetOption.click();

  const createDialog = page.getByRole("dialog").filter({ hasText: /new spreadsheet/i }).first();
  await createDialog.waitFor({ state: "visible", timeout: timeoutMs });

  const nameInput = createDialog.locator('input[type="text"]').first();
  await nameInput.waitFor({ state: "visible", timeout: timeoutMs });
  await nameInput.fill(`${fileName}.xlsx`);

  const createButton = createDialog.getByRole("button", { name: /create/i }).first();
  await createButton.click();

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
      if (nextcloudAdminUser && nextcloudAdminPassword) {
        await deleteNextcloudUserByEmail({
          baseUrl: normalizeBaseUrl(baseUrl),
          email: createdSmokeUserEmail,
          adminUser: nextcloudAdminUser,
          adminPassword: nextcloudAdminPassword,
        });
      }
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

async function dismissFirstRunWizard(page) {
  const wizard = page.locator("#firstrunwizard");
  if (!(await wizard.count())) {
    return;
  }
  if (!(await wizard.first().isVisible().catch(() => false))) {
    return;
  }

  const closeCandidates = [
    page.locator('#firstrunwizard button[aria-label*="Close" i]').first(),
    page.locator('#firstrunwizard button[title*="Close" i]').first(),
    page.locator('#firstrunwizard [class*="close"]').first(),
  ];

  for (const locator of closeCandidates) {
    if (await locator.count()) {
      await locator.click({ force: true }).catch(() => {});
      if (!(await wizard.first().isVisible().catch(() => false))) {
        return;
      }
    }
  }

  await page.keyboard.press("Escape").catch(() => {});
  await wizard.first().waitFor({ state: "hidden", timeout: 10000 }).catch(() => {});
}

function createManagedSmokeUser({ email, password, keycloakUrl, realm, keycloakAdminPassword }) {
  const scriptPath = join(SCRIPT_DIR, "manage-smoke-user.sh");
  const commandArgs = [
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
  ];
  if (insecure) {
    commandArgs.push("--insecure");
  }
  execFileSync(
    "bash",
    commandArgs,
    { stdio: "inherit" }
  );
}

function deleteManagedSmokeUser({ email, keycloakUrl, realm, keycloakAdminPassword }) {
  const scriptPath = join(SCRIPT_DIR, "manage-smoke-user.sh");
  const commandArgs = [
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
  ];
  if (insecure) {
    commandArgs.push("--insecure");
  }
  execFileSync(
    "bash",
    commandArgs,
    { stdio: "inherit" }
  );
}

async function deleteNextcloudUserByEmail({ baseUrl, email, adminUser, adminPassword }) {
  const auth = Buffer.from(`${adminUser}:${adminPassword}`).toString("base64");
  const headers = {
    Authorization: `Basic ${auth}`,
    "OCS-APIRequest": "true",
    Accept: "application/json",
  };

  const usersUrl = `${baseUrl}/ocs/v2.php/cloud/users?search=${encodeURIComponent(email)}&format=json`;
  const usersResponse = await fetch(usersUrl, { headers });
  if (!usersResponse.ok) {
    throw new Error(`Nextcloud user lookup failed with HTTP ${usersResponse.status}`);
  }

  const usersJson = await usersResponse.json();
  const userIds = usersJson?.ocs?.data?.users || [];
  for (const userId of userIds) {
    const detailsUrl = `${baseUrl}/ocs/v2.php/cloud/users/${encodeURIComponent(userId)}?format=json`;
    const detailsResponse = await fetch(detailsUrl, { headers });
    if (!detailsResponse.ok) {
      continue;
    }
    const detailsJson = await detailsResponse.json();
    const userEmail = detailsJson?.ocs?.data?.email || "";
    if (userEmail !== email) {
      continue;
    }

    const deleteUrl = `${baseUrl}/ocs/v2.php/cloud/users/${encodeURIComponent(userId)}?format=json`;
    const deleteResponse = await fetch(deleteUrl, {
      method: "DELETE",
      headers,
    });
    if (!deleteResponse.ok) {
      throw new Error(`Nextcloud user delete failed for ${userId} with HTTP ${deleteResponse.status}`);
    }
  }
}
