/**
 * photo3d.js — 照片分身引擎（照片像素 + 2D 骨骼蒙皮 + 3D 透视）
 *
 * 和"机器人"不同：整套人物就是用户照片本身（抠图透明 PNG），
 * 把照片切成一张高密度网格，按身体关节绑定权重，骨骼怎么动照片就怎么变形，
 * 因此身材、衣服、脸、发型都与照片完全一致；配合透视相机+曲面深度，
 * 转身/走动时有 3D 体积感（2.5D 数字人）。
 *
 * 用法：
 *   var p3 = new Photo3D(el, {
 *     img: "/avatars/wxx_full_1.jpg",          // 原图（取色/表情用）
 *     cutout: "/avatars/wxx_full_1.cutout.png",// 透明抠图（没抠图则自动用原图）
 *     body: "/avatars/wxx_full_1.body.json",   // 关节标定；没有则按人物包围盒自动生成
 *     anchor: "/avatars/wxx_full_1.anchor.json"// 眼/嘴锚点（表情用，可选）
 *   });
 *   p3.setState("walk", 3000);
 *   p3.speak("你好呀", audioElement);
 *   p3.stopTalking(); p3.resize(); p3.dispose();
 */
(function (global) {
  "use strict";

  var DEG = Math.PI / 180;

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  /* ---------- 2x3 仿射矩阵（[a,b,c,d,e,f]，与 canvas 一致） ---------- */
  function mT(x, y) { return [1, 0, 0, 1, x, y]; }
  function mR(t) { var c = Math.cos(t), s = Math.sin(t); return [c, s, -s, c, 0, 0]; }
  function mMul(m, n) {
    return [
      m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1],
      m[0] * n[2] + m[2] * n[3], m[1] * n[2] + m[3] * n[3],
      m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5]
    ];
  }
  function mApply(m, x, y) { return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]]; }

  /* ---------- 点到线段距离 ---------- */
  function distSeg(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var L2 = dx * dx + dy * dy;
    var t = L2 > 1e-9 ? clamp(((px - x1) * dx + (py - y1) * dy) / L2, 0, 1) : 0;
    var qx = x1 + dx * t, qy = y1 + dy * t;
    return Math.hypot(px - qx, py - qy);
  }

  /* ---------- 文本 → 开口度 ---------- */
  function charOpen(ch) {
    if (/[aoeāáǎàēéěèōóǒò]/.test(ch)) return 1.0;
    if (/[iīíǐìüǖǘǚǜy]/.test(ch)) return 0.55;
    if (/[mbpwf]/.test(ch)) return 0.14;
    if (/[，。！？；：、…—\s,.!?;:…\-]/.test(ch)) return 0.04;
    if (/[0-9a-zA-Z]/.test(ch)) return 0.6;
    return 0.42;
  }

  /* ---------- 文本 → 唇宽（圆唇/展唇） ----------
   * 负值=圆唇收窄（a/o），正值=展唇拉开（i/ü），0=自然
   */
  function charWide(ch) {
    if (/[aoōóǒòūúǔù]/.test(ch)) return -0.30;   // 圆唇：a o u
    if (/[iīíǐìüǖǘǚǜy]/.test(ch)) return 0.40;   // 展唇：i ü y
    if (/[eēéěè]/.test(ch)) return 0.15;          // 微展：e
    if (/[mbp]/.test(ch)) return 0;               // 双唇音：闭嘴不展
    if (/[，。！？；：、…—\s,.!?;:…\-]/.test(ch)) return 0;
    if (/[0-9a-zA-Z]/.test(ch)) return 0.10;
    return 0.05;
  }

  /* ================================================================
   * 构造
   * ================================================================ */
  function Photo3D(container, opts) {
    opts = opts || {};
    this.el = container;
    this.opts = opts;
    this.ready = false;
    this.state = "idle";
    this.stateTil = 0;
    this.t0 = performance.now();
    this.rootDrift = 0;
    this.rootLift = 0;
    this.facing = 0;
    this.breath = 0;

    // 表情/口型
    this.blink = 0; this.blinkPhase = 0; this.blinkT = 0;
    this.nextBlink = this.t0 + 2600 + Math.random() * 2600;
    this.visemes = null; this.speakT0 = 0;
    this.analyser = null; this.rmsBuf = null; this._audioCtx = null;
    this.audioEl = null; this.openSmooth = 0; this.wideSmooth = 0;
    this._lastTex = 0;

    // 真 3D 深度图（来自 MiDaS）
    this.depthData = null; this.depthW = 0; this.depthH = 0;
    this.depthScale = 0.07;   // 深度位移幅度（越大越鼓）

    // 骨骼角度（当前值 / 目标值）
    this.cur = {}; this.tgt = {};

    // Three.js 场景
    var w = container.clientWidth || 480, h = container.clientHeight || 720;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setSize(w, h);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.domElement.style.position = "absolute";
    this.renderer.domElement.style.inset = "0";
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.renderer.domElement.style.filter = "drop-shadow(0 10px 18px rgba(0,0,0,.45))";
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, w / h, 0.1, 80);

    this.group = new THREE.Group();
    this.scene.add(this.group);

    this.setImage(opts.img, opts.cutout, opts.body, opts.anchor);
    window.addEventListener("resize", this._onResize = function () { this.resize(); }.bind(this));
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  /* ================================================================
   * 素材加载
   * ================================================================ */
  Photo3D.prototype.setImage = function (imgUrl, cutoutUrl, body, anchorUrl) {
    if (!imgUrl) return;
    this.ready = false;
    this._disposeMesh();
    this.imgUrl = imgUrl;
    this.cutUrl = cutoutUrl || String(imgUrl).replace(/\.(jpe?g|png)$/i, "") + ".cutout.png";
    var self = this;

    var bodyPromise = (body && typeof body === "object")
      ? Promise.resolve(body)
      : fetch(body || String(imgUrl).replace(/\.(jpe?g|png)$/i, "") + ".body.json")
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; });
    var anchorPromise = anchorUrl
      ? fetch(anchorUrl).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
      : Promise.resolve(null);

    var img = new Image();
    var cut = new Image();
    var art = new Image();
    var jobs = [
      new Promise(function (res) { img.onload = res; img.onerror = res; img.src = imgUrl; }),
      new Promise(function (res) { cut.onload = res; cut.onerror = res; cut.src = self.cutUrl; }),
      new Promise(function (res) { art.onload = res; art.onerror = res; art.src = body && body.anchor ? body.anchor : (anchorUrl || String(imgUrl).replace(/\.(jpe?g|png)$/i, "") + ".anchor.json"); }),
      bodyPromise, anchorPromise
    ];
    Promise.all(jobs).then(function (r) {
      var useCut = cut.naturalWidth ? cut : img;
      if (!useCut.naturalWidth) return;
      self.img = img;
      self.cut = useCut;
      self.anchor = r[4] || r[3] && r[3].anchor || null;
      body = r[3];
      self._initBody(body, useCut);
      self._buildMesh(useCut);
      self.ready = true;
      self.resize();
      // 自动加载真 3D 深度图
      self.enableDepth();
    });
  };

  /** 从服务端获取 MiDaS 深度图，把平面网格顶点沿 z 轴位移成真 3D */
  Photo3D.prototype.enableDepth = function () {
    var self = this;
    if (!this.imgUrl) return;
    var fname = String(this.imgUrl).split("/").pop();
    fetch("/training/depth3d", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ img: fname, out_w: 48, out_h: 90 })
    }).then(function (r) { return r.json(); }).then(function (info) {
      if (!info || !info.ok || !info.depth_png) return;
      var im = new Image();
      im.onload = function () {
        var w = info.out_w || im.naturalWidth, h = info.out_h || im.naturalHeight;
        var c = document.createElement("canvas");
        c.width = w; c.height = h;
        var cx = c.getContext("2d");
        cx.drawImage(im, 0, 0, w, h);
        var d = cx.getImageData(0, 0, w, h).data;
        var arr = new Uint8Array(w * h);
        for (var i = 0; i < w * h; i++) arr[i] = d[i * 4]; // 灰度取 R
        self.depthData = arr; self.depthW = w; self.depthH = h;
      };
      im.src = "data:image/png;base64," + info.depth_png;
    }).catch(function () { /* 深度图失败则退回平面 */ });
  };

  /** 计算人物包围盒（从透明 alpha），供自动骨骼使用 */
  Photo3D.prototype._personBox = function (imgEl) {
    try {
      var W = 160, H = Math.max(2, Math.round(W * imgEl.naturalHeight / imgEl.naturalWidth));
      var c = document.createElement("canvas");
      c.width = W; c.height = H;
      var x = c.getContext("2d");
      x.drawImage(imgEl, 0, 0, W, H);
      var d = x.getImageData(0, 0, W, H).data;
      var minX = W, minY = H, maxX = 0, maxY = 0, found = false;
      for (var y = 0; y < H; y++) {
        for (var xx = 0; xx < W; xx++) {
          if (d[(y * W + xx) * 4 + 3] > 60) {
            found = true;
            if (xx < minX) minX = xx; if (xx > maxX) maxX = xx;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
          }
        }
      }
      if (!found) return { x0: 0.15, y0: 0.02, x1: 0.85, y1: 0.98 };
      return { x0: minX / W, y0: minY / H, x1: (maxX + 1) / W, y1: (maxY + 1) / H };
    } catch (e) {
      return { x0: 0.15, y0: 0.02, x1: 0.85, y1: 0.98 };
    }
  }

  /** 自动按人物比例生成关节（没有标定文件时的兜底） */
  Photo3D.prototype._autoBody = function (imgEl) {
    var b = this._personBox(imgEl);
    var pw = b.x1 - b.x0, ph = b.y1 - b.y0;
    var cx = (b.x0 + b.x1) / 2;
    var full = (ph * imgEl.naturalHeight) / Math.max(1, pw * imgEl.naturalWidth) >= 2.05;
    var J;
    if (full) {
      J = {
        head: { cx: cx, cy: b.y0 + ph * 0.075, w: pw * 0.52, h: ph * 0.145 },
        neck: { x: cx, y: b.y0 + ph * 0.155 },
        shoulder_l: { x: b.x0 + pw * 0.18, y: b.y0 + ph * 0.195 },
        shoulder_r: { x: b.x1 - pw * 0.18, y: b.y0 + ph * 0.19 },
        elbow_l: { x: b.x0 + pw * 0.12, y: b.y0 + ph * 0.33 },
        elbow_r: { x: b.x1 - pw * 0.12, y: b.y0 + ph * 0.32 },
        wrist_l: { x: b.x0 + pw * 0.11, y: b.y0 + ph * 0.45 },
        wrist_r: { x: b.x1 - pw * 0.13, y: b.y0 + ph * 0.44 },
        hip_l: { x: cx - pw * 0.20, y: b.y0 + ph * 0.47 },
        hip_r: { x: cx + pw * 0.20, y: b.y0 + ph * 0.47 },
        knee_l: { x: cx - pw * 0.18, y: b.y0 + ph * 0.72 },
        knee_r: { x: cx + pw * 0.18, y: b.y0 + ph * 0.72 },
        ankle_l: { x: cx - pw * 0.16, y: b.y0 + ph * 0.96 },
        ankle_r: { x: cx + pw * 0.16, y: b.y0 + ph * 0.96 }
      };
    } else {
      // 半身照：只绑定头/躯干/手臂
      J = {
        head: { cx: cx, cy: b.y0 + ph * 0.28, w: pw * 0.62, h: ph * 0.44 },
        neck: { x: cx, y: b.y0 + ph * 0.46 },
        shoulder_l: { x: b.x0 + pw * 0.12, y: b.y0 + ph * 0.56 },
        shoulder_r: { x: b.x1 - pw * 0.12, y: b.y0 + ph * 0.56 },
        elbow_l: { x: b.x0 + pw * 0.05, y: b.y0 + ph * 0.76 },
        elbow_r: { x: b.x1 - pw * 0.05, y: b.y0 + ph * 0.76 },
        wrist_l: { x: b.x0 + pw * 0.03, y: b.y0 + ph * 0.94 },
        wrist_r: { x: b.x1 - pw * 0.03, y: b.y0 + ph * 0.94 },
        hip_l: { x: cx - pw * 0.22, y: b.y0 + ph * 0.94 },
        hip_r: { x: cx + pw * 0.22, y: b.y0 + ph * 0.94 },
        knee_l: null, knee_r: null, ankle_l: null, ankle_r: null
      };
    }
    J.bbox = b;
    return J;
  }

  Photo3D.prototype._initBody = function (body, imgEl) {
    var J = body;
    if (!J || !J.head) J = this._autoBody(imgEl);
    // 兼容扁平格式（author 手填时可能省略 bbox）
    this.J = J;
    this.A = imgEl.naturalHeight / Math.max(1, imgEl.naturalWidth);
    // 图片归一化坐标 → 网格坐标
    this._mx = 1.0; this._my = this.A;
  }

  /* 归一化图片坐标 → 网格坐标（x ∈ [-0.5,0.5]，y 向上为正） */
  Photo3D.prototype._P = function (p) {
    return [p.x - 0.5, (0.5 - p.y) * this.A];
  }

  /* ---------- 构建骨骼与蒙皮网格 ---------- */
  Photo3D.prototype._buildMesh = function (imgEl) {
    var self = this, J = this.J, A = this.A;
    var M = function (p) { return self._P(p); };

    var hipC = { x: (J.hip_l.x + J.hip_r.x) / 2, y: (J.hip_l.y + J.hip_r.y) / 2 };
    this.hipC = M(hipC);

    var defs = [
      { name: "hips", parent: null, head: this.hipC, seg: [this.hipC, this.hipC] },
      { name: "spine", parent: "hips", head: this.hipC, seg: [this.hipC, M(J.neck)] },
      { name: "neck", parent: "spine", head: M(J.neck), seg: [M(J.neck), M(J.neck)] },
      { name: "head", parent: "neck", head: M(J.neck), seg: [M(J.neck), M({ x: J.head.cx, y: J.head.cy })] },
      { name: "shoulderL", parent: "spine", head: M(J.shoulder_l), seg: [M(J.shoulder_l), M(J.elbow_l)] },
      { name: "elbowL", parent: "shoulderL", head: M(J.elbow_l), seg: [M(J.elbow_l), M(J.wrist_l)] },
      { name: "shoulderR", parent: "spine", head: M(J.shoulder_r), seg: [M(J.shoulder_r), M(J.elbow_r)] },
      { name: "elbowR", parent: "shoulderR", head: M(J.elbow_r), seg: [M(J.elbow_r), M(J.wrist_r)] },
      { name: "hipL", parent: "hips", head: M(J.hip_l), seg: [M(J.hip_l), M(J.knee_l || J.hip_l)] },
      { name: "kneeL", parent: "hipL", head: M(J.knee_l || J.hip_l), seg: [M(J.knee_l || J.hip_l), M(J.ankle_l || J.hip_l)] },
      { name: "hipR", parent: "hips", head: M(J.hip_r), seg: [M(J.hip_r), M(J.knee_r || J.hip_r)] },
      { name: "kneeR", parent: "hips" === "hips" ? "hipR" : "hipR", head: M(J.knee_r || J.hip_r), seg: [M(J.knee_r || J.hip_r), M(J.ankle_r || J.hip_r)] }
    ];
    this.bones = defs;
    this.boneIndex = {};
    defs.forEach(function (b, i) { self.boneIndex[b.name] = i; b.angle = 0; b.cur = 0; });

    // 贴图画布（表情/口型在纹理空间绘制）
    var tw = Math.min(560, Math.round(imgEl.naturalWidth));
    var th = Math.round(tw * A);
    if (th > 1000) { th = 1000; tw = Math.round(th / A); }
    this.tc = document.createElement("canvas");
    this.tc.width = tw; this.tc.height = th;
    this.tx = this.tc.getContext("2d");
    this.tex = new THREE.CanvasTexture(this.tc);
    if (THREE.sRGBEncoding) this.tex.encoding = THREE.sRGBEncoding;
    this.tex.minFilter = THREE.LinearFilter;
    this.tex.magFilter = THREE.LinearFilter;
    this._redrawTexture(true);

    // 网格：24 x 44 足够平滑，CPU 蒙皮开销小
    var geo = new THREE.PlaneGeometry(1, A, 24, 44);
    this.rest = new Float32Array(geo.attributes.position.array); // xyz
    this.weights = this._computeWeights(this.rest);
    this.pos = geo.attributes.position;
    this.mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      map: this.tex, transparent: true, side: THREE.DoubleSide, depthWrite: false
    }));
    this.mesh.frustumCulled = false;
    this.group.add(this.mesh);

    // 初始角度
    this.cur = {}; this.tgt = {};
    ["spine", "neck", "head", "shoulderL", "elbowL", "shoulderR", "elbowR",
     "hipL", "kneeL", "hipR", "kneeR"].forEach(function (n) { self.cur[n] = 0; self.tgt[n] = 0; });

    // 自适应相机：整个人物刚好完整出现在画面里
    this._fitCamera();

    // 表情锚点：没有标定就按头部框推算
    this.face = this._faceFromAnchor();
    this._redrawTexture(true);
  }

  Photo3D.prototype._fitCamera = function () {
    var A = this.A;
    var fov = 30 * DEG;
    this.camera.fov = 30;
    var dist = (A / 2) / Math.tan(fov / 2) * 1.14;
    this.camera.position.set(0, 0, dist);
    this.camera.lookAt(0, 0, 0);
    this.camera.updateProjectionMatrix();
    // 让网格底边贴近画面底部
    this.group.position.y = 0;
  }

  Photo3D.prototype._faceFromAnchor = function () {
    var J = this.J, a = this.anchor;
    if (a && a.mouth && a.eye_l && a.eye_r) return a;
    var h = J.head;
    var ex = h.w * 0.19, ey = h.cy - h.h * 0.02, ew = h.w * 0.28, eh = h.h * 0.11;
    return {
      eye_l: { x: h.cx - ex, y: ey, w: ew, h: eh },
      eye_r: { x: h.cx + ex, y: ey, w: ew, h: eh },
      mouth: { x: h.cx, y: h.cy + h.h * 0.36, w: h.w * 0.42, h: h.h * 0.16 }
    };
  }

  /** 顶点权重：到每根骨骼线段的距离反比，取最近 3 根归一化 */
  Photo3D.prototype._computeWeights = function (arr) {
    var n = arr.length / 3, bones = this.bones;
    var out = [];
    for (var i = 0; i < n; i++) {
      var vx = arr[i * 3], vy = arr[i * 3 + 1];
      var cand = [];
      for (var b = 0; b < bones.length; b++) {
        var s = bones[b].seg;
        var d = distSeg(vx, vy, s[0][0], s[0][1], s[1][0], s[1][1]);
        cand.push([b, d]);
      }
      cand.sort(function (p, q) { return p[1] - q[1]; });
      var top = cand.slice(0, 3);
      var sum = 0;
      top.forEach(function (c) { c[1] = 1 / (Math.pow(c[1], 2.6) + 1e-5); sum += c[1]; });
      out.push(top.map(function (c) { return [c[0], c[1] / sum]; }));
    }
    return out;
  }

  /* ================================================================
   * 纹理：表情 / 口型 / 眨眼（在照片像素上做，随骨骼一起变形）
   * ================================================================ */
  Photo3D.prototype._redrawTexture = function (force) {
    if (!this.tx || !this.cut) return;
    var now = performance.now();
    if (!force && now - this._lastTex < 60) return;
    if (!force && Math.abs(this._texKey - (this.openSmooth * 10 + this.blink * 7 + this.wideSmooth * 6)) < 0.3) return;
    this._lastTex = now;
    this._texKey = this.openSmooth * 10 + this.blink * 7 + this.wideSmooth * 6;

    var ctx = this.tx, W = this.tc.width, H = this.tc.height;
    var img = this.cut;
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(img, 0, 0, W, H);

    var f = this.face;
    if (!f || !f.eye_l || !f.mouth) return;

    // 眨眼：把眼睛上方皮肤条下移盖住眼睛
    if (this.blink > 0.02) {
      ["eye_l", "eye_r"].forEach(function (k) {
        var e = f[k];
        var ex = (e.x - e.w / 2) * W, ey = (e.y - e.h / 2) * H;
        var ew = e.w * W, eh = e.h * H;
        var cover = eh * 1.3 * this.blink;
        ctx.drawImage(this.tc, ex, ey - eh * 0.7, ew, Math.max(1, cover),
                      ex, ey - 1, ew, cover + 2);
      }, this);
    }

    // 口型：下唇下移 + 暗红口腔 + 上排牙齿 + 唇宽变化（圆唇收窄 / 展唇拉开）
    var open = this.openSmooth;
    var wide = this.wideSmooth || 0;
    if (open > 0.03 || Math.abs(wide) > 0.06) {
      var m = f.mouth;
      var mx = (m.x - m.w / 2) * W, my = (m.y - m.h / 2) * H;
      var mw = m.w * W, mh = m.h * H;
      // 唇宽：wide>0 展唇变宽，wide<0 圆唇变窄，以嘴中心为轴
      var mwEff = mw * (1 + wide * 0.45);
      var mxEff = mx + (mw - mwEff) / 2;
      var cx = mxEff + mwEff / 2;
      var drop = open * mh * 1.6;
      var cy = my + mh / 2 + drop * 0.35;
      if (open > 0.03) {
        // 口腔：暗红径向渐变
        var g = ctx.createRadialGradient(cx, cy, 1, cx, cy, Math.max(mwEff, drop + mh) * 0.55);
        g.addColorStop(0, "rgba(98,20,26,0.95)");
        g.addColorStop(0.7, "rgba(62,12,18,0.9)");
        g.addColorStop(1, "rgba(62,12,18,0)");
        ctx.save();
        ctx.beginPath();
        ctx.ellipse(cx, cy, mwEff * 0.46, (drop + mh * 0.4) * 0.62, 0, 0, Math.PI * 2);
        ctx.clip();
        ctx.fillStyle = g;
        ctx.fillRect(cx - mwEff * 0.6, cy - mh, mwEff * 1.2, mh * 2.4);
        // 上排牙齿：张嘴时露出上牙（白色，位于口腔顶部）
        if (open > 0.12) {
          var teethH = Math.min(drop * 0.45, mh * 0.5);
          var teethY = my + mh * 0.42;
          var teethW = mwEff * 0.78;
          var tg = ctx.createLinearGradient(0, teethY, 0, teethY + teethH);
          tg.addColorStop(0, "rgba(250,248,240,0.95)");
          tg.addColorStop(1, "rgba(220,215,200,0.8)");
          ctx.fillStyle = tg;
          ctx.beginPath();
          ctx.ellipse(cx, teethY + teethH * 0.5, teethW * 0.5, teethH * 0.5, 0, 0, Math.PI * 2);
          ctx.fill();
          // 牙齿间细缝
          ctx.strokeStyle = "rgba(180,170,155,0.5)";
          ctx.lineWidth = 0.6;
          for (var ti = -2; ti <= 2; ti++) {
            ctx.beginPath();
            ctx.moveTo(cx + ti * teethW * 0.16, teethY);
            ctx.lineTo(cx + ti * teethW * 0.16, teethY + teethH);
            ctx.stroke();
          }
          // 舌头：嘴张得大时露出一点舌尖（粉红）
          if (open > 0.35) {
            ctx.fillStyle = "rgba(200,90,90,0.7)";
            ctx.beginPath();
            ctx.ellipse(cx, cy + (drop + mh * 0.4) * 0.28, mwEff * 0.3, (drop + mh * 0.4) * 0.18, 0, 0, Math.PI * 2);
            ctx.fill();
          }
        }
        ctx.restore();
      }
      // 下半嘴下移 + 横向缩放（展唇时拉伸、圆唇时压缩）
      if (open > 0.03 || Math.abs(wide) > 0.06) {
        ctx.drawImage(this.tc, mx, my + mh * 0.45, mw, mh * 0.5,
                      mxEff, my + mh * 0.45 + drop, mwEff, mh * 0.5);
        // 下唇阴影：增强立体感
        if (open > 0.1) {
          ctx.strokeStyle = "rgba(60,30,30,0.35)";
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.ellipse(cx, my + mh * 0.5 + drop, mwEff * 0.42, mh * 0.12 + drop * 0.15, 0, 0, Math.PI);
          ctx.stroke();
        }
      }
    }
    this.tex.needsUpdate = true;
  }

  /* ================================================================
   * 状态机：骨架目标角度
   * ================================================================ */
  Photo3D.prototype.setState = function (name, durMs) {
    this.state = name || "idle";
    this.stateTil = durMs ? performance.now() + durMs : 0;
  };

  Photo3D.prototype._poseTarget = function (t) {
    var s = this.state, T = {};
    var names = ["spine", "neck", "head", "shoulderL", "elbowL", "shoulderR", "elbowR",
                 "hipL", "kneeL", "hipR", "kneeR"];
    names.forEach(function (n) { T[n] = 0; });
    // 呼吸：两个谐波叠加，避免机械感
    var breath = 0.007 * Math.sin(t * 1.4) + 0.003 * Math.sin(t * 2.1);
    var r = { drift: 0, lift: 0, face: 0, breath: breath };

    switch (s) {
      case "talk":
        // 说话：头轻点 + 微手势（右手小幅度比划）
        T.head = 0.05 * Math.sin(t * 6.0) + 0.02 * Math.sin(t * 2.3);
        T.neck = 0.025 * Math.sin(t * 2.3);
        T.spine = 0.02 * Math.sin(t * 1.9);
        T.shoulderR = 0.32 + 0.16 * Math.sin(t * 5.5);
        T.elbowR = 0.50 + 0.12 * Math.sin(t * 5.5 + 0.5);
        T.shoulderL = 0.06 + 0.03 * Math.sin(t * 1.7);
        T.elbowL = 0.15;
        break;
      case "wave":
        // 挥手：右臂抬至肩高，肘部带动手掌小幅摆动
        T.shoulderR = 1.10 + 0.10 * Math.sin(t * 7.0);
        T.elbowR = 0.60 + 0.22 * Math.sin(t * 7.0);
        T.head = 0.06 * Math.sin(t * 2.5);
        T.neck = 0.03 * Math.sin(t * 2.5);
        T.spine = 0.025 * Math.sin(t * 2.5);
        T.shoulderL = 0.05;
        r.face = 0.04;
        break;
      case "walk":
        // 行走：腿前后摆、膝抬腿时弯、臂腿反向、躯干微扭+上下起伏
        var phase = t * 4.2;
        var sw = Math.sin(phase);
        T.hipL = 0.22 * sw;
        T.hipR = -0.22 * sw;
        T.kneeL = 0.32 * Math.max(0, -sw) + 0.05;
        T.kneeR = 0.32 * Math.max(0, sw) + 0.05;
        T.shoulderL = -0.20 * sw;
        T.shoulderR = 0.20 * sw;
        T.elbowL = 0.40 + 0.10 * Math.max(0, sw);
        T.elbowR = 0.40 + 0.10 * Math.max(0, -sw);
        T.spine = 0.025 * Math.sin(phase * 2);
        r.lift = 0.018 * (0.5 + 0.5 * Math.cos(phase * 2));
        r.drift = 0.06 * Math.sin(phase);
        r.face = 0.05 * Math.sin(phase * 0.5);
        break;
      case "dance":
        // 跳舞：双臂交替上举 + 躯干扭转 + 轻跳
        var d = Math.sin(t * 3.2);
        var d2 = Math.sin(t * 3.2 + 1.5);
        T.spine = 0.12 * d;
        T.neck = 0.05 * d2;
        T.head = 0.08 * d2;
        T.shoulderR = 0.85 + 0.45 * d;
        T.shoulderL = 0.85 + 0.45 * d2;
        T.elbowR = 0.55 + 0.25 * d;
        T.elbowL = 0.55 + 0.25 * d2;
        T.hipL = 0.18 * d;
        T.hipR = -0.18 * d;
        r.drift = 0.08 * d;
        r.lift = 0.02 * Math.abs(Math.sin(t * 3.2));
        r.face = 0.10 * d;
        break;
      case "sit":
        // 坐下：髋膝弯曲，身体微后倾
        T.hipL = 0.55; T.hipR = -0.55;
        T.kneeL = 0.85; T.kneeR = 0.85;
        T.spine = -0.04;
        T.shoulderL = 0.18; T.shoulderR = -0.18;
        T.elbowL = 0.30; T.elbowR = -0.30;
        r.lift = -0.14;
        break;
      case "happy":
        // 开心：双臂张开上举，小幅度挥动
        var hp = Math.sin(t * 5.0);
        T.shoulderR = 0.95 + 0.15 * hp;
        T.shoulderL = 0.95 + 0.15 * hp;
        T.elbowR = 0.40 + 0.12 * hp;
        T.elbowL = 0.40 + 0.12 * hp;
        T.head = 0.07 * Math.sin(t * 4.0);
        T.spine = 0.04 * Math.sin(t * 2.5);
        r.lift = 0.03 * Math.abs(Math.sin(t * 5.0));
        break;
      case "sad":
        // 难过：低头含胸，手臂微收
        T.head = -0.13; T.neck = -0.05; T.spine = -0.06;
        T.shoulderL = 0.12; T.shoulderR = -0.12;
        T.elbowL = 0.22; T.elbowR = -0.22;
        r.lift = -0.03;
        r.face = -0.04;
        break;
      case "greet":
        // 打招呼：微微倾身，右手轻抬
        T.shoulderR = 0.80;
        T.elbowR = 0.50;
        T.head = 0.05; T.spine = 0.025; T.neck = 0.02;
        T.shoulderL = 0.05;
        r.face = 0.03;
        break;
      default: // idle：自然站立，微呼吸+轻微摆动
        T.spine = 0.015 * Math.sin(t * 0.85) + 0.008 * Math.sin(t * 1.6);
        T.neck = 0.018 * Math.sin(t * 1.05);
        T.head = 0.03 * Math.sin(t * 0.7 + 0.6) + 0.012 * Math.sin(t * 1.9);
        T.shoulderL = 0.04 + 0.02 * Math.sin(t * 1.1);
        T.shoulderR = -0.04 - 0.02 * Math.sin(t * 1.1);
        T.elbowL = 0.14 + 0.02 * Math.sin(t * 1.3);
        T.elbowR = -0.14 - 0.02 * Math.sin(t * 1.3);
        r.drift = 0.018 * Math.sin(t * 0.45);
        r.face = 0.025 * Math.sin(t * 0.35);
        break;
    }
    T.__r = r;
    return T;
  }

  /* ================================================================
   * 说话（口型）：viseme + RMS 门控
   * ================================================================ */
  Photo3D.prototype.speak = function (text, audioEl) {
    var dur = (audioEl && audioEl.duration && isFinite(audioEl.duration))
      ? audioEl.duration : Math.max(0.9, String(text || "").length * 0.16);
    var chars = Array.from(String(text || ""));
    var now = performance.now();
    this.speakT0 = now;
    // 自然口型节奏：元音稍长、辅音短促、标点停顿，字间留 25ms 闭嘴过渡
    var visemes = [];
    var t = now;
    var totalWeight = 0;
    var weights = chars.map(function (ch) {
      var w;
      if (/[，。！？；：、…—,.!?;:]/.test(ch)) w = 0.5;           // 标点：短停顿
      else if (/[aoeiuüāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]/.test(ch)) w = 1.3; // 元音：长
      else if (/[mbpf]/.test(ch)) w = 0.5;                         // 双唇音：短
      else w = 1.0;
      totalWeight += w;
      return w;
    });
    var unit = (dur * 1000) / Math.max(1, totalWeight);
    chars.forEach(function (ch, i) {
      var openT = unit * weights[i] * 0.7;     // 张嘴时长（留 30% 给闭嘴过渡）
      var gap = unit * weights[i] * 0.3;
      visemes.push({
        t0: t, t1: t + openT,
        open: charOpen(ch), wide: charWide(ch)
      });
      t += openT + gap;
    });
    this.visemes = visemes;
    this.analyser = null;
    try {
      if (audioEl) {
        if (!this._audioCtx) this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (this._audioCtx.state === "suspended") this._audioCtx.resume();
        // 同一个 <audio> 只能建一次 MediaElementSource，否则抛 InvalidStateError
        if (!this._srcCache) this._srcCache = new WeakMap();
        var src = this._srcCache.get(audioEl);
        if (!src) {
          src = this._audioCtx.createMediaElementSource(audioEl);
          this._srcCache.set(audioEl, src);
          var an = this._audioCtx.createAnalyser();
          an.fftSize = 512;
          src.connect(an); an.connect(this._audioCtx.destination);
          this._anCache = this._anCache || new WeakMap();
          this._anCache.set(audioEl, an);
        }
        this.analyser = this._anCache && this._anCache.get(audioEl);
        this.rmsBuf = new Uint8Array(this.analyser.frequencyBinCount);
      }
    } catch (e) { this.analyser = null; }
    this.audioEl = audioEl || null;
    this.setState("talk", dur * 1000 + 300);
  };

  Photo3D.prototype.stopTalking = function () {
    this.visemes = null; this.analyser = null; this.audioEl = null; this.openSmooth = 0; this.wideSmooth = 0;
    if (this.state === "talk") this.setState("idle");
  };

  Photo3D.prototype._open = function (now) {
    if (!this.visemes || !this.visemes.length) return 0;
    if (this.audioEl && this.audioEl.ended) return 0;
    var target = 0, wideTarget = 0;
    for (var i = 0; i < this.visemes.length; i++) {
      var v = this.visemes[i];
      if (now >= v.t0 && now < v.t1) { target = v.open; wideTarget = v.wide; break; }
      if (now >= v.t1) { target = 0; wideTarget = 0; }
    }
    var gate = 1;
    if (this.audioEl && this.audioEl.muted) gate = 1;   // 静音时仍按文字口型
    else if (this.analyser) {
      try {
        this.analyser.getByteTimeDomainData(this.rmsBuf);
        var acc = 0;
        for (var j = 0; j < this.rmsBuf.length; j++) {
          var val = (this.rmsBuf[j] - 128) / 128;
          acc += val * val;
        }
        gate = Math.min(1, Math.sqrt(acc / this.rmsBuf.length) * 3.2);
      } catch (e) { gate = 1; }
    }
    target *= (0.25 + 0.75 * gate);
    // 微抖动：说话时嘴巴不是完全静止的，加 8~12Hz 的小幅颤动，避免机械感
    if (target > 0.02) {
      var jitter = 0.06 * Math.sin(now * 0.053) + 0.04 * Math.sin(now * 0.037 + 1.2);
      target = Math.max(0, target + jitter);
    }
    this.openSmooth += (target - this.openSmooth) * 0.42;
    // 唇宽变化稍慢，避免横向抖动
    this.wideSmooth += (wideTarget - this.wideSmooth) * 0.30;
    return this.openSmooth;
  };

  Photo3D.prototype._blink = function (now) {
    if (this.blinkPhase === 0 && now >= this.nextBlink) { this.blinkPhase = 1; this.blinkT = now; }
    if (this.blinkPhase === 1) {
      this.blink = Math.min(1, (now - this.blinkT) / 90);
      if (this.blink >= 1) { this.blinkPhase = 2; this.blinkT = now; }
    } else if (this.blinkPhase === 2) {
      this.blink = Math.max(0, 1 - (now - this.blinkT) / 130);
      if (this.blink <= 0) { this.blinkPhase = 0; this.nextBlink = now + 3000 + Math.random() * 3000; }
    }
  };

  /* ================================================================
   * 主循环：骨骼世界矩阵 → 顶点蒙皮 → z 曲面 → 渲染
   * ================================================================ */
  Photo3D.prototype._loop = function () {
    this._raf = requestAnimationFrame(this._loop);
    if (!this.el.isConnected) return;
    if (!this.ready) return;
    var now = performance.now();
    var t = (now - this.t0) / 1000;

    if (this.stateTil && now > this.stateTil) { this.state = "idle"; this.stateTil = 0; }
    this._blink(now);
    this._open(now);
    this._redrawTexture(false);

    var T = this._poseTarget(t), r = T.__r;
    // 帧时间归一化：不同帧率下动作速度一致
    var dt = Math.min(0.05, (now - (this._lastT || now)) / 1000);
    this._lastT = now;
    // 每根骨骼独立插值速率（次/秒）：越远端越快，越有"跟随"的层次感
    var RATE = {
      spine: 4.5, neck: 6.5, head: 8.0,
      shoulderL: 6.5, elbowL: 9.5, shoulderR: 6.5, elbowR: 9.5,
      hipL: 4.0, kneeL: 8.0, hipR: 4.0, kneeR: 8.0
    };
    for (var key in this.cur) {
      if (key === "__r") continue;
      var k = 1 - Math.exp(-(RATE[key] || 6.0) * dt);
      this.cur[key] += ((T[key] || 0) - this.cur[key]) * k;
    }
    // 根节点运动更慢，显得沉稳
    var rk = 1 - Math.exp(-3.0 * dt);
    this.rootDrift += ((r.drift || 0) - this.rootDrift) * rk;
    this.rootLift += ((r.lift || 0) - this.rootLift) * rk;
    this.facing += ((r.face || 0) - this.facing) * (1 - Math.exp(-2.5 * dt));
    this.breath += ((r.breath || 0) - this.breath) * (1 - Math.exp(-4.0 * dt));

    // 世界矩阵：world = parentWorld * T(head) * R(angle) * T(-head)
    var world = {};
    for (var bi = 0; bi < this.bones.length; bi++) {
      var b = this.bones[bi];
      var ang = this.cur[{
        hips: "spine", spine: "spine", neck: "neck", head: "head",
        shoulderL: "shoulderL", elbowL: "elbowL", shoulderR: "shoulderR", elbowR: "elbowR",
        hipL: "hipL", kneeL: "kneeL", hipR: "hipR", kneeR: "kneeR"
      }[b.name]] || 0;
      if (b.name === "hips") ang = 0;
      if (b.name === "spine" || b.name === "neck") ang = this.cur[b.name];

      var local = mMul(mMul(mT(b.head[0], b.head[1]), mR(ang)), mT(-b.head[0], -b.head[1]));
      if (b.name === "hips") {
        local = mMul(mT(this.rootDrift, this.rootLift), local);
      }
      world[b.name] = b.parent ? mMul(world[b.parent], local) : local;
    }

    // 顶点蒙皮
    var pos = this.pos, arr = pos.array, rest = this.rest, W = this.weights;
    var dd = this.depthData, dw = this.depthW, dh = this.depthH;
    var halfA = this.A / 2, dScale = this.depthScale;
    for (var i = 0; i < W.length; i++) {
      var rx = rest[i * 3], ry = rest[i * 3 + 1];
      var x = 0, y = 0;
      for (var w = 0; w < W[i].length; w++) {
        var wi = W[i][w], m = world[this.bones[wi[0]].name];
        x += wi[1] * (m[0] * rx + m[2] * ry + m[4]);
        y += wi[1] * (m[1] * rx + m[3] * ry + m[5]);
      }
      arr[i * 3] = x;
      arr[i * 3 + 1] = y;
      // z 位移：MiDaS 深度（近=大值→向相机鼓出）+ 轻微两侧后弯
      var z = -0.05 * x * x;
      if (dd) {
        var u = (rx + 0.5);                       // [0,1]
        var v = 1 - (ry + halfA) / this.A;        // [0,1]，图像上下翻转
        var col = (u * (dw - 1)) | 0;
        var row = (v * (dh - 1)) | 0;
        if (col < 0) col = 0; else if (col >= dw) col = dw - 1;
        if (row < 0) row = 0; else if (row >= dh) row = dh - 1;
        z += (dd[row * dw + col] / 255) * dScale;
      }
      arr[i * 3 + 2] = z;
    }
    pos.needsUpdate = true;

    // 整体 3D 摆动 / 呼吸（水平/竖直位移已在 hips 骨骼里处理，避免重复位移）
    this.group.rotation.y = this.facing;
    this.mesh.scale.y = 1 + this.breath;

    this.renderer.render(this.scene, this.camera);
  };

  Photo3D.prototype.resize = function () {
    if (!this.renderer) return;
    var w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  };

  Photo3D.prototype._disposeMesh = function () {
    if (!this.mesh) return;
    this.group.remove(this.mesh);
    this.mesh.geometry.dispose();
    this.mesh.material.dispose();
    if (this.tex) this.tex.dispose();
    this.mesh = null;
  };

  Photo3D.prototype.dispose = function () {
    cancelAnimationFrame(this._raf);
    this._disposeMesh();
    if (this.renderer) {
      if (this.renderer.domElement.parentNode)
        this.renderer.domElement.parentNode.removeChild(this.renderer.domElement);
      this.renderer.dispose();
      this.renderer = null;
    }
    window.removeEventListener("resize", this._onResize);
  };

  global.Photo3D = Photo3D;
  global.Photo3D.mount = function (el, opts) { return new Photo3D(el, opts); };
})(window);
