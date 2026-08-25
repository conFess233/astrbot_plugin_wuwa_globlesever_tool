const bridge = window.AstrBotPluginPage;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const ICONS = {
  alert: '<path d="M12 9v4m0 4h.01M10.3 3.7 2.4 17.4A2 2 0 0 0 4.1 20h15.8a2 2 0 0 0 1.7-2.6L13.7 3.7a2 2 0 0 0-3.4 0Z"/>',
  archive: '<path d="M4 7h16v13H4zM3 3h18v4H3zm6 8h6"/>',
  backup: '<path d="M4 7v13h16V7M8 3h8l4 4H4l4-4Zm4 7v7m-3-3 3 3 3-3"/>',
  chevronLeft: '<path d="m15 18-6-6 6-6"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  dashboard: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  delete: '<path d="M4 7h16m-10 4v6m4-6v6M9 4h6l1 3H8l1-3Zm-3 3 1 14h10l1-14"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M5 21h14"/>',
  filter: '<path d="M4 5h16M7 12h10m-7 7h4"/>',
  folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
  groups: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m7-10a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm8 4a4 4 0 0 1 4 4v2m-4-10a4 4 0 0 0 0-8"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 2"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 20"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9h.01"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5m10.5-3A8 8 0 0 0 6.2 6.2L4 8m16 8-2.2 1.8A8 8 0 0 1 4.5 15"/>',
  save: '<path d="M5 3h12l3 3v15H4V3h1Zm3 0v6h8V3M8 21v-7h8v7"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  storage: '<path d="M4 4h16v5H4zM4 9v5h16V9M4 14v6h16v-6M8 6.5h.01M8 11.5h.01M8 17h.01"/>',
  sync: '<path d="M20 7v5h-5M4 17v-5h5m10.5-3A8 8 0 0 0 6.2 6.2L4 8m16 8-2.2 1.8A8 8 0 0 1 4.5 15"/>',
  type: '<path d="M4 6V4h16v2M9 20h6M12 4v16"/>',
  upload: '<path d="M12 16V4m-5 5 5-5 5 5M5 20h14"/>',
};

