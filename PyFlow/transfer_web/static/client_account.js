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
      const logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) logoutBtn.addEventListener("click", logout);
      pollRequests();
      setInterval(pollRequests, POLL_MS);
    },
    logout: logout,
  };
})();
