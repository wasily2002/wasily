/* ============================================================
 * LiveAvatar — 照片木偶动画引擎（纯 CPU，Canvas 2D）
 * 用一张真人正面照片 + 锚点(嘴/眼)做实时"伪数字人"：
 *   - 呼吸（上身缓动 scaleY）
 *   - 眨眼（每 3~6s 用眼睛上方皮肤条下移盖住眼睛再弹回）
 *   - 口型：speak(text, audioEl) 时按文本构建 viseme 时间线
 *       a/e/o=大开  i/ü/y=咧  m/b/p/w=闭  其余=半开
 *     时间按音频时长均摊 + AnalyserNode RMS 振幅门控（无声闭嘴），
 *     保证嘴型与正在播的回答文字一致。
 *   - 嘴部绘制：嘴盒上半固定 + 下半下移 openPx + 中间暗红口腔渐变
 * 用法：
 *   const la = new LiveAvatar(canvas, '/avatars/wxx_live_1.jpg', '/avatars/wxx_live_anchor.json');
 *   la.speak(sentenceText, audioElement);   // 每句调用一次
 *   la.stopTalking();
 * ============================================================ */
(function () {
  "use strict";

  // 拼音/字符 → 开口度分类
  function charOpen(ch) {
    if (/[aoeāáǎàēéěèōóǒò]/.test(ch)) return 1.0;   // 大开
    if (/[iīíǐìüǖǘǚǜy]/.test(ch)) return 0.55;       // 咧
    if (/[mbpwf]/.test(ch)) return 0.12;              // 闭/近闭
    if (/[，。！？；：、…—\s,.!?;:…\-]/.test(ch)) return 0.05; // 标点/空格闭嘴
    if (/[0-9a-zA-Z]/.test(ch)) return 0.6;
    return 0.45;                                       // 其余半开
  }

  class LiveAvatar {
    constructor(canvas, imgSrc, anchorUrl, opts) {
      opts = opts || {};
      this.cv = canvas;
      this.cx = canvas.getContext("2d");
      this.srcImg = imgSrc;
      this.img = new Image();
      this.img.src = imgSrc;
      this.anchor = null;       // {mouth:{x,y,w,h}, eye_l, eye_r} 分数坐标
      this.ready = false;
      // 动画状态
      this.t0 = performance.now();
      this.blink = 0;           // 0~1 当前眨眼闭合度
      this.nextBlink = this.t0 + 2500 + Math.random() * 2500;
      this.blinkPhase = 0;      // 0 无, 1 合, 2 开
      this.blinkT = 0;
      // 说话状态
      this.visemes = null;      // [{t0,t1,open}]
      this.speakT0 = 0;
      this.analyser = null;
      this.rmsBuf = null;
      this.audioEl = null;
      this._audioCtx = null;
      this.openSmooth = 0;
      // URL 微调参数（?mx=&my=&mw=&mh= 嘴盒偏移，分数）
      this.tweak = this._readTweak();
      // 背景 / 抠图人像（人物与背景分离）
      this.bg = opts.bg || { type: "none" };
      this.bgImg = null;
      this.cutImg = null;
      this._loadCut(imgSrc, opts.cutout);
      this.setBackground(this.bg);
      const jobs = [];
      jobs.push(new Promise(res => {
        if (this.img.complete && this.img.naturalWidth) return res();
        this.img.onload = res; this.img.onerror = res;
      }));
      if (anchorUrl) {
        jobs.push(fetch(anchorUrl).then(r => r.json()).then(j => { this.anchor = j; })
          .catch(() => { this.anchor = null; }));
      }
      Promise.all(jobs).then(() => {
        this.ready = true;
        this._resize();
        requestAnimationFrame(this._loop.bind(this));
      });
      window.addEventListener("resize", () => this._resize());
    }

    _readTweak() {
      const q = new URLSearchParams(location.search);
      const g = k => parseFloat(q.get(k) || "0") || 0;
      return { mx: g("mx"), my: g("my"), mw: g("mw"), mh: g("mh") };
    }

    /** 换用另一张真人照片（设置面板切换形象照时调用） */
    setImage(imgSrc, anchorUrl, cutoutSrc) {
      this.srcImg = imgSrc;
      const img = new Image();
      img.onload = () => { this.img = img; this.lastFrame = null; };
      img.src = imgSrc;
      this._loadCut(imgSrc, cutoutSrc);
      this.anchor = null;
      if (anchorUrl) {
        fetch(anchorUrl).then(r => (r.ok ? r.json() : null))
          .then(j => { this.anchor = j || null; }).catch(() => { this.anchor = null; });
      }
    }

    /** 加载抠图人像；不存在时静默回退为整张照片 */
    _loadCut(imgSrc, cutoutSrc) {
      if (cutoutSrc === false) { this.cutImg = null; return; }   // 抠图开关关闭
      const cut = cutoutSrc ||
        String(imgSrc).replace(/\.(jpe?g|png)$/i, "") + ".cutout.png";
      const im = new Image();
      im.onload = () => { this.cutImg = im; };
      im.onerror = () => { this.cutImg = null; };
      im.src = cut;
    }

    /** 抠图人像（透明 PNG）：传 url 则换背景时用抠图人物，传 null 用原照片 */
    setCutout(url) {
      if (!url) { this.cutImg = null; return; }
      const img = new Image();
      img.onload = () => { this.cutImg = img; };
      img.onerror = () => { this.cutImg = null; };
      img.src = url;
    }

    /** 背景：{type:"none"} | {type:"solid",c1,c2} | {type:"img",src,dim} */
    setBackground(spec) {
      this.bg = spec || { type: "none" };
      if (this.bg.type === "img" && this.bg.src) {
        const im = new Image();
        im.onload = () => { this.bgImg = im; };
        im.onerror = () => { this.bgImg = null; };
        im.src = this.bg.src;
      } else {
        this.bgImg = null;
      }
    }

    _drawBackground(W, H) {
      const ctx = this.cx, bg = this.bg;
      if (!bg || bg.type === "none") return;
      if (bg.type === "img" && this.bgImg && this.bgImg.naturalWidth) {
        // 背景图 cover 铺满
        const s = Math.max(W / this.bgImg.naturalWidth, H / this.bgImg.naturalHeight);
        const dw = this.bgImg.naturalWidth * s, dh = this.bgImg.naturalHeight * s;
        ctx.drawImage(this.bgImg, (W - dw) / 2, (H - dh) / 2, dw, dh);
        if (bg.dim) {
          ctx.fillStyle = "rgba(0,0,0," + bg.dim + ")";
          ctx.fillRect(0, 0, W, H);
        }
        return;
      }
      const g = ctx.createLinearGradient(0, 0, 0, H);
      g.addColorStop(0, bg.c1 || "#0b1220");
      g.addColorStop(1, bg.c2 || "#16233c");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
    }

    _resize() {
      const r = this.cv.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.cv.width = Math.max(2, r.width * dpr);
      this.cv.height = Math.max(2, r.height * dpr);
      this.dpr = dpr;
    }

    /** 每句：文本 viseme 时间线 + 音频 RMS 门控 */
    speak(text, audioEl) {
      const dur = (audioEl && audioEl.duration && isFinite(audioEl.duration))
        ? audioEl.duration : Math.max(0.9, (text || "").length * 0.16);
      const chars = [...(text || "")];
      const per = dur / Math.max(1, chars.length);
      const now = performance.now();
      this.speakT0 = now;
      this.visemes = chars.map((ch, i) => ({
        t0: now + i * per * 1000,
        t1: now + (i + 1) * per * 1000,
        open: charOpen(ch.toLowerCase ? ch.toLowerCase() : ch),
      }));
      // AnalyserNode 振幅门控
      try {
        if (audioEl && !this._audioCtx) this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioEl && this._audioCtx) {
          const src = this._audioCtx.createMediaElementSource(audioEl);
          const an = this._audioCtx.createAnalyser();
          an.fftSize = 512;
          src.connect(an);
          an.connect(this._audioCtx.destination);
          this.analyser = an;
          this.rmsBuf = new Uint8Array(an.frequencyBinCount);
        }
      } catch (e) { this.analyser = null; }
      this.audioEl = audioEl || null;
    }

    stopTalking() {
      this.visemes = null;
      this.analyser = null;
      this.audioEl = null;
      this.openSmooth = 0;
    }

    // 兼容旧页面引用（混合残留版本会调用此方法）
    stopTalkOnly() { this.stopTalking(); }

    _rms() {
      if (!this.analyser) return 1;
      try {
        this.analyser.getByteTimeDomainData(this.rmsBuf);
        let s = 0;
        for (let i = 0; i < this.rmsBuf.length; i++) {
          const v = (this.rmsBuf[i] - 128) / 128;
          s += v * v;
        }
        return Math.min(1, Math.sqrt(s / this.rmsBuf.length) * 3.2);
      } catch (e) { return 1; }
    }

    _currentOpen(now) {
      if (!this.visemes || !this.visemes.length) return 0;
      if (this.audioEl && this.audioEl.ended) return 0;
      let target = 0;
      for (let i = 0; i < this.visemes.length; i++) {
        const v = this.visemes[i];
        if (now >= v.t0 && now < v.t1) { target = v.open; break; }
        if (now >= v.t1) target = 0;   // 已过最后一字
      }
      const gate = this._rms();        // 无声时强制闭嘴
      target *= (0.25 + 0.75 * gate);
      this.openSmooth += (target - this.openSmooth) * 0.42;
      return this.openSmooth;
    }

    _updateBlink(now) {
      if (this.blinkPhase === 0 && now >= this.nextBlink) {
        this.blinkPhase = 1; this.blinkT = now;
      }
      if (this.blinkPhase === 1) {
        this.blink = Math.min(1, (now - this.blinkT) / 90);
        if (this.blink >= 1) { this.blinkPhase = 2; this.blinkT = now; }
      } else if (this.blinkPhase === 2) {
        this.blink = Math.max(0, 1 - (now - this.blinkT) / 130);
        if (this.blink <= 0) {
          this.blinkPhase = 0;
          this.nextBlink = now + 3000 + Math.random() * 3000;
        }
      }
    }

    _mouthBox(W, H) {
      const m = (this.anchor && this.anchor.mouth) || { x: 0.455, y: 0.672, w: 0.135, h: 0.075 };
      const t = this.tweak;
      return {
        x: (m.x + t.mx - m.w / 2) * W, y: (m.y + t.my - m.h / 2) * H,
        w: (m.w + t.mw) * W, h: (m.h + t.mh) * H,
      };
    }

    _eyeBox(W, H, key) {
      const e = (this.anchor && this.anchor[key]) || { x: 0.41, y: 0.435, w: 0.095, h: 0.045 };
      return { x: (e.x - e.w / 2) * W, y: (e.y - e.h / 2) * H, w: e.w * W, h: e.h * H };
    }

    _loop() {
      if (!this.ready) return;
      const now = performance.now();
      const ctx = this.cx, cv = this.cv;
      const W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      this._drawBackground(W, H);

      // 抠图模式（有背景 + 抠图人像）：用透明 PNG，人物高度铺满、底部对齐
      const useCut = !!(this.cutImg && this.cutImg.naturalWidth &&
                        this.bg && this.bg.type !== "none");
      const img = useCut ? this.cutImg : this.img;
      if (!img || !img.naturalWidth) { requestAnimationFrame(this._loop.bind(this)); return; }

      const iw = img.naturalWidth, ih = img.naturalHeight;
      const breath = 1 + Math.sin((now - this.t0) / 1400) * 0.006;
      const scale = (useCut ? H / ih : Math.min(W / iw, H / ih)) * breath;
      const dw = iw * scale, dh = ih * scale;
      const dx = (W - dw) / 2;
      const dy = useCut ? (H - dh) : (H - dh) / 2;   // 抠图时人物贴底
      ctx.drawImage(img, 0, 0, iw, ih, dx, dy, dw, dh);

      this._updateBlink(now);
      const open = this._currentOpen(now);
      const srcScale = scale;   // 源图像素 → 画布像素 的缩放

      // ---------- 眨眼：用眼睛上方皮肤条下移盖住眼盒 ----------
      if (this.blink > 0) {
        for (const key of ["eye_l", "eye_r"]) {
          const b = this._eyeBox(dw, dh, key);       // 画布坐标尺寸
          const coverH = b.h * 1.25 * this.blink;
          ctx.drawImage(img,
            b.x / srcScale, (b.y - b.h * 0.6) / srcScale,
            b.w / srcScale, Math.max(1, coverH / srcScale),
            dx + b.x, dy + b.y - 1, b.w, coverH + 2);
        }
      }

      // ---------- 口型：下半嘴下移 + 暗口腔 ----------
      if (open > 0.02) {
        const m = this._mouthBox(dw, dh);
        const openPx = open * m.h * 1.15;
        const cx = dx + m.x + m.w / 2, cy = dy + m.y + m.h / 2 + openPx * 0.35;
        const g = ctx.createRadialGradient(cx, cy, 1, cx, cy, Math.max(m.w, openPx + m.h) * 0.55);
        g.addColorStop(0, "rgba(96,20,26,0.95)");
        g.addColorStop(0.7, "rgba(60,12,18,0.88)");
        g.addColorStop(1, "rgba(60,12,18,0)");
        ctx.save();
        ctx.beginPath();
        ctx.ellipse(cx, cy, m.w * 0.46, (openPx + m.h * 0.4) * 0.62, 0, 0, Math.PI * 2);
        ctx.clip();
        ctx.fillStyle = g;
        ctx.fillRect(cx - m.w * 0.5, cy - m.h, m.w, m.h * 2.2);
        ctx.restore();
        // 下半嘴下移
        ctx.drawImage(img,
          m.x / srcScale, (m.y + m.h * 0.5) / srcScale, m.w / srcScale, (m.h * 0.55) / srcScale,
          dx + m.x, dy + m.y + m.h * 0.5 + openPx, m.w, m.h * 0.55);
      }

      requestAnimationFrame(this._loop.bind(this));
    }
  }

  window.LiveAvatar = LiveAvatar;
})();
