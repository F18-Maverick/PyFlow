/* PyFlow server account UI.
 *
 * Default-credential warning, credential change and user management for the
 * server web backend (/api/login, /api/account, /api/users).  Independent of
 * common.js, so both the server status page and the startup-configuration page
 * can mount it.
 *
 * Templates call PyFlowAccount.mount({role, username, mustChange}) and add the
 * optional #users-btn / #logout-btn buttons.
 */
(function () {
  "use strict";

  const state = { role: "user", username: "", user_id: "", email: "", mustChange: false };
  const MIN_PASSWORD_LENGTH = 8;

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;");
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
      // No session any more (logged out or the account was removed).
      location.href = "/";
      throw new Error("login required");
    }
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || "HTTP " + resp.status);
    }
    return data;
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

  /* ---------------- change own credentials ---------------- */

  function openCredentialsModal() {
    const backdrop = openModal(
      "<h2>Change your credentials</h2>" +
        '<div class="field"><label for="acc-current">Current password</label>' +
        '<input type="password" id="acc-current" autocomplete="current-password"></div>' +
        '<div class="field"><label for="acc-username">Username</label>' +
        '<input type="text" id="acc-username" value="' +
        escAttr(state.username) +
        '" autocomplete="username"></div>' +
        '<div class="field"><label for="acc-email">Email (used for verification codes)</label>' +
        '<input type="email" id="acc-email" value="' +
        escAttr(state.email) +
        '" autocomplete="email"></div>' +
        '<div class="field"><label for="acc-password">New password (at least ' +
        MIN_PASSWORD_LENGTH +
        " characters)</label>" +
        '<input type="password" id="acc-password" autocomplete="new-password"></div>' +
        '<div class="field"><label for="acc-confirm">Confirm new password</label>' +
        '<input type="password" id="acc-confirm" autocomplete="new-password"></div>' +
        '<div class="status-line" id="acc-status"></div>' +
        '<div class="actions"><button class="btn" id="acc-save">Save</button> ' +
        '<button class="btn btn-ghost" id="acc-cancel">Cancel</button></div>'
    );
    const status = backdrop.querySelector("#acc-status");
    const fail = (message) => {
      status.className = "status-line err";
      status.textContent = message;
    };
    backdrop.querySelector("#acc-cancel").addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#acc-save").addEventListener("click", async () => {
      const current = backdrop.querySelector("#acc-current").value;
      const username = backdrop.querySelector("#acc-username").value.trim();
      const email = backdrop.querySelector("#acc-email").value.trim();
      const password = backdrop.querySelector("#acc-password").value;
      const confirm = backdrop.querySelector("#acc-confirm").value;
      if (!current) return fail("Enter your current password.");
      if (!username) return fail("Enter a username.");
      if (password.length < MIN_PASSWORD_LENGTH) {
        return fail("The new password must be at least " + MIN_PASSWORD_LENGTH + " characters.");
      }
      if (password !== confirm) return fail("The two passwords do not match.");
      try {
        await api("/api/account", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current_password: current, username, password, email }),
        });
        closeModal(backdrop);
        toast("Credentials updated", "ok");
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        fail(e.message);
      }
    });
  }

  /* ---------------- user management (administrators) ---------------- */

  function openUsersModal() {
    const backdrop = openModal(
      "<h2>Users</h2>" +
        '<div class="user-list" id="users-list"></div>' +
        '<div class="field"><label for="user-username">Username</label>' +
        '<input type="text" id="user-username" autocomplete="off"></div>' +
        '<div class="field"><label for="user-email">Email (used for verification codes)</label>' +
        '<input type="email" id="user-email" autocomplete="off"></div>' +
        '<div class="field"><label for="user-password">Password (at least ' +
        MIN_PASSWORD_LENGTH +
        " characters)</label>" +
        '<input type="password" id="user-password" autocomplete="new-password"></div>' +
        '<div class="field"><label for="user-role">Role</label>' +
        '<select id="user-role"><option value="user">User</option>' +
        '<option value="admin">Administrator</option></select></div>' +
        '<div class="status-line" id="users-status"></div>' +
        '<div class="actions"><button class="btn" id="user-add-btn">Add user</button> ' +
        '<button class="btn btn-ghost" id="users-close-btn">Close</button></div>'
    );
    const list = backdrop.querySelector("#users-list");
    const status = backdrop.querySelector("#users-status");
    const fail = (message) => {
      status.className = "status-line err";
      status.textContent = message;
    };

    function render(users) {
      list.innerHTML = "";
      users.forEach((u) => {
        const row = document.createElement("div");
        row.className = "user-row";
        row.innerHTML =
          '<span class="name">' +
          esc(u.username) +
          (u.username === state.username ? " (you)" : "") +
          '<span class="meta">' +
          esc(u.email || "no email") +
          " &middot; ID " +
          esc(u.user_id) +
          '</span></span><span class="role">' +
          esc(u.role) +
          "</span>";
        if (u.username !== state.username) {
          const removeBtn = document.createElement("button");
          removeBtn.className = "btn btn-danger";
          removeBtn.textContent = "Remove";
          removeBtn.addEventListener("click", async () => {
            try {
              const data = await api("/api/users/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: u.username }),
              });
              status.className = "status-line";
              status.textContent = "";
              render(data.users || []);
              toast("User removed", "ok");
            } catch (e) {
              fail(e.message);
            }
          });
          row.appendChild(removeBtn);
        }
        list.appendChild(row);
      });
      if (!users.length) list.textContent = "No users";
    }

    async function refresh() {
      try {
        render((await api("/api/users")).users || []);
      } catch (e) {
        fail(e.message);
      }
    }

    backdrop.querySelector("#users-close-btn").addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#user-add-btn").addEventListener("click", async () => {
      const username = backdrop.querySelector("#user-username").value.trim();
      const email = backdrop.querySelector("#user-email").value.trim();
      const password = backdrop.querySelector("#user-password").value;
      const role = backdrop.querySelector("#user-role").value;
      if (!username) return fail("Enter a username.");
      if (!email) return fail("Enter the email address of the account.");
      if (password.length < MIN_PASSWORD_LENGTH) {
        return fail("The password must be at least " + MIN_PASSWORD_LENGTH + " characters.");
      }
      try {
        const data = await api("/api/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, email, password, role }),
        });
        status.className = "status-line";
        status.textContent = "";
        backdrop.querySelector("#user-username").value = "";
        backdrop.querySelector("#user-email").value = "";
        backdrop.querySelector("#user-password").value = "";
        render(data.users || []);
        toast("User added", "ok");
      } catch (e) {
        fail(e.message);
      }
    });

    refresh();
  }

  /* ---------------- default-credential warning ---------------- */

  function openDefaultCredentialsWarning() {
    const backdrop = openModal(
      '<div class="warn-title">&#9888; The default administrator credentials are still in use</div>' +
        '<div class="warn-body">' +
        "<p>This server is administered with the default account: username <b>admin</b>, " +
        "password <b>admin</b>. Anyone who can reach the server can log in as " +
        "administrator with these credentials.</p>" +
        "<p><b>Change the admin username and password before this server goes " +
        "online on a public network.</b></p>" +
        "</div>" +
        '<div class="actions"><button class="btn btn-danger" id="warn-change-btn">' +
        "Change credentials now</button> " +
        '<button class="btn btn-ghost" id="warn-later-btn">Later</button></div>',
      "warn"
    );
    backdrop.querySelector("#warn-change-btn").addEventListener("click", () => {
      closeModal(backdrop);
      openCredentialsModal();
    });
    backdrop.querySelector("#warn-later-btn").addEventListener("click", () => closeModal(backdrop));
  }


  /* ---------------- "ftp" server (not FTP: the protocol's own transfers) ---------------- */

  // The brand of the feature is the literal quoted "ftp": this is not the FTP
  // protocol, it browses one folder of this host and hands the chosen entries
  // to clients over the PyFlow file transfer commands.
  const FTP_BUTTON_ADD = '+ "ftp" server';
  const FTP_BUTTON_VIEW = '"ftp" server status';

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

  function ftpButton() {
    return document.getElementById("ftp-btn");
  }

  async function refreshFtpButton() {
    const btn = ftpButton();
    if (!btn) return null;
    try {
      const data = await api("/api/ftp");
      const shared = !!data.shared;
      btn.textContent = shared ? FTP_BUTTON_VIEW : FTP_BUTTON_ADD;
      return data.root || null;
    } catch (e) {
      return null;
    }
  }

  function openFtpAddDialog(currentRoot) {
    const backdrop = openModal(
      "<h2>Add an &quot;ftp&quot; server</h2>" +
        '<div class="note">This is not FTP: the chosen folder is served to connected ' +
        "clients over the PyFlow file transfer protocol.</div>" +
        '<div class="field"><label for="ftp-path">Folder to share (path on this host)</label>' +
        '<input type="text" id="ftp-path" placeholder="e.g. /home/user/share" value="' +
        escAttr(currentRoot || "") +
        '"></div>' +
        '<div class="status-line" id="ftp-status"></div>' +
        '<div class="actions"><button class="btn" id="ftp-add-btn">Add</button> ' +
        '<button class="btn btn-ghost" id="ftp-add-cancel">Cancel</button></div>'
    );
    const status = backdrop.querySelector("#ftp-status");
    backdrop.querySelector("#ftp-add-cancel").addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#ftp-add-btn").addEventListener("click", async () => {
      const path = backdrop.querySelector("#ftp-path").value.trim();
      if (!path) {
        status.className = "status-line err";
        status.textContent = "Enter a folder path";
        return;
      }
      try {
        const data = await api("/api/ftp/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        closeModal(backdrop);
        await refreshFtpButton();
        toast('"ftp" server added: ' + data.root, "ok");
        openFtpBrowser("");
      } catch (e) {
        status.className = "status-line err";
        status.textContent = e.message;
      }
    });
  }

  // Share-relative path of one child entry of ``base``.
  function ftpJoin(base, name) {
    return base ? base + "/" + name : name;
  }

  function ftpRow(entry, relBase, openFolder) {
    const row = document.createElement("div");
    row.className = "ftp-row" + (entry.dir ? " dir" : "");
    row.innerHTML =
      '<span class="ftp-icon">' + (entry.dir ? "&#128193;" : "&#128196;") + "</span>" +
      '<span class="ftp-name">' + esc(entry.name) + "</span>" +
      '<span class="ftp-meta">' + (entry.dir ? "folder" : fmtSize(entry.size)) + "</span>" +
      '<span class="ftp-meta">' + new Date(entry.mtime * 1000).toLocaleString() + "</span>";
    if (entry.dir) {
      row.title = "Open " + entry.name;
      row.addEventListener("click", () => openFolder(ftpJoin(relBase, entry.name)));
    }
    return row;
  }

  async function openFtpBrowser(relPath) {
    let data;
    try {
      data = await api("/api/ftp/list?path=" + encodeURIComponent(relPath || ""));
    } catch (e) {
      toast("Cannot list the shared folder: " + e.message, "err");
      return;
    }
    const listing = data.listing || { entries: [] };
    const backdrop = openModal(
      "<h2>&quot;ftp&quot; server</h2>" +
        '<div class="ftp-bar"><button class="btn btn-ghost" id="ftp-up">&uarr; Up</button>' +
        '<span class="ftp-path" id="ftp-path">' +
        esc("/" + (listing.path || "")) +
        "</span>" +
        '<span class="spacer"></span>' +
        '<button class="btn btn-ghost" id="ftp-change">Change folder</button>' +
        '<button class="btn btn-ghost" id="ftp-close">Close</button></div>' +
        '<div class="note">Shared host folder: <code>' +
        esc(data.root || "") +
        "</code></div>" +
        '<div class="ftp-list" id="ftp-list"></div>'
    );
    const list = backdrop.querySelector("#ftp-list");

    function render() {
      list.innerHTML = "";
      if (!listing.entries.length) {
        const empty = document.createElement("div");
        empty.className = "empty-hint";
        empty.textContent = "This folder is empty";
        list.appendChild(empty);
        return;
      }
      listing.entries.forEach((entry) =>
        list.appendChild(
          ftpRow(entry, listing.path, (rel) => {
            closeModal(backdrop);
            openFtpBrowser(rel);
          })
        )
      );
    }

    render();
    const upBtn = backdrop.querySelector("#ftp-up");
    if (listing.parent === null || listing.parent === undefined) {
      upBtn.disabled = true;
    } else {
      upBtn.addEventListener("click", () => {
        closeModal(backdrop);
        openFtpBrowser(listing.parent);
      });
    }
    backdrop.querySelector("#ftp-close").addEventListener("click", () => closeModal(backdrop));
    backdrop.querySelector("#ftp-change").addEventListener("click", () => {
      closeModal(backdrop);
      openFtpAddDialog(data.root || "");
    });
  }

  async function openFtpServer() {
    const root = await refreshFtpButton();
    if (root) openFtpBrowser("");
    else openFtpAddDialog("");
  }

  /* ---------------- session ---------------- */

  async function logout() {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (e) {
      /* the session is gone either way */
    }
    location.href = "/";
  }

  window.PyFlowAccount = {
    mount(options) {
      Object.assign(state, options || {});
      const usersBtn = document.getElementById("users-btn");
      if (usersBtn) usersBtn.addEventListener("click", openUsersModal);
      const ftpBtn = document.getElementById("ftp-btn");
      if (ftpBtn) {
        ftpBtn.addEventListener("click", openFtpServer);
        refreshFtpButton();
      }
      const logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) logoutBtn.addEventListener("click", logout);
      if (state.mustChange) openDefaultCredentialsWarning();
    },
    openCredentials: openCredentialsModal,
    openUsers: openUsersModal,
    openFtpServer: openFtpServer,
    logout: logout,
  };
})();
