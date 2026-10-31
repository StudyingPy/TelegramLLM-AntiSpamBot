/* 指纹复核台 — Telegram Mini App client.
 *
 * Security notes:
 * - Identity comes only from the signed Telegram.WebApp.initData string, verified by the
 *   server. The unsigned user object is never read and no reviewer ID is embedded here.
 * - All untrusted text (patterns, evidence, model reasons) is inserted with textContent
 *   or text nodes; HTML strings are never parsed.
 * - Every write carries a request_id (reused on network retry) and the version the
 *   reviewer was looking at, so retries are idempotent and stale pages cannot overwrite
 *   newer state.
 */
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var TOKEN_KEY = "fp-review-token";
  var PAGE_SIZE = 20;
  var VIEWS = [
    { id: "pending", label: "待复核", review: true },
    { id: "kept", label: "已保留", review: true },
    { id: "disabled", label: "已停用", review: true },
    { id: "library", label: "指纹库", review: false },
  ];
  var LIB_DEFAULTS = {
    type: "", enabled: "", source: "", weight_min: "", weight_max: "",
    hits_min: "", hits_max: "", fp_min: "", fp_max: "", sort: "weight",
  };
  var SORT_LABELS = {
    weight: "按权重", hits: "按命中次数", false_positives: "按误判次数",
    newest: "按创建时间", last_hit: "按最近命中",
  };

  var state = {
    token: null,
    labels: {},
    filterOptions: { types: [], sources: [] },
    batchMax: 50,
    summary: null,
    view: "pending",
    query: "",
    reviewType: "",
    lib: Object.assign({}, LIB_DEFAULTS),
    items: [],
    total: 0,
    loading: false,
    listToken: 0,
    selected: new Map(),
    current: null,
    detail: null,
    busy: false,
    scrollY: 0,
  };

  var el = {};

  // ------------------------------------------------------------------ helpers

  function h(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, String(value));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "string" || typeof child === "number"
      ? document.createTextNode(String(child)) : child);
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function label(group, code) {
    var table = state.labels[group] || {};
    if (code === null || code === undefined || code === "") return "未知";
    return table[code] || "未知（" + code + "）";
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function formatTime(seconds) {
    if (!seconds) return "—";
    var d = new Date(seconds * 1000);
    var now = new Date();
    var md = pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    return d.getFullYear() === now.getFullYear() ? md : d.getFullYear() + "-" + md;
  }

  function formatAge(seconds) {
    if (!seconds) return "";
    var now = state.summary ? state.summary.server_time : Math.floor(Date.now() / 1000);
    var diff = Math.max(0, now - seconds);
    if (diff < 3600) return Math.max(1, Math.round(diff / 60)) + " 分钟";
    if (diff < 86400) return Math.round(diff / 3600) + " 小时";
    return Math.round(diff / 86400) + " 天";
  }

  function percent(value) { return Math.round((value || 0) * 100) + "%"; }

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return pad16(b); }).join("");
  }

  function pad16(b) { return (b < 16 ? "0" : "") + b.toString(16); }

  function storage(action, value) {
    try {
      if (action === "get") return window.sessionStorage.getItem(TOKEN_KEY);
      if (action === "set") window.sessionStorage.setItem(TOKEN_KEY, value);
      if (action === "clear") window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* storage unavailable: keep the token in memory only */ }
    return null;
  }

  function haptic(kind) {
    try { if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(kind); } catch (e) { /* ignore */ }
  }

  function toast(message, kind) {
    var node = h("div", { class: "toast" + (kind ? " toast--" + kind : "") , text: message });
    el.toasts.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "error" ? 6000 : 3500);
  }

  // ---------------------------------------------------------------------- API

  function ApiError(status, code, message) {
    this.status = status;
    this.code = code;
    this.message = message;
  }

  function request(path, options) {
    options = options || {};
    var headers = {};
    if (state.token) headers.Authorization = "Bearer " + state.token;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "omit",
      cache: "no-store",
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (res.ok) return data;
        var err = data && data.error;
        throw new ApiError(res.status, err ? err.code : "http",
          err ? err.message : "请求失败（HTTP " + res.status + "），请稍后重试。");
      });
    }, function () {
      throw new ApiError(0, "network", "网络连接失败，请检查网络后重试。");
    });
  }

  // Reads and writes share one wrapper: an expired session is renewed once from
  // initData; writes are retried on network failure with the same request_id.
  function api(path, options, attempt) {
    attempt = attempt || 0;
    return request(path, options).catch(function (error) {
      if (error.status === 401 && path !== "api/session" && attempt === 0) {
        return login().then(function () { return api(path, options, 1); });
      }
      if (error.status === 0 && options && options.body !== undefined && attempt < 2) {
        return new Promise(function (resolve) { setTimeout(resolve, 800 * (attempt + 1)); })
          .then(function () { return api(path, options, attempt + 1); });
      }
      throw error;
    });
  }

  function login() {
    state.token = null;
    storage("clear");
    return request("api/session", { method: "POST", body: { init_data: tg.initData } })
      .then(function (data) {
        state.token = data.token;
        storage("set", data.token);
      });
  }

  function handleError(error) {
    if (!(error instanceof ApiError)) {
      console.error(error);
      toast("页面出现错误，请刷新后重试。", "error");
      return;
    }
    if (error.status === 401 || error.status === 403) {
      fatal(error.message);
      return;
    }
    haptic("error");
    toast(error.message, "error");
  }

  // ---------------------------------------------------------------- bootstrap

  function fatal(message) {
    clear(el.app);
    el.app.removeAttribute("aria-busy");
    el.app.className = "app";
    el.app.appendChild(h("div", { class: "boot" },
      h("p", { class: "boot-title", text: "无法打开复核台" }),
      h("p", { class: "boot-text", text: message })));
  }

  function applyTheme() {
    var dark = tg ? tg.colorScheme === "dark"
      : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-scheme", dark ? "dark" : "light");
  }

  function boot() {
    el.app = document.getElementById("app");
    el.toasts = document.getElementById("toasts");
    el.sheet = document.getElementById("sheet");
    applyTheme();
    if (!tg || !tg.initData) {
      fatal("请在 Telegram 中通过机器人的“打开复核台”按钮或聊天菜单进入。");
      return;
    }
    try { tg.ready(); tg.expand(); } catch (e) { /* older clients */ }
    try { tg.onEvent("themeChanged", applyTheme); } catch (e) { /* ignore */ }
    if (tg.BackButton) tg.BackButton.onClick(closeDetail);

    state.token = storage("get");
    var start = state.token ? api("api/meta") : login().then(function () { return api("api/meta"); });
    start.then(function (meta) {
      state.labels = meta.labels || {};
      state.filterOptions = meta.filters || state.filterOptions;
      state.batchMax = meta.batch_max || 50;
      buildShell();
      return Promise.all([loadSummary(), loadList(false)]);
    }).catch(function (error) {
      if (error instanceof ApiError) fatal(error.message);
      else { console.error(error); fatal("页面加载失败，请关闭后重新打开。"); }
    });
  }

  // -------------------------------------------------------------------- shell

  function buildShell() {
    clear(el.app);
    el.app.removeAttribute("aria-busy");

    el.summary = h("p", { class: "summary", text: "正在统计……" });
    el.tabs = h("nav", { class: "tabs", role: "tablist", "aria-label": "视图" });
    VIEWS.forEach(function (view) {
      var count = h("span", { class: "count" });
      var tab = h("button", {
        class: "tab", role: "tab", type: "button", "data-view": view.id,
        "aria-selected": String(view.id === state.view),
        onclick: function () { switchView(view.id); },
      }, view.label, count);
      tab._count = count;
      el.tabs.appendChild(tab);
    });
    el.top = h("header", { class: "top" },
      h("div", { class: "top-row" },
        h("h1", { text: "指纹复核台" }),
        h("button", { class: "btn btn--link", type: "button", text: "刷新", onclick: refreshAll })),
      el.summary, el.tabs);

    el.search = h("input", {
      class: "search", type: "search", placeholder: "搜索短语、模板或指纹编号",
      "aria-label": "搜索短语、模板或指纹编号", maxlength: "100", enterkeyhint: "search",
    });
    var searchTimer = null;
    el.search.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        state.query = el.search.value.trim();
        loadList(false);
      }, 350);
    });
    el.filterToggle = h("button", {
      class: "btn btn--outline", type: "button", "aria-expanded": "false", text: "筛选",
      onclick: function () { toggleFilters(); },
    });
    el.filters = h("form", { class: "filters", hidden: true, onsubmit: applyFilters });
    el.listMeta = h("div", { class: "list-meta" });
    el.list = h("ul", { class: "list", "aria-label": "列表" });
    el.more = h("div", { class: "more" });
    el.listPane = h("section", { class: "list-pane", "aria-label": "列表" },
      h("div", { class: "toolbar" }, el.search, el.filterToggle),
      el.filters, el.listMeta, el.list, el.more);
    el.detailPane = h("section", { class: "detail-pane", "aria-label": "详情" });
    el.selection = h("div", { class: "selection-bar", hidden: true });

    el.app.appendChild(el.top);
    el.app.appendChild(h("main", { class: "layout" }, el.listPane, el.detailPane));
    el.app.appendChild(el.selection);
    renderDetailPlaceholder();
    measureTop();
    window.addEventListener("resize", measureTop);
  }

  function measureTop() {
    if (el.top) document.documentElement.style.setProperty("--top-height", el.top.offsetHeight + "px");
  }

  function currentView() {
    for (var i = 0; i < VIEWS.length; i++) if (VIEWS[i].id === state.view) return VIEWS[i];
    return VIEWS[0];
  }

  function switchView(view) {
    if (view === state.view) return;
    state.view = view;
    state.selected.clear();
    Array.prototype.forEach.call(el.tabs.children, function (tab) {
      tab.setAttribute("aria-selected", String(tab.getAttribute("data-view") === view));
    });
    el.filters.hidden = true;
    el.filterToggle.setAttribute("aria-expanded", "false");
    closeDetail();
    loadList(false);
  }

  function refreshAll() {
    loadSummary();
    loadList(false);
    if (state.current) openDetail(state.current.kind, state.current.id, { keepScroll: true });
  }

  // ------------------------------------------------------------------ summary

  function loadSummary() {
    return api("api/summary").then(function (summary) {
      state.summary = summary;
      renderSummary();
    }).catch(handleError);
  }

  function renderSummary() {
    var s = state.summary;
    if (!s) return;
    var pending = s.reviews.pending;
    clear(el.summary);
    if (pending > 0) {
      append(el.summary, ["待复核 ", h("strong", { text: String(pending) }), " 条"]);
      if (s.pending_with_manual_ham > 0) {
        append(el.summary, ["，其中 ", h("strong", { text: String(s.pending_with_manual_ham) }), " 条有人工误判证据"]);
      }
      if (s.oldest_pending_at) append(el.summary, "，最久已等待 " + formatAge(s.oldest_pending_at));
      append(el.summary, "。");
    } else {
      append(el.summary, "没有待复核的建议。");
    }
    var scan = s.scan;
    if (!scan) append(el.summary, "质量扫描尚未运行。");
    else if (scan.running) append(el.summary, "质量扫描进行中，剩余 " + scan.remaining + " / " + scan.total + " 条。");
    else append(el.summary, "上次扫描完成于 " + formatTime(scan.finished_at) + "。");

    var counts = { pending: pending, kept: s.reviews.kept, disabled: s.reviews.disabled, library: s.library_total };
    Array.prototype.forEach.call(el.tabs.children, function (tab) {
      tab._count.textContent = String(counts[tab.getAttribute("data-view")] || 0);
    });
    measureTop();
  }

  // ------------------------------------------------------------------ filters

  function toggleFilters(force) {
    var open = force !== undefined ? force : el.filters.hidden;
    if (open) renderFilters();
    el.filters.hidden = !open;
    el.filterToggle.setAttribute("aria-expanded", String(open));
  }

  function select(name, value, options) {
    var node = h("select", { name: name });
    options.forEach(function (opt) {
      var option = h("option", { value: opt[0], text: opt[1] });
      if (opt[0] === value) option.selected = true;
      node.appendChild(option);
    });
    return node;
  }

  function numberInput(name, value, placeholder, step) {
    return h("input", {
      name: name, type: "number", inputmode: "decimal", min: "0", step: step || "1",
      value: value, placeholder: placeholder, "aria-label": placeholder,
    });
  }

  function codeOptions(codes, group, anyLabel) {
    return [["", anyLabel]].concat(codes.map(function (code) { return [code, label(group, code)]; }));
  }

  function renderFilters() {
    clear(el.filters);
    var types = codeOptions(state.filterOptions.types || [], "fingerprint_types", "全部类型");
    if (currentView().review) {
      append(el.filters, [
        h("label", { class: "field field--wide" }, "指纹类型", select("type", state.reviewType, types)),
      ]);
    } else {
      var lib = state.lib;
      append(el.filters, [
        h("label", { class: "field" }, "指纹类型", select("type", lib.type, types)),
        h("label", { class: "field" }, "状态",
          select("enabled", lib.enabled, [["", "全部状态"], ["1", "启用中"], ["0", "已停用"]])),
        h("label", { class: "field field--wide" }, "来源",
          select("source", lib.source, codeOptions(state.filterOptions.sources || [], "sources", "全部来源"))),
        h("div", { class: "field field--wide" }, "权重（0–100）",
          h("div", { class: "range" }, numberInput("weight_min", lib.weight_min, "最低", "any"), "至",
            numberInput("weight_max", lib.weight_max, "最高", "any"))),
        h("div", { class: "field" }, "命中次数",
          h("div", { class: "range" }, numberInput("hits_min", lib.hits_min, "最少"), "至",
            numberInput("hits_max", lib.hits_max, "最多"))),
        h("div", { class: "field" }, "误判次数",
          h("div", { class: "range" }, numberInput("fp_min", lib.fp_min, "最少"), "至",
            numberInput("fp_max", lib.fp_max, "最多"))),
        h("label", { class: "field field--wide" }, "排序",
          select("sort", lib.sort, Object.keys(SORT_LABELS).map(function (k) { return [k, SORT_LABELS[k]]; }))),
      ]);
    }
    append(el.filters, h("div", { class: "filters-actions" },
      h("button", { class: "btn btn--quiet", type: "button", text: "重置", onclick: resetFilters }),
      h("button", { class: "btn btn--primary", type: "submit", text: "应用筛选" })));
  }

  function applyFilters(event) {
    if (event) event.preventDefault();
    var data = new FormData(el.filters);
    if (currentView().review) {
      state.reviewType = String(data.get("type") || "");
    } else {
      Object.keys(LIB_DEFAULTS).forEach(function (key) {
        var value = data.get(key);
        state.lib[key] = value === null ? LIB_DEFAULTS[key] : String(value).trim();
      });
      var lo = state.lib.weight_min, hi = state.lib.weight_max;
      if (lo !== "" && hi !== "" && Number(lo) > Number(hi)) {
        toast("最低权重不能高于最高权重。", "warn");
        return;
      }
    }
    toggleFilters(false);
    loadList(false);
  }

  function resetFilters() {
    if (currentView().review) state.reviewType = "";
    else state.lib = Object.assign({}, LIB_DEFAULTS);
    renderFilters();
  }

  function activeFilterCount() {
    if (currentView().review) return state.reviewType ? 1 : 0;
    return Object.keys(LIB_DEFAULTS).filter(function (key) {
      return key !== "sort" && state.lib[key] !== "";
    }).length;
  }

  // --------------------------------------------------------------------- list

  function listUrl(offset) {
    var params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    params.set("offset", String(offset));
    params.set("limit", String(PAGE_SIZE));
    if (currentView().review) {
      params.set("status", state.view);
      if (state.reviewType) params.set("type", state.reviewType);
      return "api/reviews?" + params.toString();
    }
    Object.keys(state.lib).forEach(function (key) {
      if (state.lib[key] !== "") params.set(key, state.lib[key]);
    });
    return "api/fingerprints?" + params.toString();
  }

  function loadList(appendPage) {
    var token = ++state.listToken;
    var offset = appendPage ? state.items.length : 0;
    state.loading = true;
    renderMore();
    var filters = activeFilterCount();
    el.filterToggle.textContent = filters ? "筛选（" + filters + "）" : "筛选";
    return api(listUrl(offset)).then(function (data) {
      if (token !== state.listToken) return;
      state.items = appendPage ? state.items.concat(data.items) : data.items;
      state.total = data.total;
      if (!appendPage) {
        clear(el.list);
        window.scrollTo(0, 0);
        el.listPane.scrollTop = 0;
      }
      data.items.forEach(function (item) { el.list.appendChild(renderRow(item)); });
      renderListMeta();
    }).catch(handleError).then(function () {
      if (token !== state.listToken) return;
      state.loading = false;
      renderMore();
      renderSelection();
    });
  }

  function renderListMeta() {
    if (!state.items.length) {
      clear(el.list);
      var empty = {
        pending: state.query || state.reviewType ? "没有符合条件的待复核建议。" : "没有待复核的建议。模型提出新的停用建议后会出现在这里。",
        kept: "暂无已保留的复核记录。",
        disabled: "暂无已停用的指纹。",
        library: "没有符合条件的指纹。可以调整搜索或筛选条件。",
      }[state.view];
      el.list.appendChild(h("li", { class: "empty", text: empty }));
      el.listMeta.textContent = "";
      return;
    }
    el.listMeta.textContent = "共 " + state.total + " 条，已显示 " + state.items.length + " 条";
  }

  function renderMore() {
    clear(el.more);
    if (state.loading) {
      el.more.appendChild(h("span", { class: "muted", text: "加载中……" }));
    } else if (state.items.length < state.total) {
      el.more.appendChild(h("button", {
        class: "btn btn--outline", type: "button", text: "加载更多",
        onclick: function () { loadList(true); },
      }));
    }
  }

  function patternNode(pattern, className) {
    if (pattern) return h("span", { class: className, text: pattern });
    return h("span", { class: className + " pattern--missing", text: "未能恢复原文" });
  }

  function itemKey(kind, id) { return kind + ":" + id; }

  function renderRow(item) {
    var isReview = currentView().review;
    var fp = isReview ? item.fingerprint : item;
    var kind = isReview ? "review" : "fingerprint";
    var selectable = state.view === "pending" || state.view === "disabled";
    var stats = h("div", { class: "stats" },
      h("span", { text: label("fingerprint_types", fp.type) }),
      h("span", null, "权重 ", h("b", { text: String(Math.round(fp.weight)) })),
      h("span", null, "命中 ", h("b", { text: String(fp.hit_count) })),
      h("span", null, "误判 ", h("b", { text: String(fp.false_positive_count) })));
    var main = h("button", {
      class: "row-main", type: "button",
      onclick: function () { openDetail(kind, item.id); },
    }, patternNode(fp.pattern_text, "pattern"), stats);

    var tags = h("div", { class: "tags" });
    if (!fp.enabled) tags.appendChild(h("span", { class: "tag tag--off", text: "已停用" }));
    if (fp.has_manual_ham) tags.appendChild(h("span", { class: "tag tag--ham", text: "有人工放行证据" }));
    if (isReview) {
      main.appendChild(h("div", { class: "reason" },
        label("decisions", item.decision) + " " + percent(item.confidence) + "：" + (item.reason || "")));
      if (item.status !== "pending" && item.resolved_at) {
        main.appendChild(h("div", { class: "muted",
          text: label("review_statuses", item.status) + "于 " + formatTime(item.resolved_at) }));
      } else if (item.status === "pending") {
        main.appendChild(h("div", { class: "muted", text: "已等待 " + formatAge(item.created_at) }));
      }
    } else {
      tags.appendChild(h("span", { class: "tag", text: label("sources", fp.source) }));
      if (item.latest_review) {
        tags.appendChild(h("span", {
          class: "tag" + (item.latest_review.status === "pending" ? " tag--warn" : ""),
          text: "复核：" + label("review_statuses", item.latest_review.status),
        }));
      }
    }
    if (tags.childNodes.length) main.insertBefore(tags, stats.nextSibling);

    var row = h("li", { class: "row", "data-key": itemKey(kind, item.id) });
    if (selectable) {
      var box = h("input", {
        type: "checkbox", "aria-label": "选择 " + (fp.pattern_text || "#" + fp.id),
      });
      box.checked = state.selected.has(item.id);
      box.addEventListener("change", function () {
        if (box.checked) {
          if (state.selected.size >= state.batchMax) {
            box.checked = false;
            toast("一次最多可选择 " + state.batchMax + " 条。", "warn");
            return;
          }
          state.selected.set(item.id, { id: item.id, version: item.version, pattern: fp.pattern_text, fid: fp.id });
        } else {
          state.selected.delete(item.id);
        }
        renderSelection();
      });
      row.appendChild(h("label", { class: "row-check" }, box));
    }
    row.appendChild(main);
    if (state.current && state.current.kind === kind && state.current.id === item.id) {
      row.setAttribute("aria-current", "true");
    }
    return row;
  }

  function listKind() { return currentView().review ? "review" : "fingerprint"; }

  function replaceRow(kind, item) {
    if (kind !== listKind()) return;
    var index = indexOf(kind, item.id);
    if (index >= 0) state.items[index] = item;
    var old = el.list.querySelector('[data-key="' + itemKey(kind, item.id) + '"]');
    if (old) old.replaceWith(renderRow(item));
  }

  function removeRow(kind, id) {
    if (kind !== listKind()) return;
    var index = indexOf(kind, id);
    if (index >= 0) {
      state.items.splice(index, 1);
      state.total = Math.max(0, state.total - 1);
    }
    state.selected.delete(id);
    var old = el.list.querySelector('[data-key="' + itemKey(kind, id) + '"]');
    if (old) old.remove();
    renderListMeta();
    renderSelection();
  }

  function indexOf(kind, id) {
    if (kind !== listKind()) return -1;
    for (var i = 0; i < state.items.length; i++) if (state.items[i].id === id) return i;
    return -1;
  }

  function markCurrentRow() {
    Array.prototype.forEach.call(el.list.querySelectorAll('[aria-current="true"]'), function (node) {
      node.removeAttribute("aria-current");
    });
    if (!state.current) return;
    var row = el.list.querySelector('[data-key="' + itemKey(state.current.kind, state.current.id) + '"]');
    if (row) row.setAttribute("aria-current", "true");
  }

  // ---------------------------------------------------------- batch selection

  function renderSelection() {
    clear(el.selection);
    var count = state.selected.size;
    if (!count || !(state.view === "pending" || state.view === "disabled")) {
      el.selection.hidden = true;
      return;
    }
    el.selection.hidden = false;
    append(el.selection, h("span", { class: "selection-count", text: "已选 " + count + " 条" }));
    if (state.view === "pending") {
      append(el.selection, [
        h("button", { class: "btn btn--primary", type: "button", text: "停用所选",
          onclick: function () { confirmBatch("disable"); } }),
        h("button", { class: "btn btn--outline", type: "button", text: "保留所选",
          onclick: function () { confirmBatch("keep"); } }),
      ]);
    } else {
      append(el.selection, h("button", { class: "btn btn--primary", type: "button", text: "恢复所选",
        onclick: function () { confirmBatch("restore"); } }));
    }
    append(el.selection, h("button", { class: "btn btn--link", type: "button", text: "取消选择",
      onclick: function () {
        state.selected.clear();
        Array.prototype.forEach.call(el.list.querySelectorAll(".row-check input"), function (box) { box.checked = false; });
        renderSelection();
      } }));
  }

  var ACTION_COPY = {
    disable: { verb: "停用", note: "停用后这些指纹立即停止匹配，记录会保留，之后可在“已停用”中恢复。" },
    keep: { verb: "保留", note: "保留后这些指纹继续生效；证据未变化时不会再次提醒。" },
    restore: { verb: "恢复", note: "恢复后这些指纹重新开始匹配。" },
  };

  function confirmBatch(action) {
    var picked = Array.from(state.selected.values());
    var copy = ACTION_COPY[action];
    var list = h("ol", { class: "sheet-list" });
    picked.forEach(function (item) {
      list.appendChild(h("li", { text: (item.pattern || "未能恢复原文") + "（#" + item.fid + "）" }));
    });
    openSheet({
      title: copy.verb + "所选的 " + picked.length + " 条指纹？",
      body: [
        h("p", { text: copy.note }),
        h("p", { class: "muted", text: "只处理下面勾选的项目。状态或证据在你打开页面后发生变化的项目会被跳过，并在结果中说明。" }),
        list,
      ],
      actions: [
        { label: "取消", kind: "quiet" },
        { label: "确认" + copy.verb + " " + picked.length + " 条", kind: action === "disable" ? "danger" : "primary",
          run: function () { return submitBatch(action, picked); } },
      ],
    });
  }

  function submitBatch(action, picked) {
    var body = {
      request_id: newRequestId(),
      action: action,
      items: picked.map(function (item) { return { review_id: item.id, version: item.version }; }),
    };
    return api("api/reviews/actions", { method: "POST", body: body }).then(function (data) {
      var byId = {};
      picked.forEach(function (item) { byId[item.id] = item; });
      var list = h("ul", { class: "sheet-list" });
      data.results.forEach(function (result) {
        var item = byId[result.review_id] || {};
        var tone = result.status === "ok" || result.status === "already_done" ? "ok"
          : result.status === "stale" ? "warn" : "bad";
        list.appendChild(h("li", { class: tone,
          text: (item.pattern || "未能恢复原文") + "（#" + (item.fid || result.review_id) + "）：" + result.message }));
      });
      var s = data.summary;
      haptic(s.stale || s.not_found || s.invalid ? "warning" : "success");
      state.selected.clear();
      loadSummary();
      loadList(false);
      openSheet({
        title: verbOf(action) + "结果",
        body: [
          h("p", { text: "成功 " + s.ok + " 条，此前已处理 " + s.already_done + " 条，状态或证据已变化 " + s.stale +
            " 条，不存在 " + s.not_found + " 条，状态不符 " + s.invalid + " 条。" }),
          s.stale ? h("p", { class: "muted", text: "已变化的项目已重新加载，请查看最新内容后再决定。" }) : null,
          list,
        ],
        actions: [{ label: "完成", kind: "primary" }],
      });
    });
  }

  function verbOf(action) { return ACTION_COPY[action] ? ACTION_COPY[action].verb : "操作"; }

  // ------------------------------------------------------------------- detail

  function renderDetailPlaceholder() {
    clear(el.detailPane);
    el.detailPane.appendChild(h("div", { class: "placeholder", text: "选择左侧条目查看详情。" }));
  }

  function openDetail(kind, id, options) {
    options = options || {};
    var wasOpen = el.app.classList.contains("app--detail");
    if (!wasOpen && !options.keepScroll) state.scrollY = window.scrollY;
    state.current = { kind: kind, id: id };
    markCurrentRow();
    el.app.classList.add("app--detail");
    if (tg && tg.BackButton && window.innerWidth < 900) tg.BackButton.show();
    clear(el.detailPane);
    el.detailPane.appendChild(h("div", { class: "placeholder", text: "加载中……" }));
    var path = kind === "review" ? "api/reviews/" + id : "api/fingerprints/" + id;
    return api(path).then(function (data) {
      if (!state.current || state.current.id !== id || state.current.kind !== kind) return;
      renderDetail(kind, kind === "review" ? data.review : data.fingerprint);
    }).catch(function (error) {
      if (error instanceof ApiError && error.status === 404) {
        toast(error.message, "warn");
        removeRow(kind, id);
        closeDetail();
        return;
      }
      handleError(error);
    });
  }

  function closeDetail() {
    var wasOpen = el.app && el.app.classList.contains("app--detail");
    state.current = null;
    state.detail = null;
    if (!el.app) return;
    el.app.classList.remove("app--detail");
    if (tg && tg.BackButton) tg.BackButton.hide();
    if (el.detailPane) renderDetailPlaceholder();
    if (el.list) markCurrentRow();
    if (wasOpen && window.innerWidth < 900) window.scrollTo(0, state.scrollY);
  }

  function highlight(text, pattern) {
    var frag = document.createDocumentFragment();
    var needle = (pattern || "").trim();
    if (!needle || needle.length > 400) { frag.appendChild(document.createTextNode(text)); return frag; }
    var hay = text.toLowerCase();
    var lower = needle.toLowerCase();
    var pos = 0, marks = 0, at;
    while ((at = hay.indexOf(lower, pos)) !== -1 && marks < 20) {
      if (at > pos) frag.appendChild(document.createTextNode(text.slice(pos, at)));
      frag.appendChild(h("mark", { text: text.slice(at, at + needle.length) }));
      pos = at + needle.length;
      marks++;
    }
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    return frag;
  }

  function fact(name, value, small) {
    return h("div", { class: "fact" }, h("dt", { text: name }), h("dd", { class: small ? "small" : null, text: value }));
  }

  function evidenceTag(labelCode) {
    var cls = labelCode === "manual_ham" ? "tag tag--ham" : labelCode === "manual_spam" ? "tag tag--spam" : "tag";
    return h("span", { class: cls, text: label("evidence_labels", labelCode) });
  }

  function samplesBlock(fp) {
    var block = h("section", { class: "block" }, h("h2", { text: "证据样本（" + fp.samples.length + "）" }));
    if (!fp.samples.length) {
      block.appendChild(h("p", { class: "muted", text: "暂无已保存的样本。" }));
      return block;
    }
    var mark = fp.type === "phrase" ? fp.pattern_text : null;
    fp.samples.forEach(function (sample) {
      var body = h("p", { class: "sample-text" });
      body.appendChild(highlight(sample.text, mark));
      var head = h("div", { class: "sample-head" }, evidenceTag(sample.label),
        h("span", { text: formatTime(sample.created_at) }),
        sample.truncated ? h("span", { text: "（原文较长，已截断）" }) : null);
      var node = h("div", { class: "sample" }, head, body);
      if (sample.text.length > 280) {
        var toggle = h("button", { class: "btn btn--link", type: "button", text: "展开全文" });
        toggle.addEventListener("click", function () {
          var open = body.classList.toggle("open");
          toggle.textContent = open ? "收起" : "展开全文";
        });
        node.appendChild(toggle);
      }
      block.appendChild(node);
    });
    return block;
  }

  function headerBlock(kind, data, fp) {
    var back = h("button", { class: "btn btn--link detail-back", type: "button", text: "返回列表", onclick: closeDetail });
    var id = kind === "review" ? "复核 #" + data.id + "，指纹 #" + fp.id : "指纹 #" + fp.id;
    var pattern = h("h1", { class: "detail-pattern" });
    if (fp.pattern_text) pattern.appendChild(h("span", { class: "ink", text: fp.pattern_text }));
    else pattern.appendChild(h("span", { class: "pattern--missing", text: "未能恢复原文" }));
    var tags = h("div", { class: "tags" },
      h("span", { class: "tag", text: label("fingerprint_types", fp.type) }),
      h("span", { class: fp.enabled ? "tag tag--ham" : "tag tag--off", text: fp.enabled ? "启用中" : "已停用" }),
      h("span", { class: "tag", text: label("sources", fp.source) }),
      fp.pattern_truncated ? h("span", { class: "tag", text: "原文过长，已截断" }) : null);
    return [back, h("div", { class: "detail-id", text: id }), pattern, tags];
  }

  function factsBlock(fp) {
    return h("dl", { class: "facts" },
      fact("权重", String(Math.round(fp.weight))),
      fact("命中次数", String(fp.hit_count)),
      fact("人工误判", String(fp.false_positive_count)),
      fact("创建", formatTime(fp.created_at), true),
      fact("最近命中", formatTime(fp.last_hit_at), true),
      fact("最近扫描", formatTime(fp.last_audited_at), true));
  }

  function humanBlock(fp) {
    var manual = fp.samples.filter(function (s) { return s.label === "manual_ham" || s.label === "manual_spam"; });
    var block = h("section", { class: "block" }, h("h2", { text: "人工反馈" }));
    if (!fp.false_positive_count && !manual.length) {
      block.appendChild(h("p", { class: "muted", text: "暂无人工投票或管理员反馈。" }));
      return block;
    }
    block.appendChild(h("p", { text: "人工放行（误判）" + fp.false_positive_count + " 次。" }));
    if (manual.length) block.appendChild(h("p", { class: "muted", text: "下方样本中有 " + manual.length + " 条来自人工处理。" }));
    return block;
  }

  function technicalBlock(fp) {
    return h("section", { class: "block" }, h("h2", { text: "指纹值" }), h("p", { class: "value", text: fp.value }));
  }

  function renderDetail(kind, data) {
    state.detail = { kind: kind, data: data };
    var fp = kind === "review" ? data.fingerprint : data;
    var root = h("article", { class: "detail" });
    append(root, headerBlock(kind, data, fp));
    append(root, factsBlock(fp));
    if (kind === "review") {
      append(root, humanBlock(fp));
      append(root, h("section", { class: "block" },
        h("h2", { text: "模型建议" }),
        h("p", { text: label("decisions", data.decision) + "，置信度 " + percent(data.confidence) + "。" }),
        h("p", { class: "muted", text: "理由：" + (data.reason || "无") }),
        h("p", { class: "muted", text: "模型只提出建议，是否停用由你决定。" })));
      append(root, samplesBlock(fp));
      if (data.history && data.history.length) {
        var history = h("ul", { class: "history" });
        data.history.forEach(function (entry) {
          history.appendChild(h("li", { text: formatTime(entry.created_at) + "  " + label("actions", entry.action) + "（操作人 " + entry.user_id + "）" }));
        });
        append(root, h("section", { class: "block" }, h("h2", { text: "操作记录" }), history));
      }
      append(root, technicalBlock(fp));
      append(root, reviewActions(data));
    } else {
      append(root, humanBlock(fp));
      append(root, samplesBlock(fp));
      append(root, reviewsBlock(fp));
      append(root, technicalBlock(fp));
      append(root, h("div", { class: "actions" },
        h("span", { class: "state-note", text: "删除会移除指纹及其证据和复核记录。" }),
        h("button", { class: "btn btn--danger", type: "button", text: "删除指纹", onclick: function () { confirmDelete(fp); } })));
    }
    clear(el.detailPane);
    el.detailPane.appendChild(root);
    el.detailPane.scrollTop = 0;
    if (window.innerWidth < 900) window.scrollTo(0, 0);
  }

  function reviewsBlock(fp) {
    var block = h("section", { class: "block" }, h("h2", { text: "复核记录" }));
    if (!fp.reviews.length) {
      block.appendChild(h("p", { class: "muted", text: "模型尚未对这个指纹提出停用建议。" }));
      return block;
    }
    var list = h("ul", { class: "history" });
    fp.reviews.forEach(function (review) {
      list.appendChild(h("li", null,
        h("button", { class: "btn btn--link", type: "button",
          text: "复核 #" + review.id + "：" + label("review_statuses", review.status) + "，" +
            label("decisions", review.decision) + " " + percent(review.confidence),
          onclick: function () { openDetail("review", review.id); } }),
        h("span", { class: "muted", text: "  " + formatTime(review.resolved_at || review.created_at) })));
    });
    block.appendChild(list);
    return block;
  }

  function reviewActions(review) {
    var bar = h("div", { class: "actions" });
    if (review.status === "pending") {
      append(bar, [
        h("button", { class: "btn btn--primary", type: "button", text: "停用并下一条",
          onclick: function () { reviewAction("disable", review); } }),
        h("button", { class: "btn btn--outline", type: "button", text: "保留并下一条",
          onclick: function () { reviewAction("keep", review); } }),
        h("button", { class: "btn btn--quiet", type: "button", text: "稍后处理",
          onclick: function () { deferCurrent(review); } }),
      ]);
    } else if (review.status === "disabled") {
      append(bar, [
        h("span", { class: "state-note", text: "已于 " + formatTime(review.resolved_at) + " 停用。" }),
        h("button", { class: "btn btn--primary", type: "button", text: "恢复",
          onclick: function () { reviewAction("restore", review); } }),
      ]);
    } else {
      bar.appendChild(h("span", { class: "state-note",
        text: label("review_statuses", review.status) + (review.resolved_at ? "于 " + formatTime(review.resolved_at) : "") + "，无需操作。" }));
    }
    return bar;
  }

  function setBusy(busy) {
    state.busy = busy;
    Array.prototype.forEach.call(el.detailPane.querySelectorAll(".actions button"), function (button) {
      button.disabled = busy;
    });
  }

  function nextReviewId(id) {
    var index = indexOf("review", id);
    if (index < 0) return null;
    var next = state.items[index + 1] || null;
    return next ? next.id : null;
  }

  // "稍后处理" only reorders this page's queue; it never changes the fingerprint.
  function deferCurrent(review) {
    if (listKind() !== "review") { closeDetail(); return; }
    var index = indexOf("review", review.id);
    var next = nextReviewId(review.id);
    if (index >= 0 && state.items.length > 1) {
      var moved = state.items.splice(index, 1)[0];
      state.items.push(moved);
      var row = el.list.querySelector('[data-key="' + itemKey("review", review.id) + '"]');
      if (row) el.list.appendChild(row);
    }
    if (next !== null) openDetail("review", next, { keepScroll: true });
    else { toast("这是当前列表的最后一条。", "warn"); closeDetail(); }
  }

  function reviewAction(action, review) {
    if (state.busy) return;
    setBusy(true);
    var advance = action !== "restore";
    var next = advance ? nextReviewId(review.id) : null;
    var body = {
      request_id: newRequestId(), action: action,
      items: [{ review_id: review.id, version: review.version }],
    };
    api("api/reviews/actions", { method: "POST", body: body }).then(function (data) {
      var result = data.results[0];
      if (result.status === "ok" || result.status === "already_done") {
        haptic("success");
        toast(result.message);
        removeRow("review", review.id);
        loadSummary();
        if (next !== null && indexOf("review", next) >= 0) openDetail("review", next, { keepScroll: true });
        else if (result.review) {
          if (advance && state.view === "pending") { toast("当前列表已处理完。"); closeDetail(); }
          else renderDetail("review", result.review);
        } else closeDetail();
      } else if (result.status === "stale") {
        haptic("warning");
        toast(result.message, "warn");
        if (result.review) {
          renderDetail("review", result.review);
          if (result.review.status === state.view) replaceRow("review", summaryFromDetail(result.review));
          else removeRow("review", review.id);
        }
        loadSummary();
      } else {
        haptic("error");
        toast(result.message, "error");
        if (result.status === "not_found") { removeRow("review", review.id); closeDetail(); }
        else if (result.review) renderDetail("review", result.review);
      }
    }).catch(handleError).then(function () { setBusy(false); });
  }

  function summaryFromDetail(review) {
    var copy = Object.assign({}, review);
    var fp = Object.assign({}, review.fingerprint);
    if (fp.pattern_text && fp.pattern_text.length > 160) {
      fp.pattern_text = fp.pattern_text.slice(0, 160);
      fp.pattern_truncated = true;
    }
    copy.fingerprint = fp;
    return copy;
  }

  function confirmDelete(fp) {
    var impact = fp.delete_impact || { evidence_count: 0, review_count: 0 };
    openSheet({
      title: "删除指纹 #" + fp.id + "？",
      body: [
        h("div", { class: "sheet-quote", text: fp.pattern_text || "未能恢复原文（" + fp.value.slice(0, 12) + "…）" }),
        h("p", { text: "删除后这个指纹立即停止匹配，同时删除 " + impact.evidence_count + " 条证据样本和 " +
          impact.review_count + " 条复核记录。此操作无法撤销。" }),
        h("p", { class: "muted", text: "如果只是暂时不想让它匹配，请改用复核中的“停用”，停用可以随时恢复。" }),
      ],
      actions: [
        { label: "取消", kind: "quiet" },
        { label: "确认删除", kind: "danger", run: function () { return submitDelete(fp); } },
      ],
    });
  }

  function submitDelete(fp) {
    var body = { request_id: newRequestId(), version: fp.version, confirm: true };
    return api("api/fingerprints/" + fp.id + "/delete", { method: "POST", body: body }).then(function (data) {
      var result = data.result;
      if (result.status === "ok" || result.status === "already_done" || result.status === "not_found") {
        haptic(result.status === "ok" ? "success" : "warning");
        toast(result.status === "ok" ? "指纹 #" + fp.id + " 已删除。" : result.message, result.status === "ok" ? null : "warn");
        removeRow("fingerprint", fp.id);
        closeDetail();
        loadSummary();
      } else {
        haptic("warning");
        toast(result.message, "warn");
        if (data.fingerprint) {
          renderDetail("fingerprint", data.fingerprint);
          replaceRow("fingerprint", Object.assign({}, findItem(fp.id) || {}, data.fingerprint));
        }
      }
    });
  }

  function findItem(id) {
    var index = indexOf("fingerprint", id);
    return index >= 0 ? state.items[index] : null;
  }

  // -------------------------------------------------------------------- sheet

  function openSheet(options) {
    var sheet = el.sheet;
    clear(sheet);
    var body = h("div", { class: "sheet-body" }, h("h2", { id: "sheet-title", text: options.title }));
    append(body, options.body);
    var actions = h("div", { class: "sheet-actions" });
    options.actions.forEach(function (action) {
      var button = h("button", { class: "btn btn--" + (action.kind || "quiet"), type: "button", text: action.label });
      button.addEventListener("click", function () {
        if (!action.run) { closeSheet(); return; }
        Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) { b.disabled = true; });
        button.textContent = "处理中……";
        Promise.resolve().then(action.run).then(function () {
          if (sheet._owner === options) closeSheet();
        }, function (error) {
          closeSheet();
          handleError(error);
        });
      });
      actions.appendChild(button);
    });
    sheet.appendChild(body);
    sheet.appendChild(actions);
    sheet._owner = options;
    if (typeof sheet.showModal === "function") {
      if (!sheet.open) sheet.showModal();
    } else {
      sheet.setAttribute("open", "");
    }
    var first = actions.querySelector("button");
    if (first) first.focus();
  }

  function closeSheet() {
    var sheet = el.sheet;
    sheet._owner = null;
    if (typeof sheet.close === "function" && sheet.open) sheet.close();
    else sheet.removeAttribute("open");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