const CONFIG = [
  ["入口与查询", "dashboard", [
    ["public_https_base_url", "登录页公网 HTTPS 地址", "text", "Cloudflare 域名；反向代理到独立登录端口，不是 Dashboard"],
    ["login_server_host", "独立登录监听地址", "text", "同机 Cloudflare Tunnel 保持 127.0.0.1"],
    ["login_server_port", "独立登录监听端口", "number", "默认 6199；Cloudflare Service 指向此端口"],
    ["login_trust_proxy_headers", "信任代理来源 IP", "bool", "同机 Cloudflare Tunnel 开启；直接开放端口时关闭"],
    ["login_trusted_proxy_cidrs", "额外可信代理网段", "list", "跨主机代理时填写来源 CIDR；同机 Tunnel 留空"],
    ["extra_command_roots", "额外命令入口", "list", "逗号分隔；永久兼容入口 /kh 不受影响"],
    ["allow_query_others", "允许查询他人", "bool", "启用后可通过 @用户 查询角色、账号信息和探索"],
    ["query_refresh_enabled", "查询前自动刷新账号数据", "bool", "账号信息、日常与探索在冷却外先获取一次最新数据"],
    ["player_refresh_cooldown_seconds", "账号数据刷新冷却（秒）", "number", "账号信息、日常与探索共用"],
    ["role_refresh_cooldown_minutes", "角色刷新冷却（分钟）", "number", "用户主动刷新按区服与 UID 分开计算"],
  ]],
  ["关键词", "search", [
    ["keyword_help", "帮助关键词", "list", "逗号分隔"],
    ["keyword_login", "登录关键词", "list", "逗号分隔"],
    ["keyword_cancel_login", "取消登录关键词", "list", "逗号分隔"],
    ["keyword_account", "账号关键词", "list", "逗号分隔"],
    ["keyword_switch", "切换账号关键词", "list", "逗号分隔"],
    ["keyword_account_info", "账号信息关键词", "list", "逗号分隔"],
    ["keyword_character", "角色关键词", "list", "逗号分隔"],
    ["keyword_daily", "日常关键词", "list", "逗号分隔；仅限本人"],
    ["keyword_exploration", "探索关键词", "list", "逗号分隔"],
    ["keyword_refresh", "刷新关键词", "list", "逗号分隔"],
  ]],
  ["登录安全", "groups", [
    ["login_link_ttl_minutes", "登录链接有效期（分钟）", "number", "允许 1–60"],
    ["login_rate_window_minutes", "登录限流窗口（分钟）", "number", "允许 1–60"],
    ["login_session_max_attempts", "会话最大尝试次数", "number", "允许 1–20"],
    ["login_email_max_attempts", "邮箱窗口最大尝试次数", "number", "允许 1–30"],
    ["login_ip_max_attempts", "IP 窗口最大尝试次数", "number", "允许 1–100"],
    ["login_freeze_minutes", "登录冻结时间（分钟）", "number", "允许 1–120"],
  ]],
  ["同步与运行", "sync", [
    ["auto_sync_enabled", "启用自动同步", "bool", "按配置周期刷新已绑定 UID"],
    ["auto_sync_interval_minutes", "自动同步周期（分钟）", "number", "允许 30–1440"],
    ["sync_concurrency", "账号同步并发", "number", "允许 1–10"],
    ["role_detail_concurrency", "角色详情并发", "number", "允许 1–5"],
    ["request_timeout_seconds", "单次上游请求超时（秒）", "number", "允许 5–120；保存后重建共享 HTTP 会话"],
    ["request_retry_count", "幂等请求重试次数", "number", "允许 0–5；登录与状态变更请求不盲目重试"],
    ["player_refresh_timeout_seconds", "账号数据整体刷新超时（秒）", "number", "允许 10–180"],
    ["role_refresh_timeout_seconds", "角色数据整体刷新超时（秒）", "number", "允许 30–600"],
    ["render_timeout_seconds", "渲染超时（秒）", "number", "允许 5–120"],
    ["resource_cache_max_mb", "图片资源缓存上限（MiB）", "number", "允许 64–4096；当前引用资源不会被回收"],
    ["resource_download_timeout_seconds", "资源与字体下载超时（秒）", "number", "允许 10–300"],
    ["admin_audit_retention_days", "管理员审计保留天数", "number", "允许 0–365；0 表示不记录"],
  ]],
];

const state = {
  overview: null,
  accounts: null,
  accountPage: 1,
  config: {},
  draft: {},
  dirty: false,
  inspection: null,
  fonts: null,
  loading: 0,
};

