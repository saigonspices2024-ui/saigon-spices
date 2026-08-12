/* Logic chung cho các màn KDS.
   window.KDS_STATION = "kitchen" (màn bếp, có dropdown chọn trạm) | "expo".
   Màn bếp: mỗi máy tự chọn trạm ở dropdown, lưu localStorage (nhớ theo máy).
   Chọn "All stations" (rỗng) = hiện tất cả món mọi trạm. */
(function () {
  const STATION = window.KDS_STATION;                       // "kitchen" | "expo"
  const pickEl = document.getElementById("stationPick");    // dropdown chọn trạm (màn bếp)
  let activeStation = (localStorage.getItem("kds_pick") || "").toLowerCase();  // "" = tất cả
  const TAPPABLE = STATION === "kitchen";                    // màn bếp tick từng món
  const HAS_SERVE = STATION === "expo";                      // chỉ Expo bấm Served
  document.body.classList.add("st-" + STATION);              // để CSS tách bếp/expo

  // Món hiển thị: màn bếp lọc theo trạm đang chọn (rỗng = tất cả); Expo luôn hiện hết.
  function viewItems(t) {
    if (STATION === "kitchen" && activeStation)
      return t.items.filter(it => (it.station || "").toLowerCase() === activeStation);
    return t.items;
  }
  // Món/đơn bị huỷ trên Square — phải ở lại màn cho tới khi có người xác nhận.
  function hasVoid(t) {
    return t.state === "CANCELLED" || viewItems(t).some(it => it.cancelled);
  }
  // Đơn còn hiện trên màn này không.
  function visible(t) {
    if (t.state === "COMPLETED") return false;
    const its = viewItems(t);
    // Vé bị huỷ KHÔNG được lặng lẽ biến mất: món có thể đã nấu xong rồi, bếp
    // phải thấy mà dừng tay. Giữ lại kể cả khi mọi món đã tick xong.
    if (hasVoid(t)) return its.length > 0;
    if (STATION === "kitchen") {
      // Cook đang xem 1 trạm: xong phần trạm mình thì ẩn đơn đi cho gọn màn.
      if (activeStation) return its.length > 0 && its.some(it => !it.done);
      // Xem "All stations": giữ đơn (kể cả tick hết món) để còn hiện nút "✓ Done".
      // Bấm Done -> bump sang READY -> đơn rời màn Kitchen sang Expo (chạy món +
      // thu tiền). Nên ẩn đơn đã READY khỏi màn bếp.
      return t.state !== "READY" && its.length > 0;
    }
    return true;
  }
  const grid = document.getElementById("grid");
  const countEl = document.getElementById("count");
  const clockEl = document.getElementById("clock");
  const conn = document.getElementById("conn");
  const soundBtn = document.getElementById("soundBtn");

  let tickets = [];
  let seen = new Set();          // id đã thấy -> phát hiện đơn mới

  /* ---- Âm thanh báo đơn mới ------------------------------------------
     QUAN TRỌNG: trên iPad, tiếng phát bằng Web Audio API bị **công tắc gạt
     im lặng** làm câm hoàn toàn (không báo lỗi gì), còn thẻ <audio> thì
     không. Nên ở đây chỉ dùng Web Audio để *dựng* tiếng chuông một lần rồi
     đóng thành file WAV, còn lúc kêu thì phát qua <audio> cho chắc ăn. */
  let soundOn = localStorage.getItem("kds_sound") !== "off";  // mặc định BẬT
  let audioReady = false;   // đã phát được ít nhất 1 lần (iOS đòi 1 cử chỉ)
  let primed = false;       // lượt tải trang đầu: không kêu cho đơn đang có sẵn
  const chimeEl = new Audio();
  chimeEl.preload = "auto";
  chimeEl.volume = 1;
  // Tiếng báo HUỶ đơn — cố ý khác hẳn chuông đơn mới (thấp dần, nghe là biết
  // có chuyện), để bếp không nhầm đơn bị void thành đơn mới về.
  const alertEl = new Audio();
  alertEl.preload = "auto";
  alertEl.volume = 1;

  function updateSoundBtn() {
    soundBtn.classList.toggle("on", soundOn);
    soundBtn.textContent = soundOn ? "🔊 Sound: On" : "🔇 Sound: Off";
    soundBtn.title = "Tap to hear the chime · hold to mute/unmute";
  }
  updateSoundBtn();

  // Lời nhắc "chạm để bật âm" — hiện khi muốn bật mà chưa phát được lần nào.
  const soundPrompt = document.createElement("div");
  soundPrompt.className = "sound-prompt";
  soundPrompt.textContent = STATION === "expo"
    ? "🔔 Tap the screen to turn on the pick-up chime"
    : "🔔 Tap the screen to turn on the new-order chime";
  document.body.appendChild(soundPrompt);
  function refreshPrompt() {
    soundPrompt.classList.toggle("show", soundOn && !audioReady);
  }

  /* --- Dựng sẵn tiếng chuông thành WAV (chạy 1 lần lúc mở trang) ------ */

  // Một tiếng chuông: nhiều bồi âm hình sin tắt dần khác nhau -> nghe ra chất
  // chuông/thanh gõ chứ không phải tiếng "bíp" điện tử.
  function bell(ctx, out, t0, freq, dur, vol) {
    // [bội số tần số, độ to, hệ số tắt dần] — bồi âm càng cao tắt càng nhanh.
    const partials = [[1, 1, 1], [2, 0.45, 0.7], [2.99, 0.28, 0.5],
                      [4.21, 0.16, 0.35], [5.43, 0.09, 0.25]];
    for (const [ratio, amp, decay] of partials) {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = freq * ratio;
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(vol * amp, t0 + 0.006);    // gõ vào: rất nhanh
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur * decay); // ngân dài
      o.connect(g); g.connect(out);
      o.start(t0);
      o.stop(t0 + dur * decay + 0.05);
    }
  }

  function encodeWav(buf) {
    const d = buf.getChannelData(0), n = d.length;
    const ab = new ArrayBuffer(44 + n * 2), v = new DataView(ab);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, buf.sampleRate, true); v.setUint32(28, buf.sampleRate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, d[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([ab], { type: "audio/wav" });
  }

  // Màn bếp: chuông này báo ĐƠN MỚI (A5->E6 đi lên). Màn Expo: KHÔNG báo đơn
  // mới — chuông này dùng cho MÓN SẴN SÀNG RA BÀN (bếp vừa tick xong 1 món), cố
  // ý cao & gọn hơn (C6->G6) để nghe khác hẳn cả đơn mới lẫn tiếng huỷ đi xuống.
  (function buildChime() {
    const OC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!OC) return;
    const rate = 44100, ctx = new OC(1, rate * 2, rate);
    // master -> compressor: hai tiếng chuông chồng nhau vượt biên độ 1.0 (rè);
    // compressor ghìm đỉnh lại mà vẫn giữ độ to.
    const master = ctx.createGain(); master.gain.value = 1;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -10; comp.ratio.value = 12;
    comp.attack.value = 0.003; comp.release.value = 0.25;
    master.connect(comp); comp.connect(ctx.destination);
    if (STATION === "expo") {
      bell(ctx, master, 0.02, 1046.5, 0.7, 0.55);  // C6
      bell(ctx, master, 0.20, 1568.0, 1.1, 0.6);   // G6 -> "đinh-đích" cao, báo pick-up
    } else {
      bell(ctx, master, 0.02, 880.0, 1.3, 0.5);    // A5
      bell(ctx, master, 0.22, 1318.5, 1.8, 0.55);  // E6  -> "đing-đong" đi lên
    }
    const done = ctx.startRendering();
    if (done && done.then) done.then(b => { chimeEl.src = URL.createObjectURL(encodeWav(b)); });
  })();

  // Tiếng báo huỷ: ba tiếng gõ ĐI XUỐNG (C5 -> A4 -> F4), tiếng cuối ngân dài.
  // Chuông đơn mới đi lên nghe vui; cái này đi xuống nghe như báo động.
  (function buildAlert() {
    const OC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!OC) return;
    const rate = 44100, ctx = new OC(1, rate * 2, rate);
    const master = ctx.createGain(); master.gain.value = 1;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -10; comp.ratio.value = 12;
    comp.attack.value = 0.003; comp.release.value = 0.25;
    master.connect(comp); comp.connect(ctx.destination);
    bell(ctx, master, 0.02, 523.25, 0.55, 0.55);   // C5
    bell(ctx, master, 0.24, 440.00, 0.55, 0.55);   // A4
    bell(ctx, master, 0.46, 349.23, 1.40, 0.60);   // F4 — ngân dài
    const done = ctx.startRendering();
    if (done && done.then) done.then(b => { alertEl.src = URL.createObjectURL(encodeWav(b)); });
  })();

  let lastAlarmAt = 0;
  function alarm() {
    if (!soundOn || !alertEl.src) return;
    if (Date.now() - lastAlarmAt < 800) return;   // nhiều món bị void 1 lượt -> 1 tiếng
    lastAlarmAt = Date.now();
    try { alertEl.currentTime = 0; } catch (_) {}
    const p = alertEl.play();
    if (p && p.catch) p.catch(() => {});
  }

  // Kêu chuông. Trả về promise để chỗ gọi biết iOS có chặn hay không.
  let lastBeepAt = 0;
  function beep() {
    if (!soundOn || !chimeEl.src) return Promise.resolve(false);
    // Nhiều đơn về cùng lúc, hoặc chạm-mở-khoá trùng với bấm nút -> chỉ 1 tiếng.
    if (Date.now() - lastBeepAt < 500) return Promise.resolve(true);
    lastBeepAt = Date.now();
    try { chimeEl.currentTime = 0; } catch (_) {}
    const p = chimeEl.play();
    if (!p || !p.then) { audioReady = true; refreshPrompt(); return Promise.resolve(true); }
    return p.then(() => { audioReady = true; refreshPrompt(); return true; })
            .catch(() => { audioReady = false; refreshPrompt(); return false; });
  }

  // Chạm đầu tiên ở bất kỳ đâu cũng mở khoá tiếng (yêu cầu của iOS/Chrome) —
  // và kêu luôn 1 tiếng để người dùng BIẾT là âm thanh đã chạy.
  ["pointerdown", "touchend", "keydown"].forEach(ev =>
    document.addEventListener(ev, () => { if (soundOn && !audioReady) beep(); }));

  soundBtn.addEventListener("click", () => {
    // Đang bật thì bấm = nghe thử (không tắt nhầm); muốn tắt thì bấm giữ.
    if (soundOn && audioReady) { beep(); return; }
    soundOn = true;
    localStorage.setItem("kds_sound", "on");
    updateSoundBtn();
    beep();
    refreshPrompt();
  });

  // Bấm giữ ~0,6s trên nút = tắt/bật hẳn âm báo (tránh lỡ tay tắt mất chuông).
  let holdTimer = null;
  const startHold = () => {
    holdTimer = setTimeout(() => {
      holdTimer = null;
      soundOn = !soundOn;
      localStorage.setItem("kds_sound", soundOn ? "on" : "off");
      updateSoundBtn();
      if (soundOn) beep(); else soundPrompt.classList.remove("show");
      refreshPrompt();
    }, 600);
  };
  const cancelHold = () => { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } };
  soundBtn.addEventListener("pointerdown", startHold);
  ["pointerup", "pointerleave", "pointercancel"].forEach(ev =>
    soundBtn.addEventListener(ev, cancelHold));
  refreshPrompt();

  /* ---- Dropdown chọn trạm (màn bếp) — nhớ theo máy qua localStorage - */
  if (pickEl) {
    fetch("/api/stations").then(r => r.json()).then(d => {
      const opts = ['<option value="">All stations</option>'].concat(
        (d.stations || []).map(s => `<option value="${esc(s.toLowerCase())}">${esc(s)}</option>`));
      pickEl.innerHTML = opts.join("");
      pickEl.value = activeStation;
      render();
    }).catch(() => {});
    pickEl.addEventListener("change", () => {
      activeStation = pickEl.value.toLowerCase();
      localStorage.setItem("kds_pick", activeStation);
      seen.clear();          // đổi trạm -> không kêu báo nhầm cho đơn đang có
      render();
    });
  }

  /* ---- Đồng hồ ------------------------------------------------------ */
  function tickClock() {
    const d = new Date();
    clockEl.textContent = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  setInterval(tickClock, 1000); tickClock();

  /* ---- Thời gian chờ + tô màu -------------------------------------- */
  function elapsed(ms) {
    const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }
  function urgency(ms) {
    const min = (Date.now() - ms) / 60000;
    if (min >= 10) return "late";
    if (min >= 5) return "warn";
    return "ok";
  }

  /* ---- Render ------------------------------------------------------- */
  function typeLabel(t) {
    return { DINE_IN: "Dine-in", TAKEAWAY: "Takeaway", DELIVERY: "Delivery",
             UNKNOWN: "No table" }[t] || t;
  }
  // Square trả tiền dạng cent + mã tiền tệ; định dạng theo đúng đồng của quán.
  function money(m) {
    if (!m || m.amount == null) return "";
    const cur = m.currency || "AUD";
    try {
      return new Intl.NumberFormat("en-AU", { style: "currency", currency: cur })
        .format(m.amount / 100);
    } catch (_) {
      return "$" + (m.amount / 100).toFixed(2);
    }
  }

  function whereLabel(t) {
    // Bàn thật -> "Table 5". Bàn ảo khu takeaway đã có nhãn sẵn ("Takeaway 3").
    if (t.table) return t.type === "DINE_IN" ? "Table " + t.table : t.table;
    if (t.guest) return t.guest;          // vé takeaway: tên khách, KHÔNG phải bàn
    const s = t.source || "";
    if (s && s.length <= 18 && !/sandbox/i.test(s)) return s;
    return typeLabel(t.type);
  }

  function render() {
    const mine = tickets.filter(visible).sort((a, b) => a.received_at - b.received_at);

    countEl.textContent = mine.length + (mine.length === 1 ? " order" : " orders");

    if (mine.length === 0) {
      grid.innerHTML = `<div class="empty">No orders yet — waiting for Square…</div>`;
      return;
    }

    grid.innerHTML = renderGroups(mine);
    // đánh dấu đã thấy
    mine.forEach(t => seen.add(t.id));
  }

  // Gom vé theo bàn: dine-in cùng số bàn -> MỘT nhóm (nhìn phát biết cả bàn, đơn
  // của người tới trễ không lạc). Takeaway / không bàn -> đứng riêng. Nhóm từ 2
  // người trở lên có nút "Pay together" (chỉ Expo) để một người bao cả bàn, trả
  // một lần.
  function renderGroups(mine) {
    const groups = [], byKey = new Map();
    mine.forEach(t => {
      const key = (t.type === "DINE_IN" && t.table) ? "tbl:" + t.table : "solo:" + t.id;
      let g = byKey.get(key);
      if (!g) { g = { table: (t.type === "DINE_IN" ? t.table : null), tickets: [] }; byKey.set(key, g); groups.push(g); }
      g.tickets.push(t);
    });
    return groups.map(g => {
      if (g.tickets.length < 2) return ticketHTML(g.tickets[0]);
      const inner = g.tickets.map(ticketHTML).join("");
      const payable = g.tickets.filter(t => t.qr && !t.paid && t.due && t.due.amount > 0);
      const together = (HAS_SERVE && payable.length >= 2)
        ? `<button class="tbl-pay" data-paygroup="${esc(g.table)}">💵 Pay together · ${payable.length}</button>` : "";
      return `<div class="tbl-group" data-table="${esc(g.table)}">` +
        `<div class="tbl-head"><span class="tbl-title">Table ${esc(g.table)}</span>` +
        `<span class="tbl-sub">${g.tickets.length} people</span>${together}</div>` +
        `<div class="tbl-cards">${inner}</div></div>`;
    }).join("");
  }

  function ticketHTML(t) {
      const stamp = t.received_at;
      const isNew = !seen.has(t.id);
      const its = viewItems(t);
      const items = its.map(it => {
        // Bếp: chạm để tick 'đã nấu xong'. Expo: chạm món ĐANG NHÁY (bếp done mà
        // chưa served) = đã chạy ra bàn -> hết nháy, gạch mờ.
        const tap = (TAPPABLE && !it.cancelled) ? `data-item="${esc(it.uid)}"`
          : (STATION === "expo" && it.done && !it.served && !it.cancelled) ? `data-serve-item="${esc(it.uid)}"`
          : "";
        return `
        <div class="item ${it.done ? "done" : ""} ${it.served ? "served" : ""} ${it.cancelled ? "voided" : ""} ${it.added_at && !it.done && !it.cancelled ? "justadded" : ""}"
             ${tap}>
          <div class="qty">${it.qty}×</div>
          <div class="detail">
            <div class="name">${esc(it.name)}${it.added_at && !it.done && !it.cancelled ? `<span class="add-tag">ADDED</span>` : ""}</div>
            ${it.cancelled ? `<div class="void-tag">✕ VOIDED — stop / don't make</div>` : ""}
            ${it.variation ? `<div class="variation">${esc(it.variation)}</div>` : ""}
            ${it.modifiers && it.modifiers.length
              ? `<div class="mods">${it.modifiers.map(m => `<span class="mod">+ ${esc(m)}</span>`).join("")}</div>`
              : ""}
            ${it.note ? `<div class="note">📝 ${esc(it.note)}</div>` : ""}
            ${(STATION === "expo" || (STATION === "kitchen" && !activeStation)) && it.station
              ? `<span class="stn stn-${esc((it.station || "").toLowerCase())}">${esc(it.station)}</span>` : ""}
          </div>
          <div class="side">
            ${it.price ? `<div class="price">${esc(money(it.price))}</div>` : ""}
            <div class="check ${TAPPABLE ? "" : "ro"}">${it.done ? "✓" : ""}</div>
          </div>
        </div>`;
      }).join("");

      // Món đã bị void thì không tính vào "cả đơn xong chưa" nữa.
      const live = its.filter(it => !it.cancelled);
      const allDone = live.length > 0 && live.every(it => it.done);
      const allServed = live.length > 0 && live.every(it => it.served);
      const voided = hasVoid(t);
      const killed = t.state === "CANCELLED";       // huỷ cả đơn (khác void lẻ 1 món)

      // Vé có tin huỷ thì nút xác nhận đè lên mọi nút khác — đọc xong mới dọn.
      // Nút đóng đơn:
      //  • Expo = quyền cao nhất: bấm Served lúc nào cũng được (kể cả bếp chưa
      //    tick hết). Chưa xong hết thì nút để kiểu viền (early) nhắc nhưng vẫn bấm.
      //  • Kitchen xem "All stations": hiện "✓ Done" KHI đã tick hết món -> tiệm
      //    1 iPad không cần màn Expo riêng vẫn đóng được đơn + đẩy vào History.
      // THANH TOÁN: chỉ trên Expo (mô hình 2 màn — Bếp nấu, Expo chạy món + thu tiền).
      const payBtn = (HAS_SERVE && !voided && t.qr && !t.paid && t.due && t.due.amount > 0)
        ? `<button class="bump pay" data-pay="${t.id}">💵 ${esc(money(t.due))}</button>` : "";
      // Kitchen "All stations": nấu xong hết -> "Done" = bếp báo XONG, đẩy đơn qua
      // Expo để chạy món + thu tiền. Bump sang READY, KHÔNG đóng bill (tiền vẫn thu
      // ở Expo) -> an toàn kể cả đơn QR chưa trả. Giữ Acknowledge cho đơn bị huỷ.
      const kitchenDone = STATION === "kitchen" && !activeStation && allDone && !voided;
      // Expo: nút "✓ Served" = đã giao HẾT món ra bàn. Chỉ hiện khi bếp đã nấu
      // xong hết (allDone) mà chưa giao hết. Bấm KHÔNG làm mất vé ngay — đơn chỉ
      // rời màn khi ĐÃ TRẢ TIỀN + ĐÃ GIAO HẾT (xem maybe_complete ở server).
      const expoServed = HAS_SERVE && !voided && allDone && !allServed;
      let footBtn = "";
      if (voided) {
        footBtn = `<button class="bump ack" data-ack="${t.id}">Acknowledge</button>`;
      } else if (kitchenDone) {
        footBtn = `<button class="bump serve" data-kdone="${t.id}">✓ Done</button>`;
      } else if (expoServed) {
        footBtn = `<button class="bump serve ${t.paid ? "" : "early"}" data-serve="${t.id}">✓ Served</button>`;
      }
      const foot = (payBtn || footBtn) ? `<div class="ticket-foot">${payBtn}${footBtn}</div>` : "";
      return `
        <div class="ticket t-${esc((t.type || "").toLowerCase())} ${isNew ? "flash" : ""} ${allDone && !voided ? "alldone" : ""} ${killed ? "cancelled" : voided ? "hasvoid" : ""}" data-id="${t.id}">
          <div class="ticket-head">
            ${t.redo ? `<span class="badge REDO">↻ REDO</span>` : ""}
            ${t.type === "DINE_IN" ? "" :
              `<span class="badge ${t.type}">${typeLabel(t.type)}</span>`}
            ${t.paid ? `<span class="badge paid">PAID</span>` : ""}
            <span class="where">${esc(whereLabel(t))}</span>
            <span class="spacer"></span>
            <span class="timer ${urgency(stamp)}" data-stamp="${stamp}">${elapsed(stamp)}</span>
          </div>
          ${t.cust_name ? `<div class="cust">👤 ${esc(t.cust_name)}${t.cust_phone ? `<span class="ph"> · ${esc(t.cust_phone)}</span>` : ""}</div>` : ""}
          ${killed ? `<div class="cancel-banner">✕ ORDER CANCELLED — stop cooking</div>` : ""}
          ${t.order_note ? `<div class="order-note">⚠ ${esc(t.order_note)}</div>` : ""}
          <div class="ticket-body">${items}</div>
          ${foot}
        </div>`;
  }

  function refreshTimers() {
    document.querySelectorAll(".timer").forEach(el => {
      const stamp = Number(el.dataset.stamp);
      el.textContent = elapsed(stamp);
      el.className = "timer " + urgency(stamp);
    });
  }
  setInterval(refreshTimers, 1000);

  /* ---- Bump / Undo -------------------------------------------------- */
  grid.addEventListener("click", async (e) => {
    const payId = e.target.closest("[data-pay]")?.dataset.pay;
    const payGroup = e.target.closest("[data-paygroup]")?.dataset.paygroup;
    const ackId = e.target.closest("[data-ack]")?.dataset.ack;
    const serveId = e.target.closest("[data-serve]")?.dataset.serve;
    const kdoneId = e.target.closest("[data-kdone]")?.dataset.kdone;
    const itemEl = e.target.closest("[data-item]");
    const serveItemEl = e.target.closest("[data-serve-item]");
    if (payId) {
      const t = tickets.find(x => x.id === payId);
      if (t) openPay(t);
      return;
    }
    if (payGroup) {
      openPayGroup(payGroup);
      return;
    }
    if (ackId) {
      e.target.disabled = true;
      // Gửi kèm trạm đang xem: trạm này xác nhận xong không xoá mất tin huỷ
      // của trạm khác. Expo / "All stations" gửi rỗng = dọn hết.
      await post(`/api/tickets/${ackId}/ack-cancel`,
                 { ack_station: STATION === "kitchen" ? activeStation : "" });
    } else if (serveId) {
      e.target.disabled = true;
      await post(`/api/tickets/${serveId}/serve`);
    } else if (kdoneId) {
      // Bếp "Done": nấu xong -> bump sang READY, đơn rời màn Kitchen sang Expo.
      // KHÔNG đóng bill (tiền thu ở Expo).
      e.target.disabled = true;
      await post(`/api/tickets/${kdoneId}/bump`, { station: "kitchen" });
    } else if (itemEl) {
      const tid = itemEl.closest(".ticket")?.dataset.id;
      const uid = itemEl.dataset.item;
      if (tid) {
        itemEl.classList.toggle("done");   // phản hồi tức thì, SSE sẽ xác nhận
        await post(`/api/tickets/${tid}/item-toggle`, { uid });
      }
    } else if (serveItemEl) {
      // Expo chạm món đang nháy = đã chạy ra bàn -> hết nháy.
      const tid = serveItemEl.closest(".ticket")?.dataset.id;
      const uid = serveItemEl.dataset.serveItem;
      if (tid) {
        serveItemEl.classList.add("served");   // phản hồi tức thì
        await post(`/api/tickets/${tid}/item-serve`, { uid });
      }
    }
  });

  async function post(url, extra) {
    try {
      await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ station: STATION, ...(extra || {}) }),
      });
    } catch (_) { /* SSE sẽ đồng bộ lại */ }
  }

  /* ---- SSE real-time + chống ngắt kết nối --------------------------- */
  let es = null;
  let lastEventAt = Date.now();

  // Mỗi tin huỷ (cả đơn, hoặc từng món) có một khoá riêng -> chỉ kêu 1 lần.
  let alertedCancel = new Set();
  function cancelKeys(list) {
    const keys = [];
    list.forEach(t => {
      if (t.state === "CANCELLED") keys.push("t:" + t.id);
      viewItems(t).forEach(it => { if (it.cancelled) keys.push("i:" + t.id + ":" + it.uid); });
    });
    return keys;
  }

  // Món được thêm vào vé đã có (khách gọi thêm) — mỗi món chỉ kêu một lần.
  let alertedAdds = new Set();
  function addedKeys(list) {
    const keys = [];
    list.forEach(t => {
      viewItems(t).forEach(it => {
        if (it.added_at && !it.done) keys.push(t.id + ":" + it.uid);
      });
    });
    return keys;
  }

  // Expo: món bếp vừa tick xong (sẵn sàng ra bàn) — mỗi món chỉ kêu một lần.
  // Món un-tick rồi tick lại sẽ rơi khỏi set nên kêu lại (đúng: có món ra tiếp).
  let alertedReady = new Set();
  function readyKeys(list) {
    const keys = [];
    list.forEach(t => {
      if (t.state === "CANCELLED") return;        // đơn huỷ không tính là món ra
      viewItems(t).forEach(it => {
        if (it.done && !it.cancelled) keys.push(t.id + ":" + it.uid);
      });
    });
    return keys;
  }

  function applyTickets(list) {
    const before = new Set(tickets.filter(visible).map(t => t.id));
    tickets = list;
    const shown = tickets.filter(visible);
    const hasNew = shown.some(t => !before.has(t.id) && !seen.has(t.id));
    const keys = cancelKeys(shown);
    const newCancel = keys.some(k => !alertedCancel.has(k));
    alertedCancel = new Set(keys);   // tin đã dọn thì bỏ khoá, khỏi phình mãi
    // Khách gọi thêm giữa bữa: món mới rơi vào vé đang nằm sẵn trên màn, không
    // kêu thì bếp chỉ phát hiện khi tình cờ nhìn lại vé cũ.
    const adds = addedKeys(shown);
    const hasAdded = adds.some(k => !alertedAdds.has(k));
    alertedAdds = new Set(adds);
    // Expo: bếp vừa tick xong 1 món -> có món ra pass, kêu chuông pick-up.
    const ready = readyKeys(shown);
    const hasReady = ready.some(k => !alertedReady.has(k));
    alertedReady = new Set(ready);
    render();
    // Ưu tiên: huỷ đơn > (Expo) món ra pass / (Bếp) đơn mới.
    // Màn Expo KHÔNG kêu cho đơn mới — chỉ kêu khi có món sẵn sàng ra bàn.
    if (newCancel && primed) alarm();
    else if (primed) {
      if (STATION === "expo") { if (hasReady) beep(); }
      else if (hasNew || hasAdded) beep();
    }
    primed = true;   // lần đầu chỉ nạp đơn đang có, không coi là đơn mới
  }

  function connect() {
    try { if (es) es.close(); } catch (_) {}
    es = new EventSource("/api/stream");
    es.onopen = () => { conn.classList.add("online"); lastEventAt = Date.now(); };
    es.onerror = () => { conn.classList.remove("online"); };  // EventSource tự thử lại
    es.onmessage = (ev) => {
      conn.classList.add("online");
      lastEventAt = Date.now();
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ping") return;          // nhịp tim giữ kết nối
      if (msg.type !== "tickets") return;
      applyTickets(msg.tickets);
    };
  }
  connect();

  // Watchdog: nếu quá 30s không nhận được gì (kể cả nhịp tim) → nối lại.
  setInterval(() => {
    if (!es || es.readyState === 2 /* CLOSED */ || Date.now() - lastEventAt > 30000) {
      conn.classList.remove("online");
      connect();
    }
  }, 5000);

  // Lưới an toàn: cứ 12s tự lấy đơn 1 lần (phòng khi SSE kẹt mà không báo lỗi).
  setInterval(async () => {
    try {
      const r = await fetch("/api/tickets", { cache: "no-store" });
      if (r.ok) { applyTickets((await r.json()).tickets); lastEventAt = Date.now();
                  conn.classList.add("online"); }
    } catch (_) {}
  }, 12000);

  // Giữ màn hình tablet luôn sáng (không tự khoá) khi có thể.
  let wakeLock = null;
  async function keepAwake() {
    try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); }
    catch (_) {}
  }
  keepAwake();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { keepAwake(); connect(); }
  });

  /* ---- PWA: đăng ký service worker để thêm vào màn hình chính ------- */
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

  /* ---- Thanh toán ngay trên Expo (đơn QR) ---------------------------- */
  // Bàn phím số kiểu tích luỹ cent chính là cái chống bấm nhầm: phải gõ số +
  // xác nhận có chủ ý mới ghi, không lỡ tay ra giao dịch ma. Thẻ thì không cần
  // (bấm nhầm vô hại — khách phải chạm thẻ). Tiền mặt tự tính tiền thối.
  // payItems: [{id, label, amount, cur, checked}]. Một người = 1 phần tử (thu
  // riêng); "Pay together" = nhiều phần tử, tick ai thì gộp người đó.
  let payItems = [], payMulti = false, payDue = 0, payCur = "AUD", cashCents = 0, cardTimer = null;
  const payModal = document.createElement("div");
  payModal.className = "pay-modal";
  payModal.hidden = true;
  payModal.innerHTML =
    '<div class="pay-backdrop" data-payclose></div>' +
    '<div class="pay-card">' +
      '<button class="pay-x" data-payclose>✕</button>' +
      '<div class="pay-h" id="payH">Payment</div>' +
      '<div class="pay-due" id="payDueEl"></div>' +
      '<div class="pay-stage" id="payChoose">' +
        '<div class="pay-people" id="payPeople" hidden></div>' +
        '<button class="pay-opt cash" id="payCashBtn">💵 Cash</button>' +
        '<button class="pay-opt card" id="payCardBtn">💳 Card · Terminal</button>' +
      '</div>' +
      '<div class="pay-stage" id="payPad" hidden>' +
        '<div class="pay-lab">Cash received</div>' +
        '<div class="pay-amt" id="payAmt">$0.00</div>' +
        '<div class="pay-info" id="payInfo"></div>' +
        '<div class="pay-quick">' +
          '<button data-q="exact">Exact</button>' +
          '<button data-q="5000">$50</button>' +
          '<button data-q="10000">$100</button>' +
        '</div>' +
        '<div class="pay-keys">' +
          '<button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button>' +
          '<button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button>' +
          '<button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button>' +
          '<button data-k="00">00</button><button data-k="0">0</button><button data-k="del">⌫</button>' +
        '</div>' +
        '<button class="pay-confirm" id="payConfirm" disabled>Confirm cash payment</button>' +
        '<button class="pay-back" data-payback>← Back</button>' +
      '</div>' +
      '<div class="pay-stage" id="payMsg" hidden><div class="pay-state" id="payState"></div></div>' +
      '<p class="pay-err" id="payErr" hidden></p>' +
    '</div>';
  document.body.appendChild(payModal);
  const pq = (sel) => payModal.querySelector(sel);

  function payStage(which) {
    ["#payChoose", "#payPad", "#payMsg"].forEach(s => { pq(s).hidden = (s !== which); });
  }
  function personLabel(t) {
    return t.cust_name || whereLabel(t);
  }
  // Tổng phải thu = cộng các phần tử đang tick.
  function selectedItems() { return payItems.filter(x => x.checked); }
  function recalcDue() {
    const sel = selectedItems();
    payDue = sel.reduce((s, x) => s + (x.amount || 0), 0);
    payCur = (sel[0] && sel[0].cur) || (payItems[0] && payItems[0].cur) || "AUD";
    pq("#payDueEl").textContent = "Amount due " + money({ amount: payDue, currency: payCur });
    // vô hiệu hoá Cash/Card khi chưa chọn ai
    pq("#payCashBtn").disabled = sel.length === 0;
    pq("#payCardBtn").disabled = sel.length === 0;
    if (!pq("#payPad").hidden) cashRender();
  }
  function renderPeople() {
    const box = pq("#payPeople");
    box.hidden = !payMulti;
    if (!payMulti) { box.innerHTML = ""; return; }
    box.innerHTML = payItems.map((x, i) =>
      `<label class="pay-person"><input type="checkbox" data-pi="${i}" ${x.checked ? "checked" : ""}>` +
      `<span class="pp-name">${esc(x.label)}</span>` +
      `<span class="pp-amt">${esc(money({ amount: x.amount, currency: x.cur }))}</span></label>`
    ).join("");
  }
  function openPayItems(items, headline, multi) {
    payItems = items;
    payMulti = !!multi;
    cashCents = 0;
    pq("#payH").textContent = headline;
    pq("#payErr").hidden = true;
    renderPeople();
    recalcDue();
    payStage("#payChoose");
    payModal.hidden = false;
  }
  function openPay(t) {
    openPayItems(
      [{ id: t.id, label: personLabel(t), amount: (t.due && t.due.amount) || 0,
         cur: (t.due && t.due.currency) || "AUD", checked: true }],
      "Payment · " + whereLabel(t), false);
  }
  function openPayGroup(table) {
    const list = tickets.filter(t =>
      t.type === "DINE_IN" && t.table === table && t.qr && !t.paid &&
      t.due && t.due.amount > 0 && visible(t))
      .sort((a, b) => a.received_at - b.received_at);
    if (list.length === 0) return;
    if (list.length === 1) return openPay(list[0]);
    openPayItems(list.map(t => ({
      id: t.id, label: personLabel(t), amount: t.due.amount,
      cur: t.due.currency || "AUD", checked: true,
    })), "Table " + table + " · pay together", true);
  }
  function closePay() {
    payModal.hidden = true;
    payItems = [];
    if (cardTimer) { clearInterval(cardTimer); cardTimer = null; }
  }
  function payFail(msg) {
    pq("#payErr").textContent = msg;
    pq("#payErr").hidden = false;
    payStage("#payChoose");
  }

  // --- tiền mặt ---
  function fmtC(c) { return money({ amount: c, currency: payCur }); }
  function cashRender() {
    pq("#payAmt").textContent = fmtC(cashCents);
    const info = pq("#payInfo"), diff = cashCents - payDue;
    if (cashCents === 0) { info.textContent = ""; info.className = "pay-info"; }
    else if (diff < 0) { info.textContent = "Short " + fmtC(-diff); info.className = "pay-info short"; }
    else { info.textContent = "Change " + fmtC(diff); info.className = "pay-info change"; }
    pq("#payConfirm").disabled = cashCents < payDue || cashCents === 0;
  }
  function cashKey(k) {
    if (k === "del") cashCents = Math.floor(cashCents / 10);
    else if (k === "00") cashCents = Math.min(cashCents * 100, 99999999);
    else cashCents = Math.min(cashCents * 10 + Number(k), 99999999);
    cashRender();
  }
  function cashQuick(q) { cashCents = (q === "exact") ? payDue : Number(q); cashRender(); }

  payModal.addEventListener("click", (e) => {
    if (e.target.closest("[data-payclose]")) return closePay();
    if (e.target.closest("[data-payback]")) return payStage("#payChoose");
    const k = e.target.closest("[data-k]"); if (k) return cashKey(k.dataset.k);
    const q = e.target.closest("[data-q]"); if (q) return cashQuick(q.dataset.q);
  });
  // Tick/bỏ tick người trong "Pay together" -> cập nhật tổng.
  payModal.addEventListener("change", (e) => {
    const cb = e.target.closest("[data-pi]");
    if (!cb) return;
    const i = Number(cb.dataset.pi);
    if (payItems[i]) payItems[i].checked = cb.checked;
    recalcDue();
  });
  {
    pq("#payCashBtn").addEventListener("click", () => { cashCents = 0; cashRender(); payStage("#payPad"); });
    pq("#payConfirm").addEventListener("click", async () => {
      const sel = selectedItems();
      if (cashCents < payDue || sel.length === 0) return;
      const received = cashCents;
      pq("#payConfirm").disabled = true;
      payStage("#payMsg");
      pq("#payState").className = "pay-state wait";
      pq("#payState").textContent = "Recording cash payment…";
      const req = sel.length === 1
        ? fetch(`/api/tickets/${sel[0].id}/pay-cash`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ received }) })
        : fetch(`/api/pay-together/cash`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids: sel.map(x => x.id), received }) });
      try {
        const d = await (await req).json();
        if (!d.ok) return payFail(d.message || "Couldn't record payment");
        pq("#payState").className = "pay-state done";
        pq("#payState").innerHTML = "✓ Cash received<br><b>Change " + money(d.change) + "</b>";
        setTimeout(closePay, 2600);
      } catch (_) { payFail("Connection lost"); }
    });
    pq("#payCardBtn").addEventListener("click", async () => {
      const sel = selectedItems();
      if (sel.length === 0) return;
      payStage("#payMsg");
      pq("#payState").className = "pay-state wait";
      pq("#payState").textContent = sel.length > 1
        ? "Combining bills, sending to Terminal…" : "Sending to Terminal…";
      const req = sel.length === 1
        ? fetch(`/api/tickets/${sel[0].id}/pay-card`, { method: "POST" })
        : fetch(`/api/pay-together/card`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids: sel.map(x => x.id) }) });
      try {
        const d = await (await req).json();
        if (!d.ok) return payFail(d.message || "Couldn't send to Terminal");
        pq("#payState").textContent = "Ask the customer to tap their card on the Terminal…";
        watchCard(d.checkout_id, d.ticket_id || sel[0].id);
      } catch (_) { payFail("Connection lost"); }
    });
  }
  function watchCard(cid, tid) {
    if (cardTimer) clearInterval(cardTimer);
    cardTimer = setInterval(async () => {
      try {
        const r = await fetch(`/api/pay-card-status?id=${encodeURIComponent(cid)}&t=${encodeURIComponent(tid)}`);
        const d = await r.json();
        if (!d.ok) return;
        if (d.status === "COMPLETED") {
          clearInterval(cardTimer); cardTimer = null;
          pq("#payState").className = "pay-state done";
          pq("#payState").textContent = "✓ Card payment complete";
          setTimeout(closePay, 1800);
        } else if (d.status === "CANCELED") {
          clearInterval(cardTimer); cardTimer = null;
          payFail("Cancelled" + (d.cancel_reason ? " (" + d.cancel_reason + ")" : ""));
        }
      } catch (_) {}
    }, 2000);
  }

  esc.__ = 0;
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
})();
