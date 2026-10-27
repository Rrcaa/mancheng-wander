// glass.js — 液态玻璃跟随高光：把指针位置写入 CSS 变量，供 dock 径向渐变使用
(function () {
  var raf = 0;
  function set(x, y) {
    document.documentElement.style.setProperty("--gx", x + "px");
    document.documentElement.style.setProperty("--gy", y + "px");
  }
  window.addEventListener("pointermove", function (e) {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      set(e.clientX, e.clientY);
    });
  }, { passive: true });
})();

// 顶栏滚动态：初始完全透明融入内容，滚动后才浮现毛玻璃
(function () {
  function bind() {
    var bar = document.querySelector(".app-shell .topbar");
    if (!bar) return false;
    var onScroll = function () {
      if (window.scrollY > 8) bar.classList.add("is-scrolled");
      else bar.classList.remove("is-scrolled");
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return true;
  }
  if (!bind()) {
    new MutationObserver(function (_, obs) {
      if (bind()) obs.disconnect();
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
