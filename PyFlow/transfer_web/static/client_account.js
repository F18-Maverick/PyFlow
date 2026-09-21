/* PyFlow client account UI.
 *
 * User info, contact search and pending contact requests for the web client.
 * The client backend proxies every request to the server the TCP client is
 * connected to, so this script only talks to its own backend.
 *
 * client_main.html sets window.WEB_USER and calls
 * PyFlowClientAccount.mount({user: window.WEB_USER}); the sidebar buttons
 * #contacts-btn, #requests-btn, #user-info-btn and #logout-btn drive it.
 */
(function () {
  "use strict";

  const POLL_MS = 5000; // pending-request polling interval
  const state = { user: null, acceptedWhileOpen: false };

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = {};
    }
    if (resp.status === 401) {
      // The session is gone (logged out, expired or dropped by the server):
      // "/" decides whether the login window has to be shown again.
      location.href = "/";
      throw new Error(data.error || "login required");
    }
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || "HTTP " + resp.status);
    }
    return data;
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function toast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }

  function openModal(html, extraClass) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML =
      '<div class="modal' + (extraClass ? " " + extraClass : "") + '">' + html + "</div>";
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) backdrop.remove();
    });
    document.body.appendChild(backdrop);
    return backdrop;
  }

  function closeModal(backdrop) {
    backdrop.remove();
  }

  /* ---------------- pending requests ---------------- */

  async function pollRequests() {
    let data;
    try {
      data = await api("/api/contact_requests");
    } catch (e) {
      return; // offline or logged out: api() already returned to "/"
    }
    updateBadge(data);
    if (state.acceptedWhileOpen) {
      // An accepted request changed the contacts of the account, so the page
      // is reloaded to show the instance list the server pushed over TCP.
      state.acceptedWhileOpen = false;
      location.reload();
    }
  }

  function updateBadge(requests) {
    const badge = document.getElementById("requests-count");
    if (!badge) return;
    const count = (requests.incoming || []).length;
    badge.textContent = count ? " (" + count + ")" : "";
  }

  async function answerRequest(requestId, accept) {
    await post("/api/contacts/respond", { request_id: requestId, accept });
    toast(accept ? "Contact added" : "Request rejected", "ok");
    if (accept) {
      state.acceptedWhileOpen = true;
      pollRequests();
    }
  }

  /* ---------------- shared rows ---------------- */

  function actionButton(label, className, handler) {
    const btn = document.createElement("button");
    btn.className = className;
    btn.textContent = label;
    if (handler) {
      btn.addEventListener("click", handler);
    } else {
      btn.disabled = true; // the action is already done or not available
    }
    return btn;
  }

  function accountRow(account, buttons) {
    const row = document.createElement("div");
    row.className = "user-row";
    row.innerHTML =
      '<span class="name">' +
      esc(account.username) +
      '<span class="meta">ID ' +
      esc(account.user_id) +
      " &middot; " +
      esc(account.email || "no email") +
      "</span></span>";
    buttons.forEach((btn) => row.appendChild(btn));
    return row;
  }

  /* ---------------- user info ---------------- */

  function openUserInfoModal() {
    const user = state.user || {};
    const backdrop = openModal(
      "<h2>User info</h2>" +
        '<div class="user-list"><div class="user-row">' +
        '<span class="name">' +
        esc(user.username || "unknown") +
        '<span class="meta">' +
        esc(user.email || "no email") +
        "</span></span>" +
        '<span class="role">' +
        esc(user.role || "user") +
        "</span></div></div>" +
        '<div class="field"><label>User ID</label><div class="help">' +
        esc(user.user_id || "unknown") +
        "</div></div>" +
        '<div class="actions"><button class="btn btn-ghost" id="user-info-close">Close</button></div>'
    );
    backdrop
      .querySelector("#user-info-close")
      .addEventListener("click", () => closeModal(backdrop));
  }

  /* ---------------- contacts ---------------- */

  function openContactsModal() {
    const backdrop = openModal(
      "<h2>Contacts</h2>" +
        '<div class="field"><label for="contact-search">Search by user ID, username or email</label>' +
        '<div class="actions"><input type="text" id="contact-search" autocomplete="off">' +
        '<button class="btn" id="contact-search-btn">Search</button></div></div>' +
        '<div class="user-list" id="contact-results"></div>' +
        '<div class="status-line" id="contact-status"></div>' +
        '<div class="actions"><button class="btn btn-ghost" id="contacts-close">Close</button></div>'
    );
    const results = backdrop.querySelector("#contact-results");
    const status = backdrop.querySelector("#contact-status");
    const searchInput = backdrop.querySelector("#contact-search");
    let pending = {}; // user id -> id of the request that account sent us

    function fail(message) {
      status.className = "status-line err";
      status.textContent = message;
    }

    async function search() {
      status.className = "status-line";
      status.textContent = "";
      pending = {};
      try {
        (await api("/api/contact_requests")).incoming.forEach((entry) => {
          pending[entry.user.user_id] = entry.id;
        });
      } catch (e) {
        /* the search still works; an accept then reports the stale request */
      }
      try {
        const data = await post("/api/contacts/search", { query: searchInput.value.trim() });
        render(data.results || []);
      } catch (e) {
        fail(e.message);
      }
    }

    async function respond(userId, accept) {
      const requestId = pending[userId];
      if (requestId === undefined) {
        fail("This request is not pending any more.");
        search();
        return;
      }
      try {
        await answerRequest(requestId, accept);
        if (accept) {
          closeModal(backdrop);
        } else {
          search();
        }
      } catch (e) {
        fail(e.message);
      }
    }

    async function add(entry) {
      try {
        await post("/api/contacts/request", { user_id: entry.user_id });
        toast("Request sent to " + entry.username, "ok");
        search();
      } catch (e) {
        fail(e.message);
      }
    }

    function render(entries) {
      results.innerHTML = "";
      if (!entries.length) {
        results.textContent = "No account matches that search";
        return;
      }
      entries.forEach((entry) => {
        const buttons = [];
        if (entry.relation === "none") {
          buttons.push(actionButton("Add", "btn", () => add(entry)));
        } else if (entry.relation === "incoming") {
          buttons.push(actionButton("Accept", "btn", () => respond(entry.user_id, true)));
          buttons.push(actionButton("Reject", "btn btn-danger", () => respond(entry.user_id, false)));
        } else if (entry.relation === "outgoing") {
          buttons.push(actionButton("Sent", "btn btn-ghost", null));
        } else {
          buttons.push(actionButton("Contact", "btn btn-ghost", null));
        }
        results.appendChild(accountRow(entry, buttons));
      });
    }

    backdrop
      .querySelector("#contacts-close")
      .addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#contact-search-btn").addEventListener("click", search);
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        search();
      }
    });
  }

  /* ---------------- contact requests ---------------- */

  function openRequestsModal() {
    const backdrop = openModal(
      "<h2>Contact requests</h2>" +
        '<div class="user-list" id="requests-list"></div>' +
        '<div class="status-line" id="requests-status"></div>' +
        '<div class="actions"><button class="btn btn-ghost" id="requests-close">Close</button></div>'
    );
    const list = backdrop.querySelector("#requests-list");
    const status = backdrop.querySelector("#requests-status");

    function fail(message) {
      status.className = "status-line err";
      status.textContent = message;
    }

    function render(requests) {
      const incoming = requests.incoming || [];
      const outgoing = requests.outgoing || [];
      status.className = "status-line";
      status.textContent = "";
      list.innerHTML = "";
      incoming.forEach((entry) => {
        list.appendChild(
          accountRow(entry.user, [
            actionButton("Accept", "btn", () => respond(entry, true)),
            actionButton("Reject", "btn btn-danger", () => respond(entry, false)),
          ])
        );
      });
      outgoing.forEach((entry) => {
        list.appendChild(accountRow(entry.user, [actionButton("Sent", "btn btn-ghost", null)]));
      });
      if (!incoming.length && !outgoing.length) {
        list.textContent = "No pending requests";
      }
    }

    async function respond(entry, accept) {
      try {
        await answerRequest(entry.id, accept);
        if (accept) {
          closeModal(backdrop);
        } else {
          refresh();
        }
      } catch (e) {
        fail(e.message);
      }
    }

    async function refresh() {
      try {
        render(await api("/api/contact_requests"));
      } catch (e) {
        fail(e.message);
      }
    }

    backdrop
      .querySelector("#requests-close")
      .addEventListener("click", () => closeModal(backdrop));
    refresh();
  }

  /* ---------------- server "ftp" share (not FTP: native protocol transfers) ---------------- */

  // Selected share-relative paths survive folder navigation, so a selection can
  // span folders; the checkbox on the left of every row is the only state.
  const ftpState = { path: "", selected: new Set(), listing: null };

  function fmtSize(n) {
    if (n == null || isNaN(n)) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + units[i];
  }

  function ftpJoin(base, name) {
    return base ? base + "/" + name : name;
  }

  function ftpEntryRow(entry, render, openFolder) {
    const rel = ftpJoin(ftpState.path, entry.name);
    const row = document.createElement("div");
    row.className = "ftp-row" + (entry.dir ? " dir" : "") + (ftpState.selected.has(rel) ? " picked" : "");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = ftpState.selected.has(rel);
    box.title = "Select " + entry.name;
    row.appendChild(box);
    const icon = document.createElement("span");
    icon.className = "ftp-icon";
    icon.innerHTML = entry.dir ? "&#128193;" : "&#128196;";
    row.appendChild(icon);
    const name = document.createElement("span");
    name.className = "ftp-name";
    name.textContent = entry.name;
    row.appendChild(name);
    const meta = document.createElement("span");
    meta.className = "ftp-meta";
    meta.textContent = entry.dir ? "folder" : fmtSize(entry.size);
    row.appendChild(meta);
    const when = document.createElement("span");
    when.className = "ftp-meta";
    when.textContent = new Date(entry.mtime * 1000).toLocaleString();
    row.appendChild(when);

    // Windows-explorer conventions: a single click toggles the checkbox of the
    // row, a double click opens a folder and is ignored on a file (the two
    // clicks of the double click cancel each other out).
    function toggle() {
      if (ftpState.selected.has(rel)) ftpState.selected.delete(rel);
      else ftpState.selected.add(rel);
      box.checked = ftpState.selected.has(rel);
      row.classList.toggle("picked", box.checked);
      render();
    }
    row.addEventListener("click", (e) => {
      if (e.target !== box) toggle();
    });
    row.addEventListener("dblclick", () => {
      if (!entry.dir) return; // double-clicking a file does nothing
      openFolder(rel);
    });
    return row;
  }

  // Refresh the download button of a modal from the current selection.
  function updateFtpFooter(backdrop) {
    const downloadBtn = backdrop.querySelector("#ftp-download");
    if (!downloadBtn) return;
    downloadBtn.textContent = "Download selected (" + ftpState.selected.size + ")";
    downloadBtn.disabled = ftpState.selected.size === 0;
  }

  function renderFtpModal(backdrop) {
    const listing = ftpState.listing || { entries: [], path: "", parent: null };
    const list = backdrop.querySelector("#ftp-list");
    backdrop.querySelector("#ftp-path").textContent = "/" + (listing.path || "");
    backdrop.querySelector("#ftp-up").disabled =
      listing.parent === null || listing.parent === undefined;
    updateFtpFooter(backdrop);
    list.innerHTML = "";
    if (!listing.entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-hint";
      empty.textContent = "This folder is empty";
      list.appendChild(empty);
      return;
    }
    const refreshFooter = () => updateFtpFooter(backdrop);
    const openFolder = (rel) => {
      ftpState.path = rel;
      loadFtpFolder(backdrop);
    };
    listing.entries.forEach((entry) =>
      list.appendChild(ftpEntryRow(entry, refreshFooter, openFolder))
    );
  }

  async function loadFtpFolder(backdrop) {
    const modal = backdrop || document.querySelector(".modal-backdrop");
    try {
      const data = await post("/api/ftp/list", { path: ftpState.path });
      ftpState.listing = data.listing || { entries: [], path: ftpState.path, parent: null };
      ftpState.path = ftpState.listing.path || "";
      if (modal) renderFtpModal(modal);
    } catch (e) {
      toast('Cannot open the server "ftp" server: ' + e.message, "err");
      if (modal) closeModal(modal);
    }
  }

  function openFtpModal() {
    ftpState.path = "";
    ftpState.selected = new Set();
    const backdrop = openModal(
      "<h2>Server &quot;ftp&quot;</h2>" +
        '<div class="note">Files and folders below live on the server. Tick what you want ' +
        "and download it over the PyFlow transfer protocol: click selects, double click opens " +
        "a folder (a file ignores the double click).</div>" +
        '<div class="ftp-bar"><button class="btn btn-ghost" id="ftp-up">&uarr; Up</button>' +
        '<span class="ftp-path" id="ftp-path">/</span>' +
        '<span class="spacer"></span>' +
        '<button class="btn btn-ghost" id="ftp-refresh">Reload</button>' +
        '<button class="btn btn-ghost" id="ftp-close">Close</button></div>' +
        '<div class="ftp-list" id="ftp-list"></div>' +
        '<div class="ftp-footer"><span class="spacer"></span>' +
        '<button class="btn" id="ftp-download" disabled>Download selected (0)</button></div>'
    );
    backdrop.querySelector("#ftp-close").addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#ftp-up").addEventListener("click", () => {
      const parent = ftpState.listing ? ftpState.listing.parent : null;
      if (parent === null || parent === undefined) return;
      ftpState.path = parent;
      loadFtpFolder(backdrop);
    });
    backdrop.querySelector("#ftp-refresh").addEventListener("click", () => loadFtpFolder(backdrop));
    backdrop.querySelector("#ftp-download").addEventListener("click", async () => {
      const paths = Array.from(ftpState.selected);
      if (!paths.length) return;
      try {
        const data = await post("/api/ftp/download", { paths });
        closeModal(backdrop);
        toast("Download started (" + (data.started || 0) + " item(s))", "ok");
      } catch (e) {
        toast("Download failed: " + e.message, "err");
      }
    });
    loadFtpFolder(backdrop);
  }

  /* ---------------- session ---------------- */

  async function logout() {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (e) {
      /* the session goes away either way */
    }
    location.href = "/";
  }

  window.PyFlowClientAccount = {
    mount(options) {
      state.user = (options && options.user) || window.WEB_USER || {};
      const contactsBtn = document.getElementById("contacts-btn");
      if (contactsBtn) contactsBtn.addEventListener("click", openContactsModal);
      const requestsBtn = document.getElementById("requests-btn");
      if (requestsBtn) requestsBtn.addEventListener("click", openRequestsModal);
      const userInfoBtn = document.getElementById("user-info-btn");
      if (userInfoBtn) userInfoBtn.addEventListener("click", openUserInfoModal);
      const ftpBtn = document.getElementById("ftp-btn");
      if (ftpBtn) ftpBtn.addEventListener("click", openFtpModal);
      const logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) logoutBtn.addEventListener("click", logout);
      pollRequests();
      setInterval(pollRequests, POLL_MS);
    },
    openFtp: openFtpModal,
    logout: logout,
  };
})();
