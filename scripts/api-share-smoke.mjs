#!/usr/bin/env node
/**
 * End-to-end: User1 logs into /api/, creates a doc, shares with User2 email;
 * User2 logs in and checks /api/session-info (documents_in_api_list >= 1).
 *
 * Credentials via env (preferred) or CLI — do not commit them.
 *
 *   USER1_EMAIL=... USER1_PASS=... USER2_EMAIL=... USER2_PASS=... \
 *     node scripts/api-share-smoke.mjs --base-url https://sheets.bytepace.com
 */

import { chromium } from "@playwright/test";

const args = parseArgs(process.argv.slice(2));
const baseUrl = normalizeBaseUrl(
  args["base-url"] || process.env.BASE_URL || "https://sheets.bytepace.com"
);
const user1Email = args["user1-email"] || process.env.USER1_EMAIL || "";
const user1Pass = args["user1-pass"] || process.env.USER1_PASS || "";
const user2Email = args["user2-email"] || process.env.USER2_EMAIL || "";
const user2Pass = args["user2-pass"] || process.env.USER2_PASS || "";
const headless = args.headless !== "false";
const timeoutMs = Number(args.timeout || 90000);
const insecure = args.insecure === "true";
const docName = args["doc-name"] || `API share smoke ${Date.now()}`;

if (!user1Email || !user1Pass || !user2Email || !user2Pass) {
  console.error(
    "[api-share-smoke] Set USER1_EMAIL, USER1_PASS, USER2_EMAIL, USER2_PASS " +
      "(or --user1-email / --user1-pass / --user2-email / --user2-pass)."
  );
  process.exit(1);
}

if (insecure) {
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
}

const browser = await chromium.launch({ headless });
const result = { ok: false, baseUrl, steps: [] };

try {
  const ctx1 = await browser.newContext({ ignoreHTTPSErrors: insecure });
  const page1 = await ctx1.newPage();
  page1.setDefaultTimeout(timeoutMs);

  await apiLogin(page1, `${baseUrl}/api/oauth/login?redirect_to=/api/`, user1Email, user1Pass, timeoutMs);
  result.steps.push({ step: "user1_login", url: page1.url() });

  const docId = await createDocument(page1, baseUrl, docName);
  result.steps.push({ step: "user1_create_doc", docId, docName });

  const shareResp = await page1.request.post(`${baseUrl}/api/docs/${docId}/share`, {
    data: { email: user2Email, role: "viewer" },
    headers: { "Content-Type": "application/json" },
  });
  if (!shareResp.ok()) {
    const body = await shareResp.text();
    throw new Error(`Share failed HTTP ${shareResp.status()}: ${body}`);
  }
  result.steps.push({ step: "user1_share", sharedWith: user2Email });

  const ctx2 = await browser.newContext({ ignoreHTTPSErrors: insecure });
  const page2 = await ctx2.newPage();
  page2.setDefaultTimeout(timeoutMs);

  await apiLogin(
    page2,
    `${baseUrl}/api/oauth/login?redirect_to=/api/session-info`,
    user2Email,
    user2Pass,
    timeoutMs
  );
  result.steps.push({ step: "user2_login", url: page2.url() });

  const sessionResp = await page2.request.get(`${baseUrl}/api/session-info`);
  if (!sessionResp.ok()) {
    const body = await sessionResp.text();
    throw new Error(`session-info HTTP ${sessionResp.status()}: ${body.slice(0, 500)}`);
  }
  const session = await sessionResp.json();
  result.user2_session = session;

  const docCount = session.documents_in_api_list ?? 0;
  if (docCount < 1) {
    throw new Error(
      `User2 documents_in_api_list=${docCount} (expected >= 1). ` +
        `api_primary_email=${session.api_primary_email}, ` +
        `nextcloud_shared_workbooks=${session.nextcloud_shared_workbooks}. ` +
        "Ensure latest API is deployed and User1 shared via API (not only /s/ link)."
    );
  }

  const workspacesResp = await page2.request.get(`${baseUrl}/api/orgs/current/workspaces`);
  if (!workspacesResp.ok()) {
    const body = await workspacesResp.text();
    throw new Error(`workspaces HTTP ${workspacesResp.status()}: ${body.slice(0, 500)}`);
  }
  const workspaces = await workspacesResp.json();
  const iosDocCount = (workspaces || []).reduce(
    (n, ws) => n + (Array.isArray(ws.docs) ? ws.docs.length : 0),
    0
  );
  result.user2_ios_doc_count = iosDocCount;
  if (iosDocCount < 1) {
    throw new Error(`iOS list empty (workspaces docs=${iosDocCount}) but session-info shows ${docCount}`);
  }

  result.ok = true;
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error("[api-share-smoke] Failed:");
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  console.error(JSON.stringify(result, null, 2));
  process.exitCode = 1;
} finally {
  await browser.close();
}

async function apiLogin(page, startUrl, username, password, timeoutMs) {
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await page.waitForURL(/\/protocol\/openid-connect\/auth/, { timeout: timeoutMs });
  await fillFirst(page, ['input[name="username"]', 'input[id="username"]', 'input[type="email"]'], username);
  await fillFirst(page, ['input[name="password"]', 'input[id="password"]', 'input[type="password"]'], password);
  await clickFirst(page, [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign In")',
    'button:has-text("Log in")',
  ]);
  await page.waitForURL(/\/api\//, { timeout: timeoutMs });
  await page.waitForLoadState("networkidle").catch(() => {});
}

async function createDocument(page, baseUrl, name) {
  const resp = await page.request.post(`${baseUrl}/api/workspaces/1/docs`, {
    data: { name },
    headers: { "Content-Type": "application/json" },
  });
  if (!resp.ok()) {
    const body = await resp.text();
    throw new Error(`Create doc HTTP ${resp.status()}: ${body}`);
  }
  const docId = (await resp.text()).replace(/^"|"$/g, "").trim();
  if (!docId) {
    throw new Error("Create doc returned empty id");
  }
  return docId;
}

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) continue;
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
  throw new Error(`No input for: ${selectors.join(", ")}`);
}

async function clickFirst(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.count()) {
      await locator.click();
      return;
    }
  }
  throw new Error(`No button for: ${selectors.join(", ")}`);
}
