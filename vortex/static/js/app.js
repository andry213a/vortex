(function () {
  "use strict";

  var DIRECT_LIMIT = 100 * 1024 * 1024;

  var state = {
    token: localStorage.getItem("vortex_token") || null,
    role: null,
    authMode: "auto",
    requiresLogin: false,
    allowGuest: false,
    currentPath: "",
    parent: null,
    items: [],
    pending: "",
    prefix: "",
    logsWs: null,
    searchTimer: null,
    paletteTimer: null,
    uploadTotal: 0,
    uploadDone: 0
  };

  var KIND_ICONS = {
    folder: "folder",
    video: "movie",
    audio: "audio",
    image: "image",
    text: "description",
    code: "code",
    pdf: "pdf",
    archive: "archive",
    doc: "article",
    spreadsheet: "table",
    presentation: "slideshow",
    design: "palette",
    font: "font",
    database: "storage",
    model3d: "view-ar",
    package: "package",
    binary: "memory",
    ebook: "book",
    other: "file"
  };

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key === "value") node.value = value;
      else node.setAttribute(key, value);
    });
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svgIcon(name, className) {
    return (
      '<svg class="icon' + (className ? " " + className : "") + '" aria-hidden="true">' +
      '<use href="#i-' + name + '"></use></svg>'
    );
  }

  function setStatus(online) {
    var pill = $("status-pill");
    if (!pill) return;
    pill.classList.toggle("offline", !online);
    $("status-text").textContent = online ? "EN LÍNEA" : "DETENIDO";
  }

  function toast(message, type) {
    type = type || "info";
    var node = el("div", { class: "toast " + type, text: message });
    $("toasts").append(node);
    setTimeout(function () {
      node.remove();
    }, 4200);
  }

  function authHeaders() {
    return state.token ? { Authorization: "Bearer " + state.token } : {};
  }

  function detailOf(payload, fallback) {
    if (!payload) return fallback;
    var detail = payload.detail;
    if (detail === undefined || detail === null) return fallback;
    if (typeof detail === "object") return detail.message || JSON.stringify(detail);
    return String(detail);
  }

  async function api(path, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {}, authHeaders());
    var response;
    try {
      response = await fetch(path, Object.assign({}, options, { headers: headers }));
    } catch (err) {
      setStatus(false);
      throw new Error("No se pudo conectar con el servidor");
    }
    if (response.status === 401) {
      handleUnauthorized();
      var unauthorized = new Error("No autenticado");
      unauthorized.status = 401;
      throw unauthorized;
    }
    if (!response.ok) {
      var message = response.statusText || "HTTP " + response.status;
      try {
        message = detailOf(await response.json(), message);
      } catch (err) {}
      var error = new Error(message);
      error.status = response.status;
      throw error;
    }
    setStatus(true);
    var contentType = response.headers.get("content-type") || "";
    return contentType.indexOf("application/json") >= 0 ? response.json() : response.text();
  }

  function handleUnauthorized() {
    if (state.token) {
      state.token = null;
      localStorage.removeItem("vortex_token");
    }
    if (state.authMode !== "none") {
      state.pending = location.pathname + location.search;
      showLogin("La sesión expiró o no es válida.");
    }
  }

  function showLogin(message) {
    $("login-overlay").classList.remove("hidden");
    $("login-error").textContent = message || "";
    $("login-secret").value = "";
    $("login-busy").classList.add("hidden");
    setTimeout(function () {
      $("login-secret").focus();
    }, 50);
  }

  function hideLogin() {
    $("login-overlay").classList.add("hidden");
    $("login-error").textContent = "";
    $("login-busy").classList.add("hidden");
  }

  function segmentPath(path) {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  function browseUrl(path) {
    return path ? "/browse/" + segmentPath(path) : "/";
  }

  function downloadUrl(path) {
    return "/descargar/" + segmentPath(path);
  }

  function fileUrl(path, inline) {
    var params = new URLSearchParams({ path: path });
    if (inline) params.set("inline", "1");
    if (state.token) params.set("token", state.token);
    return "/api/files/download?" + params.toString();
  }

  function zipUrl(path) {
    var params = new URLSearchParams({ path: path });
    if (state.token) params.set("token", state.token);
    return "/api/files/zip?" + params.toString();
  }

  function humanSize(size) {
    size = Number(size) || 0;
    if (size <= 0) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var value = size;
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return index === 0 ? Math.round(value) + " " + units[index] : value.toFixed(1) + " " + units[index];
  }

  function humanSpeed(value) {
    if (!value || value <= 0) return "";
    return humanSize(value) + "/s";
  }

  function formatTime(timestamp) {
    try {
      var date = new Date(timestamp * 1000);
      var pad = function (value) {
        return String(value).padStart(2, "0");
      };
      return (
        pad(date.getDate()) + "/" + pad(date.getMonth() + 1) + "/" + date.getFullYear() +
        " " + pad(date.getHours()) + ":" + pad(date.getMinutes())
      );
    } catch (err) {
      return "-";
    }
  }

  function setRoleBadge() {
    var badge = $("role-badge");
    var labels = {
      host: "HOST",
      guest_write: "INVITADO · TOTAL",
      guest_read: "INVITADO · LECTURA"
    };
    $("role-label").textContent = labels[state.role] || "INVITADO";
    badge.className = "role-badge" + (state.role === "host" ? " host" : "");
    $("btn-logs").classList.toggle("hidden", state.role !== "host");
    $("btn-logout").classList.toggle("hidden", !state.token);
    $("btn-login").classList.toggle("hidden", state.authMode === "none" || !!state.token);
  }

  async function resolveRole() {
    try {
      var me = await api("/api/auth/me");
      state.role = me.role;
      state.authMode = me.auth_mode;
    } catch (err) {
      if (err.status !== 401) throw err;
    }
  }

  async function boot() {
    var status;
    try {
      status = await fetch("/api/auth/status").then(function (response) {
        return response.json();
      });
    } catch (err) {
      setStatus(false);
      toast("No se pudo contactar con el servidor", "error");
      return;
    }
    setStatus(true);
    state.authMode = status.auth_mode;
    state.requiresLogin = status.requires_login;
    state.allowGuest = status.allow_guest;
    if (state.token) {
      try {
        var me = await api("/api/auth/me");
        state.role = me.role;
      } catch (err) {
        if (err.status === 401) {
          state.token = null;
          localStorage.removeItem("vortex_token");
        }
      }
    }
    if (!state.token) {
      if (state.requiresLogin) {
        state.pending = location.pathname + location.search;
        showLogin("");
        return;
      }
      try {
        var guest = await api("/api/auth/me");
        state.role = guest.role;
        state.authMode = guest.auth_mode;
      } catch (err) {
        showLogin("");
        return;
      }
    }
    await startApp();
  }

  async function startApp() {
    hideLogin();
    $("app").classList.remove("hidden");
    setStatus(true);
    try {
      await resolveRole();
    } catch (err) {
      toast(err.message, "error");
    }
    setRoleBadge();
    await routeFromLocation();
  }

  async function routeFromLocation() {
    var pathname = location.pathname;
    try {
      pathname = decodeURIComponent(pathname);
    } catch (err) {}
    if (pathname.indexOf("/descargar/") === 0) {
      var target = pathname.slice("/descargar/".length);
      triggerDownload(target);
      var parent = target.indexOf("/") >= 0 ? target.slice(0, target.lastIndexOf("/")) : "";
      state.pending = "";
      await loadDir(parent, true);
    } else if (pathname.indexOf("/browse/") === 0) {
      await loadDir(pathname.slice("/browse/".length), false);
    } else {
      await loadDir(state.currentPath || "", false);
    }
  }

  async function loadDir(path, updateHistory) {
    updateHistory = updateHistory !== false;
    try {
      var data = await api("/api/files?path=" + encodeURIComponent(path));
      state.currentPath = data.path;
      state.parent = data.parent;
      state.items = data.items;
      $("search").value = "";
      renderBreadcrumb();
      renderItems(data.items);
      $("count").textContent = data.total + " elementos";
      if (updateHistory) {
        try {
          history.pushState({ path: data.path }, "", browseUrl(data.path));
        } catch (err) {}
      }
    } catch (err) {
      if (err.status !== 401) toast(err.message, "error");
    }
  }

  function renderBreadcrumb() {
    var nav = $("breadcrumb");
    nav.innerHTML = "";
    var root = el("button", { class: "crumb root", html: svgIcon("home") + "<span>Raíz</span>" });
    root.addEventListener("click", function () {
      loadDir("");
    });
    nav.append(root);
    if (!state.currentPath) return;
    var accumulated = [];
    state.currentPath.split("/").forEach(function (part) {
      accumulated.push(part);
      var target = accumulated.join("/");
      nav.append(el("span", { class: "crumb-sep", text: "/" }));
      var crumb = el("button", { class: "crumb", text: part });
      crumb.addEventListener("click", function () {
        loadDir(target);
      });
      nav.append(crumb);
    });
  }

  function renderItems(items) {
    var listing = $("listing");
    listing.innerHTML = "";
    if (!items.length) {
      listing.append(
        el(
          "div",
          { class: "empty" },
          el("div", { class: "big", html: svgIcon("search-off") }),
          el("div", { text: "Sin resultados" }),
          el("div", { class: "empty-hint", text: "Prueba con otro término o usa operadores type:, ext:, size:" })
        )
      );
      return;
    }
    items.forEach(function (item) {
      listing.append(buildRow(item));
    });
  }

  function buildRow(item) {
    var kind = item.kind || "other";
    var row = el("div", { class: "row" });
    row.addEventListener("click", function () {
      openItem(item);
    });
    var icon = el("div", {
      class: "row-icon kind-" + kind,
      html: svgIcon(KIND_ICONS[kind] || KIND_ICONS.other)
    });
    var meta = item.is_dir ? "Carpeta" : humanSize(item.size);
    meta += "  ·  " + formatTime(item.modified);
    var main = el(
      "div",
      { class: "row-main" },
      el("div", { class: "row-name", text: item.name }),
      el("div", { class: "row-meta", text: meta })
    );
    var actions = el("div", { class: "row-actions" });
    if (item.is_dir) {
      actions.append(actionButton("archive", "Descargar .zip", function () {
        triggerDownloadZip(item.path);
      }));
    } else {
      actions.append(actionButton("download", "Descargar", function () {
        triggerDownload(item.path);
      }));
    }
    if (item.editable && !item.is_dir) {
      actions.append(actionButton("edit", "Editar", function () {
        openEditor(item);
      }, "accent"));
    }
    actions.append(actionButton("rename", "Renombrar", function () {
      openRename(item);
    }));
    actions.append(actionButton("delete", "Eliminar", function () {
      confirmDelete(item);
    }, "danger"));
    row.append(icon, main, actions);
    return row;
  }

  function actionButton(iconName, title, handler, className) {
    var button = el("button", {
      class: "icon-btn" + (className ? " " + className : ""),
      title: title,
      "aria-label": title,
      html: svgIcon(iconName)
    });
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function openItem(item) {
    if (item.is_dir) loadDir(item.path);
    else openPreview(item);
  }

  function triggerDownload(path) {
    var link = el("a", { href: fileUrl(path) });
    document.body.append(link);
    link.click();
    link.remove();
  }

  function triggerDownloadZip(path) {
    var link = el("a", { href: zipUrl(path) });
    document.body.append(link);
    link.click();
    link.remove();
  }

  function openModal(options) {
    var backdrop = $("modal-backdrop");
    var modal = $("modal");
    modal.className = "modal" + (options.narrow ? " narrow" : "");
    modal.innerHTML = "";
    modal.append(el("div", { class: "modal-head" }, el("span", { text: options.title })));
    var body = el("div", { class: "modal-body" });
    if (typeof options.body === "string") body.innerHTML = options.body;
    else if (options.body) body.append(options.body);
    modal.append(body);
    if (options.actions && options.actions.length) {
      var foot = el("div", { class: "modal-foot" });
      options.actions.forEach(function (action) {
        var button = el("button", { class: "btn " + (action.class || "") });
        if (action.icon) button.innerHTML = svgIcon(action.icon);
        button.append(document.createTextNode(action.label));
        button.addEventListener("click", function () {
          if (action.onClick) action.onClick();
          else closeModal();
        });
        foot.append(button);
      });
      modal.append(foot);
    }
    backdrop.classList.remove("hidden");
    return { modal: modal, body: body };
  }

  function closeModal() {
    $("modal-backdrop").classList.add("hidden");
    $("modal").innerHTML = "";
  }

  function openPreview(item) {
    var body = el("div");
    var actions = [
      { label: "Cerrar", class: "ghost", onClick: closeModal },
      { label: "Descargar", class: "primary", icon: "download", onClick: function () {
        triggerDownload(item.path);
      } }
    ];
    if (item.editable && !item.is_dir) {
      actions.splice(1, 0, { label: "Editar", class: "", icon: "edit", onClick: function () {
        closeModal();
        openEditor(item);
      } });
    }
    if (item.kind === "image") {
      body.append(el("img", { src: fileUrl(item.path, true), alt: item.name }));
    } else if (item.kind === "video") {
      body.append(el("video", { src: fileUrl(item.path, true), controls: "controls" }));
    } else if (item.kind === "audio") {
      body.append(el("audio", { src: fileUrl(item.path, true), controls: "controls" }));
    } else if (item.kind === "pdf") {
      body.append(el("iframe", { src: fileUrl(item.path, true), style: "width:100%;height:62vh;border:0;border-radius:8px;" }));
    } else if (item.kind === "text" || item.kind === "code" || item.editable) {
      body.append(el("div", { class: "muted", text: "Cargando vista previa…" }));
      api("/api/files/read?path=" + encodeURIComponent(item.path))
        .then(function (data) {
          body.innerHTML = "";
          body.append(el("pre", { text: data.content }));
        })
        .catch(function (err) {
          body.innerHTML = "";
          body.append(el("div", { class: "muted", text: err.message }));
        });
    } else {
      body.append(
        el("div", { class: "empty" },
          el("div", { class: "big kind-" + item.kind, html: svgIcon(KIND_ICONS[item.kind] || KIND_ICONS.other) }),
          el("div", { class: "muted", text: "Vista previa no disponible para este formato." })
        )
      );
    }
    openModal({ title: item.name, body: body, actions: actions });
  }

  async function openEditor(item) {
    var data;
    try {
      data = await api("/api/files/read?path=" + encodeURIComponent(item.path));
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    var textarea = el("textarea", { class: "editor-area" });
    textarea.value = data.content;
    var footer = el("div", { class: "modal-foot" });
    var cancel = el("button", { class: "btn ghost", text: "Cancelar" });
    cancel.addEventListener("click", closeModal);
    var save = el("button", { class: "btn success", html: svgIcon("save") + "Guardar" });
    save.addEventListener("click", async function () {
      try {
        await api("/api/files/write", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: item.path, content: textarea.value })
        });
        closeModal();
        toast("Archivo guardado", "success");
        loadDir(state.currentPath, false);
      } catch (err) {
        toast(err.message, "error");
      }
    });
    footer.append(cancel, save);
    var modal = $("modal");
    modal.className = "modal";
    modal.innerHTML = "";
    modal.append(
      el("div", { class: "modal-head" }, el("span", { text: "Editar · " + item.name })),
      el("div", { class: "modal-body" }, textarea),
      footer
    );
    $("modal-backdrop").classList.remove("hidden");
  }

  function promptDialog(title, label, initial, onAccept) {
    var input = el("input", { class: "input", type: "text" });
    input.value = initial || "";
    var field = el("div", {}, el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px;", text: label }), input);
    openModal({
      title: title,
      narrow: true,
      body: field,
      actions: [
        { label: "Cancelar", class: "ghost", onClick: closeModal },
        { label: "Aceptar", class: "primary", onClick: function () {
          var value = input.value.trim();
          if (!value) return;
          onAccept(value);
        } }
      ]
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        var value = input.value.trim();
        if (value) onAccept(value);
      }
    });
    setTimeout(function () { input.focus(); input.select(); }, 50);
  }

  async function openRename(item) {
    promptDialog("Renombrar", "Nuevo nombre para «" + item.name + "»", item.name, async function (newName) {
      try {
        await api("/api/files/rename", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: item.path, new_name: newName })
        });
        closeModal();
        toast("Elemento renombrado", "success");
        loadDir(state.currentPath, false);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  function confirmDelete(item) {
    openModal({
      title: "Eliminar",
      narrow: true,
      body: el("div", { class: "muted", text: "¿Eliminar «" + item.name + "» de forma permanente?" }),
      actions: [
        { label: "Cancelar", class: "ghost", onClick: closeModal },
        { label: "Eliminar", class: "danger", onClick: async function () {
          try {
            await api("/api/files?path=" + encodeURIComponent(item.path), { method: "DELETE" });
            closeModal();
            toast("Elemento eliminado", "success");
            loadDir(state.currentPath, false);
          } catch (err) {
            toast(err.message, "error");
          }
        } }
      ]
    });
  }

  function createFolder() {
    promptDialog("Nueva carpeta", "Nombre de la carpeta", "", async function (name) {
      try {
        await api("/api/files/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.currentPath, name: name })
        });
        closeModal();
        toast("Carpeta creada", "success");
        loadDir(state.currentPath, false);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function doSearch(query) {
    if (!query) {
      loadDir(state.currentPath, false);
      return;
    }
    try {
      var data = await api(
        "/api/files/search?path=" + encodeURIComponent(state.currentPath) +
        "&q=" + encodeURIComponent(query) + "&limit=300"
      );
      renderItems(data.items);
      $("count").textContent = data.total + " coincidencias" + (data.truncated ? " · truncado" : "");
    } catch (err) {
      if (err.status !== 401) toast(err.message, "error");
    }
  }

  function openPalette() {
    $("palette").classList.remove("hidden");
    var input = $("palette-input");
    input.value = "";
    setTimeout(function () { input.focus(); }, 30);
    paletteSearch("");
  }

  function closePalette() {
    $("palette").classList.add("hidden");
  }

  function paletteSearch(query) {
    var results = $("palette-results");
    results.innerHTML = "";
    var promise;
    if (query) {
      promise = api("/api/files/search?path=" + encodeURIComponent(state.currentPath) + "&q=" + encodeURIComponent(query) + "&limit=80");
    } else {
      promise = api("/api/files?path=" + encodeURIComponent(state.currentPath));
    }
    promise.then(function (data) {
      results.innerHTML = "";
      var items = data.items || [];
      if (!items.length) {
        results.append(el("div", { class: "muted", style: "padding:16px;font-size:12px;", text: "Sin coincidencias" }));
        return;
      }
      items.forEach(function (item) {
        var row = el(
          "div",
          { class: "palette-item" },
          el("span", { class: "palette-icon kind-" + item.kind, html: svgIcon(KIND_ICONS[item.kind] || KIND_ICONS.other) }),
          el("span", { class: "name", text: item.name }),
          el("span", { class: "size", text: item.is_dir ? "Carpeta" : humanSize(item.size) })
        );
        row.addEventListener("click", function () {
          closePalette();
          openItem(item);
        });
        results.append(row);
      });
    }).catch(function (err) {
      if (err.status !== 401) toast(err.message, "error");
    });
  }

  function showMini(name) {
    $("mini-vortex").classList.remove("hidden");
    $("mini-name").textContent = name;
    $("mini-speed").textContent = "";
    $("mini-bar").style.width = "0%";
  }

  function updateMini(fraction, speed) {
    $("mini-bar").style.width = Math.max(0, Math.min(100, fraction * 100)) + "%";
    $("mini-speed").textContent = humanSpeed(speed);
  }

  function hideMini() {
    $("mini-vortex").classList.add("hidden");
  }

  function uploadDirect(file, target) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/files/upload");
      if (state.token) xhr.setRequestHeader("Authorization", "Bearer " + state.token);
      var started = performance.now();
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) {
          updateMini(event.loaded / event.total, event.loaded / ((performance.now() - started) / 1000));
        }
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) {
          resolve();
        } else {
          var message = "HTTP " + xhr.status;
          try {
            message = detailOf(JSON.parse(xhr.responseText), message);
          } catch (err) {}
          reject(new Error(message));
        }
      };
      xhr.onerror = function () {
        reject(new Error("Error de red"));
      };
      var form = new FormData();
      form.append("file", file);
      form.append("target_dir", target);
      showMini(file.name);
      xhr.send(form);
    });
  }

  async function uploadChunked(file, target) {
    showMini(file.name);
    var init = await api("/api/files/upload/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size: file.size, target_dir: target })
    });
    var offset = 0;
    var started = performance.now();
    for (var index = 0; index < init.chunk_count; index++) {
      var chunk = file.slice(offset, offset + init.chunk_size);
      var uploaded = false;
      var lastError = null;
      for (var attempt = 0; attempt < 3 && !uploaded; attempt++) {
        var response = await fetch("/api/files/upload/" + init.upload_id + "/" + index, {
          method: "PUT",
          headers: authHeaders(),
          body: chunk
        });
        if (response.ok) {
          uploaded = true;
        } else if (response.status === 401) {
          handleUnauthorized();
          throw new Error("Sesión expirada durante la subida");
        } else {
          lastError = "HTTP " + response.status;
          try {
            lastError = detailOf(await response.json(), lastError);
          } catch (err) {}
        }
      }
      if (!uploaded) throw new Error(lastError || "Fallo al subir un bloque");
      offset += chunk.size;
      updateMini(offset / Math.max(file.size, 1), offset / ((performance.now() - started) / 1000));
    }
    await api("/api/files/upload/" + init.upload_id + "/complete", { method: "POST" });
    updateMini(1, 0);
  }

  async function uploadFiles(files) {
    if (!files || !files.length) return;
    var target = state.currentPath;
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      try {
        if (file.size <= DIRECT_LIMIT) await uploadDirect(file, target);
        else await uploadChunked(file, target);
        toast("Subido: " + file.name, "success");
      } catch (err) {
        toast("Error al subir " + file.name + ": " + err.message, "error");
      }
    }
    hideMini();
    loadDir(target, false);
  }

  function toggleLogs() {
    if (state.role !== "host") {
      toast("La consola de logs solo está disponible para el host", "error");
      return;
    }
    var overlay = $("logs-overlay");
    var hidden = overlay.classList.contains("hidden");
    if (hidden && !state.logsWs) {
      var protocol = location.protocol === "https:" ? "wss:" : "ws:";
      var url = protocol + "//" + location.host + "/api/ws/logs";
      if (state.token) url += "?token=" + encodeURIComponent(state.token);
      try {
        state.logsWs = new WebSocket(url);
        state.logsWs.onmessage = function (event) {
          var text = event.data;
          var severity = "INFO";
          try {
            var record = JSON.parse(text);
            severity = String(record.severity || "INFO");
            text = [
              record.timestamp,
              severity.padEnd(8),
              record.action,
              record.client_ip,
              record.target_path,
              record.status_code
            ].join("  ");
          } catch (err) {}
          var body = $("logs-body");
          body.append(el("div", { class: "log-line sev-" + severity, text: text }));
          if (body.textContent.length > 120000) {
            while (body.textContent.length > 90000 && body.firstChild) {
              body.removeChild(body.firstChild);
            }
          }
          body.scrollTop = body.scrollHeight;
        };
        state.logsWs.onclose = function () {
          state.logsWs = null;
        };
      } catch (err) {
        toast("No se pudo abrir la consola", "error");
      }
    }
    overlay.classList.toggle("hidden");
  }

  function logout() {
    api("/api/auth/logout", { method: "POST" }).catch(function () {});
    state.token = null;
    state.role = null;
    localStorage.removeItem("vortex_token");
    if (state.logsWs) {
      state.logsWs.close();
      state.logsWs = null;
    }
    $("logs-overlay").classList.add("hidden");
    $("logs-body").textContent = "";
    state.pending = location.pathname + location.search;
    showLogin("");
  }

  function bindEvents() {
    $("login-form").addEventListener("submit", async function (event) {
      event.preventDefault();
      var secret = $("login-secret").value;
      if (!secret) return;
      $("login-busy").classList.remove("hidden");
      try {
        var response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ secret: secret })
        });
        if (!response.ok) {
          var payload = null;
          try {
            payload = await response.json();
          } catch (err) {}
          $("login-error").textContent = detailOf(payload, "Credenciales inválidas");
          return;
        }
        var data = await response.json();
        state.token = data.access_token;
        state.role = data.role;
        localStorage.setItem("vortex_token", state.token);
        $("login-busy").classList.add("hidden");
        await startApp();
      } catch (err) {
        $("login-error").textContent = "No se pudo conectar con el servidor";
      } finally {
        $("login-busy").classList.add("hidden");
      }
    });

    $("btn-login").addEventListener("click", function () {
      showLogin("");
    });
    $("btn-logout").addEventListener("click", logout);
    $("btn-logs").addEventListener("click", toggleLogs);
    $("logs-close").addEventListener("click", function () {
      $("logs-overlay").classList.add("hidden");
    });
    $("win-minimize").addEventListener("click", function () {
      $("app").classList.toggle("minimized");
    });
    $("win-maximize").addEventListener("click", function () {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(function () {});
      } else if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(function () {});
      }
    });
    $("win-close").addEventListener("click", function () {
      window.close();
      setTimeout(function () {
        toast("El navegador no permite cerrar esta pestaña", "info");
      }, 120);
    });

    $("btn-up").addEventListener("click", function () {
      if (state.parent !== null) loadDir(state.parent);
    });
    $("btn-refresh").addEventListener("click", function () {
      loadDir(state.currentPath, false);
    });
    $("btn-mkdir").addEventListener("click", createFolder);
    $("btn-upload").addEventListener("click", function () {
      $("file-input").click();
    });
    $("btn-zip").addEventListener("click", function () {
      triggerDownloadZip(state.currentPath);
    });
    $("file-input").addEventListener("change", function (event) {
      uploadFiles(Array.from(event.target.files || []));
      event.target.value = "";
    });

    $("search").addEventListener("input", function (event) {
      var query = event.target.value.trim();
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(function () {
        doSearch(query);
      }, 220);
    });

    $("modal-backdrop").addEventListener("click", function (event) {
      if (event.target === $("modal-backdrop")) closeModal();
    });

    $("palette-input").addEventListener("input", function (event) {
      var query = event.target.value.trim();
      clearTimeout(state.paletteTimer);
      state.paletteTimer = setTimeout(function () {
        paletteSearch(query);
      }, 200);
    });
    $("palette").addEventListener("click", function (event) {
      if (event.target === $("palette")) closePalette();
    });

    ["dragenter", "dragover"].forEach(function (name) {
      document.addEventListener(name, function (event) {
        event.preventDefault();
        if (!state.token && state.requiresLogin) return;
        $("dropzone").classList.remove("hidden");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      document.addEventListener(name, function (event) {
        if (event.target === document || event.target === document.documentElement) {
          $("dropzone").classList.add("hidden");
        }
      });
    });
    document.addEventListener("drop", function (event) {
      event.preventDefault();
      $("dropzone").classList.add("hidden");
      if (!state.token && state.requiresLogin) return;
      uploadFiles(Array.from(event.dataTransfer.files || []));
    });

    document.addEventListener("keydown", function (event) {
      var key = (event.key || "").toLowerCase();
      if (event.ctrlKey && key === "k") {
        event.preventDefault();
        if ($("palette").classList.contains("hidden")) openPalette();
        else closePalette();
      } else if (key === "escape") {
        if (!$("palette").classList.contains("hidden")) closePalette();
        else if (!$("modal-backdrop").classList.contains("hidden")) closeModal();
        else $("logs-overlay").classList.add("hidden");
      }
    });

    window.addEventListener("popstate", function () {
      routeFromLocation();
    });
  }

  bindEvents();
  boot();
})();
