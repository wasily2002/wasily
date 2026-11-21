/**
 * wasily 照片立绘动画模块（avatar.html / video_call.html 共用）
 * 真实照片无法做骨骼动作，这里用整体 CSS 动画表达：
 *   idle 呼吸 / talking 说话节奏 / 挥手·点头·跳跃·摇摆·倾斜·鼓掌等
 * 用法: var ph = new PhotoAvatar(imgEl); ph.play("wave"); ph.setTalking(true)
 */
(function (global) {
  "use strict";

  // ---- 注入 CSS（keyframes）----
  var CSS = [
    "@keyframes ph-idle {0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-7px) scale(1.006)}}",
    "@keyframes ph-talk {0%,100%{transform:translateY(0) scale(1,1)}25%{transform:translateY(-3px) scale(1.004,0.998)}50%{transform:translateY(-6px) scale(1.006)}75%{transform:translateY(-2px) rotate(-0.4deg)}}",
    "@keyframes ph-wave {0%,100%{transform:rotate(0)}20%{transform:rotate(-4deg) translateY(-6px)}40%{transform:rotate(3deg) translateY(-12px)}60%{transform:rotate(-3deg) translateY(-10px)}80%{transform:rotate(2deg) translateY(-5px)}}",
    "@keyframes ph-nod {0%,100%{transform:scale(1)}35%{transform:translateY(8px) scaleY(0.985)}70%{transform:translateY(2px) scale(1.002)}}",
    "@keyframes ph-bounce {0%,100%{transform:translateY(0)}30%{transform:translateY(-34px)}45%{transform:translateY(0)}60%{transform:translateY(-16px)}78%{transform:translateY(0)}}",
    "@keyframes ph-swing {0%,100%{transform:rotate(-2.5deg)}50%{transform:rotate(2.5deg) translateY(-8px)}}",
    "@keyframes ph-tilt {0%,100%{transform:rotate(0)}45%{transform:rotate(7deg) translateY(4px)}70%{transform:rotate(3deg)}}",
    "@keyframes ph-clap {0%,100%{transform:translateX(0)}20%{transform:translateX(-5px) rotate(-1.5deg)}40%{transform:translateX(5px) rotate(1.5deg)}60%{transform:translateX(-5px) rotate(-1.5deg)}80%{transform:translateX(4px)}}",
    "@keyframes ph-cheer {0%{transform:translateY(0) scale(1)}35%{transform:translateY(-26px) scale(1.02)}55%{transform:translateY(0) scale(1)}72%{transform:translateY(-12px)}100%{transform:translateY(0)}}",
    ".ph-idle{animation:ph-idle 3.6s ease-in-out infinite}",
    ".ph-talking{animation:ph-talk 0.72s ease-in-out infinite}",
    ".ph-go{animation-duration:1.6s;animation-timing-function:ease-in-out;animation-iteration-count:1}"
  ].join("");
  var styleEl = document.createElement("style");
  styleEl.textContent = CSS;
  document.head.appendChild(styleEl);

  // ---- motions.json id/关键词 → keyframes 名 ----
  var MAP = [
    [["wave", "挥手", "salute", "敬礼", "thumb", "ok", "fist"], "ph-wave"],
    [["nod", "点头", "bow", "鞠躬", "stand"], "ph-nod"],
    [["jump", "跳", "hop"], "ph-bounce"],
    [["dance", "舞", "swing", "摇摆", "twist", "扭", "walk", "run", "走路", "跑"], "ph-swing"],
    [["think", "思考", "tilt", "歪头", "shy", "害羞", "sleep", "睡", "meditate", "冥想", "look", "环顾"], "ph-tilt"],
    [["clap", "拍手", "拍手"], "ph-clap"],
    [["cheer", "欢呼", "celebrate", "庆祝", "heart", "比心"], "ph-cheer"]
  ];
  function mapAnim(idOrName) {
    var s = String(idOrName || "").toLowerCase();
    for (var i = 0; i < MAP.length; i++) {
      var keys = MAP[i][0];
      for (var j = 0; j < keys.length; j++) {
        if (s.indexOf(keys[j]) >= 0) return MAP[i][1];
      }
    }
    return "ph-wave";
  }

  function PhotoAvatar(imgEl) {
    this.el = imgEl;
    this.talking = false;
    this._end = null;
    var self = this;
    this.el.addEventListener("animationend", function () {
      self.el.classList.remove("ph-go");
      self.el.style.animationName = "";
      if (!self.talking) self.el.classList.add("ph-idle");
    });
    this.startIdle();
  }

  PhotoAvatar.prototype.startIdle = function () {
    if (!this.talking) this.el.classList.add("ph-idle");
  };

  PhotoAvatar.prototype.stopIdle = function () {
    this.el.classList.remove("ph-idle");
  };

  /** 播放一次性动作 */
  PhotoAvatar.prototype.play = function (idOrName, dur) {
    var anim = mapAnim(idOrName);
    this.el.classList.remove("ph-idle", "ph-talking", "ph-go");
    this.el.style.animationName = "";
    void this.el.offsetWidth;                 // 强制 reflow 重启动画
    this.el.classList.add("ph-go");
    this.el.style.animationName = anim;
    if (dur) this.el.style.animationDuration = dur + "s";
  };

  PhotoAvatar.prototype.setTalking = function (on) {
    this.talking = !!on;
    if (on) {
      this.el.classList.remove("ph-idle", "ph-go");
      this.el.style.animationName = "";
      this.el.classList.add("ph-talking");
    } else {
      this.el.classList.remove("ph-talking");
      this.el.classList.add("ph-idle");
    }
  };

  global.PhotoAvatar = PhotoAvatar;
})(window);
