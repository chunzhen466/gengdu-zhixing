/* 耕读知行 · 前端逻辑
   无第三方依赖，离线可用。 */

(function () {
  "use strict";

  var state = {
    token: sessionStorage.getItem("gd_token") || "",
    history: [],
    busy: false,
    timer: null,
    llm: false
  };

  var $ = function (id) { return document.getElementById(id); };

  /* ---------------- 工具函数 ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "返回格式错误" }; })
        .then(function (data) {
          if (r.status === 401) { logout(true); }
          return data;
        });
    });
  }

  /* ---------------- 极简 Markdown 渲染 ---------------- */
  function inline(text) {
    var s = esc(text);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return s;
  }

  function renderMarkdown(src) {
    var lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    var out = [], i = 0, listOpen = null, para = [];

    function flushPara() {
      if (para.length) {
        out.push("<p>" + para.map(inline).join("<br>") + "</p>");
        para = [];
      }
    }
    function closeList() {
      if (listOpen) { out.push("</" + listOpen + ">"); listOpen = null; }
    }

    while (i < lines.length) {
      var line = lines[i];
      var t = line.trim();

      // 表格
      if (/^\|.*\|$/.test(t) && i + 1 < lines.length &&
          /^\|[\s:\-\|]+\|$/.test(lines[i + 1].trim())) {
        flushPara(); closeList();
        var head = t.split("|").slice(1, -1).map(function (c) { return c.trim(); });
        i += 2;
        var rows = [];
        while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) {
          rows.push(lines[i].trim().split("|").slice(1, -1)
            .map(function (c) { return inline(c.trim()); }));
          i++;
        }
        var tb = "<table><thead><tr>" +
          head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>";
        rows.forEach(function (r) {
          tb += "<tr>" + r.map(function (c) { return "<td>" + c + "</td>"; }).join("") + "</tr>";
        });
        out.push(tb + "</tbody></table>");
        continue;
      }

      if (!t) { flushPara(); closeList(); i++; continue; }

      if (/^---+$/.test(t)) { flushPara(); closeList(); out.push("<hr>"); i++; continue; }

      var h = t.match(/^(#{1,3})\s+(.*)$/);
      if (h) {
        flushPara(); closeList();
        var lvl = Math.min(h[1].length + 0, 3);
        out.push("<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">");
        i++; continue;
      }

      var q = t.match(/^>\s?(.*)$/);
      if (q) {
        flushPara(); closeList();
        out.push("<blockquote>" + inline(q[1]) + "</blockquote>");
        i++; continue;
      }

      var ul = t.match(/^[-*]\s+(.*)$/);
      if (ul) {
        flushPara();
        if (listOpen !== "ul") { closeList(); out.push("<ul>"); listOpen = "ul"; }
        out.push("<li>" + inline(ul[1]) + "</li>");
        i++; continue;
      }

      var ol = t.match(/^(\d+)[.、]\s+(.*)$/);
      if (ol) {
        flushPara();
        if (listOpen !== "ol") { closeList(); out.push("<ol>"); listOpen = "ol"; }
        out.push("<li>" + inline(ol[2]) + "</li>");
        i++; continue;
      }

      closeList();
      para.push(t);
      i++;
    }
    flushPara(); closeList();
    return out.join("");
  }

  /* ---------------- 登录 ---------------- */
  function showGate(msg) {
    $("gate").classList.remove("hidden");
    $("app").classList.add("hidden");
    $("gateErr").textContent = msg || "";
  }

  function enterApp() {
    $("gate").classList.add("hidden");
    $("app").classList.remove("hidden");
    loadMeta();
    loadPlots();
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(loadPlots, 5000);
    autoAskFromUrl();
  }

  /* 支持 ?q=问题 直接打开并提问：便于做二维码、也便于演示时预设问题 */
  var autoAsked = false;
  function autoAskFromUrl() {
    if (autoAsked) return;
    var params = new URLSearchParams(location.search);
    var q = (params.get("q") || "").trim();
    if (!q) return;
    autoAsked = true;
    setTimeout(function () { ask(q); }, 350);
  }

  function logout(silent) {
    state.token = "";
    sessionStorage.removeItem("gd_token");
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    showGate(silent ? "会话已过期，请重新登录" : "");
  }

  $("loginForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = $("loginBtn");
    btn.disabled = true; $("gateErr").textContent = "";
    api("/api/login", {
      method: "POST",
      body: { username: $("username").value.trim(), password: $("password").value }
    }).then(function (d) {
      btn.disabled = false;
      if (d.ok && d.token) {
        state.token = d.token;
        sessionStorage.setItem("gd_token", d.token);
        if (d.workspace) {
          $("brandSub").textContent = (d.workspace.school || "") +
            " · " + (d.workspace.team || "") + " · 校园劳动实践物联网智能体";
        }
        enterApp();
      } else {
        $("gateErr").textContent = d.error || "登录失败";
      }
    }).catch(function () {
      btn.disabled = false;
      $("gateErr").textContent = "无法连接服务，请确认 app.py 正在运行";
    });
  });

  $("logoutBtn").addEventListener("click", function () { logout(false); });

  /* ---------------- 元信息 ---------------- */
  function loadMeta() {
    api("/api/meta").then(function (d) {
      if (!d.ok) return;
      var w = d.workspace || {};
      if (w.school) {
        $("brandSub").textContent = w.school + " · " + (w.team || "") +
          " · 校园劳动实践物联网智能体";
      }
      state.llm = !!d.llm_enabled;
      var pill = $("modePill");
      pill.textContent = state.llm ? "大模型已接入" : "本地检索模式";
      pill.classList.toggle("on", state.llm);
      $("gateFoot").innerHTML = "知识库 " + (d.knowledge ?
        d.knowledge.documents + " 篇文档 · " + d.knowledge.chunks + " 个知识段" : "—");
    }).catch(function () {});
  }

  /* ---------------- 地块数据 ---------------- */
  function loadPlots() {
    if (!state.token) return;
    api("/api/plots").then(function (d) {
      if (!d.ok) return;
      renderPlots(d.plots || []);
    }).catch(function () {});
  }

  function renderPlots(plots) {
    var box = $("plotList");
    box.innerHTML = "";
    plots.forEach(function (p) {
      var warn = p.status && p.status !== "正常";
      var el = document.createElement("div");
      el.className = "plot" + (warn ? " warn" : "");
      el.innerHTML =
        '<div class="plot-top">' +
          '<span class="plot-name">' + esc(p.name) + '</span>' +
          '<span class="plot-crop">' + esc(p.crop) + '</span>' +
        '</div>' +
        '<div class="plot-metrics">' +
          '<div class="metric' + (warn ? " hl" : "") + '">' +
            '<div class="v">' + p.soil + '<span style="font-size:10px">%</span></div>' +
            '<div class="k">土壤湿度</div></div>' +
          '<div class="metric"><div class="v">' + p.temp + '<span style="font-size:10px">℃</span></div>' +
            '<div class="k">气温</div></div>' +
          '<div class="metric"><div class="v">' + Math.round(p.light / 100) / 10 +
            '<span style="font-size:10px">klx</span></div>' +
            '<div class="k">光照</div></div>' +
        '</div>' +
        '<div class="plot-foot">' +
          '<span>' + esc(p.plot_id) + ' · ' + esc(p.owner || "") + '</span>' +
          '<span class="badge' + (warn ? " warn" : "") + '">' +
            esc(p.status || "—") + '</span>' +
        '</div>';
      el.title = "点击向智能体询问该地块";
      el.style.cursor = "pointer";
      el.addEventListener("click", function () {
        ask(p.name + "现在情况怎么样");
      });
      box.appendChild(el);
    });

    // 欢迎语按真实状态生成，避免界面上出现"明明正常却说告警"的矛盾
    var tip = $("welcomeTip");
    if (tip) {
      // 注意：等级不能用 0，否则会被 `|| 9` 当成假值，导致"告警"反而排到最后
      var rank = { "告警": 1, "注意": 2, "正常": 3 };
      var bad = plots.filter(function (p) { return p.status && p.status !== "正常"; })
        .sort(function (a, b) { return (rank[a.status] || 9) - (rank[b.status] || 9); });
      if (bad.length && bad[0].alerts && bad[0].alerts.length) {
        tip.textContent = bad[0].name + "的" + bad[0].crop +
          "当前读数异常：" + bad[0].alerts[0] + "。可以直接问我怎么处理。";
      } else {
        tip.textContent = "各地块读数目前都在适宜区间，可以问我养护、时令或生成成长报告。";
      }
    }

    var real = plots.filter(function (p) { return p.source === "device"; }).length;
    $("iotNote").textContent = real > 0
      ? real + " 台真实设备上报中"
      : "演示数据（接入 ESP32 后自动切换）";
  }

  /* ---------------- 对话 ---------------- */
  function addMessage(role, html) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    wrap.innerHTML = '<div class="avatar">' + (role === "user" ? "我" : "耕") + "</div>" +
      '<div class="bubble">' + html + "</div>";
    $("messages").appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function scrollBottom() {
    var m = $("messages");
    m.scrollTop = m.scrollHeight;
  }

  function ask(text) {
    if (state.busy || !text.trim()) return;
    state.busy = true;
    $("sendBtn").disabled = true;
    $("input").value = "";
    $("input").style.height = "auto";

    addMessage("user", "<p>" + esc(text) + "</p>");

    var holder = addMessage("bot", '<div class="typing"><i></i><i></i><i></i></div>');

    api("/api/chat", {
      method: "POST",
      body: { message: text, history: state.history.slice(-6) }
    }).then(function (d) {
      state.busy = false;
      $("sendBtn").disabled = false;
      if (!d.ok) {
        holder.querySelector(".bubble").innerHTML =
          "<p>出错了：" + esc(d.error || "未知错误") + "</p>";
        return;
      }
      var bubble = holder.querySelector(".bubble");
      var html = renderMarkdown(d.answer);

      var tools = d.tools || [];
      if (tools.length) {
        html += '<div class="toolrow">';
        tools.forEach(function (t) {
          html += '<span class="tool">' + (t.ok ? "✓" : "✗") + " " + esc(t.name) + "</span>";
        });
        html += "</div>";
      }

      var cites = d.citations || [];
      if (cites.length) {
        html += '<details class="meta"><summary>引用来源（' + cites.length + " 条）· " +
          (d.mode === "llm" ? "大模型模式" : "本地检索模式") + "</summary>";
        cites.forEach(function (c, idx) {
          html += '<div class="cite"><b>[' + (idx + 1) + "] " + esc(c.label) +
            "</b>（相关度 " + c.score + "）<br>" + esc(c.snippet) + "</div>";
        });
        html += "</details>";
      } else {
        html += '<div class="meta">' +
          (d.mode === "llm" ? "大模型模式" : "本地检索模式") + "</div>";
      }

      bubble.innerHTML = html;
      state.history.push({ role: "user", content: text });
      state.history.push({ role: "assistant", content: d.answer });
      scrollBottom();
    }).catch(function () {
      state.busy = false;
      $("sendBtn").disabled = false;
      holder.querySelector(".bubble").innerHTML =
        "<p>无法连接服务，请确认 app.py 正在运行。</p>";
    });
  }

  $("chatForm").addEventListener("submit", function (e) {
    e.preventDefault();
    ask($("input").value);
  });

  $("input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(this.value);
    }
  });

  $("input").addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 150) + "px";
  });

  $("chips").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (b && b.dataset.q) ask(b.dataset.q);
  });

  /* ---------------- 启动 ---------------- */
  if (state.token) {
    api("/api/plots").then(function (d) {
      if (d.ok) enterApp(); else showGate();
    }).catch(function () { showGate("无法连接服务"); });
  } else {
    showGate();
    loadMeta();
  }
})();
