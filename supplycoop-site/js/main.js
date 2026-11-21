// ===== 供销合作社 官方网站 交互脚本 =====

(function () {
  'use strict';

  // 1. 导航栏滚动效果
  const header = document.getElementById('siteHeader');
  const onScroll = () => {
    if (window.scrollY > 20) {
      header.classList.add('scrolled');
    } else {
      header.classList.remove('scrolled');
    }
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // 2. 移动端菜单
  const navToggle = document.getElementById('navToggle');
  const mainNav = document.getElementById('mainNav');
  if (navToggle && mainNav) {
    navToggle.addEventListener('click', () => {
      mainNav.classList.toggle('open');
    });
    mainNav.querySelectorAll('a').forEach(a => {
      a.addEventListener('click', () => mainNav.classList.remove('open'));
    });
  }

  // 3. 数字动画 (统计数据)
  const animateCounters = () => {
    const counters = document.querySelectorAll('[data-count]');
    counters.forEach(el => {
      const target = parseInt(el.getAttribute('data-count'), 10);
      const duration = 1800;
      const start = performance.now();
      const step = (now) => {
        const progress = Math.min((now - start) / duration, 1);
        const ease = 1 - Math.pow(1 - progress, 3);
        el.textContent = Math.floor(target * ease).toLocaleString();
        if (progress < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  };

  // 4. 滚动淡入
  const setupFadeIn = () => {
    const targets = document.querySelectorAll('.section > .container');
    targets.forEach(t => t.classList.add('fade-in'));

    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          // 如果是 hero 之后的 section，触发数字动画（只触发一次）
          if (entry.target.querySelector('[data-count]')) {
            animateCounters();
            observer.unobserve(entry.target);
          }
        }
      });
    }, { threshold: 0.15 });

    targets.forEach(t => observer.observe(t));
  };
  setupFadeIn();

  // 5. 表单提交 (仅前端演示)
  const form = document.getElementById('contactForm');
  if (form) {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type=submit]');
      const original = btn.textContent;
      btn.textContent = '✓ 提交成功，我们会尽快联系您';
      btn.style.background = '#2ecc71';
      setTimeout(() => {
        btn.textContent = original;
        btn.style.background = '';
        form.reset();
      }, 3000);
    });
  }

})();
