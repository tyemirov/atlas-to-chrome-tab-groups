/* global chrome */

const PALETTE = new Set(["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"]);
const parameters = new URLSearchParams(location.search);
const statusElement = document.getElementById("status");
const detailsElement = document.getElementById("details");

function setStatus(message, kind = "") {
  statusElement.textContent = message;
  statusElement.className = kind;
}

function setDetails(message) {
  detailsElement.textContent = message;
}

function migrationEndpoint() {
  const source = parameters.get("source");
  const token = parameters.get("token");
  if (!source || !token) {
    throw new Error("This page was not opened by the migration script.");
  }
  const endpoint = new URL(source);
  if (
    endpoint.protocol !== "http:" ||
    endpoint.hostname !== "127.0.0.1" ||
    endpoint.pathname !== "/migration.json" ||
    endpoint.searchParams.get("token") !== token
  ) {
    throw new Error("The migration source is not the script's local one-time server.");
  }
  return { endpoint, token };
}

function validateMigration(migration) {
  if (!migration || migration.schema_version !== 1 || !Array.isArray(migration.windows) || migration.windows.length === 0) {
    throw new Error("The local export is not a compatible Atlas migration file.");
  }
  let tabs = 0;
  let groups = 0;
  for (const windowSpec of migration.windows) {
    if (!windowSpec || !Array.isArray(windowSpec.tabs) || windowSpec.tabs.length === 0 || !Array.isArray(windowSpec.groups)) {
      throw new Error("The local export has an invalid window specification.");
    }
    let sawUnpinned = false;
    windowSpec.tabs.forEach((tab, index) => {
      if (!tab || typeof tab.url !== "string" || typeof tab.pinned !== "boolean") {
        throw new Error("The local export has an invalid tab specification.");
      }
      if (!tab.pinned) sawUnpinned = true;
      if (tab.pinned && sawUnpinned) throw new Error("Pinned Atlas tabs must appear before ordinary tabs.");
      tabs += 1;
    });
    const used = new Set();
    windowSpec.groups.forEach((group) => {
      if (
        !group ||
        typeof group.title !== "string" ||
        !PALETTE.has(group.color) ||
        typeof group.collapsed !== "boolean" ||
        !Array.isArray(group.tab_indexes) ||
        group.tab_indexes.length === 0
      ) {
        throw new Error("The local export has an invalid group specification.");
      }
      group.tab_indexes.forEach((tabIndex, offset) => {
        if (!Number.isInteger(tabIndex) || tabIndex !== group.tab_indexes[0] + offset || tabIndex < 0 || tabIndex >= windowSpec.tabs.length) {
          throw new Error("A group in the local export is not contiguous.");
        }
        if (windowSpec.tabs[tabIndex].pinned || used.has(tabIndex)) {
          throw new Error("A group in the local export overlaps another group or contains a pinned tab.");
        }
        used.add(tabIndex);
      });
      groups += 1;
    });
  }
  return { windows: migration.windows.length, tabs, groups };
}

async function report(endpoint, token, payload) {
  const reportEndpoint = new URL("/report", endpoint.origin);
  reportEndpoint.searchParams.set("token", token);
  await fetch(reportEndpoint, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function createWindow(windowSpec) {
  const createdWindow = await chrome.windows.create({ url: "about:blank", focused: false, type: "normal" });
  const blankTabs = await chrome.tabs.query({ windowId: createdWindow.id });
  const firstTab = blankTabs[0];
  if (!firstTab || !Number.isInteger(firstTab.id)) throw new Error("Chrome did not create the initial tab.");

  const chromeTabIds = [];
  for (let index = 0; index < windowSpec.tabs.length; index += 1) {
    const spec = windowSpec.tabs[index];
    let tab;
    if (index === 0) {
      tab = await chrome.tabs.update(firstTab.id, { url: spec.url, pinned: spec.pinned, active: false });
    } else {
      tab = await chrome.tabs.create({ windowId: createdWindow.id, url: spec.url, pinned: spec.pinned, active: false, index });
    }
    if (!Number.isInteger(tab.id)) throw new Error("Chrome did not return a tab ID.");
    chromeTabIds.push(tab.id);
  }

  let createdGroups = 0;
  for (const groupSpec of windowSpec.groups) {
    const tabIds = groupSpec.tab_indexes.map((index) => chromeTabIds[index]);
    const groupId = await chrome.tabs.group({ tabIds, createProperties: { windowId: createdWindow.id } });
    await chrome.tabGroups.update(groupId, {
      title: groupSpec.title,
      color: groupSpec.color,
      collapsed: groupSpec.collapsed,
    });
    createdGroups += 1;
  }
  const selectedIndex = Number.isInteger(windowSpec.selected_tab_index) ? windowSpec.selected_tab_index : 0;
  await chrome.tabs.update(chromeTabIds[selectedIndex], { active: true });
  return { windowId: createdWindow.id, tabs: chromeTabIds.length, groups: createdGroups };
}

async function run() {
  let endpoint;
  let token;
  try {
    ({ endpoint, token } = migrationEndpoint());
    setStatus("Loading the local Atlas export…");
    const response = await fetch(endpoint, { cache: "no-store" });
    if (!response.ok) throw new Error("The local migration server did not provide an export.");
    const migration = await response.json();
    const summary = validateMigration(migration);
    setDetails(`${summary.windows} window(s), ${summary.tabs} tab(s), and ${summary.groups} named group(s).\nOnly this local export is read; no cookies or passwords are involved.`);
    if (parameters.get("mode") !== "apply") throw new Error("The migration script did not explicitly authorize apply mode.");

    setStatus("Recreating windows, tabs, and tab groups in Chrome…");
    let createdWindows = 0;
    let createdTabs = 0;
    let createdGroups = 0;
    let firstWindowId = null;
    for (const windowSpec of migration.windows) {
      const result = await createWindow(windowSpec);
      firstWindowId ??= result.windowId;
      createdWindows += 1;
      createdTabs += result.tabs;
      createdGroups += result.groups;
    }
    if (firstWindowId !== null) await chrome.windows.update(firstWindowId, { focused: true });
    await report(endpoint, token, { status: "ok", created_windows: createdWindows, created_tabs: createdTabs, created_groups: createdGroups });
    setStatus("Import complete.", "ok");
    setDetails(`${createdWindows} new Chrome window(s), ${createdTabs} tab(s), and ${createdGroups} tab group(s) were created. Existing Chrome tabs were not changed.`);
  } catch (error) {
    if (endpoint && token) {
      try {
        await report(endpoint, token, { status: "error" });
      } catch (_) {
        // The visible message below is still useful if the local server is gone.
      }
    }
    setStatus("Import stopped.", "error");
    setDetails("No browser credentials were copied. Check the migration script's terminal message before rerunning; a failed import can have created partial new windows.");
  }
}

void run();
