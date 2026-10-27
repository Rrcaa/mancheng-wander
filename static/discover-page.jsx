// discover-page.jsx — 发现：热门景点
// 版式对齐 Mindtrip：一排放 3 个，图上压标题 + 城市/评分，只留一个标题「热门景点」。
// 数据来自 /api/discover/hot（后端按名字实时取高德评分/营业时间/照片）。

function DiscPinIcon({ size = 11 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 21s7-6.3 7-11a7 7 0 1 0-14 0c0 4.7 7 11 7 11z" />
      <circle cx="12" cy="10" r="2.4" />
    </svg>
  );
}

function HotPlaceCard({ place, onOpen }) {
  return (
    <button className="disc-card" onClick={onOpen} aria-label={`查看 ${place.title || place.name} · ${place.city}`}>
      <img className="disc-card-img" src={place.photo} alt={place.title || place.name} loading="lazy" />
      <span className="disc-card-shade" aria-hidden="true" />
      <span className="disc-card-body">
        <strong>{place.title || place.name}</strong>
        <span className="disc-card-meta">
          <DiscPinIcon />
          <em>{place.city}</em>
          {place.rating != null ? <span className="disc-dot">·</span> : null}
          {place.rating != null ? <em>{Number(place.rating).toFixed(1)}</em> : null}
        </span>
      </span>
    </button>
  );
}

function HotPlaceSheet({ place, onClose, onPlan }) {
  const sheetRef = React.useRef(null);
  useDialogFocus(sheetRef, onClose);
  const facts = [];
  if (place.open_time) facts.push({ icon: "clock", text: place.open_time });
  if (place.address) facts.push({ icon: "pin", text: place.address });

  // 挂到 body：页面容器带着 page-fade 动画（会生成层叠上下文），
  // 留在页面里的话浮层会被底部导航压住
  return ReactDOM.createPortal(
    <div className="disc-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="disc-sheet" ref={sheetRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={place.title || place.name}>
        <div className="disc-sheet-hero">
          <img src={place.photo} alt={place.title || place.name} />
          <span className="disc-sheet-shade" aria-hidden="true" />
          <button className="disc-sheet-close" onClick={onClose} aria-label="关闭"><UiIcon name="close" size={16} /></button>
          <div className="disc-sheet-head">
            <h3>{place.title || place.name}</h3>
            <p>
              <DiscPinIcon size={12} />
              <span>{place.city}</span>
              {place.district ? <span> · {place.district}</span> : null}
              {place.rating != null ? <span> · {Number(place.rating).toFixed(1)} 分</span> : null}
            </p>
          </div>
        </div>
        <div className="disc-sheet-body">
          {facts.map((fact, index) => (
            <div className="disc-sheet-row" key={index}>
              <span className="disc-sheet-row-icon">
                {fact.icon === "pin" ? <DiscPinIcon size={14} /> : <UiIcon name="clock" size={14} />}
              </span>
              <span>{fact.text}</span>
            </div>
          ))}
          <div className="disc-sheet-foot">
            <button className="disc-sheet-cta" onClick={() => onPlan(place)}>
              规划一趟{place.city}
              <UiIcon name="arrow-right" size={15} />
            </button>
            <p>把 {place.title || place.name} 排进去，再看看前后怎么走。</p>
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}

function DiscoverPage({ onPlan }) {
  const [items, setItems] = React.useState([]);
  const [error, setError] = React.useState("");
  const [active, setActive] = React.useState(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  // 后端是边抓边给的：先拿回几张就先亮几张，剩下的过一会儿再补
  React.useEffect(() => {
    let alive = true;
    let timer = null;
    let rounds = 0;
    const tick = async () => {
      try {
        const list = await getHotPlaces(12);
        if (!alive) return;
        setItems(list);
        setError("");
        rounds += 1;
        if (list.length >= 9 || rounds >= 16) {
          if (!list.length) setError("暂时没取到热门景点，稍后再看看。");
          return;
        }
        timer = window.setTimeout(tick, 1500);
      } catch (err) {
        if (!alive) return;
        setError(err.message || "热门景点暂时加载失败");
      }
    };
    tick();
    return () => { alive = false; if (timer) window.clearTimeout(timer); };
  }, [reloadKey]);

  const startFrom = place => {
    setActive(null);
    onPlan?.(`${place.city}，想去${place.title || place.name}，帮我看看怎么安排`);
  };

  const placeholders = items.length ? 0 : 6;

  return (
    <main className="disc-page page-fade">
      <header className="disc-head">
        <h1>热门景点</h1>
      </header>

      {error && !items.length ? (
        <div className="disc-empty">
          <span>{error}</span>
          <button onClick={() => setReloadKey(key => key + 1)}>重新加载</button>
        </div>
      ) : null}

      <div className="disc-grid">
        {items.map(place => (
          <HotPlaceCard key={place.id || `${place.city}-${place.wanted}`} place={place} onOpen={() => setActive(place)} />
        ))}
        {Array.from({ length: placeholders }).map((_, index) => (
          <span className="disc-card disc-card-skeleton" key={`skeleton-${index}`} aria-hidden="true" />
        ))}
      </div>

      {active ? <HotPlaceSheet place={active} onClose={() => setActive(null)} onPlan={startFrom} /> : null}
    </main>
  );
}
