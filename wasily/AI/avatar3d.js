/**
 * wasily 3D 数字人（游戏式骨骼人形 + 真人照片贴脸）
 * 依赖 three.min.js (r12x UMD)。用法：
 *   var av = Avatar3D.mount(document.getElementById("av3d"), { face: "/avatars/xxx.jpg" });
 *   av.setFace(url); av.setState("idle|talk|walk|sit|greet|happy|sad", durMs可选); av.resize(); av.dispose();
 */
(function (global) {
  "use strict";

  var SKIN = 0xf2c9a4, HAIR = 0x3a2a22, TOP = 0xe86a8a, BOTTOM = 0x3c4a6b, SHOE = 0x2a2f45;

  function capsule(r, len, color) {
    var g = new THREE.CylinderGeometry(r, r * 0.92, len, 10);
    g.translate(0, -len / 2, 0);           // 原点移到骨骼顶端（绕关节旋转）
    return new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: color }));
  }
  function ball(r, color) {
    return new THREE.Mesh(new THREE.SphereGeometry(r, 14, 12),
      new THREE.MeshLambertMaterial({ color: color }));
  }

  function Avatar3D(container, opts) {
    opts = opts || {};
    this.el = container;
    this.state = "idle";
    this.stateTil = 0;
    this.cur = { tilt: 0, headX: 0, headY: 0, headZ: 0, uaLx: 0, uaLz: 0, uaRx: 0, uaRz: 0,
                 faL: 0, faR: 0, thL: 0, thR: 0, caL: 0, caR: 0, bob: 0, drift: 0, rotY: 0 };
    this._t0 = performance.now();

    var w = container.clientWidth || 320, h = container.clientHeight || 400;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setSize(w, h);
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(38, w / h, 0.1, 50);
    this.camera.position.set(0, 1.3, 2.9);
    this.camera.lookAt(0, 1.0, 0);
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    var dl = new THREE.DirectionalLight(0xfff2e0, 0.8);
    dl.position.set(1.5, 3, 2.5);
    this.scene.add(dl);

    // 地面阴影盘
    var shadow = new THREE.Mesh(new THREE.CircleGeometry(0.5, 24),
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35 }));
    shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.01;
    this.scene.add(shadow);

    this._build();
    this.setFace(opts.face || "/avatars/wxx_live_1.jpg");
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  Avatar3D.prototype._build = function () {
    // 骨骼层级：root → hips → spine → neck → head；spine → 肩→肘→手；hips → 胯→膝→脚
    var root = this.root = new THREE.Group(); this.scene.add(root);
    var hips = this.hips = new THREE.Group(); hips.position.y = 0.95; root.add(hips);
    var pelvis = ball(0.13, BOTTOM); pelvis.scale.set(1.25, 0.85, 0.95); hips.add(pelvis);

    var spine = this.spine = new THREE.Group(); spine.position.y = 0.10; hips.add(spine);
    var chestG = new THREE.CylinderGeometry(0.155, 0.12, 0.42, 12);
    chestG.translate(0, 0.21, 0);                          // 向上生长：原点在髋部
    var chest = new THREE.Mesh(chestG, new THREE.MeshLambertMaterial({ color: TOP }));
    chest.scale.z = 0.78;
    spine.add(chest);

    var neck = this.neck = new THREE.Group(); neck.position.y = 0.46; spine.add(neck);
    var neckG = new THREE.CylinderGeometry(0.045, 0.05, 0.07, 8);
    neckG.translate(0, 0.035, 0);
    neck.add(new THREE.Mesh(neckG, new THREE.MeshLambertMaterial({ color: SKIN })));

    var head = this.head = new THREE.Group(); head.position.y = 0.09; neck.add(head);
    var skull = ball(0.135, SKIN); skull.position.y = 0.1; skull.scale.set(0.92, 1.08, 0.98);
    head.add(skull);
    // 脸：照片贴在开口圆柱面上（正对 +z）
    var fg = new THREE.CylinderGeometry(0.128, 0.128, 0.17, 16, 1, true, -0.85, 1.7);
    this.faceMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    this.faceMesh = new THREE.Mesh(fg, this.faceMat);
    this.faceMesh.position.y = 0.09; head.add(this.faceMesh);
    // 头发：头顶半球 + 后脑壳
    var hairTop = new THREE.Mesh(new THREE.SphereGeometry(0.145, 14, 10, 0, Math.PI * 2, 0, Math.PI * 0.52),
      new THREE.MeshLambertMaterial({ color: HAIR }));
    hairTop.position.y = 0.115; head.add(hairTop);
    var hairBack = ball(0.13, HAIR); hairBack.position.set(0, 0.08, -0.045); hairBack.scale.set(1, 1.12, 0.9);
    head.add(hairBack);

    // 手臂（肩=spine 上端两侧）
    this.arms = {};
    ["L", "R"].forEach(function (s) {
      var sgn = s === "L" ? -1 : 1;
      var sh = new THREE.Group(); sh.position.set(0.19 * sgn, 0.40, 0); spine.add(sh);
      sh.add(capsule(0.048, 0.27, TOP));
      var el = new THREE.Group(); el.position.y = -0.27; sh.add(el);
      el.add(capsule(0.04, 0.24, SKIN));
      var hand = ball(0.05, SKIN); hand.position.y = -0.26; el.add(hand);
      this["sh" + s] = sh; this["el" + s] = el;
    }, this);

    // 腿（胯=hips 两侧）
    ["L", "R"].forEach(function (s) {
      var sgn = s === "L" ? -1 : 1;
      var hp = new THREE.Group(); hp.position.set(0.09 * sgn, -0.03, 0); hips.add(hp);
      hp.add(capsule(0.062, 0.42, BOTTOM));
      var kn = new THREE.Group(); kn.position.y = -0.42; hp.add(kn);
      kn.add(capsule(0.05, 0.40, SKIN));
      var foot = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.055, 0.17),
        new THREE.MeshLambertMaterial({ color: SHOE }));
      foot.position.set(0, -0.415, 0.045); kn.add(foot);
      this["hip" + s] = hp; this["knee" + s] = kn;
    }, this);
  };

  Avatar3D.prototype.setFace = function (url) {
    if (!url) return;
    var self = this;
    new THREE.TextureLoader().load(url, function (tex) {
      if (THREE.sRGBEncoding) tex.encoding = THREE.sRGBEncoding;
      // 照片映射到正面弧面：脸居中
      tex.offset.set(0.13, 0.05); tex.repeat.set(0.74, 0.9);
      self.faceMat.map = tex; self.faceMat.needsUpdate = true;
    });
  };

  /** 状态切换；durMs 后自动回 idle */
  Avatar3D.prototype.setState = function (name, durMs) {
    this.state = name || "idle";
    this.stateTil = durMs ? performance.now() + durMs : 0;
  };

  /* 各状态的目标姿态（t 秒），lerp 平滑过渡 */
  Avatar3D.prototype._target = function (t) {
    var T = { tilt: 0, headX: 0, headY: 0.04 * Math.sin(t * 0.7), headZ: 0,
      uaLx: 0, uaLz: 0.1 + 0.02 * Math.sin(t * 1.1), uaRx: 0, uaRz: -0.1 - 0.02 * Math.sin(t * 1.1),
      faL: -0.15, faR: -0.15, thL: 0, thR: 0, caL: 0, caR: 0,
      bob: 0.012 * Math.sin(t * 1.6), drift: 0, rotY: 0 };
    switch (this.state) {
      case "talk":
        T.headX = 0.07 * Math.sin(t * 6.5);
        T.tilt = 0.05;
        T.uaRz = -0.35; T.faR = -0.55;                    // 说话时抬右手比划
        T.uaRx = 0.15 * Math.sin(t * 3.2);
        break;
      case "walk": {
        var s = Math.sin(t * 5.0);
        T.thL = 0.5 * s; T.thR = -0.5 * s;
        T.caL = 0.85 * Math.max(0, -s); T.caR = 0.85 * Math.max(0, s);
        T.uaLx = -0.38 * s; T.uaRx = 0.38 * s; T.uaLz = 0.06; T.uaRz = -0.06;
        T.bob = 0.03 * Math.abs(Math.sin(t * 5.0));
        T.drift = 0.62 * Math.sin(t * 0.42);              // 在舞台上来回走动
        T.rotY = 0.55 * Math.cos(t * 0.42);               // 朝行进方向转身
        break;
      }
      case "sit":
        T.thL = -1.5; T.thR = -1.5; T.caL = 1.45; T.caR = 1.45;
        T.tilt = -0.1; T.bob = -0.42;                     // 重心下降呈坐姿
        T.uaLx = -0.45; T.uaRx = -0.45; T.faL = -0.5; T.faR = -0.5;
        break;
      case "greet":
        T.uaRz = -2.45; T.faR = -0.35 + 0.3 * Math.sin(t * 8);  // 举手挥动
        T.headZ = 0.1; T.tilt = 0.03;
        break;
      case "happy":
        T.bob = 0.045 * Math.abs(Math.sin(t * 5.5));
        T.uaLz = 0.55; T.uaRz = -0.55; T.faL = -0.4; T.faR = -0.4;
        T.headZ = 0.08 * Math.sin(t * 2.8);
        break;
      case "sad":
        T.tilt = 0.2; T.headX = 0.3; T.bob = -0.05;
        T.uaLz = 0.04; T.uaRz = -0.04; T.faL = -0.05; T.faR = -0.05;
        break;
    }
    return T;
  };

  Avatar3D.prototype._loop = function () {
    if (!this.el.isConnected) return;                     // 元素被移除即停止
    this._raf = requestAnimationFrame(this._loop);
    var now = performance.now();
    if (this.stateTil && now > this.stateTil) { this.state = "idle"; this.stateTil = 0; }
    var t = (now - this._t0) / 1000;
    var tgt = this._target(t), cur = this.cur, k = 0.14;
    for (var key in tgt) cur[key] += (tgt[key] - cur[key]) * k;

    this.spine.rotation.x = cur.tilt;
    this.head.rotation.set(cur.headX, cur.headY, cur.headZ);
    this.shL.rotation.set(cur.uaLx, 0, cur.uaLz); this.elL.rotation.x = cur.faL;
    this.shR.rotation.set(cur.uaRx, 0, cur.uaRz); this.elR.rotation.x = cur.faR;
    this.hipL.rotation.x = cur.thL; this.kneeL.rotation.x = cur.caL;
    this.hipR.rotation.x = cur.thR; this.kneeR.rotation.x = cur.caR;
    this.hips.position.y = 0.95 + cur.bob;
    this.root.position.x = cur.drift;
    this.root.rotation.y = cur.rotY;
    this.renderer.render(this.scene, this.camera);
  };

  Avatar3D.prototype.resize = function () {
    var w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  };

  Avatar3D.prototype.dispose = function () {
    cancelAnimationFrame(this._raf);
    if (this.renderer.domElement.parentNode) this.renderer.domElement.parentNode.removeChild(this.renderer.domElement);
    this.renderer.dispose();
  };

  global.Avatar3D = { mount: function (el, opts) { return new Avatar3D(el, opts); } };
})(window);
