import type { Metadata } from "next";

import { Shell } from "@/components/shell";

import "./globals.css";

export const metadata: Metadata = {
  title: "QuantSieve · AI 投研领航员",
  description: "数字可溯源的 AI 投研与量化回测工作台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
