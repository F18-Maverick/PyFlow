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

  const state = { role: "user", username: "", mustChange: false };
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
          body: JSON.stringify({ current_password: current, username, password }),
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
        '<div class="field"><label for="user-username">New username</label>' +
        '<input type="text" id="user-username" autocomplete="off"></div>' +
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
          '</span><span class="role">' +
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
      const password = backdrop.querySelector("#user-password").value;
      const role = backdrop.querySelector("#user-role").value;
      if (!username) return fail("Enter a username.");
      if (password.length < MIN_PASSWORD_LENGTH) {
        return fail("The password must be at least " + MIN_PASSWORD_LENGTH + " characters.");
      }
      try {
        const data = await api("/api/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password, role }),
        });
        status.className = "status-line";
        status.textContent = "";
        backdrop.querySelector("#user-username").value = "";
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
      const logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) logoutBtn.addEventListener("click", logout);
      if (state.mustChange) openDefaultCredentialsWarning();
    },
    openCredentials: openCredentialsModal,
    openUsers: openUsersModal,
    logout: logout,
  };
})();