function icon(name, className = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  svg.innerHTML = ICONS[name] || ICONS.info;
  return svg;
}

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon-host]").forEach((host) => host.replaceChildren(icon(host.dataset.iconHost)));
  root.querySelectorAll("[data-icon]").forEach((button) => {
    if (!button.querySelector("svg")) button.prepend(icon(button.dataset.icon));
  });
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1048576) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / 1048576).toFixed(1)} MiB`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

function statusName(value) {
  return ({ valid: "有效", success: "成功", needs_login: "需登录", invalid: "失效", failed: "失败", unknown: "未知" })[value] || value || "—";
}

function statusClass(value) {
  if (["valid", "success"].includes(value)) return "status success";
  if (value === "needs_login") return "status warning";
  return "status danger";
}

function applyTheme(context) {
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
}

function setLoading(active) {
  state.loading = Math.max(0, state.loading + (active ? 1 : -1));
  $("#progress").hidden = state.loading === 0;
}

async function task(operation) {
  setLoading(true);
  try {
    return await operation();
  } catch (error) {
    throw new Error(error?.message || "请求失败");
  } finally {
    setLoading(false);
  }
}

function notify(message, error = false) {
  const toast = element("div", `toast${error ? " error" : ""}`);
  toast.append(icon(error ? "alert" : "info"), element("span", "", message));
  $("#toast-stack").append(toast);
  window.setTimeout(() => {
    toast.classList.add("out");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, 4200);
}

function definitionList(target, entries) {
  target.replaceChildren(...entries.map(([label, value]) => {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", String(value ?? "—")));
    return row;
  }));
}

function activateTab(name) {
  $$(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === name));
  $$(".panel").forEach((item) => item.classList.toggle("active", item.id === `${name}-panel`));
}

function allowDiscardDraft() {
  return !state.dirty || window.confirm("插件配置还有未保存修改，确定放弃并离开吗？");
}

function renderMetric(label, value, iconName) {
  const card = element("article", "metric");
  const visual = element("span", "metric-icon");
  visual.append(icon(iconName));
  const copy = element("div", "metric-copy");
  copy.append(element("span", "metric-label", label), element("strong", "metric-value", String(value ?? 0)));
  card.append(visual, copy);
  return card;
}

async function loadOverview() {
  state.overview = await bridge.apiGet("dashboard/overview");
  const data = state.overview;
  const metrics = [
    ["QQ 用户", data.users, "groups"],
    ["国际服 UID", data.accounts, "database"],
    ["本地角色记录", data.characters, "archive"],
    ["有效凭据", data.token_valid, "settings"],
  ];
  $("#metrics").replaceChildren(...metrics.map((item) => renderMetric(...item)));
  definitionList($("#sync-health"), [
    ["需重新登录", data.needs_login],
    ["同步失败", data.sync_failed],
    ["上次全局成功同步", formatTime(data.last_global_sync)],
    ["自动同步", data.auto_sync?.enabled ? `${data.auto_sync.running ? "运行中" : "已启用"} · ${data.auto_sync.interval_hours} 小时` : "未启用"],
  ]);
  definitionList($("#overview-resource"), [
    ["插件 / 数据库", `v${data.version} / schema ${data.schema_version}`],
    ["角色目录", `${data.resources.character_count} 项 · ${data.resources.source}`],
    ["目录快照", data.resources.snapshot_date || "—"],
    ["卡片缓存", `${data.resources.card_cache.count} 项 · ${formatBytes(data.resources.card_cache.bytes)}`],
  ]);
  $("#header-status").textContent = `插件 v${data.version} · 数据库 schema ${data.schema_version} · 已连接`;
}

function renderAccountSkeleton() {
  const rows = Array.from({ length: 5 }, () => {
    const tr = element("tr");
    for (let index = 0; index < 6; index += 1) {
      const td = element("td");
      td.append(element("div", "skeleton"));
      tr.append(td);
    }
    return tr;
  });
  $("#account-rows").replaceChildren(...rows);
}

function actionButton(label, iconName, handler) {
  const button = element("button", "btn btn-danger-tonal icon-only");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(iconName));
  button.addEventListener("click", handler);
  return button;
}

function accountActions(item) {
  const box = element("div", "row-actions");
  box.append(
    actionButton(`解绑 ${item.region_id}/${item.uid}`, "history", () => forceUnbind(item.region_id, item.uid)),
    actionButton(`删除 QQ ${item.qq_id}`, "delete", () => deleteUser(item.qq_id)),
  );
  return box;
}

async function loadAccounts() {
  renderAccountSkeleton();
  const query = {
    q: $("#account-query").value.trim(),
    token_status: $("#token-filter").value,
    sync_status: $("#sync-filter").value,
    page: state.accountPage,
    page_size: 20,
  };
  state.accounts = await bridge.apiGet("dashboard/accounts", query);
  const data = state.accounts;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  state.accountPage = Math.min(state.accountPage, pages);
  $("#account-total").textContent = `${data.total} 项`;
  $("#account-page").textContent = `${state.accountPage} / ${pages}`;
  $("#account-prev").disabled = state.accountPage <= 1;
  $("#account-next").disabled = state.accountPage >= pages;

  const rows = data.items.map((item) => {
    const tr = element("tr");
    const identity = element("td");
    identity.append(element("strong", "", item.qq_id), element("div", "muted", item.email_masked || "—"));
    const account = element("td");
    account.append(element("strong", "", `${item.uid}${item.is_default ? " · 默认" : ""}`), element("div", "muted", item.region_name || item.region_id));
    const statuses = element("td");
    statuses.append(element("span", statusClass(item.token_status), statusName(item.token_status)), " ", element("span", statusClass(item.sync_status), statusName(item.sync_status)));
    const actions = element("td");
    actions.append(accountActions(item));
    tr.append(identity, account, element("td", "", item.player_name || "—"), statuses, element("td", "", formatTime(item.last_sync_success_at)), actions);
    return tr;
  });
  if (!rows.length) {
    const tr = element("tr");
    const td = element("td", "empty", "没有符合条件的账号");
    td.colSpan = 6;
    tr.append(td);
    rows.push(tr);
  }
  $("#account-rows").replaceChildren(...rows);

  $("#account-cards").replaceChildren(...data.items.map((item) => {
    const card = element("article", "record-card");
    const head = element("div", "record-card-head");
    head.append(element("strong", "", `${item.qq_id} · ${item.uid}`), element("span", statusClass(item.sync_status), statusName(item.sync_status)));
    const meta = element("div", "record-card-meta");
    meta.append(element("span", "", item.email_masked || "—"), element("span", "", item.region_name || item.region_id));
    card.append(head, meta, element("span", "muted", `最近同步：${formatTime(item.last_sync_success_at)}`), accountActions(item));
    return card;
  }));
}

function setDirty(value) {
  state.dirty = value;
  $("#dirty-bar").hidden = !value;
}

function renderConfig() {
  const sections = CONFIG.map(([title, iconName, fields], groupIndex) => {
    const card = element("article", "section-card config-section");
    card.id = `config-section-${groupIndex}`;
    const heading = element("div", "section-heading");
    const headingCopy = element("div");
    headingCopy.append(element("span", "eyebrow", "插件设置"), element("h2", "", title));
    const visual = element("span", "section-icon primary");
    visual.append(icon(iconName));
    heading.append(headingCopy, visual);
    const grid = element("div", "config-grid");

    fields.forEach(([key, label, type, help]) => {
      const box = element("label", "config-item");
      box.dataset.key = key;
      box.append(element("strong", "", label), element("p", "", help));
      let input;
      if (type === "bool") {
        const line = element("span", "switch-row");
        const copy = element("span");
        copy.append(element("span", "", "开关状态"), element("small", "", state.draft[key] ? "已启用" : "未启用"));
        input = document.createElement("input");
        input.type = "checkbox";
        input.setAttribute("role", "switch");
        input.checked = Boolean(state.draft[key]);
        input.addEventListener("change", () => {
          state.draft[key] = input.checked;
          copy.lastChild.textContent = input.checked ? "已启用" : "未启用";
          setDirty(true);
        });
        line.append(copy, input);
        box.append(line);
      } else {
        input = document.createElement("input");
        input.type = type === "number" ? "number" : "text";
        input.value = type === "list" ? (state.draft[key] || []).join(", ") : (state.draft[key] ?? "");
        input.addEventListener("input", () => {
          state.draft[key] = type === "number" ? Number(input.value) : type === "list" ? input.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean) : input.value.trim();
          setDirty(true);
        });
        box.append(input);
      }
      grid.append(box);
    });
    card.append(heading, grid);
    return card;
  });
  $("#config-groups").replaceChildren(...sections);

  $("#config-nav").replaceChildren(...CONFIG.map(([title, iconName], index) => {
    const button = element("button", index === 0 ? "active" : "", title);
    button.type = "button";
    button.prepend(icon(iconName));
    button.addEventListener("click", () => {
      $$("#config-nav button").forEach((item) => item.classList.toggle("active", item === button));
      $(`#config-section-${index}`).scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return button;
  }));
}

