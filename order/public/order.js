/* Saigon Spices — QR order & pay. JS thuần, không build.
   Thanh toán: Square Web Payments SDK (thẻ / Apple Pay / Google Pay). SDK sinh mã thẻ
   dùng 1 lần trên trình duyệt; server chỉ nhận mã đó, không bao giờ thấy số thẻ. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var RAW_T = (new URLSearchParams(location.search).get("t") || "").trim();
  var TABLE = normTable(RAW_T);            // "21" | "TA" | null
  var IS_TA = TABLE === "TA";

  var CFG = {};                            // từ /api/menu
  var menu = [];                           // nhóm đã sắp
  var cart = [];                           // [{key, item, variation, mods:[opt], qty, note, unit}]
  var sel = null;
  var dining = IS_TA ? "away" : "here";
  var payments = null, card = null, gpay = null, apay = null, payReq = null;
  var busy = false;

  function normTable(s) {
    if (!s) return null;
    s = s.replace(/^(table|tbl|t)\s*(?=\d)/i, "").replace(/[\s\-_]/g, "").toUpperCase();
    if (/^\d{1,3}$/.test(s)) return String(parseInt(s, 10));
    if (/^TA\d*$/.test(s) || s === "TAKEAWAY") return "TA";
    return null;
  }
  function money(c) { return "$" + (c / 100).toFixed(2); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function openSheet(s) { s.hidden = false; document.body.style.overflow = "hidden"; }
  function closeSheet(s) {
    s.hidden = true; document.body.style.overflow = "";
    if (s === $("cartSheet")) teardownPay();
  }
  document.addEventListener("click", function (e) {
    var h = e.target.closest && e.target.closest("[data-close]");
    if (h) closeSheet(h.closest(".sheet"));
  });

  // ---------------------------------------------------------------- menu
  function itemOff(it) {
    if (!CFG.open) return "Closed";
    if (it._lunch && !CFG.lunch_open) return "Mon–Fri lunch only";
    if (it.variations.every(function (v) { return v.sold_out; })) return "Sold out";
    return "";
  }

  function render() {
    var nav = $("gnav"), box = $("menu");
    nav.innerHTML = ""; box.innerHTML = "";
    menu.forEach(function (g, gi) {
      var b = el("button", gi === 0 ? "on" : "", g.name);
      b.type = "button";
      b.onclick = function () {
        var t = $("g" + gi).getBoundingClientRect().top + window.pageYOffset - 120;
        window.scrollTo(0, Math.max(0, t));
      };
      nav.appendChild(b);

      var h = el("div", "gtitle"); h.id = "g" + gi;
      h.appendChild(el("h2", null, g.name));
      if (g.tag === "lunch") h.appendChild(el("small", null, "Mon–Fri " + fmtHours(CFG.lunch_hours)));
      box.appendChild(h);

      g.items.forEach(function (it) {
        var off = itemOff(it);
        var c = el("button", "card" + (off && off !== "Closed" ? " off" : "")); c.type = "button";
        var m = el("div", "card-main");
        var n = el("div", "card-name", it.name);
        if (off && off !== "Closed") n.appendChild(el("span", "tag", off));
        m.appendChild(n);
        if (it.description) m.appendChild(el("p", "card-desc", it.description));
        m.appendChild(el("div", "card-price", money(minPrice(it))));
        c.appendChild(m);
        c.appendChild(art(it));
        c.onclick = function () { openItem(it); };
        box.appendChild(c);
      });
    });
    spy();
  }

  function minPrice(it) {
    return Math.min.apply(null, it.variations.map(function (v) { return v.price; }));
  }
  function art(it) {
    if (it.image) {
      var i = el("img", "card-img"); i.src = it.image; i.alt = ""; i.loading = "lazy";
      i.onerror = function () { i.replaceWith(mono(it)); };
      return i;
    }
    return mono(it);
  }
  function mono(it) { return el("div", "mono", (it.name || "S")[0].toUpperCase()); }
  function fmtHours(h) {
    return String(h || "").split("-").map(function (x) {
      var p = x.split(":"), hh = +p[0], mm = p[1];
      var ap = hh >= 12 ? "pm" : "am", h12 = hh % 12 || 12;
      return h12 + (mm && mm !== "00" ? ":" + mm : "") + ap;
    }).join("–");
  }

  var spyOn = false;
  function spy() {
    if (spyOn) return; spyOn = true;
    window.addEventListener("scroll", function () {
      var ts = document.querySelectorAll(".gtitle"), k = 0;
      for (var i = 0; i < ts.length; i++) if (ts[i].getBoundingClientRect().top < 140) k = i;
      var bs = $("gnav").children;
      for (var j = 0; j < bs.length; j++) bs[j].className = j === k ? "on" : "";
    }, { passive: true });
  }

  // ---------------------------------------------------------------- món
  function openItem(it) {
    sel = { item: it, variation: it.variations.find(function (v) { return !v.sold_out; }) || it.variations[0],
            picks: {}, qty: 1 };
    $("siName").textContent = it.name;
    $("siDesc").textContent = it.description || "";
    $("siDesc").hidden = !it.description;
    $("siNote").value = "";
    $("siQty").textContent = "1";
    var img = $("siImg");
    if (it.image) { img.src = it.image; img.hidden = false; } else { img.hidden = true; }

    var box = $("siMods"); box.innerHTML = "";
    if (it.variations.length > 1) {
      box.appendChild(modGroup({ id: "_var", name: "Size", min: 1, max: 1,
        options: it.variations.map(function (v) { return { id: v.id, name: v.name, price: v.price, sold_out: v.sold_out, _abs: true }; }) }));
      sel.picks._var = [sel.variation.id];
    }
    it.modifiers.forEach(function (ml) {
      sel.picks[ml.id] = [];
      box.appendChild(modGroup(ml));
    });
    refreshMods();
    openSheet($("itemSheet"));
  }

  function modGroup(ml) {
    var g = el("div", "mgroup"); g.dataset.id = ml.id;
    var h = el("div", "mhead");
    h.appendChild(el("b", null, ml.name));
    var sub = ml.min > 0 ? (ml.max === 1 ? "Required" : "Choose " + ml.min + (ml.max > ml.min ? "–" + ml.max : ""))
                         : (ml.max === 1 ? "Optional" : "Optional · up to " + ml.max);
    var req = el("span", "req", sub); req.dataset.req = ml.min > 0 ? "1" : "";
    h.appendChild(req);
    g.appendChild(h);
    var single = ml.max === 1;
    ml.options.forEach(function (o) {
      var lab = el("label", "opt" + (o.sold_out ? " dis" : ""));
      var inp = document.createElement("input");
      inp.type = single ? "radio" : "checkbox";
      inp.name = "m_" + ml.id;
      inp.value = o.id;
      inp.disabled = !!o.sold_out;
      if (ml.id === "_var" && sel.variation.id === o.id) inp.checked = true;
      inp.onchange = function () {
        var cur = sel.picks[ml.id] || [];
        if (single) cur = inp.checked ? [o.id] : [];
        else if (inp.checked) cur = cur.concat([o.id]);
        else cur = cur.filter(function (x) { return x !== o.id; });
        sel.picks[ml.id] = cur;
        if (ml.id === "_var") sel.variation = sel.item.variations.find(function (v) { return v.id === o.id; });
        refreshMods();
      };
      lab.appendChild(inp);
      lab.appendChild(el("span", null, o.name + (o.sold_out ? " (sold out)" : "")));
      if (o._abs) lab.appendChild(el("em", null, money(o.price)));
      else if (o.price) lab.appendChild(el("em", null, "+" + money(o.price)));
      g.appendChild(lab);
    });
    return g;
  }

  function refreshMods() {
    var it = sel.item, missing = 0;
    it.modifiers.forEach(function (ml) {
      var n = (sel.picks[ml.id] || []).length;
      var g = document.querySelector('.mgroup[data-id="' + ml.id + '"]');
      if (!g) return;
      var req = g.querySelector(".req");
      if (ml.min > 0) req.className = "req" + (n >= ml.min ? " ok" : "");
      if (n < ml.min) missing++;
      if (ml.max > 1) {   // khoá ô còn lại khi đã chọn đủ max
        g.querySelectorAll("input").forEach(function (i) {
          if (!i.checked) i.disabled = n >= ml.max || i.closest(".opt").classList.contains("dis");
        });
      }
    });
    var off = itemOff(it);
    var btn = $("siAdd");
    if (off) { btn.disabled = true; btn.textContent = off === "Closed" ? "Ordering closed" : off; return; }
    btn.disabled = missing > 0;
    btn.textContent = missing ? "Choose required options" : "Add · " + money(unitPrice() * sel.qty);
  }
  function selectedOpts() {
    var out = [];
    sel.item.modifiers.forEach(function (ml) {
      (sel.picks[ml.id] || []).forEach(function (id) {
        var o = ml.options.find(function (x) { return x.id === id; });
        if (o) out.push(o);
      });
    });
    return out;
  }
  function unitPrice() {
    return sel.variation.price + selectedOpts().reduce(function (s, o) { return s + o.price; }, 0);
  }
  $("siMinus").onclick = function () { if (sel.qty > 1) { sel.qty--; $("siQty").textContent = sel.qty; refreshMods(); } };
  $("siPlus").onclick = function () { if (sel.qty < 20) { sel.qty++; $("siQty").textContent = sel.qty; refreshMods(); } };
  $("siAdd").onclick = function () {
    var mods = selectedOpts(), note = $("siNote").value.trim();
    var key = [sel.variation.id].concat(mods.map(function (o) { return o.id; }).sort()).join("|") + "|" + note;
    var same = cart.find(function (l) { return l.key === key; });
    if (same) same.qty = Math.min(20, same.qty + sel.qty);
    else cart.push({ key: key, item: sel.item, variation: sel.variation, mods: mods, qty: sel.qty,
                     note: note, unit: unitPrice() });
    closeSheet($("itemSheet"));
    renderBar();
  };

  // ---------------------------------------------------------------- giỏ
  function total() { return cart.reduce(function (s, l) { return s + l.unit * l.qty; }, 0); }
  function renderBar() {
    var n = cart.reduce(function (s, l) { return s + l.qty; }, 0);
    $("cartBar").hidden = n === 0;
    $("cartCount").textContent = n;
    $("cartTotal").textContent = money(total());
  }
  function lineRow(l, onDel) {
    var r = el("div", "line");
    r.appendChild(el("span", "line-qty", l.qty + "×"));
    var m = el("div", "line-main");
    var nm = l.item.name + (l.item.variations.length > 1 && l.variation.name ? " · " + l.variation.name : "");
    m.appendChild(el("div", "line-name", nm));
    var sub = l.mods.map(function (o) { return o.name; });
    if (l.note) sub.push("“" + l.note + "”");
    if (sub.length) m.appendChild(el("div", "line-sub", sub.join(", ")));
    if (onDel) { var d = el("button", "line-del", "Remove"); d.type = "button"; d.onclick = onDel; m.appendChild(d); }
    r.appendChild(m);
    r.appendChild(el("span", "line-price", money(l.unit * l.qty)));
    return r;
  }
  function renderCart() {
    var box = $("cartList"); box.innerHTML = "";
    cart.forEach(function (l, i) {
      box.appendChild(lineRow(l, function () {
        cart.splice(i, 1); renderBar();
        if (!cart.length) { closeSheet($("cartSheet")); return; }
        renderCart(); setupPay();
      }));
    });
    $("cartSum").textContent = money(total());
    $("cardPay").textContent = "Pay " + money(total());
  }
  $("cartBar").onclick = function () {
    $("payError").hidden = true;
    renderCart();
    openSheet($("cartSheet"));
    setupPay();
  };

  $("diningBlock").hidden = IS_TA;
  $("hereSub").textContent = TABLE && !IS_TA ? "Brought to Table " + TABLE : "";
  $("diningSeg").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    dining = b.dataset.dining;
    [].forEach.call(this.children, function (x) { x.classList.toggle("on", x === b); });
  });
  try { $("cName").value = localStorage.getItem("ss.name") || ""; $("cEmail").value = localStorage.getItem("ss.email") || ""; } catch (e) {}

  // ---------------------------------------------------------------- thanh toán
  function loadSdk() {
    if (window.Square) return Promise.resolve();
    return new Promise(function (ok, bad) {
      var s = document.createElement("script");
      s.src = CFG.env === "sandbox" ? "https://sandbox.web.squarecdn.com/v1/square.js"
                                    : "https://web.squarecdn.com/v1/square.js";
      s.onload = ok; s.onerror = bad;
      document.head.appendChild(s);
    });
  }
  function teardownPay() {
    [card, gpay, apay].forEach(function (m) { try { m && m.destroy && m.destroy(); } catch (e) {} });
    card = gpay = apay = payReq = null;
    $("applePayBtn").hidden = true; $("googlePayBtn").hidden = true; $("googlePayBtn").innerHTML = "";
    $("payOr").hidden = true; $("cardBox").innerHTML = ""; $("cardPay").disabled = true;
  }
  function setupPay() {
    teardownPay();
    if (!CFG.open) { showErr("Sorry, QR ordering is open " + fmtHours(CFG.open_hours) + " daily."); return; }
    if (!CFG.app_id || !CFG.location_id) { showErr("Online payment isn't ready yet. Please order at the counter."); return; }
    var amount = (total() / 100).toFixed(2);
    loadSdk().then(function () {
      if (!payments) payments = window.Square.payments(CFG.app_id, CFG.location_id);
      payReq = payments.paymentRequest({ countryCode: "AU", currencyCode: "AUD",
        total: { amount: amount, label: "Saigon Spices" } });
      payments.card().then(function (c) {
        card = c;
        return c.attach("#cardBox");
      }).then(function () { $("cardPay").disabled = false; })
        .catch(function (e) { console.warn("card", e); showErr("Card payment couldn't load. Please refresh."); });
      payments.applePay(payReq).then(function (a) {
        apay = a; $("applePayBtn").hidden = false; $("payOr").hidden = false;
      }).catch(function () { /* không phải Safari/iPhone, hoặc chưa đăng ký domain */ });
      payments.googlePay(payReq).then(function (g) {
        gpay = g; $("googlePayBtn").hidden = false; $("payOr").hidden = false;
        return g.attach("#googlePayBtn", { buttonColor: "black", buttonSizeMode: "fill", buttonType: "pay" });
      }).catch(function () { $("googlePayBtn").hidden = true; });
    }).catch(function () { showErr("Couldn't load secure payment. Check your connection and refresh."); });
  }
  function showErr(m) { var e = $("payError"); e.textContent = m; e.hidden = !m; }

  function checkName() {
    var n = $("cName").value.trim();
    if (!n) { showErr("Please enter your name first."); $("cName").focus(); return null; }
    return n;
  }

  function pay(method) {
    if (busy) return;
    var name = checkName(); if (!name) return;
    showErr("");
    busy = true;
    var tok;
    if (method === "card") {
      tok = card.tokenize({ amount: (total() / 100).toFixed(2), currencyCode: "AUD", intent: "CHARGE",
        customerInitiated: true, sellerKeyedIn: false, billingContact: { givenName: name } });
    } else {
      tok = (method === "apple" ? apay : gpay).tokenize();
    }
    tok.then(function (r) {
      if (r.status !== "OK") {
        busy = false;
        var msg = (r.errors && r.errors[0] && r.errors[0].message) || "";
        if (r.status !== "Cancel") showErr(msg || "Please check your card details.");
        return;
      }
      $("paying").hidden = false;
      return fetch("/api/checkout", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          t: TABLE, dining: dining, name: name, note: $("cNote").value.trim(),
          email: $("cEmail").value.trim(),
          source_id: r.token, verification_token: (r.details && r.details.verificationToken) || null,
          total: total(),
          idem: "q" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10),
          items: cart.map(function (l) {
            return { variation_id: l.variation.id, qty: l.qty, note: l.note,
                     modifiers: l.mods.map(function (o) { return o.id; }) };
          })
        })
      }).then(function (res) { return res.json(); }).then(function (d) {
        $("paying").hidden = true; busy = false;
        if (!d.ok) {
          showErr(d.message || "Payment didn't go through. Please try again.");
          if (d.code === "TOTAL_CHANGED") loadMenu();
          setupPay();     // mã thẻ chỉ dùng được 1 lần -> dựng lại form
          return;
        }
        try { localStorage.setItem("ss.name", name); localStorage.setItem("ss.email", $("cEmail").value.trim()); } catch (e) {}
        done(d, name);
      });
    }).catch(function (e) {
      console.warn(e);
      $("paying").hidden = true; busy = false;
      showErr("Connection problem. Check your wifi — if you were charged, please show this screen to our staff.");
    });
  }
  $("cardPay").onclick = function () { pay("card"); };
  $("applePayBtn").onclick = function () { pay("apple"); };
  $("googlePayBtn").addEventListener("click", function () { pay("google"); });

  function done(d, name) {
    closeSheet($("cartSheet"));
    $("dName").textContent = name;
    if (d.dining === "here" && d.table !== "TA") {
      $("dWhere").textContent = "Table " + d.table;
      $("dHint").textContent = "Your food is being prepared — we'll bring it to your table.";
    } else {
      $("dWhere").textContent = "Take away";
      $("dHint").textContent = "We'll call your name when your order is ready.";
    }
    var box = $("dList"); box.innerHTML = "";
    cart.forEach(function (l) { box.appendChild(lineRow(l, null)); });
    $("dSum").textContent = money(d.total);
    var a = $("dReceipt");
    if (d.receipt_url) { a.href = d.receipt_url; a.hidden = false; } else a.hidden = true;
    cart = []; renderBar();
    openSheet($("doneSheet"));
  }
  $("dMore").onclick = function () { closeSheet($("doneSheet")); };

  // ---------------------------------------------------------------- khởi động
  function loadMenu() {
    return fetch("/api/menu").then(function (r) { return r.json(); }).then(function (d) {
      CFG = d;
      var lunch = [], rest = [];
      (d.groups || []).forEach(function (g) {
        g.items.forEach(function (it) { it._lunch = g.tag === "lunch"; });
        (g.tag === "lunch" ? lunch : rest).push(g);
      });
      menu = d.lunch_open ? lunch.concat(rest) : rest.concat(lunch);
      if (!menu.length) { $("menu").innerHTML = '<p class="status">The menu isn\'t ready yet. Please order at the counter.</p>'; return; }
      var b = $("closedBanner");
      b.hidden = !!d.open;
      b.textContent = "QR ordering is open " + fmtHours(d.open_hours) + " daily. You can browse the menu now.";
      render();
    }).catch(function () {
      $("menu").innerHTML = '<p class="status">Couldn\'t load the menu. Check your wifi and refresh.</p>';
    });
  }

  if (!TABLE) {
    $("tablePill").textContent = "No table";
    $("menu").innerHTML = '<p class="status">Please scan the QR code on your table to order.</p>';
    return;
  }
  $("tablePill").textContent = IS_TA ? "Take away" : "Table " + TABLE;
  loadMenu();
  setInterval(function () { if ($("cartSheet").hidden && $("itemSheet").hidden) loadMenu(); }, 5 * 60 * 1000);
})();
