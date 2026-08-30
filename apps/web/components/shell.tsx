"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import {
  ChartIcon,
  ChatIcon,
  FactorIcon,
  GridIcon,
  MarkIcon,
  PortfolioIcon,
  PulseIcon,
  TrackingIcon,
} from "./icons";

const NAV = [
  { href: "/", label: "投研对话", icon: ChatIcon },
  { href: "/backtest", label: "回测工作台", icon: ChartIcon },
  { href: "/portfolio", label: "组合实验", icon: PortfolioIcon },
  { href: "/factors", label: "因子研究", icon: FactorIcon },
  { href: "/tracking", label: "纸面跟踪", icon: TrackingIcon },
  { href: "/monitor", label: "市场脉搏", icon: PulseIcon },
  { href: "/strategies", label: "策略模板", icon: GridIcon },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/">
          <span className="brand-mark">
            <MarkIcon />
          </span>
          <span>
            QuantSieve
            <small>Research terminal</small>
          </span>
        </Link>
        <nav className="primary-nav" aria-label="主要导航">
          {NAV.map(({ href, label, icon: NavIcon }) => (
            <Link
              className={pathname === href ? "nav-link active" : "nav-link"}
              href={href}
              key={href}
            >
              <NavIcon />
              <span>{label}</span>
            </Link>
          ))}
        </nav>
        <div className="sidebar-note">
          <span className="status-dot" />
          <div>
            <strong>数据可溯源</strong>
            <small>观点之前，先看证据</small>
          </div>
        </div>
      </aside>
      <main className="main-content">{children}</main>
      <nav className="mobile-nav" aria-label="移动端导航">
        {NAV.map(({ href, label, icon: NavIcon }) => (
          <Link className={pathname === href ? "active" : ""} href={href} key={href}>
            <NavIcon />
            <span>{label.slice(0, 2)}</span>
          </Link>
        ))}
      </nav>
    </div>
  );
}