async function loadConfig() {
  state.config = await bridge.apiGet("dashboard/config");
  state.draft = structuredClone(state.config);
  setDirty(false);
  renderConfig();
}

async function saveConfig() {
  state.config = await bridge.apiPost("dashboard/config/save", state.draft);
  state.draft = structuredClone(state.config);
  setDirty(false);
  renderConfig();
  notify("插件配置已保存并应用。");
  await loadOverview();
}

function renderResources(data) {
  $("#resource-source").textContent = data.source === "runtime" ? "运行时更新" : "插件内置";
  definitionList($("#resource-details"), [
    ["目录 schema", data.schema_version],
    ["快照日期", data.snapshot_date],
    ["角色数量", data.character_count],
    ["可回滚", data.can_rollback ? "是" : "否"],
  ]);
  definitionList($("#cache-details"), [
    ["受管图片", `${data.managed_images.count} 项 · ${formatBytes(data.managed_images.bytes)} / ${formatBytes(data.managed_images.limit_bytes)}`],
    ["当前引用", `${data.managed_images.referenced} 项`],
    ["自定义字体", `${data.fonts.count} 项 · ${data.fonts.default || "系统字体"}`],
    ["旧版角色图片", `${data.character_cache.count} 项 · ${formatBytes(data.character_cache.bytes)}`],
    ["渲染卡片", `${data.card_cache.count} 项 · ${formatBytes(data.card_cache.bytes)}`],
    ["临时文件", `${data.temp.count} 项 · ${formatBytes(data.temp.bytes)}`],
  ]);
  $("#rollback-resource").disabled = !data.can_rollback;
}

