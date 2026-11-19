// Cliente web de Vortex: búsqueda, archivos, subidas y sesión.
(function () {
  "use strict";

  // Límite a partir del cual una subida se divide en bloques.
  var LIMITE_DIRECTO = 100 * 1024 * 1024;

  // Estado compartido de la sesión, la carpeta actual y las subidas.
  var estadoGlobal = {
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
    uploadDone: 0,
    fileRevision: 0,
    fileSyncTimer: null,
    fileSyncBusy: false
  };

  // Iconos asociados a cada tipo de archivo reconocido.
  var ICONOS_TIPO = {
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

  // Utilidades para consultar y construir elementos del DOM.
  function $(id) {
    return document.getElementById(id);
  }

  function el(etiquetaHtml, atributos) {
    var nodo = document.createElement(etiquetaHtml);
    atributos = atributos || {};
    Object.keys(atributos).forEach(function (clave) {
      var valor = atributos[clave];
      if (valor === null || valor === undefined) return;
      if (clave === "class") nodo.className = valor;
      else if (clave === "text") nodo.textContent = valor;
      else if (clave === "html") nodo.innerHTML = valor;
      else if (clave === "dataset") Object.assign(nodo.dataset, valor);
      else if (clave.indexOf("on") === 0) nodo.addEventListener(clave.slice(2).toLowerCase(), valor);
      else if (clave === "value") nodo.value = valor;
      else nodo.setAttribute(clave, valor);
    });
    for (var i = 2; i < arguments.length; i++) {
      var hijo = arguments[i];
      if (hijo === null || hijo === undefined) continue;
      nodo.append(hijo.nodeType ? hijo : document.createTextNode(String(hijo)));
    }
    return nodo;
  }

  function svgIcon(nombre, claseCss) {
    return (
      '<svg class="icon' + (claseCss ? " " + claseCss : "") + '" aria-hidden="true">' +
      '<use href="#i-' + nombre + '"></use></svg>'
    );
  }

  function setStatus(conectado) {
    var pastilla = $("status-pill");
    if (!pastilla) return;
    pastilla.classList.toggle("offline", !conectado);
    $("status-text").textContent = conectado ? "EN LÍNEA" : "DETENIDO";
  }

  function toast(mensaje, tipoDato) {
    tipoDato = tipoDato || "info";
    var nodo = el("div", { class: "toast " + tipoDato, text: mensaje });
    $("toasts").append(nodo);
    setTimeout(function () {
      nodo.remove();
    }, 4200);
  }

  function authHeaders() {
    return estadoGlobal.token ? { Authorization: "Bearer " + estadoGlobal.token } : {};
  }

  function detailOf(carga, respaldo) {
    if (!carga) return respaldo;
    var detalle = carga.detail;
    if (detalle === undefined || detalle === null) return respaldo;
    if (typeof detalle === "object") return detalle.message || JSON.stringify(detalle);
    return String(detalle);
  }

  // Acceso a la API, cabeceras de sesión y manejo común de errores.
  async function api(ruta, opciones) {
    opciones = opciones || {};
    var cabeceras = Object.assign({}, opciones.headers || {}, authHeaders());
    var respuesta;
    try {
      respuesta = await fetch(ruta, Object.assign({}, opciones, { headers: cabeceras }));
    } catch (errorCapturado) {
      setStatus(false);
      throw new Error("No se pudo conectar con el servidor");
    }
    if (respuesta.status === 401) {
      handleUnauthorized();
      var noAutorizado = new Error("No autenticado");
      noAutorizado.status = 401;
      throw noAutorizado;
    }
    if (!respuesta.ok) {
      var mensaje = respuesta.statusText || "HTTP " + respuesta.status;
      try {
        mensaje = detailOf(await respuesta.json(), mensaje);
      } catch (errorCapturado) {}
      var error = new Error(mensaje);
      error.status = respuesta.status;
      throw error;
    }
    setStatus(true);
    var tipoContenido = respuesta.headers.get("content-type") || "";
    return tipoContenido.indexOf("application/json") >= 0 ? respuesta.json() : respuesta.text();
  }

  function handleUnauthorized() {
    if (estadoGlobal.token) {
      estadoGlobal.token = null;
      localStorage.removeItem("vortex_token");
    }
    if (estadoGlobal.authMode !== "none") {
      estadoGlobal.pending = location.pathname + location.search;
      showLogin("La sesión expiró o no es válida.");
    }
  }

  function showLogin(mensaje) {
    $("login-overlay").classList.remove("hidden");
    $("login-error").textContent = mensaje || "";
    $("login-secret").value = "";
    $("login-busy").classList.add("hidden");
    $("login-guest").classList.toggle("hidden", !(estadoGlobal.allowGuest && !estadoGlobal.requiresLogin));
    setTimeout(function () {
      $("login-secret").focus();
    }, 50);
  }

  function hideLogin() {
    $("login-overlay").classList.add("hidden");
    $("login-error").textContent = "";
    $("login-busy").classList.add("hidden");
  }

  function segmentPath(ruta) {
    return ruta.split("/").map(encodeURIComponent).join("/");
  }

  function browseUrl(ruta) {
    return ruta ? "/browse/" + segmentPath(ruta) : "/";
  }

  function downloadUrl(ruta) {
    return "/descargar/" + segmentPath(ruta);
  }

  function fileUrl(ruta, enLinea) {
    var parametros = new URLSearchParams({ path: ruta });
    if (enLinea) parametros.set("inline", "1");
    if (estadoGlobal.token) parametros.set("token", estadoGlobal.token);
    return "/api/files/download?" + parametros.toString();
  }

  function zipUrl(ruta) {
    var parametros = new URLSearchParams({ path: ruta });
    if (estadoGlobal.token) parametros.set("token", estadoGlobal.token);
    return "/api/files/zip?" + parametros.toString();
  }

  function humanSize(tamano) {
    tamano = Number(tamano) || 0;
    if (tamano <= 0) return "0 B";
    var unidades = ["B", "KB", "MB", "GB", "TB"];
    var valor = tamano;
    var indice = 0;
    while (valor >= 1024 && indice < unidades.length - 1) {
      valor /= 1024;
      indice += 1;
    }
    return indice === 0 ? Math.round(valor) + " " + unidades[indice] : valor.toFixed(1) + " " + unidades[indice];
  }

  function humanSpeed(valor) {
    if (!valor || valor <= 0) return "";
    return humanSize(valor) + "/s";
  }

  function formatTime(marcaTiempo) {
    try {
      var fecha = new Date(marcaTiempo * 1000);
      var relleno = function (valor) {
        return String(valor).padStart(2, "0");
      };
      return (
        relleno(fecha.getDate()) + "/" + relleno(fecha.getMonth() + 1) + "/" + fecha.getFullYear() +
        " " + relleno(fecha.getHours()) + ":" + relleno(fecha.getMinutes())
      );
    } catch (errorCapturado) {
      return "-";
    }
  }

  // Arranque: comprobación de sesión, rol y sondeo de cambios en disco.
  function setRoleBadge() {
    var insignia = $("role-badge");
    var etiquetas = {
      host: "HOST",
      guest_write: "INVITADO · TOTAL",
      guest_read: "INVITADO · LECTURA"
    };
    $("role-label").textContent = etiquetas[estadoGlobal.role] || "INVITADO";
    insignia.className = "role-badge" + (estadoGlobal.role === "host" ? " host" : "");
    $("btn-logs").classList.toggle("hidden", estadoGlobal.role !== "host");
    $("btn-logout").classList.toggle("hidden", !estadoGlobal.token);
    $("btn-login").classList.toggle("hidden", estadoGlobal.authMode === "none" || !!estadoGlobal.token);
  }

  async function resolveRole() {
    try {
      var yo = await api("/api/auth/me");
      estadoGlobal.role = yo.role;
      estadoGlobal.authMode = yo.auth_mode;
    } catch (errorCapturado) {
      if (errorCapturado.status !== 401) throw errorCapturado;
    }
  }

  async function boot() {
    var estado;
    try {
      estado = await fetch("/api/auth/status").then(function (respuesta) {
        return respuesta.json();
      });
    } catch (errorCapturado) {
      setStatus(false);
      toast("No se pudo contactar con el servidor", "error");
      return;
    }
    setStatus(true);
    estadoGlobal.authMode = estado.auth_mode;
    estadoGlobal.requiresLogin = estado.requires_login;
    estadoGlobal.allowGuest = estado.allow_guest;
    if (estadoGlobal.token) {
      try {
        var yo = await api("/api/auth/me");
        estadoGlobal.role = yo.role;
      } catch (errorCapturado) {
        if (errorCapturado.status === 401) {
          estadoGlobal.token = null;
          localStorage.removeItem("vortex_token");
        }
      }
    }
    if (!estadoGlobal.token) {
      if (estadoGlobal.requiresLogin) {
        estadoGlobal.pending = location.pathname + location.search;
        showLogin("");
        return;
      }
      try {
        var invitado = await api("/api/auth/me");
        estadoGlobal.role = invitado.role;
        estadoGlobal.authMode = invitado.auth_mode;
      } catch (errorCapturado) {
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
    } catch (errorCapturado) {
      toast(errorCapturado.message, "error");
    }
    setRoleBadge();
    await routeFromLocation();
    startFileSync();
  }

  function startFileSync() {
    if (estadoGlobal.fileSyncTimer) clearInterval(estadoGlobal.fileSyncTimer);
    estadoGlobal.fileSyncBusy = false;
    pollFileChanges();
    estadoGlobal.fileSyncTimer = setInterval(pollFileChanges, 1000);
  }

  async function pollFileChanges() {
    if (estadoGlobal.fileSyncBusy || document.hidden) return;
    estadoGlobal.fileSyncBusy = true;
    try {
      var datos = await api("/api/system/changes?since=" + estadoGlobal.fileRevision);
      var cambios = datos.changes || [];
      estadoGlobal.fileRevision = Number(datos.revision) || 0;
      if (!cambios.length) return;

      var ruta = estadoGlobal.currentPath;
      var refrescar = false;
      cambios.forEach(function (cambio) {
        if (cambio.removed && (ruta === cambio.removed || ruta.indexOf(cambio.removed + "/") === 0)) {
          ruta = cambio.removed.indexOf("/") >= 0 ? cambio.removed.slice(0, cambio.removed.lastIndexOf("/")) : "";
          refrescar = true;
        }
        if (cambio.renamed && (ruta === cambio.renamed.from || ruta.indexOf(cambio.renamed.from + "/") === 0)) {
          ruta = cambio.renamed.to + ruta.slice(cambio.renamed.from.length);
          refrescar = true;
        }
        if ((cambio.paths || []).indexOf(ruta) >= 0) refrescar = true;
      });
      if (refrescar) {
        try {
          history.replaceState({ path: ruta }, "", browseUrl(ruta));
        } catch (errorCapturado) {}
        await loadDir(ruta, false);
      }
    } catch (errorCapturado) {
      if (errorCapturado.status !== 401 && errorCapturado.status !== 404) setStatus(false);
    } finally {
      estadoGlobal.fileSyncBusy = false;
    }
  }

  // Navegación, listado y pintado de la carpeta actual.
  async function routeFromLocation() {
    var rutaUrl = location.pathname;
    try {
      rutaUrl = decodeURIComponent(rutaUrl);
    } catch (errorCapturado) {}
    if (rutaUrl.indexOf("/descargar/") === 0) {
      var objetivo = rutaUrl.slice("/descargar/".length);
      triggerDownload(objetivo);
      var padre = objetivo.indexOf("/") >= 0 ? objetivo.slice(0, objetivo.lastIndexOf("/")) : "";
      estadoGlobal.pending = "";
      await loadDir(padre, true);
    } else if (rutaUrl.indexOf("/browse/") === 0) {
      await loadDir(rutaUrl.slice("/browse/".length), false);
    } else {
      await loadDir(estadoGlobal.currentPath || "", false);
    }
  }

  async function loadDir(ruta, updateHistory) {
    updateHistory = updateHistory !== false;
    try {
      var datos = await api("/api/files?path=" + encodeURIComponent(ruta));
      estadoGlobal.currentPath = datos.path;
      estadoGlobal.parent = datos.parent;
      estadoGlobal.items = datos.items;
      $("search").value = "";
      renderBreadcrumb();
      renderItems(datos.items);
      $("count").textContent = datos.total + " elementos";
      if (updateHistory) {
        try {
          history.pushState({ path: datos.path }, "", browseUrl(datos.path));
        } catch (errorCapturado) {}
      }
    } catch (errorCapturado) {
      if (errorCapturado.status !== 401) toast(errorCapturado.message, "error");
    }
  }

  function renderBreadcrumb() {
    var navegacion = $("breadcrumb");
    navegacion.innerHTML = "";
    var raiz = el("button", { class: "crumb root", html: svgIcon("home") + "<span>Raíz</span>" });
    raiz.addEventListener("click", function () {
      loadDir("");
    });
    navegacion.append(raiz);
    if (!estadoGlobal.currentPath) return;
    var acumulado = [];
    estadoGlobal.currentPath.split("/").forEach(function (parte) {
      acumulado.push(parte);
      var objetivo = acumulado.join("/");
      navegacion.append(el("span", { class: "crumb-sep", text: "/" }));
      var migaja = el("button", { class: "crumb", text: parte });
      migaja.addEventListener("click", function () {
        loadDir(objetivo);
      });
      navegacion.append(migaja);
    });
  }

  function renderItems(elementos) {
    var listado = $("listing");
    listado.innerHTML = "";
    if (!elementos.length) {
      listado.append(
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
    elementos.forEach(function (elemento) {
      listado.append(buildRow(elemento));
    });
  }

  function buildRow(elemento) {
    var tipo = elemento.kind || "other";
    var fila = el("div", { class: "row" });
    fila.addEventListener("click", function () {
      openItem(elemento);
    });
    var icono = el("div", {
      class: "row-icon kind-" + tipo,
      html: svgIcon(ICONOS_TIPO[tipo] || ICONOS_TIPO.other)
    });
    var metadatos = elemento.is_dir ? "Carpeta" : humanSize(elemento.size);
    metadatos += "  ·  " + formatTime(elemento.modified);
    var principal = el(
      "div",
      { class: "row-main" },
      el("div", { class: "row-name", text: elemento.name }),
      el("div", { class: "row-meta", text: metadatos })
    );
    var acciones = el("div", { class: "row-actions" });
    if (elemento.is_dir) {
      acciones.append(actionButton("archive", "Descargar .zip", function () {
        triggerDownloadZip(elemento.path);
      }));
    } else {
      acciones.append(actionButton("download", "Descargar", function () {
        triggerDownload(elemento.path);
      }));
    }
    if (elemento.editable && !elemento.is_dir) {
      acciones.append(actionButton("edit", "Editar", function () {
        openEditor(elemento);
      }, "accent"));
    }
    acciones.append(actionButton("rename", "Renombrar", function () {
      openRename(elemento);
    }));
    acciones.append(actionButton("delete", "Eliminar", function () {
      confirmDelete(elemento);
    }, "danger"));
    fila.append(icono, principal, acciones);
    return fila;
  }

  function actionButton(nombreIcono, titulo, manejador, claseCss) {
    var boton = el("button", {
      class: "icon-btn" + (claseCss ? " " + claseCss : ""),
      title: titulo,
      "aria-label": titulo,
      html: svgIcon(nombreIcono)
    });
    boton.addEventListener("click", function (evento) {
      evento.stopPropagation();
      manejador();
    });
    return boton;
  }

  function openItem(elemento) {
    if (elemento.is_dir) loadDir(elemento.path);
    else openPreview(elemento);
  }

  function triggerDownload(ruta) {
    var enlace = el("a", { href: fileUrl(ruta) });
    document.body.append(enlace);
    enlace.click();
    enlace.remove();
  }

  function triggerDownloadZip(ruta) {
    var enlace = el("a", { href: zipUrl(ruta) });
    document.body.append(enlace);
    enlace.click();
    enlace.remove();
  }

  // Modales, vista previa y edición de archivos de texto.
  function openModal(opciones) {
    var fondoModal = $("modal-backdrop");
    var ventanaModal = $("modal");
    ventanaModal.className = "modal" + (opciones.narrow ? " narrow" : "");
    ventanaModal.innerHTML = "";
    ventanaModal.append(el("div", { class: "modal-head" }, el("span", { text: opciones.title })));
    var cuerpo = el("div", { class: "modal-body" });
    if (typeof opciones.body === "string") cuerpo.innerHTML = opciones.body;
    else if (opciones.body) cuerpo.append(opciones.body);
    ventanaModal.append(cuerpo);
    if (opciones.actions && opciones.actions.length) {
      var pie = el("div", { class: "modal-foot" });
      opciones.actions.forEach(function (accion) {
        var boton = el("button", { class: "btn " + (accion.class || "") });
        if (accion.icon) boton.innerHTML = svgIcon(accion.icon);
        boton.append(document.createTextNode(accion.label));
        boton.addEventListener("click", function () {
          if (accion.onClick) accion.onClick();
          else closeModal();
        });
        pie.append(boton);
      });
      ventanaModal.append(pie);
    }
    fondoModal.classList.remove("hidden");
    return { modal: ventanaModal, body: cuerpo };
  }

  function closeModal() {
    $("modal-backdrop").classList.add("hidden");
    $("modal").innerHTML = "";
  }

  function openPreview(elemento) {
    var cuerpo = el("div");
    var acciones = [
      { label: "Cerrar", class: "ghost", onClick: closeModal },
      { label: "Descargar", class: "primary", icon: "download", onClick: function () {
        triggerDownload(elemento.path);
      } }
    ];
    if (elemento.editable && !elemento.is_dir) {
      acciones.splice(1, 0, { label: "Editar", class: "", icon: "edit", onClick: function () {
        closeModal();
        openEditor(elemento);
      } });
    }
    if (elemento.kind === "image") {
      cuerpo.append(el("img", { src: fileUrl(elemento.path, true), alt: elemento.name }));
    } else if (elemento.kind === "video") {
      cuerpo.append(el("video", { src: fileUrl(elemento.path, true), controls: "controls" }));
    } else if (elemento.kind === "audio") {
      cuerpo.append(el("audio", { src: fileUrl(elemento.path, true), controls: "controls" }));
    } else if (elemento.kind === "pdf") {
      cuerpo.append(el("iframe", { src: fileUrl(elemento.path, true), style: "width:100%;height:62vh;border:0;border-radius:8px;" }));
    } else if (elemento.kind === "text" || elemento.kind === "code" || elemento.editable) {
      cuerpo.append(el("div", { class: "muted", text: "Cargando vista previa…" }));
      api("/api/files/read?path=" + encodeURIComponent(elemento.path))
        .then(function (datos) {
          cuerpo.innerHTML = "";
          cuerpo.append(el("pre", { text: datos.content }));
        })
        .catch(function (errorCapturado) {
          cuerpo.innerHTML = "";
          cuerpo.append(el("div", { class: "muted", text: errorCapturado.message }));
        });
    } else {
      cuerpo.append(
        el("div", { class: "empty" },
          el("div", { class: "big kind-" + elemento.kind, html: svgIcon(ICONOS_TIPO[elemento.kind] || ICONOS_TIPO.other) }),
          el("div", { class: "muted", text: "Vista previa no disponible para este formato." })
        )
      );
    }
    openModal({ title: elemento.name, body: cuerpo, actions: acciones });
  }

  async function openEditor(elemento) {
    var datos;
    try {
      datos = await api("/api/files/read?path=" + encodeURIComponent(elemento.path));
    } catch (errorCapturado) {
      toast(errorCapturado.message, "error");
      return;
    }
    var areaTexto = el("textarea", { class: "editor-area" });
    areaTexto.value = datos.content;
    var piePagina = el("div", { class: "modal-foot" });
    var cancelar = el("button", { class: "btn ghost", text: "Cancelar" });
    cancelar.addEventListener("click", closeModal);
    var guardar = el("button", { class: "btn success", html: svgIcon("save") + "Guardar" });
    guardar.addEventListener("click", async function () {
      try {
        await api("/api/files/write", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: elemento.path, content: areaTexto.value })
        });
        closeModal();
        toast("Archivo guardado", "success");
        loadDir(estadoGlobal.currentPath, false);
      } catch (errorCapturado) {
        toast(errorCapturado.message, "error");
      }
    });
    piePagina.append(cancelar, guardar);
    var ventanaModal = $("modal");
    ventanaModal.className = "modal";
    ventanaModal.innerHTML = "";
    ventanaModal.append(
      el("div", { class: "modal-head" }, el("span", { text: "Editar · " + elemento.name })),
      el("div", { class: "modal-body" }, areaTexto),
      piePagina
    );
    $("modal-backdrop").classList.remove("hidden");
  }

  // Diálogos de creación, renombrado y confirmación de borrado.
  function promptDialog(titulo, etiqueta, valorInicial, alAceptar) {
    var entrada = el("input", { class: "input", type: "text" });
    entrada.value = valorInicial || "";
    var campo = el("div", {}, el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px;", text: etiqueta }), entrada);
    openModal({
      title: titulo,
      narrow: true,
      body: campo,
      actions: [
        { label: "Cancelar", class: "ghost", onClick: closeModal },
        { label: "Aceptar", class: "primary", onClick: function () {
          var valor = entrada.value.trim();
          if (!valor) return;
          alAceptar(valor);
        } }
      ]
    });
    entrada.addEventListener("keydown", function (evento) {
      if (evento.key === "Enter") {
        evento.preventDefault();
        var valor = entrada.value.trim();
        if (valor) alAceptar(valor);
      }
    });
    setTimeout(function () { entrada.focus(); entrada.select(); }, 50);
  }

  async function openRename(elemento) {
    promptDialog("Renombrar", "Nuevo nombre para «" + elemento.name + "»", elemento.name, async function (nombreNuevo) {
      try {
        await api("/api/files/rename", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: elemento.path, new_name: nombreNuevo })
        });
        closeModal();
        toast("Elemento renombrado", "success");
        loadDir(estadoGlobal.currentPath, false);
      } catch (errorCapturado) {
        toast(errorCapturado.message, "error");
      }
    });
  }

  function confirmDelete(elemento) {
    openModal({
      title: "Eliminar",
      narrow: true,
      body: el("div", { class: "muted", text: "¿Eliminar «" + elemento.name + "» de forma permanente?" }),
      actions: [
        { label: "Cancelar", class: "ghost", onClick: closeModal },
        { label: "Eliminar", class: "danger", onClick: async function () {
          try {
            await api("/api/files?path=" + encodeURIComponent(elemento.path), { method: "DELETE" });
            closeModal();
            toast("Elemento eliminado", "success");
            loadDir(estadoGlobal.currentPath, false);
          } catch (errorCapturado) {
            toast(errorCapturado.message, "error");
          }
        } }
      ]
    });
  }

  function createFolder() {
    promptDialog("Nueva carpeta", "Nombre de la carpeta", "", async function (nombre) {
      try {
        await api("/api/files/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: estadoGlobal.currentPath, name: nombre })
        });
        closeModal();
        toast("Carpeta creada", "success");
        loadDir(estadoGlobal.currentPath, false);
      } catch (errorCapturado) {
        toast(errorCapturado.message, "error");
      }
    });
  }

  function createTextFile() {
    var nombre = el("input", { class: "input", type: "text", value: "texto", maxlength: "240" });
    var areaTexto = el("textarea", { class: "editor-area", placeholder: "Escribe aquí el texto que quieres guardar…", style: "min-height:240px;max-height:50vh;" });
    var cuerpo = el(
      "div",
      {},
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px;", text: "Nombre del archivo" }),
      nombre,
      el("div", { class: "muted", style: "font-size:12px;margin:14px 0 6px;", text: "Contenido" }),
      areaTexto
    );
    openModal({
      title: "Escribir archivo de texto",
      body: cuerpo,
      actions: [
        { label: "Cancelar", class: "ghost", onClick: closeModal },
        { label: "Crear .txt", class: "primary", onClick: save }
      ]
    });
    nombre.focus();

    async function save() {
      var limpiarNombre = nombre.value.trim();
      if (limpiarNombre.toLowerCase().endsWith(".txt")) limpiarNombre = limpiarNombre.slice(0, -4);
      if (!limpiarNombre || limpiarNombre === "." || limpiarNombre === ".." || /[\/\\\0]/.test(limpiarNombre)) {
        toast("Escribe un nombre válido, sin rutas ni barras.", "error");
        return;
      }
      var nombreArchivo = limpiarNombre + ".txt";
      try {
        var listado = await api("/api/files?path=" + encodeURIComponent(estadoGlobal.currentPath));
        if ((listado.items || []).some(function (elemento) { return elemento.name.toLocaleLowerCase() === nombreArchivo.toLocaleLowerCase(); })) {
          toast("Ya existe «" + nombreArchivo + "» en esta carpeta.", "error");
          return;
        }
        var ruta = (estadoGlobal.currentPath ? estadoGlobal.currentPath + "/" : "") + nombreArchivo;
        await api("/api/files/write", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: ruta, content: areaTexto.value })
        });
        closeModal();
        toast(nombreArchivo + " creado", "success");
        loadDir(estadoGlobal.currentPath, false);
      } catch (errorCapturado) {
        toast(errorCapturado.message, "error");
      }
    }
  }

  // Búsqueda global y paleta rápida de comandos.
  async function doSearch(consulta) {
    if (!consulta) {
      loadDir(estadoGlobal.currentPath, false);
      return;
    }
    try {
      var datos = await api(
        "/api/files/search?path=" + encodeURIComponent(estadoGlobal.currentPath) +
        "&q=" + encodeURIComponent(consulta) + "&limit=300"
      );
      renderItems(datos.items);
      $("count").textContent = datos.total + " coincidencias" + (datos.truncated ? " · truncado" : "");
    } catch (errorCapturado) {
      if (errorCapturado.status !== 401) toast(errorCapturado.message, "error");
    }
  }

  function openPalette() {
    $("palette").classList.remove("hidden");
    var entrada = $("palette-input");
    entrada.value = "";
    setTimeout(function () { entrada.focus(); }, 30);
    paletteSearch("");
  }

  function closePalette() {
    $("palette").classList.add("hidden");
  }

  function paletteSearch(consulta) {
    var resultados = $("palette-results");
    resultados.innerHTML = "";
    var promesa;
    if (consulta) {
      promesa = api("/api/files/search?path=" + encodeURIComponent(estadoGlobal.currentPath) + "&q=" + encodeURIComponent(consulta) + "&limit=80");
    } else {
      promesa = api("/api/files?path=" + encodeURIComponent(estadoGlobal.currentPath));
    }
    promesa.then(function (datos) {
      resultados.innerHTML = "";
      var elementos = datos.items || [];
      if (!elementos.length) {
        resultados.append(el("div", { class: "muted", style: "padding:16px;font-size:12px;", text: "Sin coincidencias" }));
        return;
      }
      elementos.forEach(function (elemento) {
        var fila = el(
          "div",
          { class: "palette-item" },
          el("span", { class: "palette-icon kind-" + elemento.kind, html: svgIcon(ICONOS_TIPO[elemento.kind] || ICONOS_TIPO.other) }),
          el("span", { class: "name", text: elemento.name }),
          el("span", { class: "size", text: elemento.is_dir ? "Carpeta" : humanSize(elemento.size) })
        );
        fila.addEventListener("click", function () {
          closePalette();
          openItem(elemento);
        });
        resultados.append(fila);
      });
    }).catch(function (errorCapturado) {
      if (errorCapturado.status !== 401) toast(errorCapturado.message, "error");
    });
  }

  // Subida de archivos con progreso, bloques y reanudación.
  function showMini(nombre) {
    $("mini-vortex").classList.remove("hidden");
    $("mini-name").textContent = nombre;
    $("mini-speed").textContent = "";
    $("mini-bar").style.width = "0%";
  }

  function updateMini(fraccion, velocidad) {
    $("mini-bar").style.width = Math.max(0, Math.min(100, fraccion * 100)) + "%";
    $("mini-speed").textContent = humanSpeed(velocidad);
  }

  function hideMini() {
    $("mini-vortex").classList.add("hidden");
  }

  function uploadDirect(archivo, objetivo) {
    return new Promise(function (resolver, rechazar) {
      var peticion = new XMLHttpRequest();
      peticion.open("POST", "/api/files/upload");
      if (estadoGlobal.token) peticion.setRequestHeader("Authorization", "Bearer " + estadoGlobal.token);
      var iniciado = performance.now();
      peticion.upload.onprogress = function (evento) {
        if (evento.lengthComputable) {
          updateMini(evento.loaded / evento.total, evento.loaded / ((performance.now() - iniciado) / 1000));
        }
      };
      peticion.onload = function () {
        if (peticion.status >= 200 && peticion.status < 400) {
          resolver();
        } else {
          var mensaje = "HTTP " + peticion.status;
          try {
            mensaje = detailOf(JSON.parse(peticion.responseText), mensaje);
          } catch (errorCapturado) {}
          rechazar(new Error(mensaje));
        }
      };
      peticion.onerror = function () {
        rechazar(new Error("Error de red"));
      };
      var formulario = new FormData();
      formulario.append("file", archivo);
      formulario.append("target_dir", objetivo);
      showMini(archivo.name);
      peticion.send(formulario);
    });
  }

  async function uploadChunked(archivo, objetivo) {
    showMini(archivo.name);
    var inicial = await api("/api/files/upload/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: archivo.name, size: archivo.size, target_dir: objetivo })
    });
    var desplazamiento = 0;
    var iniciado = performance.now();
    for (var indice = 0; indice < inicial.chunk_count; indice++) {
      var bloque = archivo.slice(desplazamiento, desplazamiento + inicial.chunk_size);
      var subido = false;
      var ultimoError = null;
      for (var intento = 0; intento < 3 && !subido; intento++) {
        var respuesta = await fetch("/api/files/upload/" + inicial.upload_id + "/" + indice, {
          method: "PUT",
          headers: authHeaders(),
          body: bloque
        });
        if (respuesta.ok) {
          subido = true;
        } else if (respuesta.status === 401) {
          handleUnauthorized();
          throw new Error("Sesión expirada durante la subida");
        } else {
          ultimoError = "HTTP " + respuesta.status;
          try {
            ultimoError = detailOf(await respuesta.json(), ultimoError);
          } catch (errorCapturado) {}
        }
      }
      if (!subido) throw new Error(ultimoError || "Fallo al subir un bloque");
      desplazamiento += bloque.size;
      updateMini(desplazamiento / Math.max(archivo.size, 1), desplazamiento / ((performance.now() - iniciado) / 1000));
    }
    await api("/api/files/upload/" + inicial.upload_id + "/complete", { method: "POST" });
    updateMini(1, 0);
  }

  async function uploadFiles(archivos) {
    if (!archivos || !archivos.length) return;
    var objetivo = estadoGlobal.currentPath;
    for (var i = 0; i < archivos.length; i++) {
      var archivo = archivos[i];
      try {
        if (archivo.size <= LIMITE_DIRECTO) await uploadDirect(archivo, objetivo);
        else await uploadChunked(archivo, objetivo);
        toast("Subido: " + archivo.name, "success");
      } catch (errorCapturado) {
        toast("Error al subir " + archivo.name + ": " + errorCapturado.message, "error");
      }
    }
    hideMini();
    loadDir(objetivo, false);
  }

  // Consola de logs en vivo y cierre de sesión.
  function toggleLogs() {
    if (estadoGlobal.role !== "host") {
      toast("La consola de logs solo está disponible para el host", "error");
      return;
    }
    var superposicion = $("logs-overlay");
    var oculto = superposicion.classList.contains("hidden");
    if (oculto && !estadoGlobal.logsWs) {
      var protocolo = location.protocol === "https:" ? "wss:" : "ws:";
      var url = protocolo + "//" + location.host + "/api/ws/logs";
      if (estadoGlobal.token) url += "?token=" + encodeURIComponent(estadoGlobal.token);
      try {
        estadoGlobal.logsWs = new WebSocket(url);
        estadoGlobal.logsWs.onmessage = function (evento) {
          var texto = evento.data;
          var severidad = "INFO";
          try {
            var registro = JSON.parse(texto);
            severidad = String(registro.severity || "INFO");
            texto = [
              registro.timestamp,
              severidad.padEnd(8),
              registro.action,
              registro.client_ip,
              registro.target_path,
              registro.status_code
            ].join("  ");
          } catch (errorCapturado) {}
          var cuerpo = $("logs-body");
          cuerpo.append(el("div", { class: "log-line sev-" + severidad, text: texto }));
          if (cuerpo.textContent.length > 120000) {
            while (cuerpo.textContent.length > 90000 && cuerpo.firstChild) {
              cuerpo.removeChild(cuerpo.firstChild);
            }
          }
          cuerpo.scrollTop = cuerpo.scrollHeight;
        };
        estadoGlobal.logsWs.onclose = function () {
          estadoGlobal.logsWs = null;
        };
      } catch (errorCapturado) {
        toast("No se pudo abrir la consola", "error");
      }
    }
    superposicion.classList.toggle("hidden");
  }

  function logout() {
    api("/api/auth/logout", { method: "POST" }).catch(function () {});
    estadoGlobal.token = null;
    estadoGlobal.role = null;
    localStorage.removeItem("vortex_token");
    if (estadoGlobal.logsWs) {
      estadoGlobal.logsWs.close();
      estadoGlobal.logsWs = null;
    }
    $("logs-overlay").classList.add("hidden");
    $("logs-body").textContent = "";
    estadoGlobal.pending = location.pathname + location.search;
    showLogin("");
  }

  // Registro de eventos de la interfaz y arranque final.
  function bindEvents() {
    $("login-form").addEventListener("submit", async function (evento) {
      evento.preventDefault();
      var secreto = $("login-secret").value;
      if (!secreto) return;
      $("login-busy").classList.remove("hidden");
      try {
        var respuesta = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ secret: secreto })
        });
        if (!respuesta.ok) {
          var carga = null;
          try {
            carga = await respuesta.json();
          } catch (errorCapturado) {}
          $("login-error").textContent = detailOf(carga, "Credenciales inválidas");
          return;
        }
        var datos = await respuesta.json();
        estadoGlobal.token = datos.access_token;
        estadoGlobal.role = datos.role;
        localStorage.setItem("vortex_token", estadoGlobal.token);
        $("login-busy").classList.add("hidden");
        await startApp();
      } catch (errorCapturado) {
        $("login-error").textContent = "No se pudo conectar con el servidor";
      } finally {
        $("login-busy").classList.add("hidden");
      }
    });

    $("login-guest").addEventListener("click", function () {
      hideLogin();
      startApp();
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
      if (estadoGlobal.parent !== null) loadDir(estadoGlobal.parent);
    });
    $("btn-refresh").addEventListener("click", function () {
      loadDir(estadoGlobal.currentPath, false);
    });
    $("btn-mkdir").addEventListener("click", createFolder);
    $("btn-new-text").addEventListener("click", createTextFile);
    $("btn-upload").addEventListener("click", function () {
      $("file-input").click();
    });
    $("btn-zip").addEventListener("click", function () {
      triggerDownloadZip(estadoGlobal.currentPath);
    });
    $("file-input").addEventListener("change", function (evento) {
      uploadFiles(Array.from(evento.target.files || []));
      evento.target.value = "";
    });

    $("search").addEventListener("input", function (evento) {
      var consulta = evento.target.value.trim();
      clearTimeout(estadoGlobal.searchTimer);
      estadoGlobal.searchTimer = setTimeout(function () {
        doSearch(consulta);
      }, 220);
    });

    $("modal-backdrop").addEventListener("click", function (evento) {
      if (evento.target === $("modal-backdrop")) closeModal();
    });

    $("palette-input").addEventListener("input", function (evento) {
      var consulta = evento.target.value.trim();
      clearTimeout(estadoGlobal.paletteTimer);
      estadoGlobal.paletteTimer = setTimeout(function () {
        paletteSearch(consulta);
      }, 200);
    });
    $("palette").addEventListener("click", function (evento) {
      if (evento.target === $("palette")) closePalette();
    });

    ["dragenter", "dragover"].forEach(function (nombre) {
      document.addEventListener(nombre, function (evento) {
        evento.preventDefault();
        if (!estadoGlobal.token && estadoGlobal.requiresLogin) return;
        $("dropzone").classList.remove("hidden");
      });
    });
    ["dragleave", "dragend"].forEach(function (nombre) {
      document.addEventListener(nombre, function (evento) {
        if (evento.target === document || evento.target === document.documentElement) {
          $("dropzone").classList.add("hidden");
        }
      });
    });
    document.addEventListener("drop", function (evento) {
      evento.preventDefault();
      $("dropzone").classList.add("hidden");
      if (!estadoGlobal.token && estadoGlobal.requiresLogin) return;
      uploadFiles(Array.from(evento.dataTransfer.files || []));
    });

    document.addEventListener("keydown", function (evento) {
      var clave = (evento.key || "").toLowerCase();
      if (evento.ctrlKey && clave === "k") {
        evento.preventDefault();
        if ($("palette").classList.contains("hidden")) openPalette();
        else closePalette();
      } else if (clave === "escape") {
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
