"use client";

import {
  KeyboardEvent,
  useEffect,
  useId,
  useRef,
  useState,
  type FocusEvent,
} from "react";

import {
  instrumentLabel,
  marketLabel,
  searchInstruments,
  type MarketFilter,
} from "@/lib/instruments";
import type { Instrument } from "@/lib/types";

type SymbolPickerProps = {
  label?: string;
  market?: MarketFilter;
  onQueryChange: (query: string) => void;
  onSelect: (instrument: Instrument | null) => void;
  placeholder?: string;
  query: string;
  selected: Instrument | null;
};

const MARKET_DIRECTORY_HINT: Record<MarketFilter, string> = {
  all: "名称、代码或交易对均可；可切换市场缩小范围",
  CN: "A 股名称或代码",
  US: "美股名称或代码",
  ETF: "ETF 名称或代码",
  INDEX: "指数名称或代码",
  FOREX: "货币对名称或代码",
  CRYPTO: "Binance 现货全量目录；支持 BTC/USDT、bitcoin usdt，优先显示 USDT 交易对",
  FUTURES: "全球期货目录，支持能源、金属、农产品与碳排放",
};

export function SymbolPicker({
  label = "研究标的",
  market = "all",
  onQueryChange,
  onSelect,
  placeholder = "输入名称、代码或交易对，如 宁德时代 / AAPL / BTC/USDT",
  query,
  selected,
}: SymbolPickerProps) {
  const listId = useId();
  const wrapper = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [activeMarket, setActiveMarket] = useState<MarketFilter>(market);
  const [results, setResults] = useState<Instrument[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true);
      setFailed(false);
      try {
        const instruments = await searchInstruments(
          query,
          activeMarket,
          12,
          controller.signal,
        );
        setResults(instruments);
        setActiveIndex(0);
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setResults([]);
          setFailed(true);
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, query.trim() ? 180 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeMarket, open, query]);

  function choose(instrument: Instrument) {
    onSelect(instrument);
    onQueryChange(instrumentLabel(instrument));
    setOpen(false);
  }

  function change(value: string) {
    onQueryChange(value);
    if (selected && value !== instrumentLabel(selected)) onSelect(null);
    setOpen(true);
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => Math.min(index + 1, Math.max(results.length - 1, 0)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter" && open && results[activeIndex]) {
      event.preventDefault();
      choose(results[activeIndex]);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  function blur(event: FocusEvent<HTMLDivElement>) {
    if (!wrapper.current?.contains(event.relatedTarget as Node | null)) setOpen(false);
  }

  return (
    <div className="symbol-field" onBlur={blur} ref={wrapper}>
      <label htmlFor={`${listId}-input`}>{label}</label>
      <div aria-label="市场筛选" className="market-filter-row" role="group">
        {(
          ["all", "CN", "US", "ETF", "INDEX", "FOREX", "CRYPTO", "FUTURES"] as const
        ).map((item) => (
          <button
            aria-pressed={activeMarket === item}
            className={activeMarket === item ? "active" : ""}
            key={item}
            onClick={() => {
              setActiveMarket(item);
              setOpen(true);
            }}
            type="button"
          >
            {marketLabel(item)}
          </button>
        ))}
      </div>
      <div className={open ? "symbol-combobox open" : "symbol-combobox"}>
        <span className="symbol-search-mark" aria-hidden="true">
          ⌕
        </span>
        <input
          aria-activedescendant={
            open && results[activeIndex] ? `${listId}-option-${activeIndex}` : undefined
          }
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={open}
          autoComplete="off"
          id={`${listId}-input`}
          onChange={(event) => change(event.target.value)}
          onFocus={() => setOpen(true)}
          onKeyDown={keyDown}
          placeholder={
            placeholder === "输入名称或代码，如 宁德时代 / AAPL"
              ? "名称或代码，如 苹果 / SPY / EURUSD / BTCUSDT / 原油"
              : placeholder
          }
          role="combobox"
          value={query}
        />
        {selected && (
          <span className={`market-pill ${selected.market.toLocaleLowerCase()}`}>
            {marketLabel(selected.market)}
          </span>
        )}
        {open && (
          <div className="symbol-results" id={listId} role="listbox">
            <div className="symbol-results-heading">
              <span>{query.trim() ? "搜索结果" : "热门标的"}</span>
              <small>{MARKET_DIRECTORY_HINT[activeMarket]}</small>
            </div>
            {loading && <div className="symbol-result-status">正在搜索标的目录…</div>}
            {!loading && failed && (
              <div className="symbol-result-status error">标的目录暂时不可用，请稍后重试</div>
            )}
            {!loading && !failed && results.length === 0 && (
              <div className="symbol-result-status">没有找到匹配的标的</div>
            )}
            {!loading &&
              results.map((instrument, index) => (
                <button
                  aria-selected={index === activeIndex}
                  className={index === activeIndex ? "symbol-option active" : "symbol-option"}
                  id={`${listId}-option-${index}`}
                  key={`${instrument.market}-${instrument.symbol}`}
                  onClick={() => choose(instrument)}
                  onMouseEnter={() => setActiveIndex(index)}
                  role="option"
                  type="button"
                >
                  <span className="symbol-option-code">{instrument.symbol}</span>
                  <span className="symbol-option-name">
                    <strong>{instrument.name}</strong>
                    <small>{instrument.exchange}</small>
                  </span>
                  <span className={`market-pill ${instrument.market.toLocaleLowerCase()}`}>
                    {marketLabel(instrument.market)}
                  </span>
                </button>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}