async function loadResources() {
  renderResources(await bridge.apiGet("dashboard/resources"));
}

function renderFonts(data) {
  state.fonts = data;
  const preset = $("#font-preset");
  const selectedPreset = preset.value;
  const options = [element("option", "", "选择预设或填写自定义链接")];
  options[0].value = "";
  for (const item of data.presets || []) {
    const option = element("option", "", item.display_name || item.id);
    option.value = item.id;
    option.dataset.url = item.download_url || "";
    option.dataset.name = item.display_name || "";
    option.dataset.note = item.note || "";
    options.push(option);
  }
  preset.replaceChildren(...options);
  if ([...preset.options].some((item) => item.value === selectedPreset)) preset.value = selectedPreset;
  $("#font-count").textContent = `${data.items.length} 项`;

  if (!data.items.length) {
    const empty = element("div", "font-empty");
    empty.append(icon("type"), element("strong", "", "尚未安装自定义字体"), element("span", "", "卡片当前使用系统字体回退。"));
    $("#font-list").replaceChildren(empty);
    return;
  }
  $("#font-list").replaceChildren(...data.items.map((item) => {
    const row = element("div", "font-row");
    const copy = element("div", "font-row-copy");
    const title = element("strong", "", item.display_name);
    if (item.is_default) title.append(element("span", "status success", "默认"));
    const source = item.source_url ? (() => { try { return new URL(item.source_url).hostname; } catch { return item.source_url; } })() : "本地字体";
    copy.append(title, element("small", "", `${item.weight} · ${item.style} · ${source}`));
    const actions = element("div", "font-row-actions");
    if (!item.is_default) {
      const select = element("button", "btn btn-tonal", "设为默认");
      select.type = "button";
      select.addEventListener("click", () => task(() => setDefaultFont(item.font_id)).catch((error) => notify(error.message, true)));
      const remove = element("button", "btn btn-danger-tonal", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => task(() => deleteFont(item)).catch((error) => notify(error.message, true)));
      actions.append(select, remove);
    }
    row.append(copy, actions);
    return row;
  }));
}

