// Wander — a winding route through layered glass, shared across all surfaces.
function BrandMark({ size = 40, style = {}, className = "", decorative = false }) {
  return <svg className={`brand-mark ${className}`} width={size} height={size} style={style} viewBox="0 0 64 64" fill="none" role={decorative ? undefined : "img"} aria-hidden={decorative || undefined} aria-label={decorative ? undefined : "漫程 Wander"}>
    <defs>
      <linearGradient id="wander-g" x1="6" y1="4" x2="58" y2="62" gradientUnits="userSpaceOnUse">
        <stop offset="0" stopColor="#4F8DFF" />
        <stop offset=".55" stopColor="#6F6CFF" />
        <stop offset="1" stopColor="#B46BFF" />
      </linearGradient>
      <linearGradient id="wander-hl" x1="0" y1="0" x2="0" y2="64" gradientUnits="userSpaceOnUse">
        <stop offset="0" stopColor="#ffffff" stopOpacity=".55" />
        <stop offset=".5" stopColor="#ffffff" stopOpacity="0" />
      </linearGradient>
    </defs>
    <rect width="64" height="64" rx="19" fill="url(#wander-g)" />
    <rect width="64" height="64" rx="19" fill="url(#wander-hl)" />
    <path d="M18 44C24 30 36 26 43 34S54 36 49 21" stroke="#ffffff" strokeWidth="3.4" strokeLinecap="round" fill="none" opacity=".96" />
    <circle cx="18" cy="44" r="4.4" fill="#ffffff" />
    <circle cx="49" cy="21" r="4.4" fill="#ffffff" fillOpacity=".8" />
  </svg>;
}

// Modal focus stays inside the active dialog and returns to its trigger.
function useDialogFocus(ref, onClose, initialFocus = null) {
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    const previous = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = () => [...(ref.current?.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]') || [])].filter(item => !item.disabled && item.getClientRects().length);
    ((initialFocus && ref.current?.querySelector(initialFocus)) || focusable()[0] || ref.current)?.focus({ preventScroll: true });
    const keydown = event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeRef.current?.(); }
      if (event.key !== "Tab") return;
      const items = focusable(), first = items[0], last = items[items.length - 1];
      if (!first) { event.preventDefault(); ref.current?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === ref.current)) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.body.style.overflow = overflow; document.removeEventListener("keydown", keydown); if (previous?.isConnected) previous.focus(); };
  }, []);
}
