/* Enterprise self-service pages. Talks to /api/v1/onboarding, privacy, me/dashboard. */
(function () {
  const ACCT_KEY = "token_rankings_account_hint";

  function tr() {
    return window.TR || {};
  }

  function esc(value) {
    const fn = tr().escapeHtml;
    if (fn) return fn(value == null ? "" : value);
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function tokens(n) {
    const fn = tr().fmtTokens;
    return fn ? fn(n) : String(n || 0);
  }

  function usd(n) {
    const fn = tr().fmtUsd;
    return fn ? fn(n) : "$" + Number(n || 0).toFixed(2);
  }

  function authHeaders() {
    const fn = tr().authHeaders;
    return fn ? fn() : {};
  }

  async function api(url, options) {
    const fn = tr().apiFetch;
    if (!fn) throw new Error("页面还没准备好");
    return fn(url, options);
  }

  function rememberedAccount() {
    const who = tr().state && tr().state.whoami;
    if (who && who.role === "member" && who.account_id) return who.account_id;
    const ids = ["onboardAccount", "settingsAccount", "meFilterAccount"];
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el && el.value && el.value.trim()) return el.value.trim();
    }
    try {
      return (sessionStorage.getItem(ACCT_KEY) || "").trim();
    } catch (_) {
      return "";
    }
  }

  function rememberAccount(value) {
    const who = tr().state && tr().state.whoami;
    if (who && who.role === "member") return;
    try {
      if (value) sessionStorage.setItem(ACCT_KEY, value);
    } catch (_) { /* ignore */ }
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    if (button) {
      const old = button.textContent;
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = old; }, 1200);
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const id = button.getAttribute("data-copy");
    const node = document.getElementById(id);
    if (node) copyText(node.textContent || "", button);
  });

  let os = "macos";
  let commandSet = null;

  function showCommand() {
    const pre = document.getElementById("installCommand");
    if (!pre || !commandSet) return;
    const direct = { macos: "macos", linux: "linux", windows: "windows" }[os] || "linux";
    pre.textContent = commandSet[direct] || commandSet.unix || "";
    const curl = document.getElementById("installCurl");
    if (curl) curl.textContent = commandSet[direct + "_curl"] || commandSet.unix_curl || "";
  }

  async function loadOnboard() {
    const input = document.getElementById("onboardAccount");
    if (input && !input.value) input.value = rememberedAccount();
    const account = input && input.value.trim();
    if (account) rememberAccount(account);
    const params = new URLSearchParams();
    if (account) params.set("account_id", account);
    const res = await api("/api/v1/onboarding?" + params.toString());
    const data = await res.json();
    commandSet = data.commands || {};
    showCommand();
    const ai = document.getElementById("aiInstallPrompt");
    if (ai) ai.textContent = data.ai_prompt || "";
    const hint = document.getElementById("installHint");
    if (hint) {
      hint.textContent = data.token_ready
        ? "命令里已经写好你的个人上报密钥，只能写入账号 " + (data.account_id || "") + "。跑完会接上原来的定时任务。每日自动同步用量，不是只上报这一次。"
        : "还没有个人密钥。先登录。管理员再填账号，然后点「按这个账号生成命令」。";
    }
  }

  async function checkConnection() {
    const box = document.getElementById("connStatus");
    const input = document.getElementById("onboardAccount");
    const account = (input && input.value.trim()) || rememberedAccount();
    if (!account) {
      if (box) {
        box.className = "conn-status bad";
        box.textContent = "先填账号，或粘贴成员密钥。";
      }
      return;
    }
    rememberAccount(account);
    const clientTime = new Date().toISOString();
    const res = await api(
      "/api/v1/onboarding/status?account_id=" + encodeURIComponent(account) +
      "&client_time=" + encodeURIComponent(clientTime)
    );
    const data = await res.json().catch(() => ({}));
    if (!box) return;
    const tools = document.getElementById("connTools");
    if (!res.ok) {
      box.className = "conn-status bad";
      const detail = data.detail || {};
      const code = detail.error === "invalid_api_key" ? "invalid_key" : (detail.error || "invalid_key");
      box.textContent = (detail.message || "密钥无效。") + " 错误码 " + code + "。" +
        (detail.next_step ? " 下一步：" + detail.next_step : " 下一步：打开登录页，重新粘贴用户密钥。不要用写入密钥。");
      if (tools) tools.textContent = "";
      return;
    }
    box.className = "conn-status " + (data.connected ? "ok" : "bad");
    const when = data.last_report_label ? "上次上报 " + data.last_report_label + "。" : "";
    box.textContent = (data.message || "") + " 错误码 " + (data.error_code || "unknown") + "。" +
      when + " 累计 " + (data.event_count || 0) + " 条。下一步：" + (data.next_step || "");
    if (tools) {
      const detected = (data.detected || []).join("、") || "还没有探测记录";
      const missing = (data.missing || []).join("、") || "还没有探测记录";
      tools.textContent = "检测到：" + detected + "。未检测到：" + missing + "。";
    }
  }

  async function loadPrivacy() {
    const res = await api("/api/v1/privacy");
    if (!res.ok) return;
    const data = await res.json();
    const body = document.getElementById("privacyBody");
    if (body && data.rows) {
      body.innerHTML = data.rows.map((row) => {
        const cls = row.bucket === "会上传" ? "tag-up" : row.bucket === "仅本地" ? "tag-local" : "tag-never";
        return `<tr><td>${esc(row.item)}</td><td class="${cls}">${esc(row.bucket)}</td><td>${esc(row.note || "")}</td></tr>`;
      }).join("");
    }
    const dry = document.getElementById("dryRunCommand");
    if (dry && data.dry_run) dry.textContent = (data.dry_run.commands || []).join("\n");
    const note = document.getElementById("dryRunNote");
    if (note && data.dry_run) note.textContent = data.dry_run.note || "";
    const sample = document.getElementById("privacySample");
    if (sample) sample.textContent = JSON.stringify(data.sample_event || {}, null, 2);
    const audit = document.getElementById("aiAuditPrompt");
    if (audit) audit.textContent = data.ai_audit_prompt || "";
  }

  function levelClass(day) {
    if (day.future) return "future";
    return "lv" + (day.level || 0);
  }

  function renderDashboard(data) {
    const host = document.getElementById("meDash");
    if (!host) return;
    const ov = data.overview || {};
    const rank = ov.rank || {};
    const anomaly = data.anomaly || {};
    const review = anomaly.status === "review"
      ? `<div class="secret-warn">${esc(anomaly.label)}：${esc(anomaly.reason || "")}</div>`
      : "";
    const heat = (data.heatmap || []).map((day) =>
      `<i class="${levelClass(day)}" title="${esc(day.date)} ${tokens(day.tokens)}"></i>`
    ).join("");
    const matrix = (data.tool_matrix || []).map((tool) => `
      <div class="tool-card ${esc(tool.state)}">
        <div class="mini-row">
          <div><strong>${esc(tool.label)}</strong> · <span class="state">${esc(tool.state_label)}</span></div>
          <div class="tok">${tool.state === "has_data" ? tokens(tool.total_tokens) : ""}</div>
        </div>
        <p class="muted">${esc(tool.copy || "")}</p>
      </div>
    `).join("");
    const tools = matrix
      ? `<div class="tool-matrix">${matrix}</div>`
      : ((data.tools || []).map((tool) => `
      <div class="mini-row"><div>${esc(tool.label)} · ${tool.share_pct}%</div><div class="tok">${tokens(tool.total_tokens)}</div></div>
      <div class="bar-wrap"><div class="bar"><i style="width:${Math.min(100, tool.share_pct || 0)}%"></i></div></div>
    `).join("") || '<div class="empty">这个时间范围还没有工具用量。回到「接入」运行安装命令，再点「再检查连接」。</div>');
    const models = (data.models || []).map((model) => `
      <div class="mini-row">
        <div>${esc(model.model)} · ${model.share_pct}%${model.estimate ? ' <span class="basis-badge estimate">估算</span>' : ""}</div>
        <div class="tok">${tokens(model.total_tokens)} · ${usd(model.estimated_cost_usd)}</div>
      </div>
      <div class="bar-wrap"><div class="bar"><i style="width:${Math.min(100, model.share_pct || 0)}%"></i></div></div>
    `).join("") || '<div class="empty">还没有模型。接入并上报之后，这里会列出模型名。不认识的模型也会留下，并标成估算。</div>';
    const cache = data.cache || {};
    const pct = cache.parts_pct || {};
    const colors = { input: "#7dcea0", output: "#5dade2", cache_read: "#f0b429", cache_write: "#bb8fce" };
    const names = { input: "输入", output: "输出", cache_read: "缓存读", cache_write: "缓存写" };
    const stack = ["input", "output", "cache_read", "cache_write"].map((key) =>
      `<i style="width:${pct[key] || 0}%;background:${colors[key]}"></i>`
    ).join("");
    const legend = ["input", "output", "cache_read", "cache_write"].map((key) =>
      `<span><i class="swatch" style="background:${colors[key]}"></i>${names[key]} ${pct[key] || 0}%</span>`
    ).join("");
    const detail = data.detail || {};
    const hours = (detail.hourly || []).filter((row) => row.tokens || row.messages).map((row) => `
      <tr>
        <td>${esc(row.hour)}</td>
        <td>${tokens(row.tokens)}</td>
        <td>${row.conversations}</td>
        <td>${row.messages}</td>
        <td>${tokens(row.net_reported)}</td>
      </tr>
    `).join("") || `<tr><td colspan="5">这个时间范围没有按小时的用量。</td></tr>`;
    const tzRows = (data.timezones || []).map((row) => `
      <tr>
        <td style="text-align:left">${esc(row.label)}</td>
        <td>${esc(row.civil_date)}</td>
        <td>${tokens(row.today_tokens)}</td>
        <td>${tokens(row.recent_7d_tokens)}</td>
      </tr>
    `).join("");
    const sync = ov.last_sync ? esc(ov.last_sync) : "还没有";
    host.innerHTML = `
      ${review}
      <div class="overview">
        <h2>我的用量 <span class="basis-badge estimate">列表价估算</span></h2>
        <div class="dash-grid">
          <div>
            <div class="ov-label">${esc(data.window_label || "")}消耗</div>
            <div class="ov-value">${tokens(ov.total_tokens)} <span style="font-size:0.9rem">Tokens</span></div>
            <div class="ov-sub">当日（${esc(ov.timezone_label || "北京时间")}）${tokens(ov.today_tokens)} · ${ov.device_count || 0} 台设备在上报 · 上次 ${sync}</div>
          </div>
          <div>
            <div class="ov-label">预估费用</div>
            <div class="ov-value accent">${usd(ov.estimated_cost_usd)}</div>
            <div class="ov-sub">不是实扣账单</div>
          </div>
          <div>
            <div class="ov-label">排名</div>
            <div class="ov-value">${rank.rank ? "#" + rank.rank : "—"}</div>
            <div class="ov-sub">${esc(rank.label || "企业内可选")}</div>
          </div>
          <div>
            <div class="ov-label">连续活跃</div>
            <div class="ov-value">${ov.streak_days || 0} 天</div>
            <div class="ov-sub">${esc(ov.org_tag || "组织标签未填")}</div>
          </div>
        </div>
      </div>
      <div class="step-card">
        <h3>用量趋势</h3>
        <p class="muted">颜色越亮，当天 Token 越多。按${esc(ov.timezone_label || "北京时间")}切日。悬停方块可看日期。</p>
        <div class="heat">${heat || ""}</div>
        <p class="muted">少 <i class="swatch" style="background:#6b5420"></i><i class="swatch" style="background:#f0b429"></i> 多</p>
      </div>
      <div class="step-card">
        <h3>用量详情</h3>
        <div class="detail-tabs">
          <button type="button" class="btn ghost active" data-detail="tools">工具与模型</button>
          <button type="button" class="btn ghost" data-detail="cost">费用与缓存</button>
          <button type="button" class="btn ghost" data-detail="hours">用量明细</button>
        </div>
        <div data-panel="tools">
          <h3>工具</h3>
          ${tools}
          <h3>模型 Top</h3>
          ${models}
        </div>
        <div data-panel="cost" hidden>
          <div class="dash-grid">
            <div><div class="ov-label">预估费用</div><div class="ov-value accent">${usd(cache.estimated_cost_usd)}</div></div>
            <div><div class="ov-label">缓存节省估算</div><div class="ov-value">${usd(cache.cache_savings_usd)}</div></div>
            <div><div class="ov-label">缓存命中率</div><div class="ov-value">${cache.cache_hit_rate_pct || 0}%</div></div>
          </div>
          <div class="stack">${stack}</div>
          <div class="legend">${legend}</div>
          <p class="muted">${esc(cache.footnote || "")}</p>
          <p class="formula">${esc(data.formula_zh || "")}</p>
          <p class="muted">${esc(data.leaderboard_zh || "")} <a href="/api/v1/pricing">价目表</a> · <a href="/api/v1/pricing/formula">公式说明</a></p>
          <p class="muted">定价表 ${(data.pricing_table && data.pricing_table.table_version) || "—"}，更新于 ${(data.pricing_table && data.pricing_table.updated_at) || "—"}。列表价估算，不是实扣账单。</p>
        </div>
        <div data-panel="hours" hidden>
          <div class="dash-grid">
            <div><div class="ov-label">对话次数</div><div class="ov-value">${detail.conversations || 0}</div></div>
            <div><div class="ov-label">有用量的日期</div><div class="ov-value">${detail.days_with_usage || 0} 天</div></div>
          </div>
          <p class="muted">${esc(detail.hourly_note || "")}</p>
          <table class="hour-table">
            <thead><tr><th>小时</th><th>含缓存 Tokens</th><th>对话次数</th><th>消息数</th><th>上报净增</th></tr></thead>
            <tbody>${hours}</tbody>
          </table>
          <h3>时区对照</h3>
          <table class="hour-table">
            <thead><tr><th>口径</th><th>日期</th><th>今日 Tokens</th><th>近 7 天 Tokens</th></tr></thead>
            <tbody>${tzRows}</tbody>
          </table>
          <p class="muted">第二时区只做对照，不会另做一张榜。</p>
        </div>
      </div>
    `;
    host.querySelectorAll("[data-detail]").forEach((button) => {
      button.addEventListener("click", () => {
        host.querySelectorAll("[data-detail]").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        const name = button.getAttribute("data-detail");
        host.querySelectorAll("[data-panel]").forEach((panel) => {
          panel.hidden = panel.getAttribute("data-panel") !== name;
        });
      });
    });
  }

  async function refreshMe() {
    const host = document.getElementById("meDash");
    if (!host || !tr().state || tr().state.page !== "me") return;
    const account = rememberedAccount();
    if (!account) {
      host.innerHTML = '<div class="empty">选好账号后，这里显示当日用量、趋势和缓存。</div>';
      return;
    }
    const windowName = tr().state.meWindow || "today";
    const res = await api(
      "/api/v1/me/dashboard?window=" + encodeURIComponent(windowName) +
      "&account_id=" + encodeURIComponent(account)
    );
    if (!res.ok) {
      host.innerHTML = '<div class="empty">用量卡片没有加载出来。</div>';
      return;
    }
    renderDashboard(await res.json());
  }

  async function loadAnomalies() {
    const host = document.getElementById("anomalyPanel");
    if (!host) return;
    const res = await api("/api/v1/anomalies");
    if (!res.ok) {
      host.innerHTML = "";
      return;
    }
    const data = await res.json();
    const items = data.items || [];
    if (!items.length) {
      host.innerHTML = `<div class="hint-card">异常用量待审：目前没有。${esc(data.rule || "")}</div>`;
      return;
    }
    host.innerHTML = `<div class="secret-warn">异常用量待审 ${items.length} 个账号</div>` +
      items.map((item) => `<div class="step-card"><strong>${esc(item.account_id)}</strong> · <span class="review-flag">${esc(item.label)}</span><p>${esc(item.reason || "")}</p></div>`).join("") +
      `<p class="muted">${esc(data.rule || "")}</p>`;
  }

  function fillTz(select, selected) {
    if (!select) return;
    const zones = [
      ["", "不对照"],
      ["Asia/Shanghai", "北京时间"],
      ["Asia/Tokyo", "东京时间"],
      ["Asia/Singapore", "新加坡时间"],
      ["Europe/London", "伦敦时间"],
      ["America/Los_Angeles", "美国太平洋时间"],
      ["America/New_York", "美国东部时间"],
      ["UTC", "UTC"],
    ];
    select.innerHTML = zones.map(([id, label]) =>
      `<option value="${esc(id)}">${esc(label)}</option>`
    ).join("");
    select.value = selected || "";
  }

  async function loadSettings() {
    const input = document.getElementById("settingsAccount");
    if (input && !input.value) input.value = rememberedAccount();
    const account = (input && input.value.trim()) || rememberedAccount();
    if (!account) return;
    rememberAccount(account);
    const res = await api("/api/v1/me/prefs?account_id=" + encodeURIComponent(account));
    if (res.ok) {
      const pref = await res.json();
      const vis = document.querySelector(`input[name="visibility"][value="${pref.visibility}"]`);
      if (vis) vis.checked = true;
      const tag = document.getElementById("orgTag");
      if (tag) tag.value = pref.org_tag || "";
      const dept = document.getElementById("deptName");
      if (dept) dept.value = pref.department || "";
      const primary = document.getElementById("primaryTz");
      fillTz(primary, pref.timezone || "Asia/Shanghai");
      if (primary && primary.querySelector('option[value=""]')) {
        primary.querySelector('option[value=""]').remove();
      }
      const compare = document.getElementById("compareTz");
      fillTz(compare, pref.compare_timezone || "");
      const show = document.getElementById("showRank");
      if (show) show.checked = !!pref.show_rank;
      const note = document.getElementById("prefsNote");
      if (note) note.textContent = pref.note || "";
    }
    const devices = await api("/api/v1/devices?account_id=" + encodeURIComponent(account));
    const list = document.getElementById("deviceList");
    if (devices.ok && list) {
      const data = await devices.json();
      const items = data.items || [];
      list.innerHTML = items.length ? items.map((device) => `
        <div class="device-row">
          <div>
            <strong>${esc(device.name)}</strong>
            <span class="muted"> · ${esc(device.status_label)} · ${esc(device.device_key)}</span>
            <div class="muted">上次 ${esc(device.last_seen || "—")} · ${device.report_count} 次</div>
          </div>
          <div>
            <button type="button" class="btn ghost" data-device="${device.id}" data-act="rename">改名</button>
            <button type="button" class="btn ghost" data-device="${device.id}" data-act="hidden">隐藏</button>
            <button type="button" class="btn ghost" data-device="${device.id}" data-act="disabled">停用</button>
            <button type="button" class="btn ghost" data-device="${device.id}" data-act="active">恢复</button>
          </div>
        </div>
      `).join("") : '<div class="empty">还没有设备。接入并上报之后，这里会按工具列出。</div>';
      const note = document.getElementById("deviceNote");
      if (note) note.textContent = data.grouping || "";
    }
    const formula = await api("/api/v1/pricing/formula");
    if (formula.ok) {
      const data = await formula.json();
      const box = document.getElementById("formulaBox");
      if (box) {
        const version = data.table_version ? `定价表 ${esc(data.table_version)}` : "定价表";
        const updated = data.updated_at ? `，更新于 ${esc(data.updated_at)}` : "";
        box.innerHTML = `<p class="formula">${esc(data.formula_zh || "")}</p><p class="muted">${esc(data.disclaimer_zh || "列表价估算，不是实扣账单。")}</p><p class="muted">${esc(data.leaderboard_zh || "")}</p><p class="muted">${version}${updated}。不认识的模型会入库，费用用默认档，并标成估算。</p><p><a href="/api/v1/pricing">打开价目表</a></p>`;
      }
    }
    const feedback = await api("/api/v1/feedback");
    const feed = document.getElementById("feedbackList");
    if (feedback.ok && feed) {
      const data = await feedback.json();
      const items = data.items || [];
      feed.innerHTML = items.length ? items.map((item) =>
        `<div class="device-row"><div><strong>${esc(item.tool_name || "未填工具")}</strong><div class="muted">${esc(item.structure_text || item.note || "")}</div></div><span class="muted">${esc(item.status)}</span></div>`
      ).join("") : '<div class="empty">还没有提交。</div>';
    }
    const diag = document.getElementById("aiDiagPrompt");
    if (diag && (!diag.textContent.trim() || diag.textContent.indexOf("打开本页后") === 0)) {
      const privacy = await api("/api/v1/privacy");
      if (privacy.ok) {
        const data = await privacy.json();
        diag.textContent = data.ai_audit_prompt || "";
      }
    }
  }

  async function savePrefs() {
    const account = rememberedAccount();
    const visibility = (document.querySelector('input[name="visibility"]:checked') || {}).value || "company";
    const body = {
      visibility,
      org_tag: (document.getElementById("orgTag") || {}).value || "",
      department: (document.getElementById("deptName") || {}).value || "",
      timezone: (document.getElementById("primaryTz") || {}).value || "Asia/Shanghai",
      compare_timezone: (document.getElementById("compareTz") || {}).value || "",
      show_rank: !!((document.getElementById("showRank") || {}).checked),
    };
    const res = await api("/api/v1/me/prefs?account_id=" + encodeURIComponent(account), {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    const msg = document.getElementById("prefsMsg");
    if (msg) {
      msg.textContent = res.ok
        ? "已保存。企业榜会按这个范围显示；管理员仍能看到全部，用来核对费用。"
        : ((data.detail && data.detail.message) || "没有保存");
    }
  }

  async function onDeviceClick(event) {
    const button = event.target.closest("[data-device]");
    if (!button) return;
    const id = button.getAttribute("data-device");
    const act = button.getAttribute("data-act");
    const body = {};
    if (act === "rename") {
      const name = window.prompt("设备显示名", "");
      if (!name) return;
      body.name = name;
    } else {
      body.status = act;
    }
    const res = await api("/api/v1/devices/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    const msg = document.getElementById("prefsMsg");
    if (msg) msg.textContent = (data.message || (data.detail && data.detail.message) || "已更新");
    if (res.ok) await loadSettings();
  }

  async function submitFeedback() {
    const res = await api("/api/v1/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        tool_name: (document.getElementById("diagTool") || {}).value || "",
        note: (document.getElementById("diagNote") || {}).value || "",
        structure_text: (document.getElementById("diagStructure") || {}).value || "",
      }),
    });
    const data = await res.json().catch(() => ({}));
    const msg = document.getElementById("diagMsg");
    if (msg) {
      msg.textContent = res.ok
        ? (data.message || "已提交")
        : ((data.detail && data.detail.message) || "提交失败");
    }
    if (res.ok) await loadSettings();
  }

  function bindOnce() {
    const osButtons = [
      ["osMacBtn", "macos"],
      ["osLinuxBtn", "linux"],
      ["osWinBtn", "windows"],
    ];
    osButtons.forEach(([id, name]) => {
      const button = document.getElementById(id);
      if (!button || button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", () => {
        os = name;
        osButtons.forEach(([otherId]) => {
          const other = document.getElementById(otherId);
          if (other) other.classList.toggle("active", otherId === id);
        });
        showCommand();
      });
    });
    const reload = document.getElementById("onboardReloadBtn");
    if (reload && !reload.dataset.bound) {
      reload.dataset.bound = "1";
      reload.addEventListener("click", () => loadOnboard().catch(() => {}));
    }
    const check = document.getElementById("checkConnBtn");
    if (check && !check.dataset.bound) {
      check.dataset.bound = "1";
      check.addEventListener("click", () => checkConnection().catch(() => {}));
    }
    const save = document.getElementById("prefsSaveBtn");
    if (save && !save.dataset.bound) {
      save.dataset.bound = "1";
      save.addEventListener("click", () => savePrefs().catch(() => {}));
    }
    const settingsReload = document.getElementById("settingsReloadBtn");
    if (settingsReload && !settingsReload.dataset.bound) {
      settingsReload.dataset.bound = "1";
      settingsReload.addEventListener("click", () => loadSettings().catch(() => {}));
    }
    const devices = document.getElementById("deviceList");
    if (devices && !devices.dataset.bound) {
      devices.dataset.bound = "1";
      devices.addEventListener("click", (event) => onDeviceClick(event).catch(() => {}));
    }
    const send = document.getElementById("diagSendBtn");
    if (send && !send.dataset.bound) {
      send.dataset.bound = "1";
      send.addEventListener("click", () => submitFeedback().catch(() => {}));
    }
  }

  window.Product = {
    async onPage(page) {
      bindOnce();
      try {
        if (page === "onboard") await loadOnboard();
        else if (page === "privacy") await loadPrivacy();
        else if (page === "settings") await loadSettings();
        else if (page === "me") await refreshMe();
        else if (page === "alerts") await loadAnomalies();
      } catch (_) { /* 页面其余部分仍可用 */ }
    },
    refreshMe,
  };
})();