async function loadFonts() {
  renderFonts(await bridge.apiGet("dashboard/fonts"));
}

async function installFont() {
  const url = $("#font-url").value.trim();
  if (!url) return notify("请选择预设或填写字体下载链接。", true);
  renderFonts(await bridge.apiPost("dashboard/fonts/install", {
    url,
    display_name: $("#font-name").value.trim(),
    make_default: $("#font-make-default").checked,
  }));
  $("#font-name").value = "";
  notify("字体已通过安全校验并安装。重新生成卡片即可查看效果。");
  await Promise.all([loadResources(), loadAudit()]);
}

async function setDefaultFont(fontId) {
  renderFonts(await bridge.apiPost("dashboard/fonts/default", { font_id: fontId }));
  notify("默认字体已切换，旧卡片缓存已清理。");
  await Promise.all([loadResources(), loadAudit()]);
}

async function deleteFont(item) {
  const confirmation = await typedConfirm("删除字体", `将删除“${item.display_name}”的本地字体文件。`, "删除字体");
  if (confirmation === null) return;
  renderFonts(await bridge.apiPost("dashboard/fonts/delete", {
    font_id: item.font_id,
    confirmation,
  }));
  notify("字体已删除。");
  await Promise.all([loadResources(), loadAudit()]);
}

async function renderCardPreview() {
  const kind = $("#preview-kind").value;
  const area = $("#card-preview");
  area.replaceChildren(element("div", "skeleton preview-skeleton"));
  const result = await bridge.apiGet("dashboard/cards/preview", { kind });
  const image = document.createElement("img");
  image.alt = `${$("#preview-kind").selectedOptions[0].textContent}预览`;
  image.src = result.data_uri;
  area.replaceChildren(image);
}

async function loadAudit() {
  const result = await bridge.apiGet("dashboard/audit", { limit: 200 });
  const rows = result.items.map((item) => {
    const tr = element("tr");
    [formatTime(item.created_at), item.admin_identity, item.action_type, item.masked_target, item.result].forEach((value) => tr.append(element("td", "", value || "—")));
    return tr;
  });
  if (!rows.length) {
    const tr = element("tr");
    const td = element("td", "empty", "暂无管理员操作记录");
    td.colSpan = 5;
    tr.append(td);
    rows.push(tr);
  }
  $("#audit-rows").replaceChildren(...rows);
  $("#audit-cards").replaceChildren(...result.items.map((item) => {
    const card = element("article", "record-card");
    const head = element("div", "record-card-head");
    head.append(element("strong", "", item.action_type), element("span", "status success", item.result));
    const meta = element("div", "record-card-meta");
    meta.append(element("span", "", item.admin_identity), element("span", "", formatTime(item.created_at)));
    card.append(head, meta, element("span", "muted", item.masked_target || "—"));
    return card;
  }));
}

function typedConfirm(title, message, expected) {
  const dialog = $("#confirm-dialog");
  const input = $("#dialog-input");
  const confirmButton = $("#dialog-confirm");
  $("#dialog-title").textContent = title;
  $("#dialog-message").textContent = message;
  $("#dialog-label").textContent = `请输入：${expected}`;
  input.value = "";
  confirmButton.disabled = true;
  const validate = () => { confirmButton.disabled = input.value !== expected; };
  input.addEventListener("input", validate);
  dialog.showModal();
  input.focus();
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    input.removeEventListener("input", validate);
    resolve(dialog.returnValue === "confirm" ? input.value : null);
  }, { once: true }));
}

async function forceUnbind(regionId, uid) {
  const accountKey = `${regionId}/${uid}`;
  const confirmation = await typedConfirm("强制解绑账号", "将删除该区服账号绑定及其 UID 档案；其他区服中的相同 UID 不受影响。", accountKey);
  if (confirmation === null) return;
  await bridge.apiPost("dashboard/accounts/unbind", { region_id: regionId, uid, confirmation });
  notify(`${accountKey} 已解绑。`);
  await Promise.all([loadAccounts(), loadOverview(), loadAudit()]);
}

