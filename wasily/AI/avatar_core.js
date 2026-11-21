/**
 * wasily AI 形象动作引擎（avatar.html 与 video_call.html 共用）
 * 依赖页面中存在以下 id 的 SVG 元素：
 *   #part-head #part-body #part-arm-l #part-arm-r #part-leg-l #part-leg-r
 *   #part-mouth #part-eye-l #part-eye-r #stage-inner（可选：整身旋转用）
 * 动作库 motions.json 帧字段: h=头 b=躯干 bx/by=躯干平移 al/ar=臂 ll/lr=腿 w=整身旋转
 */
(function (global) {
  "use strict";

  var PART_IDS = {
    h: "part-head", b: "part-body",
    al: "part-arm-l", ar: "part-arm-r",
    ll: "part-leg-l", lr: "part-leg-r"
  };
  var POSE_KEYS = ["h", "b", "bx", "by", "al", "ar", "ll", "lr", "w"];

  function WasilyAvatar(opts) {
    opts = opts || {};
    this.server = String(opts.server || "http://127.0.0.1:8000").replace(/\/+$/, "");
    this.motionsUrl = opts.motionsUrl || (this.server + "/motions.json");
    this.motions = [];
    this.motionMap = {};
    this.current = null;
    this.currentLoop = false;
    this.startTime = 0;
    this.talking = false;
    this.eyesClosed = false;
    this.onMotionEnd = null;
    this._prev = {};
    this._els = {};
    for (var k in PART_IDS) this._els[k] = document.getElementById(PART_IDS[k]);
    this._els.mouth = document.getElementById("part-mouth");
    this._els.eyeL = document.getElementById("part-eye-l");
    this._els.eyeR = document.getElementById("part-eye-r");
    this._els.stage = document.getElementById("stage-inner");
    this.resetPose();
    this._loop();
    this._scheduleBlink();
  }

  WasilyAvatar.prototype.resetPose = function () {
    var keys = ["h", "b", "al", "ar", "ll", "lr"];
    for (var i = 0; i < keys.length; i++) {
      var el = this._els[keys[i]];
      if (el) el.style.transform = "";
    }
    if (this._els.stage) this._els.stage.style.transform = "";
    this._prev = {};
  };

  /** 加载动作库 */
  WasilyAvatar.prototype.loadMotions = function () {
    var self = this;
    return fetch(self.motionsUrl, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        self.setMotions(data.motions || []);
        return true;
      })
      .catch(function (e) {
        console.warn("[Avatar] 动作库加载失败", e);
        return false;
      });
  };

  WasilyAvatar.prototype.setMotions = function (list) {
    this.motions = list || [];
    this.motionMap = {};
    for (var i = 0; i < this.motions.length; i++) {
      this.motionMap[this.motions[i].id] = this.motions[i];
    }
  };

  /** 播放动作：支持 id 或中文名模糊匹配 */
  WasilyAvatar.prototype.play = function (idOrName, opts) {
    opts = opts || {};
    var m = this.motionMap[idOrName];
    if (!m) {
      var kw = String(idOrName || "").trim().toLowerCase();
      if (kw) {
        for (var i = 0; i < this.motions.length && !m; i++) {
          if (this.motions[i].name === idOrName) m = this.motions[i];
        }
        for (var j = 0; j < this.motions.length && !m; j++) {
          if ((this.motions[j].name || "").indexOf(idOrName) >= 0) m = this.motions[j];
        }
        for (var n = 0; n < this.motions.length && !m; n++) {
          if ((this.motions[n].id || "").indexOf(kw) >= 0) m = this.motions[n];
        }
      }
    }
    if (!m) return false;
    this.current = m;
    this.currentLoop = !!m.loop && opts.loop !== false;
    this.startTime = performance.now();
    this._setEyes(m.eyes === "closed");
    return true;
  };

  WasilyAvatar.prototype.stopMotion = function () {
    this.current = null;
    this._setEyes(false);
    this.resetPose();
  };

  /** 说话嘴型开关（TTS 播放/生成中置 true） */
  WasilyAvatar.prototype.setTalking = function (on) {
    this.talking = !!on;
    if (!this.talking && this._els.mouth) {
      this._els.mouth.style.transform = "";
    }
  };

  WasilyAvatar.prototype._setEyes = function (closed) {
    this.eyesClosed = closed;
    var scale = closed ? "scaleY(0.12)" : "";
    var els = [this._els.eyeL, this._els.eyeR];
    for (var i = 0; i < els.length; i++) {
      if (els[i]) els[i].style.transform = scale;
    }
  };

  WasilyAvatar.prototype._scheduleBlink = function () {
    var self = this;
    var blink = function () {
      if (!self.eyesClosed) {
        var els = [self._els.eyeL, self._els.eyeR];
        for (var i = 0; i < els.length; i++) {
          if (els[i]) {
            els[i].style.transition = "transform 90ms ease";
            els[i].style.transform = "scaleY(0.1)";
          }
        }
        setTimeout(function () {
          for (var i = 0; i < els.length; i++) {
            if (els[i]) els[i].style.transform = "";
          }
        }, 110);
      }
      self._blinkTimer = setTimeout(blink, 2600 + Math.random() * 3200);
    };
    this._blinkTimer = setTimeout(blink, 2000);
  };

  /** 按帧插值计算当前姿态 */
  WasilyAvatar.prototype._poseAt = function (ms) {
    var m = this.current;
    var frames = m.frames || [];
    if (!frames.length) return {};
    var t = (ms - this.startTime) / 1000;
    if (this.currentLoop && m.dur > 0) t = t % m.dur;
    var last = frames[frames.length - 1];
    if (t >= (last.t || 0)) {
      if (!this.currentLoop) {
        var cb = this.onMotionEnd;
        this.current = null;
        this._setEyes(false);
        this.resetPose();
        if (cb) { try { cb(m); } catch (e) {} }
        return null;
      }
      t = last.t || 0;
    }
    var i = 0;
    while (i < frames.length - 1 && frames[i + 1].t <= t) i++;
    var a = frames[i];
    var b = frames[Math.min(i + 1, frames.length - 1)];
    var span = Math.max(0.0001, (b.t || 0) - (a.t || 0));
    var k = Math.min(1, Math.max(0, (t - (a.t || 0)) / span));
    k = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;   // easeInOut
    var pose = {};
    for (var p = 0; p < POSE_KEYS.length; p++) {
      var key = POSE_KEYS[p];
      var av = a[key] !== undefined ? a[key] : (this._prev[key] || 0);
      var bv = b[key] !== undefined ? b[key] : av;
      pose[key] = av + (bv - av) * k;
    }
    this._prev = pose;
    return pose;
  };

  WasilyAvatar.prototype._applyPose = function (pose) {
    var set = function (el, transform) { if (el) el.style.transform = transform; };
    if (pose.h !== undefined) set(this._els.h, "rotate(" + pose.h + "deg)");
    if (pose.b !== undefined || pose.bx !== undefined || pose.by !== undefined) {
      var bx = pose.bx || 0, by = pose.by || 0, br = pose.b || 0;
      set(this._els.b, "translate(" + bx + "px," + by + "px) rotate(" + br + "deg)");
    }
    if (pose.al !== undefined) set(this._els.al, "rotate(" + pose.al + "deg)");
    if (pose.ar !== undefined) set(this._els.ar, "rotate(" + pose.ar + "deg)");
    if (pose.ll !== undefined) set(this._els.ll, "rotate(" + pose.ll + "deg)");
    if (pose.lr !== undefined) set(this._els.lr, "rotate(" + pose.lr + "deg)");
    if (pose.w !== undefined) set(this._els.stage, "rotate(" + pose.w + "deg)");
  };

  WasilyAvatar.prototype._loop = function () {
    var self = this;
    var tick = function () {
      if (self.current) {
        var pose = self._poseAt(performance.now());
        if (pose) self._applyPose(pose);
      }
      if (self.talking && self._els.mouth) {
        var s = 0.35 + 0.65 * Math.abs(Math.sin(performance.now() / 110));
        self._els.mouth.style.transform = "scaleY(" + s.toFixed(3) + ")";
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };

  /** 自动待机：无动作时每 25~55s 随机来个小动作 */
  WasilyAvatar.prototype.startIdleGestures = function () {
    var self = this;
    var idle = ["tilt", "look", "breathe", "wave", "nod", "stretch"];
    var pick = function () {
      if (!self.current && !self.talking) {
        self.play(idle[Math.floor(Math.random() * idle.length)]);
      }
      setTimeout(pick, 25000 + Math.random() * 30000);
    };
    setTimeout(pick, 12000);
  };

  /** 按 agent 阶段联动（轮询 /agent_status 后调用） */
  WasilyAvatar.prototype.reactPhase = function (phase) {
    if (phase === "prefill") {
      if (!this.current) this.play("think");
    } else if (phase === "decode") {
      this.setTalking(true);
    } else if (phase === "tool") {
      if (!this.current) this.play("point_r");
    } else if (phase === "idle") {
      this.setTalking(false);
    }
  };

  global.WasilyAvatar = WasilyAvatar;
})(window);