async function deleteUser(qq) {
  const confirmation = await typedConfirm("删除 QQ 全部数据", "将删除该 QQ 的凭据、UID、档案和角色记录，此操作不可撤销。", qq);
  if (confirmation === null) return;
  await bridge.apiPost("dashboard/users/delete", { qq_id: qq, confirmation });
  notify(`QQ ${qq} 的插件数据已删除。`);
  await Promise.all([loadAccounts(), loadOverview(), loadAudit()]);
}

async function inspectBackup() {
  const file = $("#import-file").files[0];
  if (!file) return notify("请先选择备份 ZIP。", true);
  state.inspection = await bridge.upload("dashboard/backup/inspect", file);
  const data = state.inspection;
  const values = [["用户", data.users], ["UID", data.accounts], ["角色", data.characters], ["凭据", data.credential_count], ["不可解密凭据", data.invalid_credentials], ["schema", data.schema_version]];
  $("#inspection").replaceChildren(...values.map(([label, value]) => {
    const box = element("div");
    box.append(element("span", "", label), element("strong", "", String(value)));
    return box;
  }));
  $("#inspection").hidden = false;
  $("#commit-backup").disabled = false;
  notify("备份预检通过，请核对摘要后再恢复。");
}

async function commitBackup() {
  if (!state.inspection) return;
  const confirmation = await typedConfirm("恢复插件备份", "恢复前会自动生成包含加密凭据的安全备份；覆盖模式会替换同键记录。", "确认恢复备份");
  if (confirmation === null) return;
  const result = await bridge.apiPost("dashboard/backup/commit", {
    token: state.inspection.token,
    confirmation,
    mode: $("#restore-mode").value,
    restore_settings: $("#restore-settings").checked,
    restore_catalog: $("#restore-catalog").checked,
    restore_credentials: $("#restore-credentials").checked,
  });
  state.inspection = null;
  $("#commit-backup").disabled = true;
  $("#inspection").hidden = true;
  notify(`恢复完成，安全备份：${result.safety_backup}`);
  await loadAll();
}

async function loadAll() {
  await Promise.all([loadOverview(), loadAccounts(), loadConfig(), loadResources(), loadFonts(), loadAudit()]);
}

const context = await bridge.ready();
applyTheme(context);
bridge.onContext?.(applyTheme);
hydrateIcons();

$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  if (tab.dataset.tab !== "config" && !allowDiscardDraft()) return;
  if (tab.dataset.tab !== "config" && state.dirty) {
    state.draft = structuredClone(state.config);
    setDirty(false);
  }
  activateTab(tab.dataset.tab);
}));

$("#refresh").addEventListener("click", () => {
  if (!allowDiscardDraft()) return;
  task(loadAll).then(() => notify("数据已刷新。")).catch((error) => notify(error.message, true));
});
$("#filter-toggle").addEventListener("click", () => $("#account-filters").classList.toggle("open"));
$("#account-search").addEventListener("click", () => { state.accountPage = 1; task(loadAccounts).catch((error) => notify(error.message, true)); });
$("#account-query").addEventListener("keydown", (event) => { if (event.key === "Enter") $("#account-search").click(); });
$("#account-prev").addEventListener("click", () => { state.accountPage -= 1; task(loadAccounts).catch((error) => notify(error.message, true)); });
$("#account-next").addEventListener("click", () => { state.accountPage += 1; task(loadAccounts).catch((error) => notify(error.message, true)); });
$("#discard-config").addEventListener("click", () => { state.draft = structuredClone(state.config); setDirty(false); renderConfig(); });
$("#save-config").addEventListener("click", () => task(saveConfig).catch((error) => notify(error.message, true)));

$("#font-preset").addEventListener("change", () => {
  const option = $("#font-preset").selectedOptions[0];
  if (!option?.value) return;
  $("#font-url").value = option.dataset.url || "";
  $("#font-name").value = option.dataset.name || "";
  if (option.dataset.note) notify(option.dataset.note);
});
$("#install-font").addEventListener("click", () => task(installFont).catch((error) => notify(error.message, true)));
$("#render-preview").addEventListener("click", () => task(renderCardPreview).catch((error) => {
  notify(error.message, true);
  const placeholder = element("div", "preview-placeholder");
  placeholder.append(icon("alert"), element("strong", "", "预览生成失败"), element("p", "", error.message));
  $("#card-preview").replaceChildren(placeholder);
}));

$("#check-resource").addEventListener("click", () => task(async () => {
  const result = await bridge.apiPost("dashboard/resources/check", {});
  notify(result.update_available ? `发现更新：${result.current_count} → ${result.remote_count}` : "角色目录已是最新。");
}).catch((error) => notify(error.message, true)));
$("#update-resource").addEventListener("click", () => task(async () => {
  renderResources(await bridge.apiPost("dashboard/resources/update", {}));
  notify("角色目录已更新。");
  await loadAudit();
}).catch((error) => notify(error.message, true)));
$("#rollback-resource").addEventListener("click", () => task(async () => {
  const confirmation = await typedConfirm("回滚角色目录", "恢复上一个运行时目录；若没有上一个版本，则恢复插件内置目录。", "回滚角色资源");
  if (confirmation === null) return;
  renderResources(await bridge.apiPost("dashboard/resources/rollback", { confirmation }));
  notify("角色目录已回滚。");
  await loadAudit();
}).catch((error) => notify(error.message, true)));
$("#cleanup-cache").addEventListener("click", () => task(async () => {
  const confirmation = await typedConfirm("清理缓存", "只清理临时文件和可再生成的渲染卡片，不删除角色图片、账号或角色数据。", "清理缓存");
  if (confirmation === null) return;
  const result = await bridge.apiPost("dashboard/cache/cleanup", { confirmation });
  renderResources(result.status);
  notify(`已清理 ${result.removed} 个文件。`);
  await loadAudit();
}).catch((error) => notify(error.message, true)));

$("#export-backup").addEventListener("click", () => task(async () => {
  const include = $("#export-credentials").checked;
  let confirmation = "";
  if (include) {
    confirmation = await typedConfirm("导出加密凭据", "文件不会包含主密钥，但包含可由当前主密钥解密的令牌密文，请妥善保管。", "导出加密凭据");
    if (confirmation !== "导出加密凭据") return;
  }
  await bridge.download("dashboard/backup/export", { include_credentials: include ? "true" : "false", confirmation }, "wuwa-global-backup.zip");
}).catch((error) => notify(error.message, true)));
$("#inspect-backup").addEventListener("click", () => task(inspectBackup).catch((error) => notify(error.message, true)));
$("#commit-backup").addEventListener("click", () => task(commitBackup).catch((error) => notify(error.message, true)));
$("#refresh-audit").addEventListener("click", () => task(loadAudit).then(() => notify("审计已刷新。")).catch((error) => notify(error.message, true)));

$("#import-file").addEventListener("change", () => {
  const file = $("#import-file").files[0];
  $("#file-name").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "最大 128 MiB";
  state.inspection = null;
  $("#commit-backup").disabled = true;
  $("#inspection").hidden = true;
});
for (const eventName of ["dragenter", "dragover"]) $("#drop-zone").addEventListener(eventName, () => $("#drop-zone").classList.add("dragging"));
for (const eventName of ["dragleave", "drop"]) $("#drop-zone").addEventListener(eventName, () => $("#drop-zone").classList.remove("dragging"));

window.addEventListener("beforeunload", (event) => {
  if (state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

try {
  await task(loadAll);
} catch (error) {
  $("#header-status").textContent = "插件数据加载失败";
  notify(error.message, true);
}
